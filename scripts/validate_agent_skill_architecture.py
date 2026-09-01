#!/usr/bin/env python3
"""Validate the v1 contract and optionally preflight real provider roots."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "agent-skill-architecture.v1.json"
SCHEMA = ROOT / "schemas" / "agent-skill-architecture.v1.schema.json"
SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SOURCE_SNAPSHOT = re.compile(r"^snapshot:[A-Za-z0-9._/-]+@sha256:([0-9a-f]{64})$")


def _load(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def schema_errors(document: Any, schema: dict[str, Any]) -> list[str]:
    """Return Draft 2020-12 errors, or a clear dependency error."""
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
    except ImportError:
        return [
            "Draft 2020-12 validation requires the 'jsonschema' package "
            "(install with: python -m pip install 'jsonschema>=4')"
        ]
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return [f"architecture schema is not valid Draft 2020-12: {exc.message}"]
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda item: list(item.absolute_path))
    rendered: list[str] = []
    for error in errors:
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        rendered.append(f"schema {location}: {error.message}")
    return rendered


def _frontmatter_name(skill_file: Path) -> tuple[str | None, str | None]:
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, f"cannot read frontmatter: {exc}"
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, "frontmatter must start with ---"
    closing = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if closing is None:
        return None, "frontmatter has no closing --- delimiter"
    try:
        import yaml
        from yaml.resolver import BaseResolver
    except ImportError:
        return None, "frontmatter validation requires PyYAML"

    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise yaml.constructor.ConstructorError(None, None, f"duplicate key: {key}", key_node.start_mark)
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    try:
        header = yaml.load("\n".join(lines[1:closing]), Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        return None, f"invalid YAML frontmatter: {exc}"
    if not isinstance(header, dict):
        return None, "frontmatter must be a YAML mapping"
    if "name" not in header or not isinstance(header["name"], str):
        return None, "frontmatter must contain exactly one scalar string name"
    name = header["name"]
    if not SKILL_NAME.fullmatch(name):
        return None, "frontmatter.name is not a valid skill name"
    return name, None


def validate(document: dict[str, Any], *, root: Path = ROOT) -> list[str]:
    """Check semantic invariants that are awkward or impossible in JSON Schema."""
    errors: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    providers = document.get("providers", {})
    expected = {
        "claude": {
            "global_discovery": "~/.claude/skills",
            "global_bridge": "per-skill-symlink",
            "global_bridge_target": "~/.agents/skills/<skill-name>",
            "project_discovery": ".claude/skills",
            "project_bridge": "root-symlink",
            "project_bridge_target": "../.agents/skills",
        },
        "codex": {
            "global_discovery": "~/.agents/skills",
            "global_bridge": "native",
            "global_bridge_target": None,
            "project_discovery": ".agents/skills",
            "project_bridge": "native",
            "project_bridge_target": None,
        },
        "hermes": {
            "global_discovery": "skills.external_dirs",
            "global_bridge": "config-reference",
            "global_bridge_target": "~/.agents/skills",
            "project_discovery": ".agents/skills",
            "project_bridge": "trusted-native",
            "project_bridge_target": None,
        },
    }
    for provider, rules in expected.items():
        require(providers.get(provider) == rules, f"{provider} discovery/bridge contract drifted")

    llmwiki = document.get("llmwiki_profile", {})
    require(
        llmwiki.get("agents_bridge") == {"path": ".agents/skills", "target": "../.claude/skills"},
        "LLMWiki .agents bridge must resolve to its canonical facade",
    )
    require(
        llmwiki.get("codex_bridge") == {"path": ".codex/skills", "target": "../.claude/skills"},
        "LLMWiki Codex bridge must resolve to its canonical facade",
    )

    portfolio = document.get("skill_portfolio", [])
    names = [skill.get("name") for skill in portfolio if isinstance(skill, dict)]
    require(len(names) == len(set(names)), "skill names must be unique across all scopes")
    declared_existing: set[str] = set()
    for index, skill in enumerate(portfolio if isinstance(portfolio, list) else []):
        if not isinstance(skill, dict) or skill.get("lifecycle") != "existing":
            continue
        name = str(skill.get("name", ""))
        source = root / str(skill.get("source", ""))
        skill_file = source / "SKILL.md"
        declared_existing.add(name)
        require(skill_file.is_file(), f"skill_portfolio[{index}].source lacks SKILL.md: {source}")
        if skill_file.is_file():
            frontmatter_name, frontmatter_error = _frontmatter_name(skill_file)
            require(
                frontmatter_error is None and frontmatter_name == name,
                f"skill_portfolio[{index}] invalid SKILL.md frontmatter: "
                f"{frontmatter_error or 'name differs from portfolio'}",
            )
    repo_skill_names = {path.parent.name for path in (root / "skills").glob("*/SKILL.md")}
    require(repo_skill_names == declared_existing, "every repository skill must have exactly one existing portfolio entry")

    zones = document.get("content_zones", {})
    for zone_name, zone in zones.items() if isinstance(zones, dict) else []:
        if isinstance(zone, dict):
            overlap = set(zone.get("allowed", [])) & set(zone.get("forbidden", []))
            require(not overlap, f"content zone {zone_name} allows and forbids the same content: {sorted(overlap)}")

    isolation = document.get("isolation", {})
    require(
        isolation.get("work_root_template") != isolation.get("output_root_template"),
        "work and output roots must be distinct",
    )
    require(
        set(isolation.get("cache_key_fields", []))
        == {"project-root-realpath-hash", "brand-id", "brand-version", "engine-version"},
        "cache key must isolate project, brand and engine versions",
    )

    versioning = document.get("versioning", {})
    require(
        set(versioning.get("required_pin_fields", []))
        == {"name", "version", "source_locator", "content_sha256"},
        "version lock fields are incomplete",
    )
    require(
        versioning.get("source_locator_format") == "snapshot:<normalized-relative-path>@sha256:<64-lowercase-hex>",
        "source locator format must be a content-addressed snapshot",
    )
    require(
        bool(versioning.get("rollback_strategy")) and versioning.get("retain_previous_release") is True,
        "rollback requires a strategy and retained previous release",
    )
    return errors


def _resolve(path: Path, label: str, errors: list[str]) -> Path | None:
    try:
        return path.resolve(strict=True)
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        errors.append(f"{label} cannot be resolved (dead or cyclic link): {path}: {exc}")
        return None


def _within(path: Path, roots: Iterable[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _git_root(start: Path) -> Path | None:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _check_bridge(path: Path, expected_target: str, expected_real: Path, errors: list[str]) -> None:
    if not path.is_symlink():
        errors.append(f"required bridge is not a symlink: {path}")
        return
    actual_target = str(path.readlink())
    if actual_target != expected_target:
        errors.append(f"bridge {path} target must be {expected_target}, got {actual_target}")
    actual_real = _resolve(path, "bridge", errors)
    if actual_real is not None and actual_real != expected_real:
        errors.append(f"bridge {path} resolves to {actual_real}, expected {expected_real}")


def _inventory_skill_tree(skill_root: Path, allow_roots: set[Path], errors: list[str]) -> bool:
    """Resolve every package symlink and recursively inventory allowed aliases."""
    visited: set[Path] = set()

    def inventory(directory: Path, ancestors: frozenset[Path]) -> bool:
        valid = True
        directory_real = _resolve(directory, "skill package directory", errors)
        if directory_real is None:
            return False
        if directory_real in ancestors:
            errors.append(
                f"skill package directory alias creates an ancestor cycle: {directory} -> {directory_real}"
            )
            return False
        if directory_real in visited:
            return True
        descendants_ancestors = ancestors | {directory_real}
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            errors.append(f"cannot inventory skill package directory {directory}: {exc}")
            return False
        for child in children:
            if child.is_symlink():
                target = _resolve(child, "skill package symlink", errors)
                if target is None:
                    valid = False
                    continue
                if not _within(target, allow_roots):
                    errors.append(f"skill package symlink resolves outside allowlisted roots: {child} -> {target}")
                    valid = False
                    continue
                if target.is_dir() and not inventory(child, descendants_ancestors):
                    valid = False
            elif child.is_dir():
                if not inventory(child, descendants_ancestors):
                    valid = False
        visited.add(directory_real)
        return valid

    valid = inventory(skill_root, frozenset())
    skill_file = skill_root / "SKILL.md"
    skill_real = _resolve(skill_file, "skill SKILL.md", errors)
    if skill_real is None:
        return False
    if not _within(skill_real, allow_roots):
        errors.append(f"SKILL.md resolves outside allowlisted roots: {skill_file} -> {skill_real}")
        valid = False
    if not skill_real.is_file():
        errors.append(f"skill entry lacks a regular SKILL.md: {skill_root}")
        valid = False
    return valid


def _read_hermes_state(path: Path, errors: list[str]) -> tuple[list[Path], list[Path]]:
    """Read only the two provider-native Hermes skill path lists."""
    try:
        raw = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            document = json.loads(raw)
        else:
            import yaml
            document = yaml.safe_load(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ImportError) as exc:
        errors.append(f"cannot read Hermes state file {path}: {exc}")
        return [], []
    except Exception as exc:
        errors.append(f"invalid Hermes state file {path}: {exc}")
        return [], []
    skills = document.get("skills") if isinstance(document, dict) else None
    if not isinstance(skills, dict):
        errors.append("Hermes state lacks a skills mapping")
        return [], []
    external = skills.get("external_dirs")
    trusted = skills.get("trusted_project_dirs")
    if not isinstance(external, list) or not all(isinstance(item, str) for item in external):
        errors.append("Hermes state skills.external_dirs must be a list of paths")
        external = []
    if not isinstance(trusted, list) or not all(isinstance(item, str) for item in trusted):
        errors.append("Hermes state skills.trusted_project_dirs must be a list of paths")
        trusted = []
    def normalize(item: str) -> Path:
        return Path(item).expanduser().resolve(strict=False)

    return [normalize(item) for item in external], [normalize(item) for item in trusted]


def preflight(
    *,
    project_root: Path,
    profile: str,
    global_root: Path,
    claude_global_root: Path | None,
    allow_roots: list[Path],
    hermes_external_dirs: list[Path],
    hermes_trusted_project_dirs: list[Path],
) -> list[str]:
    """Read-only discovery, realpath, collision, allowlist, and trust checks."""
    errors: list[str] = []
    project_real = _resolve(project_root, "project root", errors)
    global_real = _resolve(global_root, "global canonical root", errors)
    if project_real is None or global_real is None:
        return errors

    git_root = _git_root(project_real)
    if git_root != project_real:
        errors.append(f"preflight root must be the exact Git root: {project_real} (nearest: {git_root})")

    trusted_reals = {_resolve(path, "Hermes trusted project dir", errors) for path in hermes_trusted_project_dirs}
    trusted_reals.discard(None)
    if project_real not in trusted_reals:
        errors.append("Hermes trust must contain the exact project Git root")

    external_reals = {_resolve(path, "Hermes external_dir", errors) for path in hermes_external_dirs}
    external_reals.discard(None)
    if global_real not in external_reals:
        errors.append("Hermes skills.external_dirs must contain the canonical global skill root")

    allow_reals: set[Path] = {project_real, global_real}
    for path in allow_roots:
        resolved = _resolve(path, "allowlisted root", errors)
        if resolved is not None:
            allow_reals.add(resolved)

    if profile == "standard":
        canonical_project = project_real / ".agents" / "skills"
        canonical_real = _resolve(canonical_project, "standard canonical project root", errors)
        if canonical_real is not None:
            _check_bridge(project_real / ".claude" / "skills", "../.agents/skills", canonical_real, errors)
        project_roots = [canonical_project, project_real / ".claude" / "skills"]
        unexpected = project_real / ".codex" / "skills"
        if unexpected.exists() or unexpected.is_symlink():
            errors.append("standard profile forbids a duplicate .codex/skills project bridge")
    else:
        facade = project_real / ".claude" / "skills"
        facade_real = _resolve(facade, "LLMWiki canonical project facade", errors)
        if facade_real is not None:
            _check_bridge(project_real / ".agents" / "skills", "../.claude/skills", facade_real, errors)
            _check_bridge(project_real / ".codex" / "skills", "../.claude/skills", facade_real, errors)
        project_roots = [facade, project_real / ".agents" / "skills", project_real / ".codex" / "skills"]

    discovery_roots: list[tuple[str, Path]] = [("canonical-global", global_root)]
    discovery_roots.extend(("project", path) for path in project_roots)
    discovery_roots.extend(("hermes-external", path) for path in hermes_external_dirs)
    if claude_global_root is not None:
        discovery_roots.append(("claude-global", claude_global_root))

    identities: dict[str, set[Path]] = defaultdict(set)
    seen_roots: set[tuple[str, Path]] = set()
    for role, root in discovery_roots:
        root_real = _resolve(root, f"{role} discovery root", errors)
        if root_real is None:
            continue
        marker = (role, root.absolute())
        if marker in seen_roots:
            continue
        seen_roots.add(marker)
        if not root_real.is_dir():
            errors.append(f"{role} discovery root is not a directory: {root}")
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            errors.append(f"cannot inventory {role} discovery root {root}: {exc}")
            continue
        for entry in entries:
            entry_real = _resolve(entry, f"{role} skill entry", errors)
            if entry_real is None:
                continue
            if not _within(entry_real, allow_reals):
                errors.append(f"skill entry resolves outside allowlisted roots: {entry} -> {entry_real}")
                continue
            if not entry_real.is_dir():
                errors.append(f"skill entry is not a directory: {entry}")
                continue
            if not _inventory_skill_tree(entry_real, allow_reals, errors):
                continue
            skill_file = entry_real / "SKILL.md"
            name, frontmatter_error = _frontmatter_name(skill_file)
            if frontmatter_error is not None or name is None:
                errors.append(f"skill entry has invalid frontmatter.name: {skill_file}: {frontmatter_error}")
                continue
            identities[name].add(entry_real)

    for name, realpaths in sorted(identities.items()):
        if len(realpaths) > 1:
            errors.append(f"skill name collision for {name}: " + ", ".join(str(path) for path in sorted(realpaths)))
    return errors


def _content_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*"), key=lambda value: value.relative_to(path).as_posix()):
        relative = item.relative_to(path).as_posix().encode()
        if item.is_symlink():
            digest.update(b"L\0" + relative + b"\0" + os.readlink(item).encode() + b"\0")
        elif item.is_file():
            digest.update(b"F\0" + relative + b"\0" + item.read_bytes() + b"\0")
        elif item.is_dir():
            digest.update(b"D\0" + relative + b"\0")
    return digest.hexdigest()


def validate_project_state(
    project_root: Path, release_roots: list[Path], document: dict[str, Any]
) -> list[str]:
    """Validate one materialized project activation and its immutable pins."""
    errors: list[str] = []
    project_real = _resolve(project_root, "project state root", errors)
    if project_real is None:
        return errors
    if _git_root(project_real) != project_real:
        errors.append("project state root must be the exact Git root")
    allowed: set[Path] = set()
    for root in release_roots:
        resolved = _resolve(root, "project release root", errors)
        if resolved is not None:
            allowed.add(resolved)
    if not allowed:
        errors.append("project state validation requires at least one release root")

    isolation = document["isolation"]
    activation_path = project_real / isolation["activation_file"]
    lock_path = project_real / isolation["lock_file"]
    try:
        activation = _load(activation_path)
        lock = _load(lock_path)
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read project activation or lock: {exc}")
        return errors
    brands = activation.get("brands") if isinstance(activation, dict) else None
    if not isinstance(brands, list) or len(brands) != 1 or not isinstance(brands[0], dict):
        errors.append("activation must resolve exactly one brand")
        return errors
    brand = brands[0]
    for key in ("id", "version"):
        if not isinstance(brand.get(key), str) or not brand[key]:
            errors.append(f"activation brand requires scalar {key}")
    run_id = activation.get("run_id")
    engine_version = activation.get("engine_version")
    if not isinstance(run_id, str) or not run_id or not isinstance(engine_version, str) or not engine_version:
        errors.append("activation requires scalar run_id and engine_version")

    pins = lock.get("pins") if isinstance(lock, dict) else None
    if not isinstance(pins, list) or not pins:
        errors.append("lock must contain a non-empty pins list")
        return errors
    required = set(document["versioning"]["required_pin_fields"])
    names: list[str] = []
    brand_matches = 0
    floating_versions = {"latest", "main", "master", "head", "*"}
    for index, pin in enumerate(pins):
        if not isinstance(pin, dict):
            errors.append(f"pin[{index}] must be an object")
            continue
        missing = sorted(required - set(pin))
        if missing:
            errors.append(f"pin[{index}] is missing required fields: {missing}")
            continue
        if not all(isinstance(pin.get(field), str) and pin[field] for field in required):
            errors.append(f"pin[{index}] required fields must be non-empty strings")
            continue
        names.append(pin["name"])
        if pin["version"].lower() in floating_versions:
            errors.append(f"pin[{index}] uses a movable version")
        locator = SOURCE_SNAPSHOT.fullmatch(pin["source_locator"])
        source_path = pin["source_locator"].removeprefix("snapshot:").split("@sha256:", 1)[0]
        normalized = PurePosixPath(source_path)
        if not locator or normalized.is_absolute() or ".." in normalized.parts or source_path != normalized.as_posix() or locator.group(1) != pin["content_sha256"]:
            errors.append(f"pin[{index}] source_locator is not a matching content-addressed snapshot")
        pin_path_value = pin.get("path")
        if not isinstance(pin_path_value, str):
            errors.append(f"pin[{index}] requires an explicit immutable path")
            continue
        pin_real = _resolve(Path(pin_path_value), f"pin[{index}] path", errors)
        if pin_real is None:
            continue
        if not _within(pin_real, allowed):
            errors.append(f"pin[{index}] path is outside release roots: {pin_real}")
            continue
        if pin_real.name != pin["version"]:
            errors.append(f"pin[{index}] path does not end in pinned version")
        if not re.fullmatch(r"[0-9a-f]{64}", pin["content_sha256"]):
            errors.append(f"pin[{index}] content_sha256 is not a SHA-256")
        elif _content_sha256(pin_real) != pin["content_sha256"]:
            errors.append(f"pin[{index}] content_sha256 mismatch")
        if pin["name"] == f"brand:{brand.get('id')}" and pin["version"] == brand.get("version"):
            brand_matches += 1
    if len(names) != len(set(names)):
        errors.append("lock contains ambiguous duplicate pins")
    if brand_matches != 1:
        errors.append("lock must resolve the activated brand to exactly one pin")

    if errors:
        return errors
    replacements = {
        "<project-root-realpath-hash>": hashlib.sha256(str(project_real).encode()).hexdigest()[:16],
        "<brand-id>": brand["id"],
        "<brand-version>": brand["version"],
        "<engine-version>": engine_version,
        "<run-id>": run_id,
    }
    for key in ("work_root_template", "cache_root_template", "output_root_template"):
        relative = isolation[key]
        for token, value in replacements.items():
            relative = relative.replace(token, value)
        materialized = project_real / relative
        if not materialized.is_dir():
            errors.append(f"required {key} is not materialized: {materialized}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--preflight-root", type=Path)
    parser.add_argument("--profile", choices=("standard", "llmwiki"))
    parser.add_argument("--global-root", type=Path)
    parser.add_argument("--claude-global-root", type=Path)
    parser.add_argument("--allow-root", type=Path, action="append", default=[])
    parser.add_argument("--hermes-mode", choices=("planned", "actual"), default="planned")
    parser.add_argument("--hermes-state-file", type=Path)
    parser.add_argument("--hermes-external-dir", type=Path, action="append", default=[])
    parser.add_argument("--hermes-trusted-project-dir", type=Path, action="append", default=[])
    parser.add_argument("--validate-project-state", type=Path)
    parser.add_argument("--release-root", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        document = _load(args.manifest)
        schema = _load(SCHEMA)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Architecture validation failed: {exc}", file=sys.stderr)
        return 1

    errors = schema_errors(document, schema)
    if not errors:
        errors.extend(validate(document))
    if args.preflight_root is not None:
        if args.profile is None or args.global_root is None:
            errors.append("--preflight-root requires --profile and --global-root")
        else:
            hermes_external_dirs = args.hermes_external_dir
            hermes_trusted_dirs = args.hermes_trusted_project_dir
            if args.hermes_mode == "actual":
                if args.hermes_state_file is None:
                    errors.append("Hermes actual mode requires --hermes-state-file")
                if args.hermes_external_dir or args.hermes_trusted_project_dir:
                    errors.append("Hermes actual mode rejects synthetic external/trusted path arguments")
                if args.hermes_state_file is not None:
                    hermes_external_dirs, hermes_trusted_dirs = _read_hermes_state(args.hermes_state_file, errors)
            errors.extend(
                preflight(
                    project_root=args.preflight_root,
                    profile=args.profile,
                    global_root=args.global_root,
                    claude_global_root=args.claude_global_root,
                    allow_roots=args.allow_root,
                    hermes_external_dirs=hermes_external_dirs,
                    hermes_trusted_project_dirs=hermes_trusted_dirs,
                )
            )
    if args.validate_project_state is not None:
        errors.extend(validate_project_state(args.validate_project_state, args.release_root, document))
    if errors:
        print("Architecture validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"Architecture validation passed: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
