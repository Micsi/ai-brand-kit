#!/usr/bin/env python3
"""Validate BrandKit skills and execute artifact-producing, fail-closed dry runs."""

from __future__ import annotations

import argparse
import ast
import hashlib
import hmac
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import jsonschema
import yaml
from yaml.events import AliasEvent

ROOT = Path(__file__).resolve().parents[1]
# Sidecar-Wurzel: der Fork liegt unter <sidecar>/code/ai-brand-kit; BRANDKIT_SIDECAR ueberschreibt.
SIDECAR = Path(os.environ.get("BRANDKIT_SIDECAR", ROOT.parents[1]))
CONTRACT = ROOT / "brandkit-skill-contract.v1.json"
FIXTURES = ROOT / "fixtures/brandkit-skills/dry-runs.v1.json"
COLLISION_FIXTURES = ROOT / "fixtures/brandkit-skills/collision-dry-runs.v1.json"
CORE_SEQUENCE = ["brandkit-setup", "brandkit-interview", "brandkit-visual-decisions", "brandkit-build", "brandkit-publish"]
CONTRACT_SKILLS = [*CORE_SEQUENCE, "brandkit-spacing"]
CANONICAL_SKILLS = [*CONTRACT_SKILLS, "brandkit-activate", "brandkit-affinity", "brandkit-impeccable", "brand-voice-pdf"]
IMPECCABLE_COMMANDS = ["craft", "shape", "init", "document", "extract", "critique", "audit", "polish", "bolder", "quieter", "distill", "harden", "onboard", "animate", "colorize", "typeset", "layout", "delight", "overdrive", "clarify", "adapt", "optimize", "live"]
IMPECCABLE_SOURCE = Path(os.environ.get(
    "BRANDKIT_IMPECCABLE_SOURCE",
    str(SIDECAR / "external-sources/impeccable/.agents/skills/impeccable"),
))
RELEASE_MANIFEST = "skill-contract-release.v1.json"
AFFINITY_TOOLCHAIN_MEMBERS = {
    "toolchain/affinity-codex/affinity-codex-bridge.mjs",
    "toolchain/affinity-codex/smoke-test.mjs",
    "toolchain/affinity-codex/LICENSE.upstream",
    "toolchain/affinity-codex/UPSTREAM.json",
    "toolchain/affinity-codex/codex-project",
}
DESIGN_ROUTES = {
    "affinity-vector-or-logo-work": "brandkit-affinity",
    "spacing-audit-or-token-role": "brandkit-spacing",
    "general-ui-redesign": "brandkit-impeccable -> impeccable",
    "brand-package-generation": "brandkit-build",
    "pdf-publication-or-layout": "brand-voice-pdf -> brandkit-publish",
    "design-md-generation-or-validation": "brandkit-impeccable",
    "visual-brand-candidates": "brandkit-visual-decisions",
}

SPACING_INVENTORY_SCHEMA = "schemas/spacing-inventory.v1.schema.json"
SPACING_PROPOSAL_SCHEMA = "schemas/spacing-proposal.v1.schema.json"
DISCOVERY_HANDOFF_SCHEMA = "schemas/brandkit-discovery-handoff.v1.schema.json"
DISCOVERY_HANDOFF_FIXTURE = "fixtures/brandkit-skills/discovery-handoff.valid.json"


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


def _python_surface(path: Path) -> tuple[set[str], set[str], set[str]]:
    """Return public function names and literal argparse commands from source."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_") and node.name != "main"
    }
    commands: set[str] = set()
    options: set[str] = set()
    loop_commands: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and isinstance(node.iter, (ast.Tuple, ast.List)):
            values = {item.value for item in node.iter.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)}
            if len(values) == len(node.iter.elts):
                loop_commands[node.target.id] = values
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr == "add_parser" and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            commands.add(node.args[0].value)
        if node.func.attr == "add_parser" and node.args and isinstance(node.args[0], ast.Name):
            commands.update(loop_commands.get(node.args[0].id, set()))
        if node.func.attr == "add_argument" and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "command":
            for keyword in node.keywords:
                if keyword.arg == "choices" and isinstance(keyword.value, (ast.Tuple, ast.List)):
                    commands.update(
                        item.value for item in keyword.value.elts
                        if isinstance(item, ast.Constant) and isinstance(item.value, str)
                    )
        if node.func.attr == "add_argument":
            options.update(
                item.value for item in node.args
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
    return functions, commands, options


def _locator(root: Path, value: str) -> tuple[Path, str]:
    relative, symbol = value.split(":", 1)
    path = root / relative
    functions, _, _ = _python_surface(path)
    if symbol not in functions:
        raise ValueError(f"unresolved Python locator: {value}")
    return path, symbol


def _import_locator(root: Path, value: str) -> Any:
    path, symbol = _locator(root, value)
    scripts = str(root / "scripts")
    old_path = list(sys.path)
    displaced = {name: sys.modules.pop(name) for name in ("brand_package", "markdown_contract") if name in sys.modules}
    try:
        sys.path.insert(0, scripts)
        spec = importlib.util.spec_from_file_location(f"brandkit_contract_{path.stem}_{id(root)}", path)
        if spec is None or spec.loader is None:
            raise ValueError(f"cannot import Python locator: {value}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        target = getattr(module, symbol, None)
        if not callable(target):
            raise ValueError(f"Python locator is not callable: {value}")
        return target
    finally:
        sys.path[:] = old_path
        for name in ("brand_package", "markdown_contract"):
            sys.modules.pop(name, None)
        sys.modules.update(displaced)


@contextmanager
def verified_impeccable_snapshot(root: Path, source_root: Path | None = None):
    """Copy descriptor-verified bytes from a materialization of the pinned Git tree."""
    source = source_root or Path(os.environ.get("BRANDKIT_IMPECCABLE_SOURCE", str(IMPECCABLE_SOURCE)))
    checkout = (SIDECAR / "external-sources/impeccable")
    verifier = _import_locator(root, "scripts/impeccable_adapter.py:verify_source_bytes")
    blobs = verifier(source, checkout)
    with tempfile.TemporaryDirectory(prefix="impeccable-verified-source-") as temporary:
        snapshot = Path(temporary) / "impeccable"
        for relative, data in blobs.items():
            target = snapshot / relative; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(data)
            if relative.startswith("scripts/"): target.chmod(0o755)
        yield snapshot


def _schema_mutations(value: Any, schema: dict[str, Any], root_schema: dict[str, Any], path: tuple[Any, ...] = (), whole: Any = None) -> list[tuple[str, Any]]:
    """Generate independent required/closed-object mutations from a schema tree."""
    if "$ref" in schema:
        target: Any = root_schema
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        schema = target
    if whole is None: whole = value
    mutations: list[tuple[str, Any]] = []
    if isinstance(value, dict) and schema.get("type") == "object":
        if schema.get("additionalProperties") is False:
            changed = json.loads(json.dumps(whole)); cursor = changed
            for part in path: cursor = cursor[part]
            cursor["generated_unknown_contract_role"] = True
            mutations.append(("unknown:" + ".".join(map(str, path or ("root",))), changed))
        for key in schema.get("required", []):
            if key in value:
                changed = json.loads(json.dumps(whole)); cursor = changed
                for part in path: cursor = cursor[part]
                del cursor[key]
                mutations.append(("missing:" + ".".join(map(str, (*path, key))), changed))
        for key, child in value.items():
            child_schema = schema.get("properties", {}).get(key)
            if isinstance(child_schema, dict):
                changed = json.loads(json.dumps(whole)); cursor = changed
                for part in path: cursor = cursor[part]
                resolved = child_schema
                if "$ref" in resolved:
                    resolved = root_schema
                    for part in child_schema["$ref"].removeprefix("#/").split("/"): resolved = resolved[part]
                cursor[key] = "__invalid__" if resolved.get("const") is None and "const" in resolved else {} if isinstance(resolved.get("type"), list) and "null" in resolved.get("type", []) else None
                mutations.append(("invalid:" + ".".join(map(str, (*path, key))), changed))
                mutations.extend(_schema_mutations(child, child_schema, root_schema, (*path, key), whole))
    elif isinstance(value, list) and value and isinstance(schema.get("items"), dict):
        mutations.extend(_schema_mutations(value[0], schema["items"], root_schema, (*path, 0), whole))
    return mutations


def impeccable_load_trace(route: str, platform: str = "web", *, edit: bool = False) -> list[str]:
    """Normative adapter trace derived from the pinned skill's route catalog.

    Init deliberately runs context without PRODUCT even when an old PRODUCT exists;
    the adapter loads the newly written platform reference afterwards.  Other native
    routes retain the real loader's early native load.  Deprecated aliases normalize
    before loading, so there is exactly one primary contract.
    """
    if platform not in {"web", "ios", "android", "adaptive"}:
        raise ValueError("unknown Impeccable platform")
    if route == "live" and platform != "web":
        raise ValueError("live is web-only")
    route = "init" if route == "teach" else "new-work" if route == "craft" else route
    if route in {"pin", "unpin"}:
        return [f"admin:pin.mjs:{route}"]
    primary = route
    if route in {"audit", "adapt"} and platform != "web":
        primary += ".native"
    trace = ["context:once"]
    if route != "init" and platform != "web":
        trace.append(f"native:{'ios.md+android.md' if platform == 'adaptive' else platform + '.md'}")
    trace.append(f"primary:{primary}.md")
    if route == "init":
        trace.append("write:PRODUCT.md")
        if platform != "web":
            trace.append(f"native:{'ios.md+android.md' if platform == 'adaptive' else platform + '.md'}")
    if edit and route not in {"routing", "init", "shape"}:
        trace.append("cross:craft-floor.md")
    return trace


def validate_impeccable_load_trace(trace: list[str], route: str, platform: str = "web", *, edit: bool = False) -> None:
    expected = impeccable_load_trace(route, platform, edit=edit)
    if trace != expected:
        raise ValueError(f"invalid Impeccable load trace: expected {expected}, got {trace}")
    if route in {"pin", "unpin"}:
        if trace != [f"admin:pin.mjs:{route}"]:
            raise ValueError("pin administration must not enter the design loader")
        return
    if trace.count("context:once") != 1 or sum(item.startswith("primary:") for item in trace) != 1:
        raise ValueError("trace must contain exactly one context run and primary playbook")
    native = [item for item in trace if item.startswith("native:")]
    if len(native) > 1 or (platform == "web" and native):
        raise ValueError("trace has forbidden native guidance")
    if route == "init" and native and trace.index(native[0]) < trace.index("write:PRODUCT.md"):
        raise ValueError("init native guidance must load after PRODUCT.md")


def impeccable_source_routes(skill_root: Path = IMPECCABLE_SOURCE) -> dict[str, dict[str, Any]]:
    """Parse commands, aliases, specials, and transitive playbook links from the pinned source."""
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    routes: dict[str, dict[str, Any]] = {}
    for command, reference in re.findall(r"^\| `([a-z-]+)(?: [^`]*)?` .*?\[reference/([^]]+\.md)\]", skill, re.M):
        routes[command] = {"source": reference, "primary": reference, "transitive": []}
    if "`teach` aliases `init`" not in skill or "`craft` is a deprecated alias" not in skill:
        raise ValueError("Impeccable alias contract missing")
    routes["teach"] = {"alias": "init", "source": "init.md", "primary": "init.md", "transitive": []}
    routes["craft"].update(primary="new-work.md", transitive=["new-work.md"], alias="new-work")
    shape = (skill_root / "reference/shape.md").read_text(encoding="utf-8")
    if "[new-work.md](new-work.md)" not in shape:
        raise ValueError("shape transitive new-work route missing")
    routes["shape"]["transitive"] = ["new-work.md"]
    for special in ("hooks", "doctor"):
        if f"reference/{special}.md" not in skill:
            raise ValueError(f"Impeccable special route missing: {special}")
        routes[special] = {"source": f"{special}.md", "primary": f"{special}.md", "transitive": []}
    for admin in ("pin", "unpin"):
        if "pin.mjs" not in skill:
            raise ValueError("Impeccable pin administration missing")
        routes[admin] = {"admin": "pin.mjs", "primary": None, "transitive": []}
    routes["routing"] = {"source":"routing.md", "primary":"routing.md", "transitive":[]}
    routes["new-work"] = {"source":"new-work.md", "primary":"new-work.md", "transitive":[]}
    return routes


def transitive_playbook_trace(route: str, skill_root: Path) -> list[dict[str, str]]:
    routes = impeccable_source_routes(skill_root)
    if route not in routes or not routes[route].get("source"):
        raise ValueError(f"unknown playbook route: {route}")
    start = routes[route]["source"]
    queue = [start]; seen: set[str] = set(); trace: list[dict[str, str]] = []
    while queue:
        relative = queue.pop(0)
        if relative in seen: continue
        seen.add(relative); path = skill_root / "reference" / relative
        text = path.read_text(encoding="utf-8")
        if route == "craft":
            role = "primary" if relative == "new-work.md" else "alias" if relative == "craft.md" else "prerequisite" if relative == "init.md" else "context"
        else:
            role = "primary" if relative == start else "delegated" if relative == "new-work.md" else "context"
        trace.append({"path": relative, "role": role})
        for target in re.findall(r"\[[^]]+\]\(([^)#]+\.md)(?:#[^)]+)?\)", text):
            name = Path(target).name
            if (skill_root / "reference" / name).is_file() and name not in seen: queue.append(name)
    if sum(item["role"] == "primary" for item in trace) != 1:
        raise ValueError("transitive trace must have exactly one primary playbook")
    return trace


def run_real_impeccable_context(project: Path, platform: str, *, init: bool = False, skill_root: Path = IMPECCABLE_SOURCE) -> list[str]:
    """Execute the pinned context.mjs and return instrumented file-load events."""
    if platform not in {"web", "ios", "android", "adaptive"}:
        raise ValueError("unknown Impeccable platform")
    run_root = project
    shadow: tempfile.TemporaryDirectory[str] | None = None
    try:
        # Canonical init wrapper defers all native authority until the freshly
        # written PRODUCT: context sees the project but not a stale PRODUCT.
        if init and (project / "PRODUCT.md").exists():
            shadow = tempfile.TemporaryDirectory(prefix="impeccable-init-defer-")
            run_root = Path(shadow.name) / "project"
            shutil.copytree(project, run_root, ignore=shutil.ignore_patterns("PRODUCT.md", ".git"))
        result = subprocess.run(
            ["node", str(skill_root / "scripts/context.mjs")], cwd=run_root,
            text=True, capture_output=True, timeout=30,
            env={**os.environ, "IMPECCABLE_NO_UPDATE_CHECK":"1"},
        )
        if result.returncode:
            raise ValueError(f"context.mjs failed: {result.stderr.strip()}")
        events = ["context:once"]
        if "# PRODUCT.md" in result.stdout:
            events.append("load:PRODUCT.md")
        for name in ("ios", "android"):
            if f"# NATIVE PLATFORM REFERENCE: {name.upper()}" in result.stdout:
                events.append(f"native:{name}.md")
        return events
    finally:
        if shadow is not None:
            shadow.cleanup()


def validate_real_impeccable_context_matrix(skill_root: Path = IMPECCABLE_SOURCE, root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    for platform in ("web", "ios", "android", "adaptive"):
        native = [] if platform == "web" else [f"native:{platform}.md"] if platform != "adaptive" else ["native:ios.md", "native:android.md"]
        for product_exists in (False, True):
            with tempfile.TemporaryDirectory(prefix=f"impeccable-{platform}-init-") as temporary:
                project = Path(temporary) / "project"; project.mkdir()
                if product_exists:
                    (project / "PRODUCT.md").write_text(f"# Product\n\n<!-- impeccable:product-schema 1 -->\n\n## Platform\n\n{platform}\n", encoding="utf-8")
                product = f"# Product\n\n<!-- impeccable:product-schema 1 -->\n\n## Platform\n\n{platform}\n".encode()
                try:
                    wrapper = _import_locator(root, "scripts/impeccable_adapter.py:init_defer")
                    result = wrapper(project, product, skill_root, (SIDECAR / "external-sources/impeccable"))
                    events = result["events"]
                    native_reads = [item["path"] for item in events if item["event"] == "read" and item["path"].startswith("reference/")]
                    expected_native = [] if platform == "web" else [f"reference/{platform}.md"] if platform != "adaptive" else ["reference/ios.md", "reference/android.md"]
                    if sum(item["event"] == "context" for item in events) != 1 or native_reads != expected_native or (project / "PRODUCT.md").read_bytes() != product:
                        errors.append(f"init/{platform}/product={product_exists}: invalid productive wrapper events: {events}")
                except Exception as exc:
                    errors.append(f"init/{platform}/product={product_exists}: productive wrapper failed: {exc}")
        with tempfile.TemporaryDirectory(prefix=f"impeccable-{platform}-route-") as temporary:
            project = Path(temporary) / "project"; project.mkdir()
            (project / "PRODUCT.md").write_text(f"# Product\n\n<!-- impeccable:product-schema 1 -->\n\n## Platform\n\n{platform}\n", encoding="utf-8")
            observed = run_real_impeccable_context(project, platform, skill_root=skill_root)
            expected = ["context:once", "load:PRODUCT.md", *native]
            if observed != expected:
                errors.append(f"context/{platform}: observed real loader events {observed}, expected {expected}")
    return errors


def validate_public_contracts(root: Path, contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    binding = contract["public_contracts"]
    for relative, expected in binding["cli"].items():
        path = root / relative
        try:
            _, actual_commands, actual_options = _python_surface(path)
            if actual_commands != set(expected["commands"]):
                errors.append(f"{relative}: CLI commands drifted: expected {sorted(expected['commands'])}, got {sorted(actual_commands)}")
            if actual_options != set(expected["options"]):
                errors.append(f"{relative}: CLI options drifted: expected {sorted(expected['options'])}, got {sorted(actual_options)}")
        except (OSError, SyntaxError) as exc:
            errors.append(f"{relative}: cannot inspect CLI: {exc}")
    for relative, expected in binding["python"].items():
        path = root / relative
        try:
            actual, _, _ = _python_surface(path)
            if actual != set(expected):
                errors.append(f"{relative}: public Python surface drifted: expected {sorted(expected)}, got {sorted(actual)}")
        except (OSError, SyntaxError) as exc:
            errors.append(f"{relative}: cannot inspect Python API: {exc}")
    package = binding["brand_package"]
    try:
        schema = load(root / package["schema"])
        if schema.get("properties", {}).get("schema_version", {}).get("const") != package["schema_version"]:
            errors.append("brand-package schema version differs from skill contract")
        package_validator = _import_locator(root, package["validator"])
        valid_package = root / "fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360"
        valid_manifest, _, _ = package_validator(valid_package)
        manifest_mutations = _schema_mutations(valid_manifest, schema, schema)
        for kind in ("fonts", "logos"):
            for index, _asset in enumerate(valid_manifest["assets"][kind]):
                for pointer, field in (("asset", "sha256"), ("rights", "evidence_sha256")):
                    changed = json.loads(json.dumps(valid_manifest))
                    target = changed["assets"][kind][index]
                    (target if pointer == "asset" else target["rights"])[field] = "0" * 64
                    manifest_mutations.append((f"pointer:{kind}:{index}:{pointer}", changed))
        for index, _document in enumerate(valid_manifest["document_types"]):
            changed = json.loads(json.dumps(valid_manifest)); changed["document_types"][index]["contract_sha256"] = "0" * 64
            manifest_mutations.append((f"pointer:document:{index}", changed))
        byte_mutations: list[tuple[str, str | None]] = []
        for kind in ("fonts", "logos"):
            for asset in valid_manifest["assets"][kind]:
                byte_mutations.extend([(f"bytes:asset:{asset['id']}", asset["path"]), (f"bytes:rights:{asset['id']}", asset["rights"]["evidence_path"])])
        byte_mutations.extend((f"bytes:document:{item['id']}", item["contract"]) for item in valid_manifest["document_types"])
        byte_mutations.append(("closed-tree:undeclared", None))
        expected_cases = {label for label, _ in manifest_mutations} | {label for label, _ in byte_mutations}; executed: set[str] = set()
        for label, invalid in manifest_mutations:
            with tempfile.TemporaryDirectory(prefix=secrets.token_hex(16)) as temporary:
                candidate = Path(temporary) / secrets.token_hex(12); shutil.copytree(valid_package, candidate)
                (candidate / "brand-package.json").write_text(json.dumps(invalid, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                try: package_validator(candidate)
                except Exception: executed.add(label)
                else: errors.append(f"brand-package production validator accepted generated mutation {label}")
        for label, relative in byte_mutations:
            with tempfile.TemporaryDirectory(prefix=secrets.token_hex(16)) as temporary:
                candidate = Path(temporary) / secrets.token_hex(12); shutil.copytree(valid_package, candidate)
                target = candidate / (relative or f"undeclared-{secrets.token_hex(8)}.bin")
                target.write_bytes((target.read_bytes() if target.exists() else b"") + b"\x00R4-mutation")
                try: package_validator(candidate)
                except Exception: executed.add(label)
                else: errors.append(f"brand-package production validator accepted generated mutation {label}")
        if executed != expected_cases:
            errors.append(f"brand-package mutation table incomplete: missing={sorted(expected_cases-executed)} extra={sorted(executed-expected_cases)}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"brand-package schema binding cannot be read: {exc}")
    documents = binding["documents"]
    try:
        schema = load(root / documents["schema"])
        validator = jsonschema.Draft202012Validator(schema)
        document_validator = _import_locator(root, documents["validator"])
        base_types = _import_locator(root, "scripts/markdown_contract.py:load_base_types")()
        declared = documents["types"]
        actual_files = {path.stem.removesuffix(".v1"): path.relative_to(root).as_posix() for path in (root / "document-types").glob("*.v1.json")}
        if declared != actual_files:
            errors.append(f"document type catalog drifted: expected {declared}, got {actual_files}")
        for document_id, relative in declared.items():
            document = load(root / relative)
            validator.validate(document)
            document_validator(document, base_types)
            if document.get("id") != document_id or document.get("schema_version") != "document-type.v1":
                errors.append(f"{relative}: document identity differs from catalog")
            for label, invalid in _schema_mutations(document, schema, schema):
                try: document_validator(invalid, base_types)
                except Exception: pass
                else: errors.append(f"{relative}: production validator accepted generated mutation {label}")
    except (OSError, ValueError, json.JSONDecodeError, jsonschema.ValidationError) as exc:
        errors.append(f"document contract binding failed: {exc}")
    return errors


def validate_optional_discovery(root: Path, contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    discovery = contract["optional_discovery"]["grill-with-docs"]
    if (discovery["activation"] != "brandkit-interview-explicit" or discovery["invocation"] != "$brandkit-interview"
            or discovery["wrapper_policy"] != "reference-only-never-invoke"
            or discovery["orchestration"] != ["grilling", "domain-modeling"]
            or discovery["progress_location"] != ".brandkit/work/<run-id>/interview-progress.md"
            or discovery["minimum_answer_turns"] != 2
            or discovery["progress_authentication"] != "derived-subkey HMAC-SHA256 project+run predecessor chain with external non-resettable head ledger"
            or discovery["receipt_progress_binding"] != "chain-id+current-head+turn"
            or discovery["review_location"] != ".brandkit/work/<run-id>/review.md"
            or discovery["confirmation"] != "explicit-chat-approval-before-internal-confirm"):
        errors.append("optional discovery orchestration identity/order drifted")
    fixture = load(root / DISCOVERY_HANDOFF_FIXTURE)
    jsonschema.Draft202012Validator(load(root / discovery["handoff_schema"])).validate(fixture)
    members = discovery["bundle"]["members"]
    digest = _import_locator(root, "scripts/brandkit_discovery.py:bundle_digest")(members)
    if digest != discovery["bundle"]["content_sha256"] or [item["path"] for item in members] != sorted(item["path"] for item in members):
        errors.append("optional discovery bundle derivation/member order drifted")
    setup = (root / "skills/brandkit-setup/SKILL.md").read_text(encoding="utf-8")
    interview = (root / "skills/brandkit-interview/SKILL.md").read_text(encoding="utf-8")
    if "installiere nichts" not in setup or "rufe keinen der" not in setup:
        errors.append("brandkit-setup does not preserve read-only optional discovery")
    if ("Rufe\n   `grill-with-docs` nie auf" not in interview
            or "Bleibe über beliebig\n   viele Turns in `grilling`" not in interview
            or "Diese Entscheidungen bestätigen?" not in interview
            or "Handoff ausschließlich intern" not in interview
            or "$brandkit-interview" not in interview or "vollständigen nativen Elf-Achsen-Pfad" not in interview):
        errors.append("brandkit-interview orchestration/wrapper/fallback contract drifted")
    return errors


def validate_progressive_disclosure(root: Path, contract: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    routes = contract["progressive_disclosure"]["routes"]
    catalog = contract["progressive_disclosure"]["catalog"]
    used: set[str] = set()
    for item_id, item in catalog.items():
        path = root / item["path"]
        role, relative = item["role"], item["path"]
        role_path_ok = (
            (role == "primary" and re.fullmatch(r"skills/[^/]+/references/[^/]+\.md", relative))
            or (role == "authoring" and relative in {
                "skills/brand-voice-pdf/references/markdown-authoring.md",
                "skills/brandkit-affinity/references/vector-workflow.md",
            })
            # The two contract documents that live at the repository root rather
            # than under schemas/: a JSON Schema decides a document's shape, these
            # two decide its content, and both are named literally so the role
            # stays a closed list instead of "any .json at the root".
            or (role == "contract" and (relative.startswith("schemas/") and relative.endswith(".json") or relative in {"markdown-contract.v2.json", "visual-decision-sources.v1.json"}))
            or (role == "release" and relative == "impeccable-adapter.v1.json")
            or (role == "discovery" and relative == "skills/brandkit-interview/references/grill-with-docs-handoff.md")
        )
        if not role_path_ok:
            errors.append(f"{item_id}: catalog role/path combination is forbidden")
        try:
            if sha256(path) != item["sha256"]:
                errors.append(f"{item_id}: catalog content hash drifted")
            if role == "primary" and (path.stat().st_size < 120 or not path.read_text(encoding="utf-8").startswith("# ")):
                errors.append(f"{item_id}: primary playbook is semantically empty")
        except OSError as exc:
            errors.append(f"{item_id}: catalog path cannot be read: {exc}")
    seen_skills: list[str] = []
    for route_id, route in routes.items():
        seen_skills.append(route["skill"])
        loads = [route["primary"], *route["context"]]
        used.update(loads)
        if route["primary"] in route["context"] or len(loads) != len(set(loads)):
            errors.append(f"{route_id}: load trace is not disjoint")
        if route["primary"] not in catalog or catalog.get(route["primary"], {}).get("role") != "primary":
            errors.append(f"{route_id}: primary is not a catalogued primary playbook")
        if any(item not in catalog or catalog.get(item, {}).get("role") == "primary" for item in route["context"]):
            errors.append(f"{route_id}: context contains unknown or primary-role entry")
        for item_id in loads:
            if item_id not in catalog:
                continue
            relative = catalog[item_id]["path"]
            path = root / relative
            try:
                resolved = path.resolve(strict=True)
                if not _is_confined(root.resolve(strict=True), resolved) or not resolved.is_file():
                    raise ValueError("reference is outside repository or nonregular")
            except (OSError, ValueError) as exc:
                errors.append(f"{route_id}: invalid load {relative}: {exc}")
        skill_path = root / "skills" / route["skill"] / "SKILL.md"
        try:
            meta, body = frontmatter(skill_path.read_text(encoding="utf-8"))
            if meta.get("name") != route["skill"]:
                errors.append(f"{route_id}: route skill identity drifted")
            primary_path = catalog.get(route["primary"], {}).get("path", "")
            relative_primary = Path(primary_path).relative_to(skill_path.parent.relative_to(root)).as_posix()
            if relative_primary not in body:
                errors.append(f"{route_id}: SKILL.md does not route to its primary playbook")
            playbook = (root / primary_path).read_text(encoding="utf-8")
            for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", playbook):
                if (root / primary_path).parent.joinpath(target).resolve() == (root / primary_path).resolve():
                    errors.append(f"{route_id}: primary playbook self-references")
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{route_id}: skill cannot be validated: {exc}")
    if sorted(seen_skills) != sorted(CANONICAL_SKILLS):
        errors.append("progressive-disclosure routes must cover every canonical skill exactly once")
    if used != set(catalog):
        errors.append("progressive-disclosure catalog contains unused or routes omit catalogued entries")
    impeccable = contract["progressive_disclosure"]["impeccable"]
    adapter = load(root / "impeccable-adapter.v1.json")
    inventory = load(root / "impeccable-upstream-inventory.v1.json")
    commands = adapter["inventory"]["routes"]
    references = {Path(item).name for item in inventory["references"]}
    try:
        with verified_impeccable_snapshot(root) as pinned_skill:
            source_routes = impeccable_source_routes(pinned_skill)
            parsed_commands = [name for name in source_routes if name in commands]
            if commands != IMPECCABLE_COMMANDS or set(parsed_commands) != set(commands):
                errors.append("Impeccable command catalog drifted from pinned SKILL.md")
            for name, route in source_routes.items():
                for reference in [route.get("source"), route.get("primary"), *route.get("transitive", [])]:
                    if reference and reference not in references:
                        errors.append(f"Impeccable source route {name} has uncatalogued {reference}")
            for transitive_route in ("craft", "shape"):
                playbook_trace = transitive_playbook_trace(transitive_route, pinned_skill)
                if sum(item["role"] == "primary" for item in playbook_trace) != 1:
                    errors.append(f"Impeccable {transitive_route} has a second primary playbook")
            matrix_routes = ["routing", "new-work", *commands, "teach", "hooks", "doctor", "pin", "unpin"]
            for route_name in matrix_routes:
                for platform in impeccable["platforms"]:
                    try:
                        trace = impeccable_load_trace(route_name, platform, edit=route_name not in {"routing", "init", "shape"})
                        validate_impeccable_load_trace(trace, route_name, platform, edit=route_name not in {"routing", "init", "shape"})
                        primary = next((item.removeprefix("primary:") for item in trace if item.startswith("primary:")), None)
                        if primary and primary not in references:
                            errors.append(f"Impeccable route {route_name}/{platform} has uncatalogued primary {primary}")
                        for native in (item.removeprefix("native:") for item in trace if item.startswith("native:")):
                            if any(part not in references for part in native.split("+")):
                                errors.append(f"Impeccable route {route_name}/{platform} has uncatalogued native guidance")
                    except ValueError as exc:
                        if not (route_name == "live" and platform != "web" and str(exc) == "live is web-only"):
                            errors.append(f"Impeccable route {route_name}/{platform}: {exc}")
            errors.extend(validate_real_impeccable_context_matrix(pinned_skill, root))
    except Exception as exc:
        errors.append(f"Impeccable pinned source cannot be authenticated or executed: {exc}")
    design = contract["progressive_disclosure"]["design_context"]
    module_path, function_name = design["generator"].split(":", 1)
    try:
        functions, _, _ = _python_surface(root / module_path)
        if function_name not in functions:
            errors.append("DESIGN.md generator is missing")
        tree = ast.parse((root / module_path).read_text(encoding="utf-8"))
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in {"shared_tokens", "yaml_lines", function_name}]
        namespace: dict[str, Any] = {"json": json, "Any": Any}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(root / module_path), "exec"), namespace)
        generator = namespace[function_name]
        cases = [
            {"brand_id":"empty","version":"1.0.0","voice":{"principles":[],"avoid":[]},"shared_visual_tokens":{},"pdf":{"tokens":{"color_roles":{"bad":"#123456"}}}},
            {"brand_id":"full","version":"1.0.0","voice":{"principles":["Quiet confidence"],"avoid":["No spectacle"]},"shared_visual_tokens":{"colors":{"primary":"#112233"},"typography":{"body":"builtin:Helvetica"},"spacing":{"section":"16px"},"components":{"card":{}},"rounded":{"card":"4px"}},"pdf":{"tokens":{"color_roles":{"pdf-only":"#abcdef"}}}}
        ]
        for manifest in cases:
            text = generator(manifest).decode("utf-8")
            meta, body = frontmatter(text)
            expected_root = {"name", "description", *manifest["shared_visual_tokens"]}
            if set(meta) != expected_root:
                errors.append("DESIGN.md frontmatter has missing or invented normative roles")
            if any(key in meta for key in ("pdf", "pdf-only")) or "#abcdef" in text or "#123456" in text:
                errors.append("DESIGN.md escalates PDF-only tokens")
            if any(meta.get(key) != manifest["shared_visual_tokens"].get(key) for key in manifest["shared_visual_tokens"]):
                errors.append("DESIGN.md frontmatter differs from shared_visual_tokens")
            expected_roles = ", ".join(sorted(manifest["shared_visual_tokens"])) or "No portable visual token roles are declared."
            actual_roles = body.split("## Tokens and Roles\n\n", 1)[1].split("\n", 1)[0] if "## Tokens and Roles\n\n" in body else None
            if actual_roles != expected_roles:
                errors.append("DESIGN.md token-role summary escalates or omits namespaces")
            positions = [body.find(f"## {section}") for section in design["required_sections"]]
            if any(position < 0 for position in positions) or positions != sorted(positions):
                errors.append("DESIGN.md sections are missing or out of order")
            if design["source_of_truth"] not in body or "PDF renderer tokens remain PDF-only" not in body:
                errors.append("DESIGN.md source-of-truth or medium separation is missing")
            if manifest["voice"]["avoid"] and not all(item in body for item in manifest["voice"]["avoid"]):
                errors.append("DESIGN.md anti-references differ from Brand Package")
    except (OSError, SyntaxError, ValueError) as exc:
        errors.append(f"DESIGN.md contract cannot be inspected: {exc}")
    return errors


_PART1_PASS_CACHE: set[tuple[str, ...]] = set()


def _verify_part1_snapshot(snapshot: Path, payload: dict[str, Any], owner_key: Path) -> None:
    snapshot_hash = sha256(snapshot)
    if snapshot_hash != payload["part1_snapshot_sha256"]:
        raise ValueError("Part1 snapshot digest mismatch")
    pass_hash = sha256_bytes((json.dumps(payload["part1_pass"], sort_keys=True, separators=(",", ":")) + "\n").encode())
    cache_key = (snapshot_hash, payload["part1_bootstrap_sha256"], sha256(owner_key), pass_hash)
    if cache_key in _PART1_PASS_CACHE: return
    with tempfile.TemporaryDirectory(prefix="part1-authenticated-snapshot-") as temporary:
        evidence = Path(temporary) / "evidence"; evidence.mkdir()
        regular: set[str] = set()
        with tarfile.open(snapshot, "r:gz") as archive:
            for member in archive.getmembers():
                name = member.name.removeprefix("./")
                if not name: continue
                pure = Path(name)
                if pure.is_absolute() or ".." in pure.parts or member.issym() or member.islnk():
                    raise ValueError("Part1 snapshot has unsafe member")
                target = evidence / pure
                if member.isdir(): target.mkdir(parents=True, exist_ok=True); continue
                if not member.isfile() or name in regular: raise ValueError("Part1 snapshot member is not a unique regular file")
                handle = archive.extractfile(member)
                if handle is None: raise ValueError("Part1 snapshot member cannot be read")
                target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(handle.read()); regular.add(name)
        root_rel = "quality-loop/evidence/base-part1-trust-f5-evidence-root.json"
        signature_rel = "quality-loop/evidence/base-part1-trust-f5-evidence-signature.json"
        generator_rel = "quality-loop/evidence/generate-base-part1-trust-f5.py"
        root_doc = load(evidence / root_rel)
        expected_members = {row["path"] for row in root_doc["members"]} | {root_rel, signature_rel}
        if regular != expected_members or root_doc.get("member_count") != payload["part1_pass"]["members"]:
            raise ValueError("Part1 snapshot closed member set mismatch")
        snapshot_parent = Path(temporary) / "trusted"; snapshot_parent.mkdir(mode=0o700)
        bootstrap = (SIDECAR / "quality-loop/trusted-verifiers/brandkit-candidate-trust-v1.py")
        result = subprocess.run([
            sys.executable, str(bootstrap), "--expected-bootstrap-sha256", payload["part1_bootstrap_sha256"],
            "--evidence-root", str(evidence), "--root", root_rel, "--signature", signature_rel,
            "--generator", generator_rel, "--owner-key", str(owner_key), "--trusted-snapshot-parent", str(snapshot_parent),
        ], text=True, capture_output=True, timeout=120)
        if result.returncode: raise ValueError(f"Part1 bootstrap rejected historical snapshot: {result.stderr.strip()}")
        observed = json.loads(result.stdout); full = observed.get("full_verifier", {}); auth = observed.get("authenticated_root", {})
        semantic = {
            "status": observed.get("status"), "bootstrap_sha256": observed.get("bootstrap_sha256"),
            "root_sha256": auth.get("root_sha256"), "key_id": auth.get("key_id"), "members": auth.get("members"),
            "full_status": full.get("status"), "tree": full.get("tree"), "entries": full.get("entries"),
            "evidence_members": full.get("evidence_members"), "authenticated_snapshot": full.get("authenticated_snapshot"),
        }
        if semantic != payload["part1_pass"] or semantic["status"] != "pass" or semantic["full_status"] != "pass" or semantic["authenticated_snapshot"] is not True:
            raise ValueError("Part1 fresh semantic PASS differs from owner attestation")
    _PART1_PASS_CACHE.add(cache_key)


def validate_release_manifest(root: Path, *, expected_attestation_sha256: str | None = None, attestation_path: Path | None = None, owner_key: Path | None = None, part1_snapshot: Path | None = None, evidence_path: Path | None = None) -> list[str]:
    errors: list[str] = []
    try:
        document = load(root / RELEASE_MANIFEST)
        if set(document) != {"schema_version", "baseline_head", "members"} or document["schema_version"] != "brandkit-skill-contract-release.v1":
            return ["skill-contract release manifest is not closed v1"]
        if document["baseline_head"] != "d65179442ca2e4c14dd2636bcb3511ca326d893f":
            errors.append("skill-contract release baseline drifted")
        if not isinstance(document["members"], list) or not document["members"]:
            return ["skill-contract release manifest has no members"]
        seen: set[str] = set()
        for member in document["members"]:
            if not isinstance(member, dict) or set(member) != {"path", "sha256"}:
                errors.append("skill-contract release member is not closed")
                continue
            relative = member["path"]
            if relative in seen or not re.fullmatch(r"[A-Za-z0-9._/-]+", relative):
                errors.append(f"skill-contract release has duplicate/invalid member: {relative}")
                continue
            seen.add(relative)
            path = root / relative
            try:
                if not _is_confined(root.resolve(strict=True), path.resolve(strict=True)) or not path.is_file() or sha256(path) != member["sha256"]:
                    errors.append(f"skill-contract release member drifted: {relative}")
            except OSError as exc:
                errors.append(f"skill-contract release member missing: {relative}: {exc}")
        contract = load(root / CONTRACT.name)
        required = {
            CONTRACT.name, "schemas/brandkit-skill-contract.v1.schema.json",
            "schemas/brandkit-skill-request.v1.schema.json", "schemas/brandkit-skill-result.v1.schema.json",
            "scripts/validate_brandkit_skills.py", "scripts/impeccable_adapter.py",
            "fixtures/brandkit-skills/dry-runs.v1.json", "fixtures/brandkit-skills/collision-dry-runs.v1.json",
            DISCOVERY_HANDOFF_FIXTURE,
            "impeccable-adapter.v1.json", "impeccable-upstream-inventory.v1.json",
        }
        public = contract["public_contracts"]
        required.update(public["cli"]); required.update(public["python"])
        required.add(public["brand_package"]["schema"]); required.add(public["documents"]["schema"])
        required.update(public["documents"]["types"].values())
        required.update(item["path"] for item in contract["progressive_disclosure"]["catalog"].values())
        required.update(f"skills/{name}/SKILL.md" for name in CANONICAL_SKILLS)
        required.update(AFFINITY_TOOLCHAIN_MEMBERS)
        if seen != required:
            errors.append(f"skill-contract release inventory differs from canonical catalogs: missing={sorted(required-seen)} extra={sorted(seen-required)}")
        expected_attestation_sha256 = expected_attestation_sha256 or os.environ.get("BRANDKIT_SKILL_RELEASE_ATTESTATION_SHA256")
        attestation_path = attestation_path or (Path(os.environ["BRANDKIT_SKILL_RELEASE_ATTESTATION"]) if os.environ.get("BRANDKIT_SKILL_RELEASE_ATTESTATION") else None)
        owner_key = owner_key or (Path(os.environ["BRANDKIT_OWNER_KEY"]) if os.environ.get("BRANDKIT_OWNER_KEY") else None)
        if not expected_attestation_sha256 or attestation_path is None or owner_key is None:
            errors.append("external release attestation inputs are required")
        else:
            raw = attestation_path.read_bytes()
            if sha256_bytes(raw) != expected_attestation_sha256:
                errors.append("external release attestation digest mismatch")
            envelope = json.loads(raw)
            if set(envelope) != {"schema_version","issuer","payload","algorithm","key_id","hmac_sha256","threat_model"}:
                errors.append("external release attestation is not closed")
            else:
                payload = envelope["payload"]
                # ``evidence_sha256`` gehoert in den SIGNIERTEN Rumpf: eine Evidence,
                # die an nichts haengt, laesst sich nach dem Urteil austauschen, ohne
                # dass es auffaellt - genau das ist geschehen.  Gebunden bricht ein
                # spaeterer Austausch die Signatur.  Die Evidence nennt ihrerseits
                # NICHT den Digest dieser Attestation, sonst haetten beide Hashes
                # einander zur Voraussetzung; ein Verweis auf den DATEINAMEN ist
                # unbedenklich, weil er sich mit dem Inhalt nicht aendert.
                required_payload = {"release_manifest_sha256","inventory_sha256","evidence_sha256","part1_root_sha256","part1_signature_sha256","part1_bootstrap_sha256","part1_snapshot_sha256","part1_pass"}
                if set(payload) != required_payload:
                    errors.append("external release attestation payload is not closed: "
                                  f"missing={sorted(required_payload - set(payload))} "
                                  f"extra={sorted(set(payload) - required_payload)}")
                key = owner_key.read_bytes()
                signed = {"schema_version":envelope["schema_version"], "issuer":envelope["issuer"], "payload":payload}
                canonical = (json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
                if envelope["algorithm"] != "HMAC-SHA256" or envelope["key_id"] != sha256_bytes(key)[:16] or not hmac.compare_digest(envelope["hmac_sha256"], hmac.new(key, canonical, hashlib.sha256).hexdigest()):
                    errors.append("external release owner-HMAC failed")
                if payload["release_manifest_sha256"] != sha256(root / RELEASE_MANIFEST):
                    errors.append("release manifest differs from external attestation")
                if payload["inventory_sha256"] != sha256(root / "impeccable-upstream-inventory.v1.json"):
                    errors.append("release inventory differs from external attestation")
                evidence = evidence_path or (Path(os.environ["BRANDKIT_RELEASE_EVIDENCE"]) if os.environ.get("BRANDKIT_RELEASE_EVIDENCE") else None)
                if evidence is None:
                    errors.append("external release evidence is required")
                elif not evidence.is_file():
                    errors.append("external release evidence is missing")
                elif payload.get("evidence_sha256") != sha256(evidence):
                    errors.append("release evidence differs from external attestation")
                part1_root = (SIDECAR / "quality-loop/evidence/base-part1-trust-f5-evidence-root.json")
                part1_signature = (SIDECAR / "quality-loop/evidence/base-part1-trust-f5-evidence-signature.json")
                bootstrap = (SIDECAR / "quality-loop/trusted-verifiers/brandkit-candidate-trust-v1.py")
                if (payload["part1_root_sha256"] != sha256(part1_root)
                    or payload["part1_signature_sha256"] != sha256(part1_signature)
                    or payload["part1_bootstrap_sha256"] != sha256(bootstrap)):
                    errors.append("external release attestation is not bound to the Part1 trust chain")
                part1_snapshot = part1_snapshot or (Path(os.environ["BRANDKIT_PART1_SNAPSHOT"]) if os.environ.get("BRANDKIT_PART1_SNAPSHOT") else None)
                if part1_snapshot is None:
                    errors.append("external authenticated Part1 snapshot is required")
                else:
                    try: _verify_part1_snapshot(part1_snapshot, payload, owner_key)
                    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc: errors.append(f"Part1 semantic verification failed: {exc}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"skill-contract release manifest cannot be verified: {exc}")
    return errors


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
        operation = state.get("operation")
        if operation in {"prepare-visual-confirmation", "consume-visual-decisions"}:
            # The generic contract runner validates routing only. The discovery
            # CLI owns descriptor-safe reads, the external owner key and HMAC.
            blockers.append("secure-discovery-cli-required")
            decision = "blocked"
        else:
            if set(state) != {"owner", "axes", "conflicts", "legal_claims"}: blockers.append("state-contract-mismatch")
            if state.get("owner") != "confirmed": blockers.append("missing_owner")
            if "normative" in state.get("conflicts", []): blockers.append("normative_brand_conflict")
            if any(value != "official-source" for value in state.get("legal_claims", [])): blockers.append("unsupported_legal_claim")
            if not state.get("axes") or "unknown" in state.get("axes", []): blockers.append("required_axes_unaccounted")
            decision = "blocked" if blockers else ("pass-with-todo" if "open" in state["axes"] else "pass")
    elif skill == "brandkit-visual-decisions":
        expected_keys = {"operation", "visual_brief", "affinity_observation", "candidate_bundle", "candidate_review", "required_domains"}
        if set(state) != expected_keys or state.get("operation") != "review-candidates": blockers.append("state-contract-mismatch")
        if not state.get("required_domains"): blockers.append("no-open-visual-domains")
        decision = "blocked" if blockers else "pass-with-todo"
    elif skill == "brandkit-build":
        expected_keys = {"provenance", "conflicts", "asset_rights", "brand_pin", "instruction_sources", "claude_bridge", "hermes_context", "visual_decisions"}
        if set(state) != expected_keys: blockers.append("state-contract-mismatch")
        if not state.get("provenance") or any(value != "complete" for value in state.get("provenance", [])): blockers.append("missing_provenance")
        if "normative" in state.get("conflicts", []): blockers.append("normative_brand_conflict")
        if any(value != "verified" for value in state.get("asset_rights", [])): blockers.append("unresolved_asset_rights")
        if state.get("brand_pin") != "verified": blockers.append("missing_brand_pin")
        if state.get("instruction_sources") != ["AGENTS.md"] or state.get("claude_bridge") != "CLAUDE.md -> AGENTS.md" or state.get("hermes_context") != "cwd/AGENTS.md": blockers.append("instruction_copy")
        visual = state.get("visual_decisions")
        if visual in {"missing", "stale", "unknown"}: blockers.append("visual_decisions_not_consumable")
        decision = "blocked" if blockers else ("package-draft-only" if visual == "required-open" else "package-ready")
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
    if skill == "brandkit-visual-decisions":
        result["visual_decision_result"] = {
            "schema_version": "brandkit-visual-decision-result.v1",
            "outcome": "blocked" if blockers else "candidates-ready",
            "domains": sorted(state.get("required_domains", [])),
            "normative": False,
            "next_operation": "brandkit-interview:prepare-visual-confirmation" if not blockers else "resolve-blockers",
        }
    jsonschema.Draft202012Validator(result_schema).validate(result)
    return result


def validate(root: Path = ROOT, **trust: Any) -> list[str]:
    errors: list[str] = []
    try:
        source_validator_path = root / "scripts/validate_visual_decision_sources.py"
        source_spec = importlib.util.spec_from_file_location("brandkit_visual_source_validator", source_validator_path)
        if source_spec is None or source_spec.loader is None:
            errors.append("visual-decision source validator cannot be loaded")
        else:
            source_module = importlib.util.module_from_spec(source_spec)
            source_spec.loader.exec_module(source_module)
            errors.extend(source_module.validate_visual_decision_sources(root))
        errors.extend(validate_release_manifest(root, **trust))
        contract = load(root / CONTRACT.name)
        jsonschema.Draft202012Validator(load(root / "schemas/brandkit-skill-contract.v1.schema.json")).validate(contract)
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
        affinity = contract.get("optional_adapters", {}).get("brandkit-affinity", {})
        expected_affinity = {
            "owner": "affinity-vector-authoring-and-evidence",
            "inputs": ["visual_brief", "project_root", "run_id", "affinity_server_observation", "optional_mutation_receipt"],
            "outputs": ["read_only_capability_observation", "staged_vector_artifact", "export_inventory", "mutation_evidence"],
            "mutates": [".brandkit/work/<run-id>/visual-decisions/staging/<document-id>"],
            "read_only": ["brand-raw.md", "visual-brief", "owner-source-assets", "brand-package.v1"],
            "gate": "a live loopback Affinity MCP server is observed; every mutation is explicitly authorized, receipt-bound and confined to run-local staging",
            "fails_on": ["affinity_unavailable", "affinity_identity_ambiguous", "missing_mutation_receipt", "receipt_scope_mismatch", "non_staging_write", "unverified_export"],
        }
        if affinity != expected_affinity:
            errors.append("brandkit-affinity optional adapter contract drifted")
        if contract.get("routing") != DESIGN_ROUTES:
            errors.append("design-skill routing contract drifted")
        for name in [*CONTRACT_SKILLS, "brandkit-affinity"]:
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
        errors.extend(validate_public_contracts(root, contract))
        errors.extend(validate_optional_discovery(root, contract))
        errors.extend(validate_progressive_disclosure(root, contract))
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError, jsonschema.ValidationError) as exc:
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
    visual_choice = bool(words & {"kandidaten", "kandidat", "farbpalette", "farbpaletten", "schriftkandidaten", "logo-kandidaten", "logoentwuerfe"})
    if visual_choice and words & {"waehle", "auswahl", "vergleiche", "pruefe", "entscheide", "zeige"}:
        return "brandkit-visual-decisions"
    affinity_work = bool(words & {"affinity", "vektor", "vektoren", "logo", "lockup", "favicon"})
    if affinity_work and words & {"erstelle", "erzeuge", "bearbeite", "optimiere", "exportiere", "pruefe"}:
        return "brandkit-affinity"
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
    parser.add_argument("--release-attestation", type=Path)
    parser.add_argument("--release-evidence", type=Path)
    parser.add_argument("--expected-release-attestation-sha256")
    parser.add_argument("--owner-key", type=Path)
    parser.add_argument("--part1-snapshot", type=Path)
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
    errors = validate(
        args.root,
        expected_attestation_sha256=args.expected_release_attestation_sha256,
        attestation_path=args.release_attestation,
        evidence_path=args.release_evidence,
        owner_key=args.owner_key,
        part1_snapshot=args.part1_snapshot,
    )
    errors.extend(dry_run(args.dry_runs or args.root / "fixtures/brandkit-skills/dry-runs.v1.json", args.root))
    errors.extend(collision_dry_run(args.root / "fixtures/brandkit-skills/collision-dry-runs.v1.json"))
    if errors:
        print("\n".join(errors), file=sys.stderr); return 1
    print("brandkit skill contract: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
