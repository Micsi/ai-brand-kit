#!/usr/bin/env python3
"""Sign, verify, and narrowly compare complete PDF E2E golden roots.

The comparison contract is intentionally fail-closed.  The only operational
variance is the real Linux-worker duration and fields cryptographically or
inventory-wise derived from that duration.  Every original envelope is
verified before any normalization takes place.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from pypdf import PdfReader


PROFILE_ORDER = (
    "report",
    "offer",
    "freeform",
    "one-pager",
    "rich-report",
    "benchmark-report",
    "report-toggle-on",
    "report-toggle-off",
)
DOCUMENT_TYPE = {
    "report": "report",
    "offer": "offer",
    "freeform": "freeform",
    "one-pager": "one-pager",
    "rich-report": "report",
    "benchmark-report": "report",
    "report-toggle-on": "report",
    "report-toggle-off": "report",
}
SOURCE_NAME = {
    "report": "report",
    "offer": "offer",
    "freeform": "freeform",
    "one-pager": "one-pager",
    "rich-report": "rich-report",
    "benchmark-report": "benchmark-report",
    "report-toggle-on": "report",
    "report-toggle-off": "report",
}
PAGE_COUNT = {
    "report": 2,
    "offer": 2,
    "freeform": 2,
    "one-pager": 1,
    "rich-report": 5,
    "benchmark-report": 5,
    "report-toggle-on": 2,
    "report-toggle-off": 2,
}
FIXTURE_OWNER_EVIDENCE = {
    "evidence_origin": "deterministic-e2e-fixture-not-provider-evidence"
}
SOURCE_PIN_PATHS = (
    "scripts/generate_pdf_e2e_goldens.py",
    "scripts/pdf_e2e_repro.py",
    "scripts/pdf_renderer.py",
    "scripts/pdf_visual_qa.py",
    "toolchain/pdf-renderer.production.v1.lock.json",
    "toolchain/mermaid-svg.v1.lock.json",
    "toolchain/mermaid-linux-worker.v2.lock.json",
    "toolchain/graphviz-svg.v1.lock.json",
    "renderer-trust.v1.json",
    "schemas/renderer-trust.v1.schema.json",
    "markdown-contract.v2.json",
    "markdown-contract-migration.v1.json",
    "document-types/report.v1.json",
    "document-types/offer.v1.json",
    "document-types/freeform.v1.json",
    "document-types/one-pager.v1.json",
    "fixtures/pdf-renderer/e2e/report.md",
    "fixtures/pdf-renderer/e2e/offer.md",
    "fixtures/pdf-renderer/e2e/freeform.md",
    "fixtures/pdf-renderer/e2e/one-pager.md",
    "fixtures/pdf-renderer/e2e/rich-report.md",
    "fixtures/pdf-renderer/e2e/benchmark-report.md",
)
WORKER = "rich-report/renderer-work/fig-flow-rich-linux-worker/worker-evidence.json"
RICH_RESULT = "rich-report/render-result.json"
PRIVATE_PATH = re.compile(r"(?:/Users/|/Volumes/|/private/|/var/folders/|/tmp/)")
OLD_ENGINE = re.compile(r"brandkit-pdf-renderer 1\.2\.(?:[0-9]|1[0-9]|2[0-7])\b")
TEMP_NAME = re.compile(r"(?:^|/)(?:\.DS_Store|[^/]+(?:\.tmp|\.temp|\.swp|~))$")


class ReproError(RuntimeError):
    """A closed reproducibility contract failed."""


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReproError(f"JSON root is not an object: {path}")
    return value


def stable_key(path: Path) -> bytes:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ReproError("issuer key must be a regular non-symlink file")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ReproError("issuer key must not be group/world accessible")
    data = path.read_bytes()
    if len(data) < 32:
        raise ReproError("issuer key is too short")
    return data


def safe_relative(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and "\\" not in value


def closed_inventory(root: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for base, dirs, files in os.walk(root, followlinks=False):
        base_path = Path(base)
        for name in dirs + files:
            path = base_path / name
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ReproError(f"golden root contains a symlink: {relative}")
            if name in files and not stat.S_ISREG(mode):
                raise ReproError(f"golden root contains a non-regular file: {relative}")
        for name in files:
            path = base_path / name
            relative = path.relative_to(root).as_posix()
            if relative == "inventory.json":
                continue
            if not safe_relative(relative) or TEMP_NAME.search(relative) or relative in seen:
                raise ReproError(f"unsafe, temporary, or duplicate inventory path: {relative}")
            seen.add(relative)
            entries.append({"path": relative, "sha256": sha(path), "size_bytes": path.stat().st_size})
    # Match pathlib's component-wise ordering used by the generator.  A raw
    # string sort would place ``report-toggle-*`` before ``report/...`` and
    # falsely reject an otherwise byte-closed generator inventory.
    entries.sort(key=lambda item: PurePosixPath(item["path"]).parts)
    return {"excluded": ["inventory.json:self-referential"], "entry_count": len(entries), "entries": entries}


def signed_envelope(value: dict[str, Any], key: bytes, schema: str) -> dict[str, Any]:
    if set(value) != {"schema_version", "issuer", "payload", "hmac_sha256"}:
        raise ReproError(f"{schema} envelope fields are not closed")
    if value.get("schema_version") != schema:
        raise ReproError(f"unexpected envelope schema: {value.get('schema_version')!r}")
    signed = {name: value[name] for name in ("schema_version", "issuer", "payload")}
    expected = hmac.new(key, canonical(signed), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, value["hmac_sha256"]):
        raise ReproError(f"{schema} HMAC differs")
    return value


def records_by_profile(run: dict[str, Any]) -> dict[str, dict[str, Any]]:
    documents = run.get("documents")
    toggles = run.get("report_activation_double_render")
    if not isinstance(documents, list) or not isinstance(toggles, list):
        raise ReproError("run profile collections are not lists")
    values: list[dict[str, Any]] = []
    for item in documents:
        if not isinstance(item, dict):
            raise ReproError("run document profile is not an object")
        values.append(item)
    for item in toggles:
        if not isinstance(item, dict) or not isinstance(item.get("document"), dict):
            raise ReproError("run activation profile is not an object")
        values.append(item["document"])
    identities = [item.get("id") for item in values]
    if any(not isinstance(profile, str) for profile in identities):
        raise ReproError("run profile id is not a string")
    if len(identities) != len(set(identities)):
        raise ReproError("run profile set contains a duplicate")
    unknown = sorted(set(identities) - set(PROFILE_ORDER))
    missing = sorted(set(PROFILE_ORDER) - set(identities))
    if unknown or missing or len(identities) != len(PROFILE_ORDER):
        raise ReproError(f"run profile set differs: unknown={unknown}, missing={missing}")
    return {item["id"]: item for item in values}


def expected_project_identity(profile: str) -> str:
    if profile not in PROFILE_ORDER:
        raise ReproError(f"unknown deterministic fixture profile: {profile!r}")
    return sha_bytes(f"brandkit-pdf-e2e-project.v1:{profile}".encode("utf-8"))


def require_project_identity(profile: str, value: object, location: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ReproError(f"project identity format differs for {profile} in {location}")
    expected = expected_project_identity(profile)
    if value != expected:
        raise ReproError(f"project identity differs for {profile} in {location}")
    return value


def verify_root_profile_summaries(
    claimed: object, observed: list[dict[str, Any]]
) -> None:
    if not isinstance(claimed, list):
        raise ReproError("root profile summaries are not a list")
    identities = [item.get("id") if isinstance(item, dict) else None for item in claimed]
    if identities != list(PROFILE_ORDER) or len(identities) != len(set(identities)):
        raise ReproError("root profile summary set/order differs")
    project_identities = [
        require_project_identity(item["id"], item.get("project_identity_sha256"), "root summary")
        for item in claimed
    ]
    if len(set(project_identities)) != len(PROFILE_ORDER):
        raise ReproError("root project identities are not unique across profiles")
    if claimed != observed:
        raise ReproError("root profile summaries differ")


def verify_profiles(
    root: Path, source_root: Path, key: bytes, run: dict[str, Any]
) -> list[dict[str, Any]]:
    actual_dirs = {path.name for path in root.iterdir() if path.is_dir()}
    if actual_dirs != set(PROFILE_ORDER):
        raise ReproError(f"profile directory set differs: {sorted(actual_dirs)}")
    records = records_by_profile(run)
    summaries: list[dict[str, Any]] = []
    project_identities: list[str] = []
    page_total = 0
    for name in PROFILE_ORDER:
        project = root / name
        record = records[name]
        required = {
            f"{name}.pdf",
            f"{name}.html",
            "humanizer-audit.json",
            "owner-approval.fixture.json",
            "render-result.json",
            "visual-qa.json",
            "renderer-work/final.ast.json",
            "renderer-work/renderer-source-map.json",
        }
        missing = sorted(relative for relative in required if not (project / relative).is_file())
        if missing:
            raise ReproError(f"required evidence missing for {name}: {missing}")
        result = signed_envelope(load(project / "render-result.json"), key, "render-result.v1")
        audit = signed_envelope(load(project / "humanizer-audit.json"), key, "humanizer-audit.v1")
        if result["issuer"] != audit["issuer"]:
            raise ReproError(f"issuer differs for {name}")
        payload = result["payload"]
        pdf = project / f"{name}.pdf"
        html = project / f"{name}.html"
        ast = project / "renderer-work/final.ast.json"
        source_map = project / "renderer-work/renderer-source-map.json"
        qa_path = project / "visual-qa.json"
        pages = sorted((project / "pages").glob("page-*.png"))
        pdf_pages = len(PdfReader(str(pdf)).pages)
        if pdf_pages != PAGE_COUNT[name] or len(pages) != pdf_pages:
            raise ReproError(f"PDF/page count differs for {name}")
        page_total += pdf_pages
        if payload["output_pdf"] != {"path": f"{name}.pdf", "sha256": sha(pdf)}:
            raise ReproError(f"PDF binding differs for {name}")
        expected_pngs = [
            {"index": index, "path": f"pages/{page.name}", "sha256": sha(page)}
            for index, page in enumerate(pages, 1)
        ]
        if payload["pages"]["count"] != pdf_pages or payload["visual_qa"]["page_pngs"] != expected_pngs:
            raise ReproError(f"page PNG binding differs for {name}")
        qa = load(qa_path)
        if qa.get("status") != "pass" or payload["visual_qa"]["status"] != "pass":
            raise ReproError(f"visual QA did not pass for {name}")
        if payload["visual_qa"]["sha256"] != sha(qa_path):
            raise ReproError(f"visual QA hash differs for {name}")
        if any(value != [] for value in qa.get("findings", {}).values()):
            raise ReproError(f"visual QA findings remain for {name}")
        if payload["binding"]["humanizer_audit_sha256"] != sha(project / "humanizer-audit.json"):
            raise ReproError(f"Humanizer binding differs for {name}")
        if payload["binding"]["owner_approval_sha256"] != sha(project / "owner-approval.fixture.json"):
            raise ReproError(f"owner-evidence binding differs for {name}")
        if load(project / "owner-approval.fixture.json") != FIXTURE_OWNER_EVIDENCE:
            raise ReproError(f"owner evidence is not the exact deterministic fixture for {name}")
        project_identity = require_project_identity(
            name, payload.get("binding", {}).get("project_identity_sha256"), "render result"
        )
        project_identities.append(project_identity)
        source = source_root / f"fixtures/pdf-renderer/e2e/{SOURCE_NAME[name]}.md"
        expected_source = {
            "path": f"fixtures/pdf-renderer/e2e/{SOURCE_NAME[name]}.md",
            "sha256": sha(source),
        }
        if record.get("source") != expected_source or record.get("document_type") != DOCUMENT_TYPE[name]:
            raise ReproError(f"source/document binding differs for {name}")
        summary = {
            "id": name,
            "document_type": DOCUMENT_TYPE[name],
            "document_type_version": payload["binding"]["document_type"]["version"],
            "document_type_sha256": payload["binding"]["document_type"]["sha256"],
            "source_path": expected_source["path"],
            "source_sha256": expected_source["sha256"],
            "page_count": pdf_pages,
            "page_png_count": len(pages),
            "pdf_sha256": sha(pdf),
            "html_sha256": sha(html),
            "ast_sha256": sha(ast),
            "source_map_sha256": sha(source_map),
            "visual_qa_sha256": sha(qa_path),
            "humanizer_audit_sha256": sha(project / "humanizer-audit.json"),
            "owner_evidence_sha256": sha(project / "owner-approval.fixture.json"),
            "project_identity_sha256": project_identity,
            "render_result_sha256": sha(project / "render-result.json"),
            "render_result_hmac_sha256": result["hmac_sha256"],
        }
        if record["pdf"]["sha256"] != summary["pdf_sha256"]:
            raise ReproError(f"root PDF summary differs for {name}")
        if record["ast"]["sha256"] != summary["ast_sha256"]:
            raise ReproError(f"root AST summary differs for {name}")
        if record["source_map"]["sha256"] != summary["source_map_sha256"]:
            raise ReproError(f"root source-map summary differs for {name}")
        if record["render_result"]["sha256"] != summary["render_result_sha256"]:
            raise ReproError(f"root render-result summary differs for {name}")
        summaries.append(summary)
    if page_total != 21:
        raise ReproError(f"closed page total differs: {page_total}")
    if len(set(project_identities)) != len(PROFILE_ORDER):
        raise ReproError("render-result project identities are not unique across profiles")
    return summaries


def source_pins(source_root: Path) -> list[dict[str, Any]]:
    pins = []
    for relative in SOURCE_PIN_PATHS:
        path = source_root / relative
        if not path.is_file() or path.is_symlink():
            raise ReproError(f"source pin is not a regular file: {relative}")
        pins.append({"path": relative, "sha256": sha(path), "size_bytes": path.stat().st_size})
    return pins


def text_hygiene(root: Path) -> None:
    binary = {".pdf", ".png", ".jpg", ".jpeg", ".woff", ".woff2", ".ttf"}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.suffix.lower() in binary:
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="strict")
        if PRIVATE_PATH.search(text):
            raise ReproError(f"private/absolute path leaked into {path.relative_to(root)}")
        if OLD_ENGINE.search(text):
            raise ReproError(f"old renderer version leaked into {path.relative_to(root)}")


def verify_root(
    root: Path,
    source_root: Path,
    key_path: Path,
    expected_tree: str,
    canonical_root: str,
) -> dict[str, Any]:
    root = root.resolve()
    source_root = source_root.resolve()
    key = stable_key(key_path)
    inventory = signed_envelope(load(root / "inventory.json"), key, "brandkit-pdf-e2e-goldens.v3")
    payload = inventory["payload"]
    expected_release = {
        "version": "1.2.28",
        "source_tree": expected_tree,
        "canonical_root": canonical_root,
    }
    if payload.get("release") != expected_release:
        raise ReproError("release/tree/root binding differs")
    trust = load(source_root / "renderer-trust.v1.json")
    if inventory["issuer"] != trust.get("issuer") or sha_bytes(key) != trust.get("issuer_key_sha256"):
        raise ReproError("root issuer/key fingerprint differs from renderer trust")
    if payload.get("source_pins") != source_pins(source_root):
        raise ReproError("root source pins differ")
    observed_inventory = closed_inventory(root)
    if payload.get("closed_file_inventory") != observed_inventory:
        raise ReproError("root closed inventory differs")
    if observed_inventory["entry_count"] != 95:
        raise ReproError(f"closed member count differs: {observed_inventory['entry_count']}")
    run = payload.get("run")
    if not isinstance(run, dict) or run.get("status") != "pass" or run.get("failures") != []:
        raise ReproError("root run did not pass cleanly")
    isolation = run.get("document_isolation", {})
    expected_counts = {"attempted": 8, "rendered": 8, "failed": 0, "skipped": 0, "materialized_projects": 8}
    if {name: isolation.get(name) for name in expected_counts} != expected_counts:
        raise ReproError("document isolation counts differ")
    summaries = verify_profiles(root, source_root, key, run)
    verify_root_profile_summaries(payload.get("profiles"), summaries)
    text_hygiene(root)
    worker = load(root / WORKER)
    rich_result = load(root / RICH_RESULT)
    if rich_result["payload"]["mermaid"][0]["linux_worker"] != worker:
        raise ReproError("Rich result does not bind exact worker evidence")
    duration = worker.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
        raise ReproError("worker duration is not a positive measurement")
    return inventory


def sign_root(
    root: Path,
    source_root: Path,
    key_path: Path,
    expected_tree: str,
    canonical_root: str,
) -> dict[str, Any]:
    root = root.resolve()
    source_root = source_root.resolve()
    key = stable_key(key_path)
    generated = load(root / "inventory.json")
    if generated.get("schema_version") != "brandkit-pdf-e2e-goldens.v2":
        raise ReproError("signing requires untouched v2 generator inventory")
    if generated.get("closed_file_inventory") != closed_inventory(root):
        raise ReproError("generator inventory is not closed before signing")
    summaries = verify_profiles(root, source_root, key, generated)
    run = {name: value for name, value in generated.items() if name not in {"schema_version", "closed_file_inventory"}}
    payload = {
        "release": {"version": "1.2.28", "source_tree": expected_tree, "canonical_root": canonical_root},
        "engine": {"name": "brandkit-pdf-renderer", "version": "1.2.28"},
        "source_pins": source_pins(source_root),
        "profiles": summaries,
        "run": run,
        "closed_file_inventory": closed_inventory(root),
    }
    signed = {
        "schema_version": "brandkit-pdf-e2e-goldens.v3",
        "issuer": "brandkit-production-renderer-v1",
        "payload": payload,
    }
    inventory = {**signed, "hmac_sha256": hmac.new(key, canonical(signed), hashlib.sha256).hexdigest()}
    (root / "inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return verify_root(root, source_root, key_path, expected_tree, canonical_root)


def json_pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def recursive_json_pointers(left: object, right: object, pointer: str = "") -> list[str]:
    if type(left) is not type(right):
        return [pointer or "/"]
    if isinstance(left, dict):
        differences: list[str] = []
        for key in sorted(set(left) | set(right)):
            child = f"{pointer}/{json_pointer_token(str(key))}"
            if key not in left or key not in right:
                differences.append(child)
            else:
                differences.extend(recursive_json_pointers(left[key], right[key], child))
        return differences
    if isinstance(left, list):
        if len(left) != len(right):
            return [f"{pointer}/length"]
        differences = []
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            differences.extend(recursive_json_pointers(left_item, right_item, f"{pointer}/{index}"))
        return differences
    return [] if left == right else [pointer or "/"]


def require_allowed(observed: list[str], allowed: set[str], label: str) -> None:
    foreign = sorted(set(observed) - allowed)
    if foreign:
        raise ReproError(f"{label} contains non-allowlisted JSON pointers: {foreign}")


def normalize_member(relative: str, data: bytes, key: bytes) -> bytes:
    if relative == WORKER:
        value = json.loads(data)
        value["duration_seconds"] = 0.0
        return canonical(value)
    if relative == RICH_RESULT:
        value = json.loads(data)
        value["payload"]["mermaid"][0]["linux_worker"]["duration_seconds"] = 0.0
        signed = {name: value[name] for name in ("schema_version", "issuer", "payload")}
        value["hmac_sha256"] = hmac.new(key, canonical(signed), hashlib.sha256).hexdigest()
        return canonical(value)
    return data


def inventory_allowlist(inventory: dict[str, Any]) -> set[str]:
    payload = inventory["payload"]
    profiles = payload["profiles"]
    profile_index = next(index for index, item in enumerate(profiles) if item["id"] == "rich-report")
    documents = payload["run"]["documents"]
    document_index = next(index for index, item in enumerate(documents) if item["id"] == "rich-report")
    entries = payload["closed_file_inventory"]["entries"]
    entry_by_path = {item["path"]: index for index, item in enumerate(entries)}
    if set((WORKER, RICH_RESULT)) - set(entry_by_path):
        raise ReproError("operational members are absent from closed inventory")
    allowed = {
        "/hmac_sha256",
        f"/payload/profiles/{profile_index}/render_result_sha256",
        f"/payload/profiles/{profile_index}/render_result_hmac_sha256",
        f"/payload/run/documents/{document_index}/render_result/sha256",
    }
    for relative in (RICH_RESULT, WORKER):
        index = entry_by_path[relative]
        allowed.add(f"/payload/closed_file_inventory/entries/{index}/sha256")
        allowed.add(f"/payload/closed_file_inventory/entries/{index}/size_bytes")
    return allowed


def normalized_inventory(
    inventory: dict[str, Any], paths: list[str], members: dict[str, bytes], key: bytes
) -> bytes:
    value = copy.deepcopy(inventory)
    profiles = value["payload"]["profiles"]
    rich_profile = next(item for item in profiles if item["id"] == "rich-report")
    normalized_result = json.loads(members[RICH_RESULT])
    rich_profile["render_result_sha256"] = sha_bytes(members[RICH_RESULT])
    rich_profile["render_result_hmac_sha256"] = normalized_result["hmac_sha256"]
    records = records_by_profile(value["payload"]["run"])
    records["rich-report"]["render_result"]["sha256"] = sha_bytes(members[RICH_RESULT])
    entries = value["payload"]["closed_file_inventory"]["entries"]
    if [item["path"] for item in entries] != paths:
        raise ReproError("root member order differs from signed inventory")
    entry_by_path = {item["path"]: item for item in entries}
    for relative in (RICH_RESULT, WORKER):
        entry_by_path[relative]["sha256"] = sha_bytes(members[relative])
        entry_by_path[relative]["size_bytes"] = len(members[relative])
    signed = {name: value[name] for name in ("schema_version", "issuer", "payload")}
    value["hmac_sha256"] = hmac.new(key, canonical(signed), hashlib.sha256).hexdigest()
    return canonical(value)


def compare_roots(
    left_root: Path,
    right_root: Path,
    left_source: Path,
    right_source: Path,
    key_path: Path,
    expected_tree: str,
    canonical_root: str,
) -> dict[str, Any]:
    key = stable_key(key_path)
    left_inventory = verify_root(left_root, left_source, key_path, expected_tree, canonical_root)
    right_inventory = verify_root(right_root, right_source, key_path, expected_tree, canonical_root)
    left_paths = [item["path"] for item in left_inventory["payload"]["closed_file_inventory"]["entries"]]
    right_paths = [item["path"] for item in right_inventory["payload"]["closed_file_inventory"]["entries"]]
    if left_paths != right_paths:
        raise ReproError("closed member path sets/orders differ")
    left_raw = {relative: (left_root / relative).read_bytes() for relative in left_paths}
    right_raw = {relative: (right_root / relative).read_bytes() for relative in right_paths}
    raw_differences = [relative for relative in left_paths if left_raw[relative] != right_raw[relative]]
    unexpected_members = sorted(set(raw_differences) - {WORKER, RICH_RESULT})
    if unexpected_members:
        raise ReproError(f"unexpected raw member differences: {unexpected_members}")

    worker_differences = recursive_json_pointers(
        json.loads(left_raw[WORKER]), json.loads(right_raw[WORKER])
    )
    result_differences = recursive_json_pointers(
        json.loads(left_raw[RICH_RESULT]), json.loads(right_raw[RICH_RESULT])
    )
    worker_allowed = {"/duration_seconds"}
    result_allowed = {"/hmac_sha256", "/payload/mermaid/0/linux_worker/duration_seconds"}
    require_allowed(worker_differences, worker_allowed, "worker evidence")
    require_allowed(result_differences, result_allowed, "Rich render result")
    if bool(worker_differences) != bool(result_differences):
        raise ReproError("duration variance and its signed Rich result do not co-vary")
    if worker_differences and set(result_differences) != result_allowed:
        raise ReproError("Rich result omits a duration-derived HMAC/payload difference")

    inventory_differences = recursive_json_pointers(left_inventory, right_inventory)
    left_allowlist = inventory_allowlist(left_inventory)
    right_allowlist = inventory_allowlist(right_inventory)
    if left_allowlist != right_allowlist:
        raise ReproError("derived root allowlist differs between originals")
    require_allowed(inventory_differences, left_allowlist, "root inventory")

    left_normalized = {relative: normalize_member(relative, left_raw[relative], key) for relative in left_paths}
    right_normalized = {relative: normalize_member(relative, right_raw[relative], key) for relative in right_paths}
    normalized_differences = [
        relative for relative in left_paths if left_normalized[relative] != right_normalized[relative]
    ]
    if normalized_differences:
        raise ReproError(f"normalized member differences remain: {normalized_differences}")
    left_root_bytes = normalized_inventory(left_inventory, left_paths, left_normalized, key)
    right_root_bytes = normalized_inventory(right_inventory, right_paths, right_normalized, key)
    if left_root_bytes != right_root_bytes:
        raise ReproError("normalized signed root inventories differ")
    normalized_root = json.loads(left_root_bytes)
    signed_envelope(normalized_root, key, "brandkit-pdf-e2e-goldens.v3")
    normalized_result = json.loads(left_normalized[RICH_RESULT])
    signed_envelope(normalized_result, key, "render-result.v1")
    descriptor = canonical(
        {
            "inventory": sha_bytes(left_root_bytes),
            "members": [
                {
                    "path": relative,
                    "sha256": sha_bytes(left_normalized[relative]),
                    "size_bytes": len(left_normalized[relative]),
                }
                for relative in left_paths
            ],
        }
    )
    protected = [
        relative
        for relative in left_paths
        if Path(relative).suffix.lower() in {".pdf", ".png", ".svg", ".html", ".mmd", ".dot"}
        or Path(relative).name in {"final.ast.json", "renderer-source-map.json", "visual-qa.json"}
    ]
    if any(left_raw[relative] != right_raw[relative] for relative in protected):
        raise ReproError("protected content differs before normalization")
    return {
        "schema_version": "brandkit-pdf-e2e-repro.v1",
        "status": "pass",
        "members": len(left_paths),
        "profiles": len(PROFILE_ORDER),
        "pages": sum(PAGE_COUNT.values()),
        "raw_identical_members": len(left_paths) - len(raw_differences),
        "raw_member_differences": raw_differences,
        "worker_json_pointer_differences": worker_differences,
        "rich_result_json_pointer_differences": result_differences,
        "root_inventory_json_pointer_differences": inventory_differences,
        "root_inventory_json_pointer_allowlist": sorted(left_allowlist),
        "normalized_member_differences": normalized_differences,
        "protected_raw_equal": len(protected),
        "original_root_hmacs": [left_inventory["hmac_sha256"], right_inventory["hmac_sha256"]],
        "original_rich_result_hmacs": [
            json.loads(left_raw[RICH_RESULT])["hmac_sha256"],
            json.loads(right_raw[RICH_RESULT])["hmac_sha256"],
        ],
        "normalized_inventory_sha256": sha_bytes(left_root_bytes),
        "normalized_root_sha256": sha_bytes(descriptor),
        "normalized_root_hmac_valid": True,
        "normalized_rich_result_hmac_valid": True,
    }


def summary(inventory: dict[str, Any]) -> dict[str, Any]:
    payload = inventory["payload"]
    return {
        "status": "pass",
        "root_hmac_sha256": inventory["hmac_sha256"],
        "members": payload["closed_file_inventory"]["entry_count"],
        "profiles": len(payload["profiles"]),
        "pages": sum(item["page_count"] for item in payload["profiles"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("sign", "verify"):
        current = subparsers.add_parser(command)
        current.add_argument("--root", type=Path, required=True)
        current.add_argument("--source-root", type=Path, required=True)
        current.add_argument("--issuer-key", type=Path, required=True)
        current.add_argument("--source-tree", required=True)
        current.add_argument("--canonical-root", required=True)
    compare = subparsers.add_parser("compare")
    compare.add_argument("--left-root", type=Path, required=True)
    compare.add_argument("--right-root", type=Path, required=True)
    compare.add_argument("--left-source", type=Path, required=True)
    compare.add_argument("--right-source", type=Path, required=True)
    compare.add_argument("--issuer-key", type=Path, required=True)
    compare.add_argument("--source-tree", required=True)
    compare.add_argument("--canonical-root", required=True)
    compare.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_tree):
        raise SystemExit("source tree must be a 40-hex Git tree")
    if args.command == "sign":
        result = summary(
            sign_root(args.root, args.source_root, args.issuer_key, args.source_tree, args.canonical_root)
        )
    elif args.command == "verify":
        result = summary(
            verify_root(args.root, args.source_root, args.issuer_key, args.source_tree, args.canonical_root)
        )
    else:
        result = compare_roots(
            args.left_root,
            args.right_root,
            args.left_source,
            args.right_source,
            args.issuer_key,
            args.source_tree,
            args.canonical_root,
        )
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8"
            )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
