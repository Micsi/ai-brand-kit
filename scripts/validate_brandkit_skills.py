#!/usr/bin/env python3
"""Validate BrandKit skills and execute artifact-producing, fail-closed dry runs."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from yaml.events import AliasEvent

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "brandkit-skill-contract.v1.json"
FIXTURES = ROOT / "fixtures/brandkit-skills/dry-runs.v1.json"
COLLISION_FIXTURES = ROOT / "fixtures/brandkit-skills/collision-dry-runs.v1.json"
CORE_SEQUENCE = ["brandkit-setup", "brandkit-interview", "brandkit-build", "brandkit-publish"]
CONTRACT_SKILLS = [*CORE_SEQUENCE, "brandkit-spacing"]
DESIGN_ROUTES = {
    "spacing-audit-or-token-role": "brandkit-spacing",
    "general-ui-redesign": "brandkit-impeccable -> impeccable",
    "brand-package-generation": "brandkit-build",
    "pdf-publication-or-layout": "brand-voice-pdf -> brandkit-publish",
    "design-md-generation-or-validation": "brandkit-impeccable",
}

SPACING_INVENTORY_SCHEMA = "schemas/spacing-inventory.v1.schema.json"
SPACING_PROPOSAL_SCHEMA = "schemas/spacing-proposal.v1.schema.json"


class GenericSmoothingError(ValueError):
    """The evidence explicitly records an unconfirmed generic smoothing pass."""


class DuplicateKeyError(ValueError):
    """A JSON object repeated a key and therefore has no unambiguous meaning."""


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_closed_object)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_confined(root: Path, path: Path) -> bool:
    return root == path or root in path.parents


def _closed(value: Any, keys: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{label}: expected exactly {sorted(keys)}")


def _strings(value: Any, label: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and (item or not nonempty) for item in value):
        raise ValueError(f"{label}: expected a list of strings")
    return value


class ClosedSafeLoader(yaml.SafeLoader):
    pass


def _mapping(loader: ClosedSafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise yaml.constructor.ConstructorError(None, None, "frontmatter keys must be strings", key_node.start_mark)
        if key in result:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate frontmatter key: {key}", key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


ClosedSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", text, re.S)
    if not match:
        raise ValueError("missing closed YAML frontmatter")
    raw, body = match.groups()
    try:
        if any(isinstance(event, AliasEvent) for event in yaml.parse(raw)):
            raise ValueError("YAML aliases are forbidden")
        meta = yaml.load(raw, Loader=ClosedSafeLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid safe YAML frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a mapping")
    if not body.strip():
        raise ValueError("SKILL.md body is empty")
    return meta, body


def validate_artifact_refs(root: Path, refs: list[dict[str, str]]) -> list[str]:
    verified: list[str] = []
    root_real = root.resolve()
    for ref in refs:
        path = root / ref["path"]
        try:
            if stat.S_ISLNK(path.lstat().st_mode):
                raise ValueError(f"input artifact is a symlink: {ref['path']}")
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"missing input artifact {ref['path']}: {exc}") from exc
        if root_real not in (resolved, *resolved.parents):
            raise ValueError(f"input artifact escapes repository: {ref['path']}")
        if not resolved.is_file() or sha256(resolved) != ref["sha256"]:
            raise ValueError(f"input artifact hash failed: {ref['path']}")
        verified.append(ref["path"])
    return verified


AUDIT_KEYS = {"register", "candidates", "advisories", "protected"}
CANDIDATE_KEYS = {"id", "source", "patterns", "reason", "goal", "action", "scope"}
PROTECTED_KEYS = {"facts", "quotes", "terms", "persona"}
REPORT_KEYS = {
    "accepted", "provider", "model", "mode", "source_sha256", "skill_sha256", "catalog_sha256",
    "runner_sha256", "prompt_sha256", "preflight_sha256", "candidate_count",
    "confirmed_candidate_count", "discarded_candidate_count", "advisory_count", "advisories",
    "edit_count", "protected_violations", "evidence_blockers", "model_calls", "model_cost_usd",
    "started_utc", "finished_utc", "catalog_source_revision", "runner_source_revision",
    "raw_model_call_sha256", "brandkit_provenance_sha256",
}
ANCHOR_KEYS = {"number", "date", "url", "doi", "paragraph", "code", "quote", "proper_name"}
FINDING_KEYS = {"severity", "kind", "message", "values"}


def _pinned_runner(release: Path) -> Any:
    script = release / "scripts/humanizer_two_pass.py"
    script_dir = str(script.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location("brandkit_pinned_humanizer_two_pass", script)
    if spec is None or spec.loader is None:
        raise ValueError("pinned humanizer runner cannot be imported")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _model_artifact_output(path: Path, provider: str) -> tuple[dict[str, Any], float | None]:
    if provider != "claude":
        raise ValueError(
            "pinned humanizer release supports Claude Code 2.1.223 only; "
            "Codex CLI 0.144.3 is fail-closed as an LLM provider"
        )
    envelope = load(path)
    schema = load(ROOT / "schemas/claude-code-2.1.223-result.schema.json")
    try:
        jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER
        ).validate(envelope)
    except jsonschema.ValidationError as exc:
        raise ValueError(f"{path.name}: not a closed Claude Code 2.1.223 result envelope: {exc.message}") from exc
    structured = envelope["structured_output"]
    if path.read_bytes() != _json_bytes(envelope):
        raise ValueError(f"{path.name}: raw envelope bytes are not the pinned runner serialization")
    if not isinstance(structured, dict):
        raise ValueError(f"{path.name}: missing Claude structured_output")
    try:
        result = json.loads(envelope["result"], object_pairs_hook=_closed_object)
    except (json.JSONDecodeError, DuplicateKeyError) as exc:
        raise ValueError(f"{path.name}: Claude result is not closed JSON") from exc
    if result != structured:
        raise ValueError(f"{path.name}: Claude result and structured_output differ")
    cost = envelope["total_cost_usd"]
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or not math.isfinite(cost) or cost < 0:
        raise ValueError(f"{path.name}: invalid Claude total_cost_usd")
    return structured, cost


def _canonical_utf8_lf(path: Path, label: str) -> tuple[bytes, str]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{label}: UTF-8 BOM is forbidden")
    if b"\r" in raw:
        raise ValueError(f"{label}: only LF line endings are canonical")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label}: expected canonical UTF-8") from exc
    if text.encode("utf-8") != raw:
        raise ValueError(f"{label}: UTF-8 bytes do not round-trip canonically")
    return raw, text


def _validate_candidate(candidate: Any, label: str, allowed_patterns: set[str]) -> None:
    _closed(candidate, CANDIDATE_KEYS, label)
    for key in ("id", "source", "reason", "goal"):
        if not isinstance(candidate[key], str) or not candidate[key]:
            raise ValueError(f"{label}.{key}: expected non-empty string")
    patterns = _strings(candidate["patterns"], f"{label}.patterns", nonempty=True)
    if len(patterns) != len(set(patterns)) or any(pattern not in allowed_patterns for pattern in patterns):
        raise ValueError(f"{label}.patterns: unknown, duplicate, aliased, or malformed catalog id")
    if candidate["action"] not in {"delete", "rewrite"} or candidate["scope"] not in {"phrase", "sentence", "heading"}:
        raise ValueError(f"{label}: invalid action/scope")


def _validate_audit(document: Any, allowed_patterns: set[str], *, confirmed: bool = False) -> None:
    keys = AUDIT_KEYS | ({"schema_version", "discarded_candidates"} if confirmed else set())
    _closed(document, keys, "confirmed-ledger" if confirmed else "audit-ledger")
    if confirmed and document["schema_version"] != 2:
        raise ValueError("confirmed-ledger.schema_version must be 2")
    if not isinstance(document["register"], str) or not document["register"]:
        raise ValueError("audit register must be non-empty")
    if not isinstance(document["candidates"], list) or not isinstance(document["advisories"], list):
        raise ValueError("audit candidates/advisories must be arrays")
    for index, candidate in enumerate(document["candidates"]):
        _validate_candidate(candidate, f"candidates[{index}]", allowed_patterns)
    for advisory in document["advisories"]:
        _closed(advisory, {"source", "reason"}, "advisory")
        if not all(isinstance(advisory[key], str) and advisory[key] for key in advisory):
            raise ValueError("advisory fields must be non-empty strings")
    _closed(document["protected"], PROTECTED_KEYS, "protected")
    for key in PROTECTED_KEYS:
        _strings(document["protected"][key], f"protected.{key}", nonempty=True)
    if confirmed:
        if not isinstance(document["discarded_candidates"], list):
            raise ValueError("discarded_candidates must be an array")
        for item in document["discarded_candidates"]:
            _closed(item, {"id", "reason"}, "discarded candidate")
            if not all(isinstance(item[key], str) and item[key] for key in item):
                raise ValueError("discarded candidate fields must be non-empty strings")


def _validate_evidence_ledger(document: Any) -> None:
    _closed(document, {"schema_version", "extraction_policy", "anchors"}, "evidence-ledger")
    if document["schema_version"] != 2 or document["extraction_policy"] != {"mode": "default"}:
        raise ValueError("evidence-ledger must use schema 2/default extraction")
    _closed(document["anchors"], ANCHOR_KEYS, "evidence-ledger.anchors")
    for key in ANCHOR_KEYS:
        values = _strings(document["anchors"][key], f"anchors.{key}")
        if len(values) != len(set(values)):
            raise ValueError(f"anchors.{key}: duplicate value")


def _validate_evidence_report(document: Any) -> None:
    _closed(document, {"ok", "findings"}, "evidence-report")
    if not isinstance(document["ok"], bool) or not isinstance(document["findings"], list):
        raise ValueError("evidence-report has invalid field types")
    for finding in document["findings"]:
        _closed(finding, FINDING_KEYS, "evidence finding")
        if finding["severity"] not in {"blocker", "warning"}:
            raise ValueError("evidence finding has invalid severity")
        if not all(isinstance(finding[key], str) and finding[key] for key in ("kind", "message")):
            raise ValueError("evidence finding has invalid text")
        _strings(finding["values"], "evidence finding values")
    if document["ok"] != (not document["findings"]):
        raise ValueError("evidence-report ok/findings disagree")


def _pattern_catalog_contract(release: Path, source_revision: str) -> dict[str, Any]:
    catalog = release / "references/patterns.md"
    rows = re.findall(r"^\| ([1-9][0-9]?) \| ([^|]+?) \|", catalog.read_text(encoding="utf-8"), re.M)
    expected_ids = [str(number) for number in range(1, 73)]
    ids = [pattern_id for pattern_id, _ in rows]
    if ids != expected_ids or len({title.strip() for _, title in rows}) != 72:
        raise ValueError("pinned pattern catalog does not enumerate canonical ids 1..72 exactly once")
    return {
        "path": "references/patterns.md",
        "source_revision": source_revision,
        "sha256": sha256(catalog),
        "canonical_pattern_ids": expected_ids,
    }


def _brandkit_provenance(
    before_bytes: bytes,
    after_bytes: bytes,
    before_text: str,
    after_text: str,
    confirmed: dict[str, Any],
    edits: dict[str, Any],
    runner: Any,
    model_hashes: dict[str, str | None],
    catalog: dict[str, Any],
    runner_revision: str,
    runner_hash: str,
) -> dict[str, Any]:
    spans = runner.candidate_spans(before_text, confirmed["candidates"])
    records: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    for edit in edits["edits"]:
        candidate_id = edit["candidate_id"]
        if candidate_id not in spans:
            raise ValueError("provenance edit references an unconfirmed candidate")
        start, end, candidate = spans[candidate_id]
        if any(start < old_end and old_start < end for old_start, old_end in occupied):
            raise ValueError("provenance candidate spans overlap")
        occupied.append((start, end))
        old = before_text[start:end]
        replacement = edit["replacement"]
        records.append({
            "candidate_id": candidate_id,
            "source": candidate["source"],
            "action": candidate["action"],
            "pattern_ids": candidate["patterns"],
            "start_codepoint": start,
            "end_codepoint": end,
            "old_substring_sha256": sha256_bytes(old.encode("utf-8")),
            "new_substring_sha256": sha256_bytes(replacement.encode("utf-8")),
        })
    return {
        "schema_version": "brandkit-humanizer-provenance.v1",
        "before_sha256": sha256_bytes(before_bytes),
        "after_sha256": sha256_bytes(after_bytes),
        "candidate_edits": records,
        "raw_model_call_sha256": model_hashes,
        "pattern_catalog": {
            "path": catalog["path"],
            "source_revision": catalog["source_revision"],
            "sha256": catalog["sha256"],
        },
        "runner": {"source_revision": runner_revision, "sha256": runner_hash},
    }


def _validate_report(
    report: Any,
    release: Path,
    before_hash: str,
    evidence_report: dict[str, Any],
    preflight: dict[str, Any],
    audit: dict[str, Any],
    confirmed: dict[str, Any],
    edits: dict[str, Any],
    runner: Any,
    before_text: str,
    model_costs: list[float | None],
    source_revision: str,
    model_hashes: dict[str, str | None],
    provenance_hash: str,
) -> None:
    _closed(report, REPORT_KEYS, "report")
    if report["accepted"] is not True or report["source_sha256"] != before_hash:
        raise ValueError("humanizer report is rejected or not bound to before bytes")
    if report["provider"] != "claude" or not isinstance(report["model"], (str, type(None))) or report["mode"] not in {"locker", "sachlich", "formal"}:
        raise ValueError("humanizer provider/model/mode is invalid")
    pinned = {
        "skill_sha256": sha256(release / "SKILL.md"),
        "catalog_sha256": sha256(release / "references/patterns.md"),
        "runner_sha256": sha256(release / "scripts/humanizer_two_pass.py"),
    }
    if any(report[key] != value for key, value in pinned.items()):
        raise ValueError("humanizer skill/catalog/runner hash does not match pinned release")
    if report["catalog_source_revision"] != source_revision or report["runner_source_revision"] != source_revision:
        raise ValueError("humanizer catalog/runner revision does not match pinned release")
    if report["raw_model_call_sha256"] != model_hashes:
        raise ValueError("humanizer report raw model-call hashes are invalid")
    if report["brandkit_provenance_sha256"] != provenance_hash:
        raise ValueError("humanizer report provenance hash is invalid")
    _closed(report["prompt_sha256"], {"audit", "rewrite"}, "report.prompt_sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", report["prompt_sha256"]["audit"] or ""):
        raise ValueError("audit prompt hash is invalid")
    rewrite_hash = report["prompt_sha256"]["rewrite"]
    if rewrite_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", rewrite_hash):
        raise ValueError("rewrite prompt hash is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", report["preflight_sha256"] or ""):
        raise ValueError("preflight hash is invalid")
    expected_audit_prompt = runner.audit_prompt(before_text, report["mode"], preflight)
    expected_rewrite_prompt = runner.rewrite_prompt(before_text, confirmed) if confirmed["candidates"] else None
    expected_prompts = {
        "audit": sha256_bytes(expected_audit_prompt.encode("utf-8")),
        "rewrite": sha256_bytes(expected_rewrite_prompt.encode("utf-8")) if expected_rewrite_prompt else None,
    }
    if report["prompt_sha256"] != expected_prompts:
        raise ValueError("humanizer prompt hashes do not match the pinned runner inputs")
    expected_preflight_hash = sha256_bytes(json.dumps(preflight, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    if report["preflight_sha256"] != expected_preflight_hash:
        raise ValueError("humanizer preflight hash does not match preflight.json")
    counts = ("candidate_count", "confirmed_candidate_count", "discarded_candidate_count", "advisory_count", "edit_count", "model_calls")
    if any(not isinstance(report[key], int) or isinstance(report[key], bool) or report[key] < 0 for key in counts):
        raise ValueError("humanizer report counts are invalid")
    if report["model_calls"] not in {1, 2} or report["model_calls"] != 1 + bool(report["confirmed_candidate_count"]):
        raise ValueError("humanizer report model call count is inconsistent")
    if report["advisory_count"] != len(report["advisories"]):
        raise ValueError("humanizer advisory count is inconsistent")
    if (
        report["candidate_count"] != len(audit["candidates"])
        or report["confirmed_candidate_count"] != len(confirmed["candidates"])
        or report["discarded_candidate_count"] != len(confirmed["discarded_candidates"])
        or report["advisory_count"] != len(confirmed["advisories"])
        or report["advisories"] != confirmed["advisories"]
        or report["edit_count"] != len(edits["edits"])
    ):
        raise ValueError("runner report counts disagree with exact runner artifacts")
    if not isinstance(report["protected_violations"], list) or report["protected_violations"]:
        raise ValueError("humanizer protected anchors were violated")
    blockers = [item for item in evidence_report["findings"] if item["severity"] == "blocker"]
    if not isinstance(report["evidence_blockers"], list) or report["evidence_blockers"] != blockers:
        raise ValueError("humanizer evidence blockers disagree with pinned lint report")
    expected_cost = sum(cost or 0 for cost in model_costs) if model_costs and model_costs[0] is not None else None
    if report["model_cost_usd"] != expected_cost:
        raise ValueError("humanizer model cost disagrees with model-call artifacts")
    if report["model_cost_usd"] is not None and (not isinstance(report["model_cost_usd"], (int, float)) or isinstance(report["model_cost_usd"], bool) or report["model_cost_usd"] < 0):
        raise ValueError("humanizer model cost is invalid")
    if not all(isinstance(report[key], str) and report[key] for key in ("started_utc", "finished_utc")):
        raise ValueError("humanizer timestamps are invalid")


def validate_humanizer_evidence(root: Path, relative: str) -> list[str]:
    evidence_path = root / relative
    evidence = load(evidence_path)
    jsonschema.Draft202012Validator(load(root / "schemas/brandkit-humanizer-evidence.v1.schema.json")).validate(evidence)
    names = [item["name"] for item in evidence["artifacts"]]
    required = {
        "original.md", "preflight.json", "audit-call.json", "audit-ledger.json",
        "confirmed-ledger.json", "edits.json", "evidence-ledger.json",
        "evidence-report.json", "report.json", "result.md", "changes.diff",
        "brandkit-provenance.json",
    }
    if evidence["generic_smoothing"]:
        raise GenericSmoothingError("humanizer evidence records generic smoothing")
    if set(names) not in (required, required | {"rewrite-call.json"}) or len(names) != len(set(names)) or not evidence["accepted"]:
        raise ValueError("humanizer evidence is incomplete or rejected")
    verified = validate_artifact_refs(root, evidence["artifacts"])
    refs = {item["name"]: root / item["path"] for item in evidence["artifacts"]}
    before, after = refs["original.md"], refs["result.md"]
    before_bytes, before_text = _canonical_utf8_lf(before, "original.md")
    after_bytes, after_text = _canonical_utf8_lf(after, "result.md")
    lint = evidence["evidence_lint"]
    if lint["before_path"] != next(item["path"] for item in evidence["artifacts"] if item["name"] == "original.md") or lint["after_path"] != next(item["path"] for item in evidence["artifacts"] if item["name"] == "result.md"):
        raise ValueError("evidence lint before/after paths are not artifact-bound")
    if lint["before_sha256"] != sha256(before) or lint["after_sha256"] != sha256(after):
        raise ValueError("evidence lint before/after hashes are invalid")
    lock = load(root / "toolchain/humanizer-de/toolchain.lock.json")["skill"]
    if evidence["humanizer_release"] != {key: lock[key] for key in ("name", "version", "source_revision", "content_sha256")}:
        raise ValueError("humanizer evidence release does not match committed lock")
    release = Path(os.path.expanduser("~/.agents/skill-store/humanizer-de/5.22.2-brandkit.2"))
    catalog = _pattern_catalog_contract(release, lock["source_revision"])
    if evidence["pattern_catalog"] != catalog:
        raise ValueError("humanizer pattern catalog contract does not match pinned catalog")
    allowed_patterns = set(catalog["canonical_pattern_ids"])
    lint_script = release / "scripts/evidence_lint.py"
    lint_python = release / ".venv/bin/python"
    if lint["script_sha256"] != sha256(lint_script):
        raise ValueError("evidence lint script hash does not match pinned release")
    completed = subprocess.run(
        [str(lint_python), str(lint_script), "--before-file", str(before), "--after-file", str(after), "--fail-on", "never"],
        capture_output=True, timeout=120,
    )
    if completed.returncode != lint["exit_code"] or sha256_bytes(completed.stdout) != lint["stdout_sha256"]:
        raise ValueError("pinned evidence lint exit/output hash does not match declaration")
    try:
        live_report = json.loads(completed.stdout, object_pairs_hook=_closed_object)
    except (json.JSONDecodeError, DuplicateKeyError) as exc:
        raise ValueError("pinned evidence lint returned invalid JSON") from exc
    audit, confirmed = load(refs["audit-ledger.json"]), load(refs["confirmed-ledger.json"])
    preflight, edits = load(refs["preflight.json"]), load(refs["edits.json"])
    evidence_ledger, evidence_report = load(refs["evidence-ledger.json"]), load(refs["evidence-report.json"])
    _validate_audit(audit, allowed_patterns)
    _validate_audit(confirmed, allowed_patterns, confirmed=True)
    _validate_evidence_ledger(evidence_ledger)
    _validate_evidence_report(evidence_report)
    if evidence_report != live_report or refs["evidence-report.json"].read_bytes() != _json_bytes(live_report):
        raise ValueError("evidence-report is not the exact pinned lint output")
    if any(item["severity"] == "blocker" for item in evidence_report["findings"]):
        raise ValueError("evidence report contains blockers")
    runner = _pinned_runner(release)
    expected_confirmed = runner.confirm_ledger(before_text, audit)
    if confirmed != expected_confirmed or refs["confirmed-ledger.json"].read_bytes() != _json_bytes(expected_confirmed):
        raise ValueError("confirmed ledger is not the exact pinned runner confirmation")
    jsonschema.Draft202012Validator(runner.EDIT_SCHEMA).validate(edits)
    spans = runner.candidate_spans(before_text, confirmed["candidates"])
    for edit in edits["edits"]:
        if edit["candidate_id"] not in spans:
            raise ValueError("edit references an unconfirmed candidate")
        start, end, candidate = spans[edit["candidate_id"]]
        old = before_text[start:end]
        if old != candidate["source"]:
            raise ValueError("edit span/source binding failed")
        old_hash = sha256_bytes(old.encode("utf-8"))
        new_hash = sha256_bytes(edit["replacement"].encode("utf-8"))
        if not re.fullmatch(r"[0-9a-f]{64}", old_hash) or not re.fullmatch(r"[0-9a-f]{64}", new_hash):
            raise ValueError("edit span old/new hash binding failed")
    proposed = runner.apply_edits(before_text, confirmed, edits)
    if after_bytes != proposed.encode("utf-8"):
        raise GenericSmoothingError("result is not exactly reconstructible from confirmed edits")
    violations = runner.protected_violations(before_text, proposed, confirmed)
    expected_diff = runner.unified_diff(before_text, proposed, "result.md").encode("utf-8")
    if refs["changes.diff"].read_bytes() != expected_diff:
        raise ValueError("changes.diff is not the exact pinned runner diff")
    report = load(refs["report.json"])
    provider = report["provider"]
    audit_output, audit_cost = _model_artifact_output(refs["audit-call.json"], provider)
    if confirmed["candidates"]:
        if "rewrite-call.json" not in refs:
            raise ValueError("confirmed candidates require the real rewrite-call artifact")
        rewrite_output, rewrite_cost = _model_artifact_output(refs["rewrite-call.json"], provider)
    else:
        if "rewrite-call.json" in refs or edits != {"edits": []}:
            raise ValueError("null-edit run has an incompatible rewrite artifact")
        rewrite_output, rewrite_cost = edits, None
    if audit_output != audit or rewrite_output != edits:
        raise ValueError("model-call structured output does not match runner artifacts")
    model_hashes = {
        "audit": sha256(refs["audit-call.json"]),
        "rewrite": sha256(refs["rewrite-call.json"]) if "rewrite-call.json" in refs else None,
    }
    expected_provenance = _brandkit_provenance(
        before_bytes, after_bytes, before_text, proposed, confirmed, edits, runner, model_hashes, catalog,
        lock["source_revision"], sha256(release / "scripts/humanizer_two_pass.py"),
    )
    provenance = load(refs["brandkit-provenance.json"])
    if provenance != expected_provenance or refs["brandkit-provenance.json"].read_bytes() != _json_bytes(expected_provenance):
        raise ValueError("BrandKit provenance is not the exact deterministic derivation")
    if provenance["before_sha256"] != lint["before_sha256"] or provenance["after_sha256"] != lint["after_sha256"]:
        raise ValueError("BrandKit provenance before/after hashes disagree with lint/manifest raw bytes")
    audit_script = release / "scripts/humanizer_audit.py"
    audited = subprocess.run(
        [str(lint_python), str(audit_script), "--file", str(before), "--mode", report["mode"]],
        capture_output=True, timeout=120,
    )
    if audited.returncode:
        raise ValueError("pinned humanizer preflight failed")
    live_audit = json.loads(audited.stdout, object_pairs_hook=_closed_object)
    live_preflight = {"preflight": live_audit["summary"]["preflight"], "findings": live_audit["findings"]}
    if preflight != live_preflight or refs["preflight.json"].read_bytes() != _json_bytes(live_preflight):
        raise ValueError("preflight.json is not the exact pinned deterministic audit")
    with tempfile.TemporaryDirectory(prefix="brandkit-evidence-ledger-") as temporary:
        live_ledger_path = Path(temporary) / "evidence-ledger.json"
        ledger_run = subprocess.run(
            [str(lint_python), str(lint_script), "--before-file", str(before), "--write-ledger", str(live_ledger_path)],
            capture_output=True, timeout=120,
        )
        if ledger_run.returncode or refs["evidence-ledger.json"].read_bytes() != live_ledger_path.read_bytes():
            raise ValueError("evidence-ledger is not the exact pinned --write-ledger output")
    _validate_report(
        report, release, sha256(before), evidence_report, preflight, audit, confirmed, edits,
        runner, before_text, [audit_cost, rewrite_cost], lock["source_revision"], model_hashes,
        sha256(refs["brandkit-provenance.json"]),
    )
    if report["protected_violations"] != violations:
        raise ValueError("report protected violations disagree with the pinned runner")
    return [relative, *verified]


def _contains_unknown(value: Any) -> bool:
    if value == "unknown":
        return True
    if isinstance(value, list):
        return any(_contains_unknown(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_unknown(item) for item in value.values())
    return False


def _spacing_artifact(root: Path, request: dict[str, Any], ref: dict[str, str], schema_path: str) -> dict[str, Any]:
    if not isinstance(ref, dict):
        raise ValueError("spacing artifact reference is missing")
    exact = {"role": "spacing-inventory" if "inventory" in schema_path else "spacing-proposal", "path": ref["path"], "sha256": ref["sha256"]}
    if exact not in request["input_artifacts"]:
        raise ValueError("spacing artifact is not bound by input_artifacts")
    path = root / ref["path"]
    if sha256(path) != ref["sha256"]:
        raise ValueError("spacing artifact hash mismatch")
    value = load(path)
    jsonschema.Draft202012Validator(load(root / schema_path)).validate(value)
    return value


def execute_request(request: dict[str, Any], root: Path = ROOT, raw_request_sha256: str | None = None) -> dict[str, Any]:
    request_schema = load(root / "schemas/brandkit-skill-request.v1.schema.json")
    result_schema = load(root / "schemas/brandkit-skill-result.v1.schema.json")
    jsonschema.Draft202012Validator(request_schema).validate(request)
    verified = validate_artifact_refs(root, request["input_artifacts"])
    skill, state = request["skill"], request["state"]
    blockers: list[str] = []
    if _contains_unknown(state):
        blockers.append("unknown-or-omitted-state")
    if skill == "brandkit-setup":
        if set(state) != {"write_target", "project_roots", "required_capabilities", "optional_renderer"}:
            blockers.append("state-contract-mismatch")
        if state.get("write_target") != "observed": blockers.append("missing_write_target")
        if len(state.get("project_roots", [])) != 1: blockers.append("ambiguous_project_root")
        if any(value not in ("observed", "fallback") for value in state.get("required_capabilities", [])): blockers.append("required_capability_without_fallback")
        decision = "blocked" if blockers else ("pass-with-fallback" if state.get("optional_renderer") == "fallback" else "pass")
    elif skill == "brandkit-interview":
        if set(state) != {"owner", "axes", "conflicts", "legal_claims"}: blockers.append("state-contract-mismatch")
        if state.get("owner") != "confirmed": blockers.append("missing_owner")
        if "normative" in state.get("conflicts", []): blockers.append("normative_brand_conflict")
        if any(value != "official-source" for value in state.get("legal_claims", [])): blockers.append("unsupported_legal_claim")
        if not state.get("axes") or "unknown" in state.get("axes", []): blockers.append("required_axes_unaccounted")
        decision = "blocked" if blockers else ("pass-with-todo" if "open" in state["axes"] else "pass")
    elif skill == "brandkit-build":
        expected_keys = {"provenance", "conflicts", "asset_rights", "brand_pin", "instruction_sources", "claude_bridge", "hermes_context"}
        if set(state) != expected_keys: blockers.append("state-contract-mismatch")
        if not state.get("provenance") or any(value != "complete" for value in state.get("provenance", [])): blockers.append("missing_provenance")
        if "normative" in state.get("conflicts", []): blockers.append("normative_brand_conflict")
        if any(value != "verified" for value in state.get("asset_rights", [])): blockers.append("unresolved_asset_rights")
        if state.get("brand_pin") != "verified": blockers.append("missing_brand_pin")
        if state.get("instruction_sources") != ["AGENTS.md"] or state.get("claude_bridge") != "CLAUDE.md -> AGENTS.md" or state.get("hermes_context") != "cwd/AGENTS.md": blockers.append("instruction_copy")
        decision = "blocked" if blockers else "package-ready"
    elif skill == "brandkit-spacing":
        expected_keys = {"brand_pin", "target_medium", "target_scope", "inventory_artifact", "proposal_artifact", "normative_sources", "requested_output"}
        if set(state) != expected_keys: blockers.append("state-contract-mismatch")
        if state.get("brand_pin") != "verified": blockers.append("missing_brand_pin")
        if state.get("target_medium") not in {"shared", "ui", "web", "pdf"}:
            blockers.extend(["ambiguous_target_medium", "medium_namespace_collision"])
        inventory: dict[str, Any] = {"entries": []}
        proposal: dict[str, Any] | None = None
        try:
            inventory = _spacing_artifact(root, request, state.get("inventory_artifact"), SPACING_INVENTORY_SCHEMA)
            if inventory["target_scope"] != state.get("target_scope") or any(entry["medium"] != state.get("target_medium") for entry in inventory["entries"]):
                blockers.append("medium_namespace_collision")
            if len({entry["id"] for entry in inventory["entries"]}) != len(inventory["entries"]):
                blockers.append("unknown_spacing_provenance")
        except (OSError, ValueError, KeyError, TypeError, jsonschema.ValidationError):
            blockers.append("unknown_spacing_provenance")
        proposal_ref = state.get("proposal_artifact")
        if proposal_ref is not None:
            try:
                proposal = _spacing_artifact(root, request, proposal_ref, SPACING_PROPOSAL_SCHEMA)
            except (OSError, ValueError, KeyError, TypeError, jsonschema.ValidationError):
                blockers.append("unknown_spacing_provenance")
        sources = state.get("normative_sources", [])
        if "conflicting" in sources: blockers.append("normative_spacing_conflict")
        if any(value not in {"pinned-brand-package", "unconfirmed-proposal"} for value in sources): blockers.append("unknown_spacing_provenance")
        output = state.get("requested_output")
        if output == "unconfirmed-token-proposal" and (proposal is None or "unconfirmed-proposal" not in sources): blockers.append("unknown_spacing_provenance")
        if output == "ui-patch-handoff" and state.get("target_medium") not in {"ui", "web"}: blockers.append("forbidden_spacing_mutation")
        if output not in {"audit-result", "unconfirmed-token-proposal", "ui-patch-handoff"}: blockers.append("forbidden_spacing_mutation")
        decision = "blocked" if blockers else ("pass-with-todo" if "unconfirmed-proposal" in sources else "pass")
    elif skill == "brandkit-publish":
        expected_keys = {"brand_pin", "facts", "todos", "rights", "legal_claims", "brand_conflicts", "markdown_contract", "humanizer_applicability", "humanizer_evidence", "impeccable_handoff", "renderer", "visual_review"}
        if set(state) != expected_keys: blockers.append("state-contract-mismatch")
        if state.get("brand_pin") != "verified": blockers.append("missing_brand_pin")
        for fact in state.get("facts", []):
            if fact == "missing": blockers.append("missing_fact")
            elif fact == "mutated": blockers.append("protected_fact_mutation")
            elif fact != "verified": blockers.append("unknown_fact_state")
        if state.get("todos"): blockers.append("open_todo")
        if state.get("rights") != "verified": blockers.append("unresolved_asset_rights")
        if any(value != "official-source" for value in state.get("legal_claims", [])): blockers.append("unsupported_legal_claim")
        if any(value != "resolved" for value in state.get("brand_conflicts", [])): blockers.append("brand_conflict")
        if state.get("markdown_contract") != "verified": blockers.append("markdown_contract_failure")
        if state.get("humanizer_applicability") != "required": blockers.append("invalid_humanizer_applicability")
        evidence = state.get("humanizer_evidence")
        try:
            verified.extend(validate_humanizer_evidence(root, evidence))
        except GenericSmoothingError:
            blockers.append("generic_smoothing")
        except (OSError, ValueError, json.JSONDecodeError, jsonschema.ValidationError, TypeError): blockers.append("missing_or_rejected_humanizer_evidence")
        if state.get("impeccable_handoff") not in ("not-applicable", "adapter-green"): blockers.append("improper_impeccable_handoff")
        if state.get("renderer") != "verified": blockers.append("renderer_evidence_failure")
        if state.get("visual_review") != "verified": blockers.append("visual_review_failure")
        decision = "blocked" if blockers else "publish"
    else:
        blockers.append("unknown-skill")
        decision = "blocked"
    raw_hash = raw_request_sha256 or sha256_bytes((json.dumps(request, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8"))
    result = {"schema_version":"brandkit-skill-result.v1", "request_id":request["request_id"], "skill":skill, "decision":decision, "blockers":sorted(set(blockers)), "verified_artifacts":sorted(set(verified)), "raw_request_sha256":raw_hash, "error":None}
    if skill == "brandkit-spacing":
        entries = inventory.get("entries", []) if isinstance(inventory, dict) else []
        token_count = sum(1 for entry in entries if entry.get("assignment", {}).get("kind") == "token")
        exception_count = sum(1 for entry in entries if entry.get("assignment", {}).get("kind") == "exception")
        handoff = "none"
        if state.get("requested_output") == "unconfirmed-token-proposal": handoff = "brandkit-build"
        elif state.get("requested_output") == "ui-patch-handoff": handoff = "brandkit-impeccable -> impeccable layout"
        result["spacing_result"] = {
            "schema_version": "spacing-result.v1",
            "inventory_sha256": state.get("inventory_artifact", {}).get("sha256", "0" * 64),
            "proposal_sha256": state.get("proposal_artifact", {}).get("sha256") if isinstance(state.get("proposal_artifact"), dict) else None,
            "assignment_count": len(entries), "token_count": token_count, "exception_count": exception_count,
            "findings": sorted(set(blockers)), "handoff": handoff,
        }
    jsonschema.Draft202012Validator(result_schema).validate(result)
    return result


def validate(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    try:
        contract = load(root / CONTRACT.name)
        if contract.get("schema_version") != "brandkit-skill-contract.v1" or contract.get("sequence") != CORE_SEQUENCE:
            errors.append("invalid skill contract/sequence")
        spacing = contract.get("optional_adapters", {}).get("brandkit-spacing", {})
        expected_spacing = {
            "owner": "spacing-tokens-roles-and-audit",
            "inputs": ["brand_pin", "target_medium", "target_scope", "hashed_spacing_inventory"],
            "outputs": ["closed_spacing_result", "unconfirmed_spacing_token_proposal", "validated_spacing_roles", "spacing_audit_findings", "ui_patch_handoff"],
            "mutates": [],
            "read_only": ["brand-package.v1", "shared_visual_tokens.spacing", "pdf.tokens", "document-type-layout", "DESIGN.md"],
            "gate": "one pinned brand and one medium namespace resolve; every inventory entry maps to exactly one token or evidenced exception",
            "fails_on": ["missing_brand_pin", "ambiguous_target_medium", "normative_spacing_conflict", "unknown_spacing_provenance", "medium_namespace_collision", "forbidden_spacing_mutation"],
        }
        if spacing != expected_spacing:
            errors.append("brandkit-spacing optional adapter contract drifted")
        if contract.get("routing") != DESIGN_ROUTES:
            errors.append("design-skill routing contract drifted")
        for name in CONTRACT_SKILLS:
            folder = root / "skills" / name
            skill_files: list[Path] = []
            for current, directories, files in os.walk(folder, followlinks=False):
                current_path = Path(current)
                for directory in list(directories):
                    candidate = current_path / directory
                    try:
                        if stat.S_ISLNK(candidate.lstat().st_mode):
                            errors.append(f"{name}: symlink directory is forbidden: {candidate.relative_to(root)}")
                            directories.remove(directory)
                    except OSError as exc:
                        errors.append(f"{name}: cannot lstat {candidate.relative_to(root)}: {exc}")
                        directories.remove(directory)
                if "SKILL.md" in files:
                    skill_files.append(current_path / "SKILL.md")
            if skill_files != [folder / "SKILL.md"]:
                errors.append(f"{name}: exactly one root SKILL.md is required")
                continue
            skill_path = skill_files[0]
            try:
                relative_parts = skill_path.relative_to(root).parts
                cursor = root
                for part in relative_parts:
                    cursor = cursor / part
                    mode = cursor.lstat().st_mode
                    if stat.S_ISLNK(mode):
                        raise ValueError(f"symlink SKILL path is forbidden: {cursor.relative_to(root)}")
                resolved = skill_path.resolve(strict=True)
                if not _is_confined(root.resolve(strict=True), resolved) or not stat.S_ISREG(resolved.stat().st_mode):
                    raise ValueError("SKILL.md is outside the repository or nonregular")
            except (OSError, ValueError) as exc:
                errors.append(f"{name}: {exc}")
                continue
            meta, body = frontmatter(skill_files[0].read_text(encoding="utf-8"))
            if not {"name", "description"}.issubset(meta): errors.append(f"{name}: Agent Skills name/description required")
            if meta.get("name") != name or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(meta.get("name", ""))): errors.append(f"{name}: invalid/mismatched name")
            description = meta.get("description")
            if not isinstance(description, str) or not 40 <= len(description) <= 320 or "Nutze den Skill" not in description: errors.append(f"{name}: invalid trigger description")
            if "## Eingabe und Ausgabe" not in body or ("## Gate" not in body and "## Publication Gate" not in body): errors.append(f"{name}: required body contract sections missing")
        publish = (root / "skills/brandkit-publish/SKILL.md").read_text(encoding="utf-8")
        for token in ("humanizer_applicability", "toolchain/humanizer-de/toolchain.lock.json", "scripts/humanizer_two_pass.py", "--file", "--out-dir", "--provider", "--model", "preflight.json", "audit-call.json", "audit-ledger.json", "confirmed-ledger.json", "edits.json", "rewrite-call.json", "evidence-ledger.json", "evidence-report.json", "changes.diff", "report.json", "result.md", "rejected.md", "scripts/evidence_lint.py", "--write-ledger", "--fail-on never", "old_hash", "rewrite_humanizer", "AST-Skelett", "generisches Glätten"):
            if token not in publish: errors.append(f"brandkit-publish: missing exact humanizer contract token {token}")
        humanizer_lock = load(root / "toolchain/humanizer-de/toolchain.lock.json")["skill"]
        if humanizer_lock.get("version") != "5.22.2-brandkit.2" or humanizer_lock.get("source_revision") != "17059735ba1cd2eea7e66a517f79b91b0797b366":
            errors.append("humanizer-de active release pin drifted from inspected canonical release")
        readme = (root / "README.md").read_text(encoding="utf-8")
        adapter_version = load(root / "impeccable-adapter.v1.json")["adapted_release"]["marker"]["version"]
        if adapter_version != "4.1.1+fcd7622.brandkit6": errors.append("impeccable-adapter canonical release drifted")
        if re.search(r"4\.1\.1\+fcd7622\.brandkit\d+", readme): errors.append("README contains a stale copied adapter release literal")
        if "impeccable-adapter.v1.json#/adapted_release/marker/version" not in readme: errors.append("README does not derive adapter release from canonical contract")
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        errors.append(str(exc))
    return errors


def dry_run(path: Path = FIXTURES, root: Path = ROOT, result_dir: Path | None = None) -> list[str]:
    errors: list[str] = []
    document = load(path)
    if set(document) != {"schema_version", "input_artifacts", "cases"} or document["schema_version"] != "brandkit-skill-dry-runs.v2":
        return ["dry-run suite is not a closed v2 artifact"]
    destination = result_dir or Path(tempfile.mkdtemp(prefix="brandkit-dry-run-results-"))
    destination.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for case in document["cases"]:
        if set(case) != {"request_id", "skill", "state", "expected"}:
            errors.append(f"{case.get('request_id')}: case artifact is not closed")
            continue
        request = {"schema_version":"brandkit-skill-request.v1", "request_id":case["request_id"], "skill":case["skill"], "input_artifacts":document["input_artifacts"], "state":case["state"]}
        request_path = destination / f"{case['request_id']}.request.json"
        result_path = destination / f"{case['request_id']}.result.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            result = execute_request(load(request_path), root, sha256(request_path))
        except (OSError, ValueError, json.JSONDecodeError, jsonschema.ValidationError) as exc:
            result = {"schema_version":"brandkit-skill-result.v1", "request_id":case["request_id"], "skill":case["skill"], "decision":"blocked", "blockers":[f"invalid-request:{type(exc).__name__}"], "verified_artifacts":[], "raw_request_sha256":sha256(request_path), "error":{"code":"schema-validation", "class":type(exc).__name__, "message":str(exc)}}
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        seen.add(case["skill"])
        if result["decision"] != case["expected"]: errors.append(f"{case['request_id']}: expected {case['expected']}, got {result['decision']} ({result['blockers']})")
    if seen != set(CONTRACT_SKILLS): errors.append("dry runs do not cover every core skill and optional adapter")
    return errors


def route_design_request(request: str) -> str | None:
    """Resolve one owner from request semantics; fixtures cannot supply intent."""
    text = request.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    words = set(re.findall(r"[a-z0-9.-]+", text))
    spacing = "spacing" in text or "abstaend" in text or "abstand" in text
    mutation = bool(words & {"korrigiere", "aendere", "fixe", "implementiere", "patche", "gestalte", "redesign"})
    ui = bool(words & {"ui", "web", "dashboard", "komponente", "komponenten", "layout"}) or "dashboard-ui" in text
    if "design.md" in text:
        return "brandkit-impeccable"
    if "pdf" in words or "pdf-layout" in words or ("bericht" in words and "publiziere" in words):
        return "brand-voice-pdf -> brandkit-publish"
    if ("brand-paket" in words or "brand-package" in words or "markenpaket" in words) and words & {"erzeuge", "baue", "versioniere", "neuer", "neue", "token"}:
        return "brandkit-build"
    if ui and mutation:
        return "brandkit-impeccable -> impeccable"
    if spacing and words & {"inventarisiere", "pruefe", "audit", "ordne", "zuordnung", "rollenpruefung"}:
        return "brandkit-spacing"
    return None


def collision_dry_run(path: Path = COLLISION_FIXTURES) -> list[str]:
    document = load(path)
    if set(document) != {"schema_version", "cases"} or document.get("schema_version") != "brandkit-design-routing-dry-runs.v1":
        return ["design routing dry runs are not a closed v1 artifact"]
    errors: list[str] = []
    seen_routes: set[str] = set()
    seen_ids: set[str] = set()
    for case in document.get("cases", []):
        if set(case) != {"request_id", "request", "expected_route", "forbidden_routes"}:
            errors.append(f"{case.get('request_id')}: routing case is not closed")
            continue
        actual = route_design_request(case.get("request", ""))
        if actual != case.get("expected_route"):
            errors.append(f"{case.get('request_id')}: expected {case.get('expected_route')}, got {actual}")
        if actual in case.get("forbidden_routes", []):
            errors.append(f"{case.get('request_id')}: selected a forbidden competing route")
        if not isinstance(case.get("request"), str) or len(case["request"].split()) < 4:
            errors.append(f"{case.get('request_id')}: request is not a realistic behavioral prompt")
        if case.get("request_id") in seen_ids:
            errors.append(f"{case.get('request_id')}: duplicate request id")
        seen_ids.add(case.get("request_id"))
        if actual: seen_routes.add(actual)
    if seen_routes != set(DESIGN_ROUTES.values()):
        errors.append("design routing dry runs do not cover every collision boundary")
    return errors


def _raw_identity(raw: bytes) -> tuple[str, str]:
    request_id, skill = "invalid-request", "invalid-request"
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
        if isinstance(value, dict):
            if isinstance(value.get("request_id"), str) and value["request_id"]:
                request_id = value["request_id"]
            if isinstance(value.get("skill"), str) and value["skill"]:
                skill = value["skill"]
    except (UnicodeError, json.JSONDecodeError, DuplicateKeyError):
        pass
    return request_id, skill


def _invalid_result(raw: bytes, exc: Exception, code: str) -> dict[str, Any]:
    request_id, skill = _raw_identity(raw)
    return {
        "schema_version": "brandkit-skill-result.v1", "request_id": request_id, "skill": skill,
        "decision": "blocked", "blockers": [f"invalid-request:{code}"], "verified_artifacts": [],
        "raw_request_sha256": sha256_bytes(raw),
        "error": {"code": code, "class": type(exc).__name__, "message": str(exc) or type(exc).__name__},
    }


def _safe_atomic_json(path: Path, value: dict[str, Any]) -> None:
    absolute = path.absolute()
    existing = absolute
    while not existing.exists() and not existing.is_symlink():
        if existing.parent == existing:
            break
        existing = existing.parent
    if existing.is_symlink() or not existing.is_dir():
        raise ValueError("unsafe result path parent")
    cursor = existing
    for part in absolute.relative_to(existing).parts[:-1]:
        cursor = cursor / part
        if cursor.exists() or cursor.is_symlink():
            mode = cursor.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ValueError("unsafe result path parent")
        else:
            cursor.mkdir()
    if absolute.exists() or absolute.is_symlink():
        mode = absolute.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise ValueError("unsafe result path target")
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{absolute.name}.", dir=absolute.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, absolute)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dry-runs", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    if args.request:
        if not args.result: parser.error("--request requires --result")
        raw = b""
        try:
            raw = args.request.read_bytes()
            try:
                request = json.loads(raw.decode("utf-8"), object_pairs_hook=_closed_object)
            except DuplicateKeyError as exc:
                result = _invalid_result(raw, exc, "duplicate-key")
            except (UnicodeError, json.JSONDecodeError) as exc:
                result = _invalid_result(raw, exc, "raw-json-parse")
            else:
                if not isinstance(request, dict):
                    raise ValueError("request must be a JSON object")
                try:
                    result = execute_request(request, args.root, sha256_bytes(raw))
                except jsonschema.ValidationError as exc:
                    result = _invalid_result(raw, exc, "schema-validation")
        except OSError as exc:
            result = _invalid_result(raw, exc, "request-load")
        except ValueError as exc:
            result = _invalid_result(raw, exc, "schema-validation")
        try:
            jsonschema.Draft202012Validator(load(args.root / "schemas/brandkit-skill-result.v1.schema.json")).validate(result)
            _safe_atomic_json(args.result, result)
        except (OSError, ValueError) as exc:
            print(f"unsafe result path: {exc}", file=sys.stderr)
            return 2
        if result["error"] is not None:
            print(f"invalid request: {result['error']['message']}", file=sys.stderr)
            return 2
        return 0 if result["decision"] != "blocked" else 1
    errors = validate(args.root)
    errors.extend(dry_run(args.dry_runs or args.root / "fixtures/brandkit-skills/dry-runs.v1.json", args.root))
    errors.extend(collision_dry_run(args.root / "fixtures/brandkit-skills/collision-dry-runs.v1.json"))
    if errors:
        print("\n".join(errors), file=sys.stderr); return 1
    print("brandkit skill contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
