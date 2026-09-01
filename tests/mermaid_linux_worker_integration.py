#!/usr/bin/env python3
"""Real fail-closed evidence harness for the buildless Linux mmdc worker."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "mermaid_linux_worker", ROOT / "toolchain/mermaid-svg/linux_worker.py")
worker = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(worker)

INPUT = b"flowchart LR\n  A[Chromium CAS] --> B[Node image mount]\n  B --> C[mmdc runtime mount]\n"
UNSHARE_NEWTIME_AARCH64 = "f0VMRgIBAQAAAAAAAAAAAAIAtwABAAAAeABAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAEAAOAABAAAAAAAAAAEAAAAFAAAAAAAAAAAAAAAAAEAAAAAAAAAAQAAAAAAAoAAAAAAAAACgAAAAAAAAAAAQAAAAAAAAABCA0igMgNIBAADUHwAA8WoAAFTgAwDLAgAAFMAIgNKoC4DSAQAA1A=="
UNSHARE_NEWTIME_SHA256 = "2b31935e04b12660d6e2f894edf1917498a8478441436aff663ba1f67dd9d4ba"


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical(value))


def docker_command(arguments: list[str]) -> list[str]:
    command = ["colima", "ssh", "--", "docker"]
    docker_host = os.environ.get("BRANDKIT_DOCKER_HOST")
    if docker_host:
        command.extend(["--host", docker_host])
    return [*command, *arguments]


def detached_render(lock: dict, state: dict, run_dir: Path, run_id: str) -> dict:
    input_dir = run_dir / "input"
    output_dir = run_dir / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir()
    (input_dir / "input.mmd").write_bytes(INPUT)
    name = f"brandkit-mermaid-evidence-{run_id}"
    arguments = worker.container_arguments(lock, state, name, input_dir, output_dir)
    arguments.remove("--rm")
    arguments.insert(1, "--detach")
    command = docker_command(arguments)
    container_id = worker.docker(arguments, timeout=120).stdout.strip()
    inspect = json.loads(worker.docker(["inspect", container_id], timeout=120).stdout)[0]
    process_samples = []
    renderer_status = ""
    for _ in range(240):
        top = worker.docker(["top", container_id, "-eo", "pid,args"], timeout=30, check=False)
        process_samples.append(top.stdout)
        if "--type=renderer" in top.stdout:
            probe = "for p in /proc/[0-9]*; do c=$(tr '\\0' ' ' <$p/cmdline 2>/dev/null || :); case \"$c\" in *--type=renderer*) echo CMD:$c; cat $p/status;; esac; done"
            renderer_status = worker.docker(
                ["exec", container_id, "/bin/sh", "-c", probe], timeout=30, check=False).stdout
            if renderer_status:
                break
        time.sleep(0.05)
    wait = worker.docker(["wait", container_id], timeout=lock["policy"]["timeout_seconds"])
    logs = worker.docker(["logs", container_id], timeout=30, check=False)
    output = output_dir / "output.svg"
    if wait.stdout.strip() != "0" or not output.is_file() or not renderer_status:
        raise worker.WorkerFailure("production evidence render did not close")
    if "--no-sandbox" in renderer_status or "--disable-setuid-sandbox" in renderer_status:
        raise worker.WorkerFailure("sandbox-disable flag found in production renderer")
    if "NoNewPrivs:\t1" not in renderer_status or "Seccomp:\t2" not in renderer_status:
        raise worker.WorkerFailure("production renderer sandbox status differs")
    worker.cleanup_container(name)
    evidence = {
        "run_id": run_id,
        "container_id": container_id,
        "container_name": name,
        "command": command,
        "command_sha256": sha(canonical(command)),
        "returncode": 0,
        "input_sha256": sha(INPUT),
        "output_sha256": worker.digest(output),
        "lock_sha256": worker.digest(worker.LOCK_PATH),
        "base_image_id": state["base_image_id"],
        "node_image_id": state["node_image_id"],
        "runtime_mount_inventory_sha256": state["runtime_mount_inventory_sha256"],
        "inspect": inspect,
        "process_samples": process_samples,
        "renderer_proc_status": renderer_status,
        "stderr": logs.stderr,
        "stdout": logs.stdout,
    }
    write_json(run_dir / "evidence.json", evidence)
    return evidence


def clone_file(source: Path, destination: Path) -> None:
    result = subprocess.run(["/bin/cp", "-c", str(source), str(destination)], capture_output=True, text=True)
    if result.returncode:
        raise worker.WorkerFailure(f"APFS file clone failed: {result.stderr.strip()}")


def clone_tree(source: Path, destination: Path) -> None:
    result = subprocess.run(["/bin/cp", "-cR", str(source), str(destination)], capture_output=True, text=True)
    if result.returncode:
        raise worker.WorkerFailure(f"APFS tree clone failed: {result.stderr.strip()}")


def negative_probes(lock: dict, state: dict, root: Path, scratch: Path) -> dict:
    probe_dir = root / "negative-probes"
    (probe_dir / "input").mkdir(parents=True)
    (probe_dir / "output").mkdir()
    (probe_dir / "input" / "input.mmd").write_bytes(INPUT)

    name = "brandkit-mermaid-negative-boundary"
    args = worker.container_arguments(lock, state, name, probe_dir / "input", probe_dir / "output")
    args[args.index("--entrypoint") + 1] = "/node/usr/local/bin/node"
    script = "const fs=require('fs'),net=require('net');let done=0;try{fs.writeFileSync('/blocked','x')}catch(e){if(e.code==='EROFS')done++}const s=net.connect(443,'1.1.1.1');s.on('error',e=>{if(e.code==='ENETUNREACH')done++;console.log(JSON.stringify({network:e.code,root_write:done>0?'EROFS':'FAIL'}));process.exit(done===2?0:70)});setTimeout(()=>process.exit(70),2000)"
    args.extend(["-e", script])
    boundary = worker.docker(args, timeout=10)

    payload = probe_dir / "oversize" / "input.mmd"
    payload.parent.mkdir()
    payload.write_bytes(b"A" * (lock["policy"]["max_input_bytes"] + 1))
    payload_failure = ""
    try:
        worker.render(payload, payload.parent / "output.svg")
    except worker.WorkerFailure as exc:
        payload_failure = str(exc)

    timeout_name = "brandkit-mermaid-negative-timeout"
    timeout_args = worker.container_arguments(lock, state, timeout_name, probe_dir / "input", probe_dir / "output")
    timeout_args.remove("--rm")
    timeout_args.insert(1, "--detach")
    timeout_args[timeout_args.index("--entrypoint") + 1] = "/bin/sleep"
    timeout_args.append("30")
    timeout_failure = ""
    try:
        worker.docker(timeout_args, timeout=30)
        worker.docker(["wait", timeout_name], timeout=1)
    except worker.WorkerFailure as exc:
        timeout_failure = str(exc)
    finally:
        worker.cleanup_container(timeout_name)

    mount_clone = scratch / "tampered-mount"
    clone_tree(Path(lock["runtime_mount"]["path"]), mount_clone)
    mount_clone.chmod(0o755)
    config = mount_clone / "MERMAID_CONFIG.json"
    config.chmod(0o644)
    config.write_bytes(config.read_bytes() + b" ")
    altered_mount = json.loads(json.dumps(lock))
    altered_mount["runtime_mount"]["path"] = str(mount_clone)
    mount_failure = ""
    try:
        worker.verify_runtime_mount(altered_mount)
    except worker.WorkerFailure as exc:
        mount_failure = str(exc)

    archive_clone = scratch / "tampered-base-image.tar"
    clone_file(Path(lock["base_image"]["archive_path"]), archive_clone)
    archive_clone.chmod(0o644)
    with archive_clone.open("r+b") as stream:
        stream.seek(1024)
        byte = stream.read(1)
        stream.seek(1024)
        stream.write(bytes([byte[0] ^ 1]))
    altered_archive = json.loads(json.dumps(lock))
    altered_archive["base_image"]["archive_path"] = str(archive_clone)
    archive_failure = ""
    try:
        worker.verify_archive(altered_archive["base_image"], "base_image")
    except worker.WorkerFailure as exc:
        archive_failure = str(exc)

    bad_lock = scratch / "wrong-version.json"
    wrong = json.loads(json.dumps(lock))
    wrong["schema_version"] = "mermaid-linux-worker.v999"
    write_json(bad_lock, wrong)
    version_failure = ""
    try:
        worker.load_lock(bad_lock, verify_mount=False)
    except worker.WorkerFailure as exc:
        version_failure = str(exc)

    symlink = scratch / "runtime-link"
    symlink.symlink_to(lock["runtime_mount"]["path"], target_is_directory=True)
    altered_link = json.loads(json.dumps(lock))
    altered_link["runtime_mount"]["path"] = str(symlink)
    symlink_failure = ""
    try:
        worker.verify_runtime_mount(altered_link)
    except worker.WorkerFailure as exc:
        symlink_failure = str(exc)

    newtime_binary = probe_dir / "input" / "unshare-newtime"
    newtime_binary.write_bytes(base64.b64decode(UNSHARE_NEWTIME_AARCH64))
    newtime_binary.chmod(0o555)
    if worker.digest(newtime_binary) != UNSHARE_NEWTIME_SHA256:
        raise worker.WorkerFailure("embedded CLONE_NEWTIME probe differs")
    time_name = "brandkit-mermaid-negative-newtime"
    time_args = worker.container_arguments(lock, state, time_name, probe_dir / "input", probe_dir / "output")
    time_args[time_args.index("--entrypoint") + 1] = "/input/unshare-newtime"
    newtime = worker.docker(time_args, timeout=30, check=False)
    newtime_failure = (newtime.stderr or newtime.stdout).strip()

    if not all((payload_failure, timeout_failure, mount_failure, archive_failure,
                version_failure, symlink_failure, newtime.returncode == 1)):
        raise worker.WorkerFailure("negative probes did not fail closed")
    result = {
        "boundary": json.loads(boundary.stdout),
        "payload_failure": payload_failure,
        "timeout_failure": timeout_failure,
        "mount_failure": mount_failure,
        "archive_failure": archive_failure,
        "version_failure": version_failure,
        "symlink_failure": symlink_failure,
        "clone_newtime_returncode": newtime.returncode,
        "clone_newtime_failure": newtime_failure,
        "clone_newtime_probe_sha256": UNSHARE_NEWTIME_SHA256,
    }
    write_json(probe_dir / "evidence.json", result)
    return result


def write_hash_manifest(root: Path) -> str:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name != "files.sha256":
            rows.append(f"{worker.digest(path)}  {path.relative_to(root).as_posix()}")
    payload = ("\n".join(rows) + "\n").encode("ascii")
    (root / "files.sha256").write_bytes(payload)
    for row in rows:
        expected, relative = row.split("  ", 1)
        if worker.digest(root / relative) != expected:
            raise worker.WorkerFailure("evidence hash manifest does not self-verify")
    return sha(payload)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: mermaid_linux_worker_integration.py EVIDENCE_DIR")
    evidence = Path(sys.argv[1])
    if evidence.exists() or evidence.is_symlink():
        raise SystemExit("evidence directory must be fresh")
    evidence.mkdir(parents=True)
    scratch = Path("/Volumes/Daten/Projekte/BrandKit/.runtime") / f"buildless-negative-{uuid.uuid4().hex}"
    scratch.mkdir()
    try:
        lock = worker.load_lock()
        state = worker.ensure_runtime(lock)
        first = detached_render(lock, state, evidence / "cleanroom-a", "cleanroom-a")
        second = detached_render(lock, state, evidence / "cleanroom-b", "cleanroom-b")
        if first["output_sha256"] != second["output_sha256"]:
            raise worker.WorkerFailure("cleanroom SVGs differ")
        negatives = negative_probes(lock, state, evidence, scratch)
        manifest = {
            "schema_version": "mermaid-buildless-worker-evidence.v1",
            "lock_sha256": worker.digest(worker.LOCK_PATH),
            "base_image_id": state["base_image_id"],
            "node_image_id": state["node_image_id"],
            "runtime_mount_inventory_sha256": state["runtime_mount_inventory_sha256"],
            "input_sha256": sha(INPUT),
            "output_sha256": first["output_sha256"],
            "cleanrooms": [first["run_id"], second["run_id"]],
            "negative_probes": negatives,
        }
        write_json(evidence / "manifest.json", manifest)
        manifest["files_sha256"] = write_hash_manifest(evidence)
        print(json.dumps(manifest, sort_keys=True))
        return 0
    finally:
        if scratch.exists():
            worker.runtime.remove_partial_tree(scratch)


if __name__ == "__main__":
    raise SystemExit(main())
