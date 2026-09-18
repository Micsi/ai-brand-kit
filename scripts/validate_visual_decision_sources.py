#!/usr/bin/env python3
"""Fail-closed validator for the BrandKit visual-decision source contract."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SOURCE = "visual-decision-sources.v1.json"
SOURCE_SCHEMA = "schemas/visual-decision-sources.v1.schema.json"
CAPTURE_SCHEMA = "schemas/reputation-evidence.v1.schema.json"
ADAPTER = "impeccable-adapter.v1.json"
CAPTURE_FIELDS = (
    "source_url",
    "registrable_domain",
    "publisher_identity",
    "publisher_family",
    "source_kind",
    "retrieved_at",
    "observed_claim",
    "sentiment",
)
REVIEWED_HOST_BASE_DOMAINS = {
    "github.com": "github.com",
    "www.github.com": "github.com",
    "api.github.com": "github.com",
    "kynth.studio": "kynth.studio",
    "www.kynth.studio": "kynth.studio",
    "skillworks.kynth.studio": "kynth.studio",
    "topgit.dev": "topgit.dev",
    "www.topgit.dev": "topgit.dev",
}


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is forbidden: {key!r}")
        result[key] = value
    return result


def _load(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(
            handle,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(131072), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schema_errors(value: Any, schema: dict[str, Any], label: str) -> list[str]:
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
    except ImportError:
        return ["jsonschema is required to validate visual-decision sources"]
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return [f"{label} schema is invalid: {exc.message}"]
    validator = Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    result = []
    for error in sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path)):
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        result.append(f"{label} {location}: {error.message}")
    return result


def _confined_regular_file(root: Path, relative: str, label: str) -> tuple[Path | None, str | None]:
    posix = PurePosixPath(relative)
    if posix.is_absolute() or not posix.parts or ".." in posix.parts or "." in posix.parts:
        return None, f"{label} path is not a confined relative path: {relative!r}"
    root = root.resolve(strict=True)
    candidate = root.joinpath(*posix.parts)
    cursor = root
    for part in posix.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return None, f"{label} path contains a symlink: {relative!r}"
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        return None, f"{label} is unavailable: {relative!r}: {exc}"
    if root != resolved and root not in resolved.parents:
        return None, f"{label} escapes repository root: {relative!r}"
    if not resolved.is_file():
        return None, f"{label} is not a regular file: {relative!r}"
    return resolved, None


def _expected_adapter_binding(adapter: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    upstream = adapter["upstream"]
    adapted = adapter["adapted_release"]
    selected_pin = {
        "repository": upstream["repository"],
        "commit": upstream["commit"],
        "tree": upstream["tree"],
        "canonical_subtree": upstream["canonical_skill_subtree"],
        "skill_tree": upstream["canonical_skill_tree"],
        "release": adapted["marker"]["version"],
        "adapted_tree_sha256": adapted["tree_sha256"],
    }
    license_binding = {
        "spdx": upstream["license"],
        "path": "LICENSE",
        "sha256": upstream["license_sha256"],
        "notice_path": upstream["notice_path"],
        "notice_sha256": upstream["notice_sha256"],
    }
    defaults = adapter["defaults"]
    security_binding = {
        "default_off": sorted(key for key, enabled in defaults.items() if enabled is False),
        "allowed_prebuild_routes": ["adapt", "colorize", "critique", "typeset"],
        "forbidden_prebuild_routes": ["extract", "image_generation", "live"],
    }
    return selected_pin, license_binding, security_binding


def validate_visual_decision_sources(
    root: Path = ROOT,
    sidecar_root: Path | None = None,
) -> list[str]:
    """Return all source-contract errors; an empty list means valid."""
    root = root.resolve(strict=True)
    sidecar_root = (sidecar_root or root.parents[1]).resolve(strict=True)
    errors: list[str] = []
    try:
        document = _load(root / SOURCE)
        source_schema = _load(root / SOURCE_SCHEMA)
        capture_schema = _load(root / CAPTURE_SCHEMA)
        adapter = _load(root / ADAPTER)
    except (OSError, ValueError, KeyError) as exc:
        return [f"visual source contract cannot be loaded: {exc}"]

    errors.extend(_schema_errors(document, source_schema, "source"))
    errors.extend(_schema_errors({}, capture_schema, "capture-schema-probe")[:0])
    if errors:
        return errors

    candidates = document["candidates"]
    names = [item["name"] for item in candidates]
    if len(names) != len(set(names)):
        errors.append("candidate names must be unique")
    selected = [item for item in candidates if item["decision"] == "selected"]
    if len(selected) != 1:
        errors.append("exactly one candidate must be selected")
        return errors
    selected_item = selected[0]
    if selected_item["name"] != document["selected_dependency"]:
        errors.append("selected_dependency must name the single selected candidate")

    try:
        expected_pin, expected_license, expected_security = _expected_adapter_binding(adapter)
    except (KeyError, TypeError) as exc:
        errors.append(f"impeccable adapter cannot produce a trust binding: {exc}")
        return errors
    if selected_item["selected_pin"] != expected_pin:
        errors.append("selected pin does not exactly match impeccable-adapter.v1.json")
    if selected_item["license"] != expected_license:
        errors.append("selected license does not exactly match impeccable-adapter.v1.json")
    actual_security = {
        key: selected_item["security"][key]
        for key in ("default_off", "allowed_prebuild_routes", "forbidden_prebuild_routes")
    }
    if actual_security != expected_security:
        errors.append("selected security capability binding does not match impeccable-adapter.v1.json")
    routes = set(adapter["inventory"]["routes"])
    if not set(actual_security["allowed_prebuild_routes"]) <= routes:
        errors.append("an allowed prebuild route is absent from the pinned adapter inventory")
    if not {"extract", "live"} <= routes or adapter["defaults"].get("image_generation") is not False:
        errors.append("the adapter cannot prove the forbidden prebuild capability boundary")

    minimum = document["policy"]["minimum_independent_positive_signals"]
    signals = selected_item["reputation_signals"]
    for field in ("path", "sha256", "source_url", "registrable_domain", "publisher_identity", "publisher_family"):
        values = [signal[field] for signal in signals]
        if len(values) != len(set(values)):
            errors.append(f"reputation signals contain duplicate {field}")
    if len(signals) < minimum:
        errors.append(f"at least {minimum} positive reputation signals are required")
    for field in ("registrable_domain", "publisher_identity", "publisher_family"):
        if len({signal[field] for signal in signals}) < minimum:
            errors.append(f"at least {minimum} independent {field} values are required")

    for index, signal in enumerate(signals):
        label = f"reputation signal {index}"
        capture_path, path_error = _confined_regular_file(root, signal["path"], label)
        if path_error:
            errors.append(path_error)
            continue
        assert capture_path is not None
        if _sha256(capture_path) != signal["sha256"]:
            errors.append(f"{label} capture SHA-256 mismatch")
            continue
        try:
            capture = _load(capture_path)
        except (OSError, ValueError) as exc:
            errors.append(f"{label} capture cannot be loaded: {exc}")
            continue
        errors.extend(_schema_errors(capture, capture_schema, label))
        for field in CAPTURE_FIELDS:
            if capture.get(field) != signal.get(field):
                errors.append(f"{label} {field} differs between descriptor and capture")
        host = (urlsplit(capture.get("source_url", "")).hostname or "").lower().rstrip(".")
        domain = str(capture.get("registrable_domain", "")).lower().rstrip(".")
        derived_domain = REVIEWED_HOST_BASE_DOMAINS.get(host)
        if derived_domain is None:
            errors.append(f"{label} source_url host is not in the reviewed host map")
        elif domain != derived_domain:
            errors.append(f"{label} registrable_domain differs from the reviewed URL base domain")

    narrative = document["narrative_source_review"]
    prefix = "sidecar:"
    if not narrative["locator"].startswith(prefix):
        errors.append("narrative source locator is not a sidecar locator")
    else:
        narrative_path, path_error = _confined_regular_file(
            sidecar_root, narrative["locator"][len(prefix):], "narrative source"
        )
        if path_error:
            errors.append(path_error)
        elif narrative_path is not None and _sha256(narrative_path) != narrative["sha256"]:
            errors.append("narrative source SHA-256 mismatch")

    for candidate in candidates:
        if candidate["decision"] == "rejected":
            if candidate["installed_by_this_integration"] is not False or candidate["runtime_route"] is not None:
                errors.append(f"rejected candidate {candidate['name']} has an install or runtime route")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--sidecar-root", type=Path)
    args = parser.parse_args()
    errors = validate_visual_decision_sources(args.root, args.sidecar_root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("visual-decision source contract: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
