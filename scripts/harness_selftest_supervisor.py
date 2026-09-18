#!/usr/bin/env python3
"""Provenance-closed, single-attempt supervisor for the H1-H4 fast suite."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import unittest
import uuid

SCHEMA = "brandkit-harness-selftest-plan.v1"
MODULES = (
    "tests.test_harness_phases",
    "tests.test_toolchain_inputs",
    "tests.test_toolchain_scanner",
    "tests.test_runtime_archives",
    "tests.test_shared_toolchain",
    "tests.test_mermaid_runtime",
)
EXCLUDED = (
    "tests.test_shared_toolchain.SharedToolchainContractTests."
    "test_real_lock_and_every_external_artifact_validate"
)
ENV_ALLOWLIST = (
    "HOME", "PATH", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "SOURCE_DATE_EPOCH", "BRANDKIT_SESSION_ROOT", "BRANDKIT_CLEANROOM_ROOT",
    "XDG_CACHE_HOME", "PIP_CACHE_DIR", "UV_CACHE_DIR", "npm_config_cache",
    "NODE_COMPILE_CACHE", "PUPPETEER_CACHE_DIR", "PLAYWRIGHT_BROWSERS_PATH",
    "PYTHONPYCACHEPREFIX",
)
TIMEOUT_SECONDS = 300.0
KILL_GRACE_SECONDS = 5.0


class SupervisorError(RuntimeError):
    pass


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: pathlib.Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise SupervisorError(f"bound file is absent, linked, or non-regular: {path}")
    return digest_bytes(path.read_bytes())


def atomic_exclusive_json(path: pathlib.Path, value: dict) -> None:
    """Publish a new JSON file without any overwrite-capable operation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o400)
    try:
        raw = canonical(value)
        if os.write(descriptor, raw) != len(raw):
            raise SupervisorError("short receipt write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def resolved_python() -> pathlib.Path:
    executable = pathlib.Path(sys.executable).resolve(strict=True)
    if sys.version_info[:2] != (3, 12):
        raise SupervisorError("selftest interpreter must be Python 3.12")
    return executable


def closed_environment(repo: pathlib.Path) -> dict[str, str]:
    environment = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    environment["PYTHONPATH"] = str(repo)
    environment["LC_CTYPE"] = environment.get("LC_ALL", environment.get("LANG", "C.UTF-8"))
    return dict(sorted(environment.items()))


def normalize_worker_environment(observed: dict[str, str], bound: dict) -> tuple[dict[str, str], dict]:
    expected = bound["environment"]
    normalized = dict(observed); allowed: list[str] = []
    launcher = normalized.get("__PYVENV_LAUNCHER__")
    if launcher is not None and sys.platform == "darwin" and launcher == bound["python_executable"]:
        del normalized["__PYVENV_LAUNCHER__"]; allowed.append("__PYVENV_LAUNCHER__")
    injected_differences = []
    expected_cf = bound.get("darwin_injected_environment", {}).get("__CF_USER_TEXT_ENCODING")
    observed_cf = normalized.get("__CF_USER_TEXT_ENCODING")
    if sys.platform == "darwin" and expected_cf is not None and observed_cf == expected_cf:
        del normalized["__CF_USER_TEXT_ENCODING"]; allowed.append("__CF_USER_TEXT_ENCODING")
    elif sys.platform == "darwin" and expected_cf is not None and observed_cf is None:
        injected_differences.append({"key": "__CF_USER_TEXT_ENCODING", "kind": "missing",
            "observed_sha256": None, "expected_sha256": digest_bytes(expected_cf.encode()),
            "expected_state": "darwin-injected"})
    missing = sorted(set(expected) - set(normalized)); unexpected = sorted(set(normalized) - set(expected))
    changed = sorted(key for key in set(expected) & set(normalized) if expected[key] != normalized[key])
    rows = []
    for key in missing:
        rows.append({"key": key, "kind": "missing", "observed_sha256": None,
                     "expected_sha256": digest_bytes(expected[key].encode()), "expected_state": "bound"})
    for key in unexpected:
        rows.append({"key": key, "kind": "unexpected", "observed_sha256": digest_bytes(normalized[key].encode()),
                     "expected_sha256": None, "expected_state": "absent"})
    for key in changed:
        rows.append({"key": key, "kind": "changed", "observed_sha256": digest_bytes(normalized[key].encode()),
                     "expected_sha256": digest_bytes(expected[key].encode()), "expected_state": "bound"})
    rows.extend(injected_differences); rows.sort(key=lambda row: (row["key"], row["kind"]))
    diagnostic = {"schema_version": "brandkit-worker-environment-diagnostic.v1",
                  "status": "pass" if not rows else "mismatch", "allowed_injected": allowed,
                  "differences": rows, "raw_environment_sha256": digest_bytes(canonical(observed)),
                  "normalized_environment_sha256": digest_bytes(canonical(normalized)),
                  "expected_environment_sha256": digest_bytes(canonical(expected))}
    return normalized, diagnostic


def verify_worker_environment(bound: dict) -> dict:
    normalized, diagnostic = normalize_worker_environment(dict(os.environ), bound)
    print(json.dumps({"event": "environment", "diagnostic": diagnostic}, sort_keys=True), flush=True)
    if normalized != bound["environment"] or diagnostic["status"] != "pass":
        raise SupervisorError("worker environment differs")
    return diagnostic


def environment_diagnostic_from_log(path: pathlib.Path) -> dict:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if value.get("event") == "environment" and isinstance(value.get("diagnostic"), dict):
                return value["diagnostic"]
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return {"schema_version": "brandkit-worker-environment-diagnostic.v1", "status": "absent",
            "allowed_injected": [], "differences": [], "raw_environment_sha256": None,
            "normalized_environment_sha256": None, "expected_environment_sha256": None}


def flatten(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def collect_tests(repo: pathlib.Path) -> tuple[list[unittest.TestCase], dict]:
    os.chdir(repo)
    if sys.path[0] != str(repo):
        sys.path.insert(0, str(repo))
    loaded = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in MODULES)
    all_tests = list(flatten(loaded))
    failed = [test.id() for test in all_tests if test.__class__.__name__ == "_FailedTest"]
    if failed:
        raise SupervisorError(f"collection contains _FailedTest: {failed}")
    all_ids = [test.id() for test in all_tests]
    if len(all_ids) != len(set(all_ids)):
        raise SupervisorError("collection contains duplicate test IDs")
    if all_ids.count(EXCLUDED) != 1:
        raise SupervisorError("the exact excluded test is absent or duplicated")
    tests = [test for test in all_tests if test.id() != EXCLUDED]
    ids = [test.id() for test in tests]
    collection = {
        "schema_version": "brandkit-harness-selftest-collection.v1",
        "modules": list(MODULES), "excluded": [EXCLUDED], "test_count": len(ids),
        "test_ids": ids, "collection_sha256": digest_bytes(canonical(ids)),
    }
    return tests, collection


def binding(repo: pathlib.Path, run_dir: pathlib.Path) -> dict:
    repo = repo.resolve(strict=True)
    run_dir = run_dir.resolve(strict=True)
    runner = pathlib.Path(__file__).resolve(strict=True)
    python = resolved_python()
    environment = closed_environment(repo)
    plan_path = run_dir / "plan.json"
    value = {
        "repo_cwd": str(repo), "python_executable": str(python),
        "python_sha256": digest_file(python), "runner_path": str(runner),
        "runner_sha256": digest_file(runner), "environment": environment,
        "environment_sha256": digest_bytes(canonical(environment)),
        "modules": list(MODULES), "excluded": [EXCLUDED],
        "run_command": [str(python), str(runner), "_run", "--plan", str(plan_path)],
        "timeout_seconds": TIMEOUT_SECONDS, "kill_grace_seconds": KILL_GRACE_SECONDS,
        "owner_euid": os.geteuid(), "sanitized_parent_environment": ["__CF_USER_TEXT_ENCODING"],
        "darwin_injected_environment": ({"__CF_USER_TEXT_ENCODING": f"0x{os.geteuid():X}:0x0:0x3"}
                                        if sys.platform == "darwin" else {}),
    }
    value["binding_sha256"] = digest_bytes(canonical(value))
    return value


def validate_plan(path: pathlib.Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise SupervisorError("plan is absent, linked, or non-regular")
    plan = json.loads(path.read_text(encoding="utf-8"))
    required = {"schema_version", "binding", "collection_path", "collection_file_sha256",
                "collection_sha256", "test_count"}
    if set(plan) != required or plan["schema_version"] != SCHEMA:
        raise SupervisorError("plan fields differ")
    repo = pathlib.Path(plan["binding"]["repo_cwd"])
    current = binding(repo, path.parent)
    require_equal_binding(plan["binding"], current)
    collection_path = pathlib.Path(plan["collection_path"])
    if collection_path != path.parent / "collection.json":
        raise SupervisorError("collection path differs")
    if digest_file(collection_path) != plan["collection_file_sha256"]:
        raise SupervisorError("collection file digest differs")
    collection = json.loads(collection_path.read_text(encoding="utf-8"))
    require_closed_collection(collection, plan["collection_sha256"], plan["test_count"])
    return plan


def require_equal_binding(expected: dict, actual: dict) -> None:
    if expected != actual:
        raise SupervisorError("execution binding differs")


def require_closed_collection(collection: dict, expected_sha256: str, expected_count: int) -> None:
    if (collection.get("modules") != list(MODULES) or collection.get("excluded") != [EXCLUDED]
            or not collection.get("test_ids") or collection.get("test_count") != expected_count
            or len(collection["test_ids"]) != expected_count
            or digest_bytes(canonical(collection["test_ids"])) != expected_sha256
            or collection.get("collection_sha256") != expected_sha256):
        raise SupervisorError("collection closure differs")


def prepare(repo: pathlib.Path, run_dir: pathlib.Path, existing_collection: pathlib.Path | None = None) -> dict:
    repo = repo.resolve(strict=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir = run_dir.resolve(strict=True)
    bound = binding(repo, run_dir)
    if existing_collection is None:
        command = [bound["python_executable"], bound["runner_path"], "_collect", "--repo", str(repo)]
        process = subprocess.run(command, cwd=repo, env=bound["environment"], capture_output=True,
                                 text=True, timeout=60, check=False)
        if process.returncode != 0:
            raise SupervisorError(f"collect preflight failed: {process.stderr.strip()}")
        collection = json.loads(process.stdout)
    else:
        collection = json.loads(existing_collection.resolve(strict=True).read_text(encoding="utf-8"))
    require_closed_collection(collection, collection.get("collection_sha256"), collection.get("test_count"))
    collection_path = run_dir / "collection.json"
    atomic_exclusive_json(collection_path, collection)
    plan = {"schema_version": SCHEMA, "binding": bound, "collection_path": str(collection_path),
            "collection_file_sha256": digest_file(collection_path),
            "collection_sha256": collection["collection_sha256"], "test_count": collection["test_count"]}
    atomic_exclusive_json(run_dir / "plan.json", plan)
    validate_plan(run_dir / "plan.json")
    return plan


def snapshot_group(pgid: int, owner_euid: int) -> dict:
    try:
        result = subprocess.run(["/bin/ps", "-axo", "pid=,pgid=,uid=,stat="], capture_output=True,
                                text=True, timeout=2.0, check=False,
                                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired):
        return {"state": "unknown", "members": [], "sha256": None}
    if result.returncode != 0:
        return {"state": "unknown", "members": [], "sha256": digest_bytes(result.stderr.encode())}
    rows = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 4:
            return {"state": "unknown", "members": [], "sha256": digest_bytes(result.stdout.encode())}
        try: pid, candidate, uid = map(int, fields[:3])
        except ValueError: return {"state": "unknown", "members": [], "sha256": digest_bytes(result.stdout.encode())}
        if not fields[3] or any(character.isspace() for character in fields[3]):
            return {"state": "unknown", "members": [], "sha256": digest_bytes(result.stdout.encode())}
        if candidate == pgid: rows.append({"pid": pid, "pgid": candidate, "uid": uid, "stat": fields[3]})
    state = "empty" if not rows else "members" if all(row["uid"] == owner_euid for row in rows) else "foreign"
    return {"state": state, "members": rows, "sha256": digest_bytes(canonical(rows))}


def cleanup_group(pgid: int, grace: float, owner_euid: int | None = None) -> dict:
    """Tri-state proof; EPERM is never interpreted as an empty group."""
    owner = os.geteuid() if owner_euid is None else owner_euid
    status = {"term_sent": False, "kill_sent": False, "group_reaped": False,
              "cleanup_probe": "unknown", "membership_sha256": None}
    try: os.killpg(pgid, 0)
    except ProcessLookupError:
        status.update(group_reaped=True, cleanup_probe="killpg-esrch"); return status
    except PermissionError:
        status["cleanup_probe"] = "killpg-eperm-ps"
    snapshots = [snapshot_group(pgid, owner), snapshot_group(pgid, owner)]
    status["membership_sha256"] = digest_bytes(canonical([row["sha256"] for row in snapshots]))
    if all(row["state"] == "empty" for row in snapshots):
        status["group_reaped"] = True; return status
    current = snapshots[-1]
    if current["state"] != "members": return status
    try: os.killpg(pgid, signal.SIGTERM); status["term_sent"] = True
    except (ProcessLookupError, PermissionError): return status
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        current = snapshot_group(pgid, owner)
        if current["state"] == "empty":
            confirm = snapshot_group(pgid, owner)
            if confirm["state"] == "empty": status["group_reaped"] = True; return status
        if current["state"] in {"foreign", "unknown"}: return status
        time.sleep(min(0.05, grace))
    current = snapshot_group(pgid, owner)
    if current["state"] != "members": return status
    try: os.killpg(pgid, signal.SIGKILL); status["kill_sent"] = True
    except (ProcessLookupError, PermissionError): return status
    for _ in range(2):
        current = snapshot_group(pgid, owner)
        if current["state"] != "empty": return status
    status["group_reaped"] = True; return status


def claim_attempt(run_dir: pathlib.Path, plan_path: pathlib.Path) -> tuple[str, pathlib.Path, dict]:
    attempt_id = uuid.uuid4().hex
    attempt_dir = run_dir / "attempts" / attempt_id
    claim = {"schema_version": "brandkit-harness-selftest-attempt-claim.v1",
             "attempt_id": attempt_id, "plan_path": str(plan_path),
             "plan_sha256": digest_file(plan_path)}
    # Claim the one counted slot before creating any mutable attempt state.
    # A crash after this point consumes the attempt rather than permitting a
    # silent replay.
    atomic_exclusive_json(run_dir / "counted-attempt.json", claim)
    attempt_dir.mkdir(parents=True, exist_ok=False)
    return attempt_id, attempt_dir, claim


def supervise(plan_path: pathlib.Path) -> dict:
    plan_path = plan_path.resolve(strict=True)
    plan = validate_plan(plan_path)
    bound = plan["binding"]
    attempt_id, attempt_dir, claim = claim_attempt(plan_path.parent, plan_path)
    log_path = attempt_dir / "selftest.log"
    start = {"schema_version": "brandkit-harness-selftest-attempt-start.v1",
             "attempt_id": attempt_id, "claim_sha256": digest_bytes(canonical(claim)),
             "plan_sha256": digest_file(plan_path), "binding_sha256": bound["binding_sha256"],
             "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    atomic_exclusive_json(attempt_dir / "start.json", start)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o400)
    started = time.monotonic(); term_sent = kill_sent = False
    try:
        process = subprocess.Popen(bound["run_command"], cwd=bound["repo_cwd"], env=bound["environment"],
                                   stdout=log_fd, stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        os.close(log_fd)
    try:
        returncode = process.wait(timeout=bound["timeout_seconds"])
        outcome = "pass" if returncode == 0 else "fail"
    except subprocess.TimeoutExpired:
        outcome = "timeout"; returncode = None
        try: os.killpg(process.pid, signal.SIGTERM); term_sent = True
        except ProcessLookupError: pass
        try: returncode = process.wait(timeout=bound["kill_grace_seconds"])
        except subprocess.TimeoutExpired:
            try: os.killpg(process.pid, signal.SIGKILL); kill_sent = True
            except ProcessLookupError: pass
            returncode = process.wait(timeout=bound["kill_grace_seconds"])
    cleanup = cleanup_group(process.pid, bound["kill_grace_seconds"], bound["owner_euid"])
    term_sent = term_sent or cleanup["term_sent"]
    kill_sent = kill_sent or cleanup["kill_sent"]
    group_reaped = cleanup["group_reaped"]
    end = {"schema_version": "brandkit-harness-selftest-attempt-end.v1",
           "attempt_id": attempt_id, "start_sha256": digest_file(attempt_dir / "start.json"),
           "plan_sha256": digest_file(plan_path), "log_sha256": digest_file(log_path),
           "ended_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "wall_seconds": round(time.monotonic() - started, 6), "outcome": outcome,
           "returncode": returncode, "pid": process.pid, "pgid": process.pid,
           "term_sent": term_sent, "kill_sent": kill_sent, "group_reaped": group_reaped,
           "environment_diagnostic": environment_diagnostic_from_log(log_path)}
    end["receipt_sha256"] = digest_bytes(canonical(end))
    atomic_exclusive_json(attempt_dir / "end.json", end)
    print(json.dumps(end, sort_keys=True), flush=True)
    return end


def integration_preflight(plan_path: pathlib.Path) -> dict:
    """One real no-test spawn proving the prepared binding and ledger path."""
    plan_path = plan_path.resolve(strict=True); plan = validate_plan(plan_path); bound = plan["binding"]
    attempt_id = uuid.uuid4().hex
    claim = {"schema_version": "brandkit-harness-selftest-preflight-claim.v1",
             "attempt_id": attempt_id, "plan_sha256": digest_file(plan_path)}
    atomic_exclusive_json(plan_path.parent / "integration-preflight-claim.json", claim)
    attempt_dir = plan_path.parent / "integration-preflight" / attempt_id
    attempt_dir.mkdir(parents=True, exist_ok=False)
    start = {"schema_version": "brandkit-harness-selftest-preflight-start.v1",
             "attempt_id": attempt_id, "claim_sha256": digest_bytes(canonical(claim)),
             "binding_sha256": bound["binding_sha256"]}
    atomic_exclusive_json(attempt_dir / "start.json", start)
    log_path = attempt_dir / "preflight.log"
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o400)
    command = [bound["python_executable"], bound["runner_path"], "_noop", "--plan", str(plan_path)]
    started = time.monotonic()
    try:
        process = subprocess.Popen(command, cwd=bound["repo_cwd"], env=bound["environment"],
                                   stdout=descriptor, stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        os.close(descriptor)
    try:
        returncode = process.wait(timeout=30.0); outcome = "pass" if returncode == 0 else "fail"
    except subprocess.TimeoutExpired:
        outcome = "timeout"; returncode = None
    cleanup = cleanup_group(process.pid, 2.0, bound["owner_euid"])
    end = {"schema_version": "brandkit-harness-selftest-preflight-end.v1",
           "attempt_id": attempt_id, "start_sha256": digest_file(attempt_dir / "start.json"),
           "plan_sha256": digest_file(plan_path), "log_sha256": digest_file(log_path),
           "command": command, "cwd": bound["repo_cwd"], "environment_sha256": bound["environment_sha256"],
           "wall_seconds": round(time.monotonic() - started, 6), "outcome": outcome,
           "returncode": returncode, "environment_diagnostic": environment_diagnostic_from_log(log_path), **cleanup}
    atomic_exclusive_json(attempt_dir / "end.json", end)
    print(json.dumps(end, sort_keys=True), flush=True)
    return end


def run_worker(plan_path: pathlib.Path) -> int:
    plan = validate_plan(plan_path.resolve(strict=True))
    if pathlib.Path.cwd().resolve() != pathlib.Path(plan["binding"]["repo_cwd"]):
        raise SupervisorError("worker cwd differs")
    verify_worker_environment(plan["binding"])
    tests, collection = collect_tests(pathlib.Path(plan["binding"]["repo_cwd"]))
    if (collection["collection_sha256"] != plan["collection_sha256"]
            or collection["test_count"] != plan["test_count"]):
        raise SupervisorError("live collection drifted from preflight")
    result = unittest.TextTestRunner(verbosity=2, stream=sys.stdout).run(unittest.TestSuite(tests))
    print(json.dumps({"event": "result", "tests_run": result.testsRun,
                      "failures": len(result.failures), "errors": len(result.errors),
                      "skipped": len(result.skipped), "successful": result.wasSuccessful()}, sort_keys=True))
    return 0 if result.wasSuccessful() else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare"); prepare_parser.add_argument("--repo", type=pathlib.Path, required=True); prepare_parser.add_argument("--run-dir", type=pathlib.Path, required=True); prepare_parser.add_argument("--collection", type=pathlib.Path)
    collect_parser = sub.add_parser("_collect"); collect_parser.add_argument("--repo", type=pathlib.Path, required=True)
    run_parser = sub.add_parser("_run"); run_parser.add_argument("--plan", type=pathlib.Path, required=True)
    noop_parser = sub.add_parser("_noop"); noop_parser.add_argument("--plan", type=pathlib.Path, required=True)
    supervise_parser = sub.add_parser("supervise"); supervise_parser.add_argument("--plan", type=pathlib.Path, required=True)
    preflight_parser = sub.add_parser("integration-preflight"); preflight_parser.add_argument("--plan", type=pathlib.Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare": print(json.dumps(prepare(args.repo, args.run_dir, args.collection), sort_keys=True)); return 0
    if args.command == "_collect":
        _, collection = collect_tests(args.repo.resolve(strict=True)); print(json.dumps(collection, sort_keys=True)); return 0
    if args.command == "_run": return run_worker(args.plan)
    if args.command == "_noop":
        plan = validate_plan(args.plan.resolve(strict=True))
        if pathlib.Path.cwd().resolve() != pathlib.Path(plan["binding"]["repo_cwd"]): raise SupervisorError("worker cwd differs")
        verify_worker_environment(plan["binding"])
        return 0
    if args.command == "integration-preflight":
        end = integration_preflight(args.plan)
        return 0 if end["outcome"] == "pass" and end["group_reaped"] else 1
    end = supervise(args.plan)
    return 0 if end["outcome"] == "pass" and end["group_reaped"] else 1


if __name__ == "__main__":
    try: raise SystemExit(main())
    except (SupervisorError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"harness-selftest-supervisor: {exc}", file=sys.stderr); raise SystemExit(2)
