#!/usr/bin/env python3
"""Closed, content-addressed Brand Package v1 lifecycle and resolver."""
from __future__ import annotations

import argparse, hashlib, json, math, os, re, shutil, stat, struct, sys
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any

from markdown_contract import load_base_types, validate_document_type

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = {name: ROOT / "schemas" / name for name in (
    "brand-package.v1.schema.json", "document-type.v1.schema.json",
    "rights-evidence.v1.schema.json", "brand-registry-index.v1.schema.json")}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
BRAND_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ALLOWED_OVERRIDES = {"toc", "section_numbering", "document_type"}
PAYLOAD_NAMES = re.compile(r"(?:secret|credential|password|private.?key|api.?key|token|customer|client|brief|final|invoice|output)", re.I)
PAYLOAD_BYTES = (b"-----BEGIN PRIVATE KEY", b"-----BEGIN RSA PRIVATE KEY", b"api_key=", b"password=", b"customer confidential", b"%PDF-", b"PK\x03\x04")
STATE_TARGET = re.compile(r"^states/([0-9a-f]{64})$")
SNAPSHOT_LOCATOR = re.compile(r"^snapshot:([A-Za-z0-9._/-]+)@sha256:([0-9a-f]{64})$")
SVG_NS = "{" + "http" + "://www.w3.org/2000/svg}"
SVG_TAGS = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "title", "desc", "defs", "clipPath", "mask", "linearGradient", "radialGradient", "stop"}
SVG_ATTRS = {"viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "d", "points", "fill", "fill-opacity", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin", "opacity", "transform", "offset", "stop-color", "stop-opacity", "gradientUnits", "gradientTransform", "id", "clip-path", "mask"}
SVG_TEXT_TAGS = {"title", "desc"}

class ContractError(Exception): pass

def load_json(path: Path) -> Any:
    try: return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite JSON number {value}")))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc: raise ContractError(f"cannot read JSON {path}: {exc}") from exc

def canonical(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()

def sha_bytes(value: bytes) -> str: return hashlib.sha256(value).hexdigest()
def sha256_file(path: Path) -> str: return sha_bytes(path.read_bytes())

def _base128(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for index in range(5):
        if offset >= len(data): raise ContractError("truncated WOFF2 table directory")
        byte = data[offset]; offset += 1
        if index == 0 and byte == 0x80: raise ContractError("non-canonical WOFF2 UIntBase128")
        if value & 0xFE000000: raise ContractError("overflowing WOFF2 UIntBase128")
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80: return value, offset
    raise ContractError("overlong WOFF2 UIntBase128")

def validate_woff2(data: bytes, label: str) -> None:
    if len(data) < 48 or data[:4] != b"wOF2": raise ContractError(f"{label} is not WOFF2")
    signature, flavor, length, tables, reserved, sfnt_size, compressed_size, major, minor, meta_offset, meta_length, meta_orig, priv_offset, priv_length = struct.unpack(">4sIIHHI IHHIIIII", data[:48])
    if signature != b"wOF2" or flavor in (0, 0x774F4632) or length != len(data) or not tables or reserved != 0: raise ContractError(f"{label} has an invalid WOFF2 header")
    if sfnt_size < 12 + 16 * tables or compressed_size <= 0: raise ContractError(f"{label} has invalid WOFF2 sizes")
    offset = 48
    known_tags = (b"cmap",b"head",b"hhea",b"hmtx",b"maxp",b"name",b"OS/2",b"post",b"cvt ",b"fpgm",b"glyf",b"loca",b"prep",b"CFF ",b"VORG",b"EBDT",b"EBLC",b"gasp",b"hdmx",b"kern",b"LTSH",b"PCLT",b"VDMX",b"vhea",b"vmtx",b"BASE",b"GDEF",b"GPOS",b"GSUB",b"EBSC",b"JSTF",b"MATH",b"CBDT",b"CBLC",b"COLR",b"CPAL",b"SVG ",b"sbix",b"acnt",b"avar",b"bdat",b"bloc",b"bsln",b"cvar",b"fdsc",b"feat",b"fmtx",b"fvar",b"gvar",b"hsty",b"just",b"lcar",b"mort",b"morx",b"opbd",b"prop",b"trak",b"Zapf",b"Silf",b"Glat",b"Gloc",b"Feat",b"Sill")
    seen: set[bytes] = set()
    for _ in range(tables):
        if offset >= len(data): raise ContractError(f"{label} has a truncated WOFF2 directory")
        flags = data[offset]; offset += 1; tag_index = flags & 0x3F; transform = flags >> 6
        if tag_index == 63:
            if offset + 4 > len(data): raise ContractError(f"{label} has a truncated custom WOFF2 tag")
            tag = data[offset:offset+4]; offset += 4
        else: tag = known_tags[tag_index]
        if tag in seen: raise ContractError(f"{label} has duplicate WOFF2 tables")
        seen.add(tag); original, offset = _base128(data, offset)
        if original <= 0: raise ContractError(f"{label} has an empty WOFF2 table")
        transformed = (tag in (b"glyf", b"loca") and transform == 0) or (tag not in (b"glyf", b"loca") and transform != 0)
        if tag in (b"glyf", b"loca") and transform not in (0, 3): raise ContractError(f"{label} uses an invalid WOFF2 transform")
        if tag not in (b"glyf", b"loca") and transform != 0: raise ContractError(f"{label} uses an unsupported WOFF2 transform")
        if transformed:
            transformed_length, offset = _base128(data, offset)
            if tag != b"loca" and transformed_length <= 0: raise ContractError(f"{label} has an empty transformed WOFF2 table")
    compressed_end = offset + compressed_size
    if compressed_end > len(data): raise ContractError(f"{label} compressed WOFF2 stream is truncated")
    if meta_offset != 0 or meta_length != 0 or meta_orig != 0: raise ContractError(f"{label} contains forbidden WOFF2 metadata")
    if priv_offset != 0 or priv_length != 0: raise ContractError(f"{label} contains forbidden WOFF2 private data")
    expected_end = (compressed_end + 3) & ~3
    if expected_end > len(data) or any(data[compressed_end:expected_end]): raise ContractError(f"{label} has invalid WOFF2 padding")
    if expected_end != len(data): raise ContractError(f"{label} WOFF2 blocks do not cover the file exactly")

def validate_svg(data: bytes, label: str, asset_id: str) -> None:
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper(): raise ContractError(f"{label} contains a forbidden XML declaration")
    try:
        text = data.decode("utf-8")
        root = ET.fromstring(text)
    except (UnicodeDecodeError, ET.ParseError) as exc: raise ContractError(f"{label} is not strict UTF-8 XML: {exc}") from exc
    if root.tag != SVG_NS + "svg" and root.tag != "svg": raise ContractError(f"{label} root is not SVG")
    text_tags: set[str] = set()
    for element in root.iter():
        local = element.tag.removeprefix(SVG_NS)
        if local not in SVG_TAGS or (element.tag.startswith("{") and not element.tag.startswith(SVG_NS)): raise ContractError(f"{label} contains forbidden SVG element: {local}")
        body = element.text or ""
        if local in SVG_TEXT_TAGS:
            if element is root or element not in list(root) or local in text_tags or element.attrib or list(element) or body != asset_id:
                raise ContractError(f"{label} has non-semantic SVG {local} text")
            text_tags.add(local)
        elif body.strip():
            raise ContractError(f"{label} contains text outside title/desc")
        if (element.tail or "").strip(): raise ContractError(f"{label} contains SVG tail text")
        for raw_name, value in element.attrib.items():
            name = raw_name.removeprefix(SVG_NS)
            lowered = value.lower()
            if name not in SVG_ATTRS or name.lower().startswith("on") or "url(" in lowered or "javascript:" in lowered or "data:" in lowered or "http:" in lowered or "https:" in lowered or value.startswith("//"): raise ContractError(f"{label} contains active, external, or unknown SVG content")

def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        rel = item.relative_to(root).as_posix().encode()
        if item.is_symlink(): raise ContractError(f"package symlinks are forbidden: {item}")
        if item.is_file(): digest.update(b"F\0" + rel + b"\0" + item.read_bytes() + b"\0")
        elif item.is_dir(): digest.update(b"D\0" + rel + b"\0")
    return digest.hexdigest()

def validate_schema(value: Any, schema_name: str, label: str) -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc: raise ContractError("jsonschema is required; install the hashed CI lock") from exc
    schema = load_json(SCHEMAS[schema_name]); Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).iter_errors(value), key=lambda e: list(e.absolute_path))
    if errors: raise ContractError("\n".join(f"{label} {'.'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors))

def safe_package_file(root: Path, relative: str, label: str) -> Path:
    posix = PurePosixPath(relative)
    if posix.is_absolute() or ".." in posix.parts or relative != posix.as_posix(): raise ContractError(f"{label} is not a normalized relative path")
    cursor = root
    for part in posix.parts:
        cursor = cursor / part
        try: mode = cursor.lstat().st_mode
        except OSError as exc: raise ContractError(f"{label} cannot be lstat'ed: {exc}") from exc
        if stat.S_ISLNK(mode): raise ContractError(f"{label} contains a symlink: {relative}")
    resolved = cursor.resolve(strict=True)
    if root != resolved and root not in resolved.parents: raise ContractError(f"{label} escapes package root")
    if not resolved.is_file(): raise ContractError(f"{label} is not a regular file")
    return resolved

def validate_package(package: Path) -> tuple[dict[str, Any], Path, str]:
    manifest = package if package.is_file() else package / "brand-package.json"
    if manifest.is_symlink(): raise ContractError("brand-package.json must not be a symlink")
    try: root = manifest.parent.resolve(strict=True)
    except OSError as exc: raise ContractError(f"package root cannot be resolved: {exc}") from exc
    doc = load_json(manifest); validate_schema(doc, "brand-package.v1.schema.json", "package schema")
    declared = {"brand-package.json"}; ids: set[str] = set(); font_ids: set[str] = set(); evidence_paths: set[str] = set()
    for kind, expected_root, expected_mime in (("fonts", "assets/fonts/", "font/woff2"), ("logos", "assets/logos/", "image/svg+xml")):
        for asset in doc["assets"][kind]:
            aid = asset["id"]
            if aid in ids: raise ContractError(f"duplicate asset id: {aid}")
            ids.add(aid)
            if not asset["path"].startswith(expected_root) or asset["media_type"] != expected_mime: raise ContractError(f"asset class/MIME mismatch: {aid}")
            target = safe_package_file(root, asset["path"], f"asset {aid}"); declared.add(asset["path"])
            data = target.read_bytes()
            if sha_bytes(data) != asset["sha256"]: raise ContractError(f"asset hash mismatch: {aid}")
            if PAYLOAD_NAMES.search(Path(asset["path"]).name) or any(marker.lower() in data.lower() for marker in PAYLOAD_BYTES): raise ContractError(f"asset resembles secret, customer input, or generated output: {aid}")
            if kind == "fonts": validate_woff2(data, f"asset {aid}"); font_ids.add(aid)
            else: validate_svg(data, f"asset {aid}", aid)
            ep = asset["rights"]["evidence_path"]
            if ep == asset["path"] or ep in evidence_paths: raise ContractError(f"rights evidence must be dedicated per asset: {aid}")
            evidence_paths.add(ep); evidence = safe_package_file(root, ep, f"rights evidence {aid}"); declared.add(ep)
            if sha256_file(evidence) != asset["rights"]["evidence_sha256"]: raise ContractError(f"rights evidence hash mismatch: {aid}")
            ev = load_json(evidence); validate_schema(ev, "rights-evidence.v1.schema.json", f"rights evidence {aid}")
            if ev["asset_id"] != aid: raise ContractError(f"rights evidence asset mismatch: {aid}")
    typography = doc["pdf"]["tokens"]["typography_roles"]
    pinned_pdf_fonts = {"builtin:Helvetica", "builtin:Helvetica-Bold", "builtin:Courier"}
    if any(font_id not in font_ids and font_id not in pinned_pdf_fonts for font_id in typography.values()):
        raise ContractError("PDF typography role references neither a declared font nor a pinned built-in PDF font")
    margin = doc["pdf"]["tokens"]["page"]["margin_mm"]
    if not math.isfinite(margin): raise ContractError("PDF theme margin must be finite")
    type_ids: set[str] = set()
    for entry in doc["document_types"]:
        if entry["id"] in type_ids: raise ContractError(f"duplicate document type: {entry['id']}")
        type_ids.add(entry["id"]); path = safe_package_file(root, entry["contract"], f"document contract {entry['id']}"); declared.add(entry["contract"])
        if sha256_file(path) != entry["contract_sha256"]: raise ContractError(f"document contract hash mismatch: {entry['id']}")
        contract = load_json(path); validate_schema(contract, "document-type.v1.schema.json", f"document contract {entry['id']}")
        validate_document_type(contract, load_base_types())
        if contract["id"] != entry["id"] or contract["engine_compatibility"] != entry["engine_compatibility"]: raise ContractError(f"document contract identity/compatibility mismatch: {entry['id']}")
        if not math.isfinite(contract["minimum_margin_mm"]) or margin < contract["minimum_margin_mm"]: raise ContractError(f"PDF theme margin is below document minimum: {entry['id']}")
        safety_floor = {"embedded-credentials", "external-resources", "active-content"}
        if not safety_floor.issubset(contract["forbidden_features"]): raise ContractError(f"document contract lacks the safety floor: {entry['id']}")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()}
    if actual != declared: raise ContractError(f"package file set differs from manifest: extra={sorted(actual-declared)} missing={sorted(declared-actual)}")
    return doc, root, tree_hash(root)

def git_root(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if not (resolved / ".git").exists(): raise ContractError(f"project must be the exact Git root: {resolved}")
    return resolved

def safe_dir(root: Path, relative: str) -> Path:
    posix = PurePosixPath(relative)
    if posix.is_absolute() or ".." in posix.parts: raise ContractError("unsafe project-relative directory")
    cursor = root
    for part in posix.parts:
        cursor = cursor / part
        try:
            mode = cursor.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode): raise ContractError(f"unsafe write-path component: {cursor}")
        except FileNotFoundError:
            try: os.mkdir(cursor, 0o700)
            except FileExistsError: pass
            mode = cursor.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode): raise ContractError(f"unsafe newly-created component: {cursor}")
        real = cursor.resolve(strict=True)
        if root != real and root not in real.parents: raise ContractError(f"write path escapes Git root: {cursor}")
    return cursor

def atomic_bytes(root: Path, relative: str, data: bytes) -> None:
    rel = PurePosixPath(relative); parent = safe_dir(root, rel.parent.as_posix())
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    dfd = os.open(parent, flags); tmp = f".{rel.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb", closefd=False) as handle: handle.write(data); handle.flush(); os.fsync(handle.fileno())
        finally: os.close(fd)
        os.replace(tmp, rel.name, src_dir_fd=dfd, dst_dir_fd=dfd); os.fsync(dfd)
    finally:
        try: os.unlink(tmp, dir_fd=dfd)
        except FileNotFoundError: pass
        os.close(dfd)

def atomic_symlink(root: Path, relative: str, target: str) -> None:
    rel = PurePosixPath(relative); parent = safe_dir(root, rel.parent.as_posix())
    dfd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    tmp = f".{rel.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}"
    try:
        os.symlink(target, tmp, dir_fd=dfd); os.replace(tmp, rel.name, src_dir_fd=dfd, dst_dir_fd=dfd); os.fsync(dfd)
    finally:
        try: os.unlink(tmp, dir_fd=dfd)
        except FileNotFoundError: pass
        os.close(dfd)

def compatible(requirement: str, engine: str) -> bool:
    if not SEMVER.fullmatch(engine): raise ContractError("engine version must be exact SemVer")
    prefix = requirement[:1] if requirement[:1] in "^~" else ""; req = tuple(map(int, requirement.lstrip("^~").split("."))); got = tuple(map(int, engine.split(".")))
    if not prefix: return got == req
    if prefix == "~": return got >= req and got[:2] == req[:2]
    return got >= req and (got[0] == req[0] if req[0] else got[:2] == req[:2] if req[1] else got == req)

def state_identity(activation_bytes: bytes, lock_bytes: bytes) -> str:
    return sha_bytes(b"brandkit-state.v1\0" + activation_bytes + b"\0" + lock_bytes)

def current_state_target(root: Path) -> str:
    for name in ("project.json", "skill-lock.json"):
        alias = root / ".brandkit" / name
        if not alias.is_symlink() or os.readlink(alias) != f"current/{name}": raise ContractError(f"{name} must be the closed current-state alias")
    current = root / ".brandkit/current"
    if not current.is_symlink(): raise ContractError("current project state must be an atomic symlink")
    target = os.readlink(current)
    match = STATE_TARGET.fullmatch(target)
    if not match: raise ContractError("current project state target is not content-addressed")
    state_root = root / ".brandkit" / target
    if state_root.is_symlink() or not state_root.is_dir(): raise ContractError("current project state directory is invalid")
    activation_path = safe_package_file(root, f".brandkit/{target}/project.json", "project activation state")
    lock_path = safe_package_file(root, f".brandkit/{target}/skill-lock.json", "skill lock state")
    if state_identity(activation_path.read_bytes(), lock_path.read_bytes()) != match.group(1): raise ContractError("current project state bytes differ from locator")
    return target

def read_state(root: Path, target: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    if not STATE_TARGET.fullmatch(target): raise ContractError("state target is not closed")
    project_path = safe_package_file(root, f".brandkit/{target}/project.json", "project activation state")
    lock_path = safe_package_file(root, f".brandkit/{target}/skill-lock.json", "skill lock state")
    # Import lazily: markdown_contract deliberately imports this module only at
    # its publication boundary.  Both activation files use the same descriptor
    # reader as every other production envelope.
    import markdown_contract as _contract
    project_bytes = _contract.stable_read(project_path, root, "project activation state")
    lock_bytes = _contract.stable_read(lock_path, root, "skill lock state")
    if state_identity(project_bytes, lock_bytes) != target.split("/", 1)[1]: raise ContractError("state bytes differ from content-addressed locator")
    try:
        project = json.loads(project_bytes.decode("utf-8")); lock = json.loads(lock_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"state is not strict UTF-8 JSON: {exc}") from exc
    return project, lock, project_bytes, lock_bytes

def read_closed_activation(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    activation, lock, _, _ = read_state(root, current_state_target(root))
    if not isinstance(activation, dict) or set(activation) != {"schema_version", "brand", "package_path", "overrides"}: raise ContractError("activation is not closed")
    pin = activation.get("brand")
    if activation.get("schema_version") != "brand-project.v1" or not isinstance(pin, dict) or set(pin) != {"id", "version", "content_sha256"}: raise ContractError("activation pin is invalid")
    if not BRAND_ID.fullmatch(str(pin["id"])) or not SEMVER.fullmatch(str(pin["version"])) or not HEX64.fullmatch(str(pin["content_sha256"])): raise ContractError("activation identity/version/hash invalid")
    overrides = activation["overrides"]
    if not isinstance(overrides, dict) or set(overrides)-ALLOWED_OVERRIDES: raise ContractError("activation overrides are not closed")
    if any(k in overrides and not isinstance(overrides[k], bool) for k in ("toc", "section_numbering")) or ("document_type" in overrides and not isinstance(overrides["document_type"], str)): raise ContractError("override type invalid")
    validate_lock_object(root, lock)
    return activation, lock

def validate_lock_object(root: Path, lock: dict[str, Any]) -> None:
    if not isinstance(lock, dict) or set(lock) != {"schema_version", "engine", "skill"} or lock.get("schema_version") != "brandkit-skill-lock.v1": raise ContractError("skill lock is not closed")
    if set(lock["engine"]) != {"name", "version"} or lock["engine"]["name"] != "brandkit-engine" or not SEMVER.fullmatch(str(lock["engine"]["version"])): raise ContractError("engine lock invalid")
    skill = lock["skill"]
    if set(skill) != {"name", "version", "source_locator", "content_sha256", "release"} or skill["name"] != "brand-package-cli" or not HEX64.fullmatch(str(skill["content_sha256"])): raise ContractError("skill lock invalid")
    locator = SNAPSHOT_LOCATOR.fullmatch(str(skill["source_locator"]))
    if not locator or locator.group(1) != "scripts/brand_package.py" or locator.group(2) != skill["content_sha256"]: raise ContractError("skill source snapshot locator is invalid")
    if skill["release"] != f".brandkit/engine-releases/brand-package-cli/{skill['content_sha256']}": raise ContractError("skill release is not content-addressed")
    skill_file = safe_package_file(root, skill["release"] + "/brand_package.py", "skill release")
    if sha256_file(skill_file) != skill["content_sha256"]: raise ContractError("skill release content differs from lock")

def validate_bound_state(root: Path, activation: dict[str, Any], lock: dict[str, Any]) -> tuple[dict[str, Any], Path, str]:
    if not isinstance(activation, dict) or set(activation) != {"schema_version", "brand", "package_path", "overrides"} or activation.get("schema_version") != "brand-project.v1": raise ContractError("bound activation is not closed")
    if not isinstance(activation.get("brand"), dict) or set(activation["brand"]) != {"id", "version", "content_sha256"}: raise ContractError("bound activation pin is invalid")
    if not isinstance(activation.get("overrides"), dict) or set(activation["overrides"])-ALLOWED_OVERRIDES: raise ContractError("bound activation overrides are invalid")
    validate_lock_object(root, lock)
    pin = activation["brand"]
    expected = f".brandkit/releases/{pin['id']}/{pin['version']}/{pin['content_sha256']}"
    if activation.get("package_path") != expected: raise ContractError("package release is not the exact content-addressed locator")
    package = safe_package_file(root, expected + "/brand-package.json", "package release").parent
    doc, package_root, content = validate_package(package); pin = activation["brand"]
    if (doc["brand_id"], doc["version"], content) != (pin["id"], pin["version"], pin["content_sha256"]): raise ContractError("materialized package differs from activation pin")
    return doc, package_root, content

def resolve(project: Path, explicit: Path|None, brand_id: str|None, engine: str, run_id: str) -> dict[str, Any]:
    root = git_root(project)
    if not RUN_ID.fullmatch(run_id): raise ContractError("run ID must be a closed lowercase identifier")
    activation, lock = read_closed_activation(root); pin = activation["brand"]
    validate_bound_state(root, activation, lock)
    if engine != lock["engine"]["version"]: raise ContractError("caller engine differs from exact skill lock")
    if brand_id is not None and brand_id != pin["id"]: raise ContractError("explicit brand differs from pin")
    if explicit is not None: candidate, source = explicit, "explicit-cli-path"
    else: candidate, source = root / activation["package_path"], "project-activation"
    doc, package_root, content = validate_package(candidate)
    if (doc["brand_id"], doc["version"], content) != (pin["id"], pin["version"], pin["content_sha256"]): raise ContractError("resolved package differs from exact pin")
    overrides = activation["overrides"]; selected = [d for d in doc["document_types"] if "document_type" not in overrides or d["id"] == overrides["document_type"]]
    if not selected: raise ContractError("selected document type is absent")
    for entry in selected:
        if not compatible(entry["engine_compatibility"], engine): raise ContractError(f"engine incompatible with {entry['id']}")
        contract = load_json(package_root / entry["contract"])
        for key in ("toc", "section_numbering"):
            if key in overrides and overrides[key] not in contract["allowed_overrides"].get(key, []): raise ContractError(f"override {key} would loosen the pinned contract")
    project_hash = sha_bytes(str(root).encode())[:24]; base = PurePosixPath(project_hash, pin["id"], pin["version"], content)
    cache = PurePosixPath(".brandkit/cache", base, engine)
    runbase = PurePosixPath(base, engine, run_id)
    roots = {"work": str(root / ".brandkit/work" / runbase), "cache": str(root / cache), "temp": str(root / ".brandkit/tmp" / runbase), "output": str(root / "output/.brandkit" / runbase)}
    return {"source": source, "project_root": str(root), "package_root": str(package_root), "engine_version": engine, "run_id": run_id, "brand": pin, "overrides": overrides, "roots": roots}

def copy_release(project_root: Path, source: Path, relative: str, expected: str) -> Path:
    parent = safe_dir(project_root, str(PurePosixPath(relative).parent)); target = parent / PurePosixPath(relative).name
    if target.exists():
        if target.is_symlink() or tree_hash(target) != expected: raise ContractError("existing immutable release differs")
        return target
    staged = parent / f".{target.name}.tmp-{os.getpid()}"
    shutil.copytree(source, staged, symlinks=True)
    if tree_hash(staged) != expected: shutil.rmtree(staged); raise ContractError("staged release hash differs")
    os.rename(staged, target)
    for path in target.rglob("*"):
        if path.is_file(): path.chmod(0o400)
    return target

def make_lock(root: Path, engine: str) -> dict[str, Any]:
    script_hash = sha256_file(Path(__file__))
    rel = f".brandkit/engine-releases/brand-package-cli/{script_hash}"
    dest = safe_dir(root, rel)
    target = dest / "brand_package.py"
    if target.exists() and sha256_file(target) != script_hash: raise ContractError("engine release hash collision")
    if not target.exists(): atomic_bytes(root, f"{rel}/brand_package.py", Path(__file__).read_bytes()); target.chmod(0o400)
    locator = f"snapshot:scripts/brand_package.py@sha256:{script_hash}"
    return {"schema_version": "brandkit-skill-lock.v1", "engine": {"name": "brandkit-engine", "version": engine}, "skill": {"name": "brand-package-cli", "version": "1.0.0", "source_locator": locator, "content_sha256": script_hash, "release": rel}}

def write_state(root: Path, activation: dict[str, Any], lock: dict[str, Any]) -> tuple[str, bytes, bytes]:
    project_bytes, lock_bytes = canonical(activation), canonical(lock)
    state_hash = state_identity(project_bytes, lock_bytes); target = f"states/{state_hash}"
    state_root = safe_dir(root, f".brandkit/{target}")
    for name, data in (("project.json", project_bytes), ("skill-lock.json", lock_bytes)):
        path = state_root / name
        if path.exists():
            if path.is_symlink() or path.read_bytes() != data: raise ContractError("state hash collision")
        else: atomic_bytes(root, f".brandkit/{target}/{name}", data); path.chmod(0o400)
    return target, project_bytes, lock_bytes

def ensure_state_aliases(root: Path) -> None:
    safe_dir(root, ".brandkit")
    for name in ("project.json", "skill-lock.json"):
        alias = root / ".brandkit" / name; expected = f"current/{name}"
        if os.path.lexists(alias):
            if not alias.is_symlink() or os.readlink(alias) != expected: raise ContractError(f"existing {name} is not the closed state alias")
        else: atomic_symlink(root, f".brandkit/{name}", expected)

def journal_binding(journal: dict[str, Any]) -> str:
    body = {key: journal[key] for key in ("schema_version", "project_identity", "previous_state", "current_state")}
    return sha_bytes(canonical(body))

def pin_project(project: Path, package: Path, engine: str) -> dict[str, Any]:
    if not SEMVER.fullmatch(engine): raise ContractError("pin requires an exact engine SemVer")
    root = git_root(project); doc, source, content = validate_package(package)
    rel = f".brandkit/releases/{doc['brand_id']}/{doc['version']}/{content}"; copy_release(root, source, rel, content)
    activation = {"schema_version": "brand-project.v1", "brand": {"id": doc["brand_id"], "version": doc["version"], "content_sha256": content}, "package_path": rel, "overrides": {}}
    lock = make_lock(root, engine); new_target, new_project_bytes, new_lock_bytes = write_state(root, activation, lock)
    current = root / ".brandkit/current"
    if os.path.lexists(current):
        old_target = current_state_target(root); old_activation, old_lock, old_project_bytes, old_lock_bytes = read_state(root, old_target); validate_bound_state(root, old_activation, old_lock)
        identity = {"realpath": str(root), "sha256": sha_bytes(str(root).encode())}
        journal = {"schema_version": "brandkit-journal.v2", "project_identity": identity, "previous_state": {"locator": old_target, "project_sha256": sha_bytes(old_project_bytes), "lock_sha256": sha_bytes(old_lock_bytes)}, "current_state": {"locator": new_target, "project_sha256": sha_bytes(new_project_bytes), "lock_sha256": sha_bytes(new_lock_bytes)}}
        journal["binding_sha256"] = journal_binding(journal); atomic_bytes(root, ".brandkit/project.journal.json", canonical(journal))
    ensure_state_aliases(root); atomic_symlink(root, ".brandkit/current", new_target)
    return activation

def rollback(project: Path) -> dict[str, Any]:
    root = git_root(project); current_target = current_state_target(root); current, current_lock, current_bytes, current_lock_bytes = read_state(root, current_target); validate_bound_state(root, current, current_lock)
    journal = load_json(root / ".brandkit/project.journal.json")
    if not isinstance(journal, dict) or set(journal) != {"schema_version", "project_identity", "previous_state", "current_state", "binding_sha256"} or journal["schema_version"] != "brandkit-journal.v2": raise ContractError("rollback journal is not closed")
    expected_identity = {"realpath": str(root), "sha256": sha_bytes(str(root).encode())}
    if journal["project_identity"] != expected_identity or journal["binding_sha256"] != journal_binding(journal): raise ContractError("rollback journal is not authenticated for this project")
    current_record = journal["current_state"]
    if current_target != current_record.get("locator") or sha_bytes(current_bytes) != current_record.get("project_sha256") or sha_bytes(current_lock_bytes) != current_record.get("lock_sha256"): raise ContractError("current pin/lock differs from journal")
    previous_record = journal["previous_state"]; previous_target = previous_record.get("locator")
    previous, plock, previous_bytes, previous_lock_bytes = read_state(root, previous_target)
    if sha_bytes(previous_bytes) != previous_record.get("project_sha256") or sha_bytes(previous_lock_bytes) != previous_record.get("lock_sha256"): raise ContractError("previous state differs from journal")
    validate_bound_state(root, previous, plock)
    atomic_symlink(root, ".brandkit/current", previous_target)
    return previous

def materialize(index_path: Path, brand: str, version: str, destination: Path) -> dict[str, Any]:
    try: index_root = index_path.parent.resolve(strict=True)
    except OSError as exc: raise ContractError(f"registry index parent cannot be resolved: {exc}") from exc
    index_file = safe_package_file(index_root, index_path.name, "registry index")
    index = load_json(index_file); validate_schema(index, "brand-registry-index.v1.schema.json", "registry index")
    matches = [e for e in index["entries"] if e["brand_id"] == brand and e["version"] == version]
    if len(matches) != 1: raise ContractError("registry index lookup is not unique")
    entry = matches[0]; source = safe_package_file(index_root, entry["immutable_release_locator"] + "/brand-package.json", "immutable registry release").parent
    _, _, content = validate_package(source)
    if content != entry["content_sha256"]: raise ContractError("registry index content pin differs")
    if destination.exists(): raise ContractError("materialization destination must be absent")
    shutil.copytree(source, destination); _, _, copied = validate_package(destination)
    if copied != content: raise ContractError("materialized bytes differ")
    return entry

def migrate(project: Path, legacy: Path, package: Path, engine: str) -> dict[str, Any]:
    old = load_json(legacy); doc, _, _ = validate_package(package)
    if not isinstance(old, dict) or set(old) != {"brand_id", "version"} or (old["brand_id"], old["version"]) != (doc["brand_id"], doc["version"]): raise ContractError("legacy identity does not match complete package")
    return pin_project(project, package, engine)

def main(argv: list[str]|None=None) -> int:
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest="command", required=True)
    v=sub.add_parser("validate"); v.add_argument("package", type=Path)
    p=sub.add_parser("pin"); p.add_argument("--project", required=True, type=Path); p.add_argument("--package", required=True, type=Path); p.add_argument("--engine-version", required=True)
    r=sub.add_parser("resolve"); r.add_argument("--project", required=True, type=Path); r.add_argument("--package", type=Path); r.add_argument("--brand-id"); r.add_argument("--engine-version", required=True); r.add_argument("--run-id", required=True); r.add_argument("--create-roots", action="store_true")
    b=sub.add_parser("rollback"); b.add_argument("--project", required=True, type=Path)
    m=sub.add_parser("migrate"); m.add_argument("--project", required=True, type=Path); m.add_argument("--legacy", required=True, type=Path); m.add_argument("--package", required=True, type=Path); m.add_argument("--engine-version", required=True)
    x=sub.add_parser("materialize"); x.add_argument("--index", required=True, type=Path); x.add_argument("--brand-id", required=True); x.add_argument("--version", required=True); x.add_argument("--destination", required=True, type=Path)
    args=parser.parse_args(argv)
    try:
        if args.command=="validate": doc, root, content=validate_package(args.package); result={"brand_id":doc["brand_id"],"version":doc["version"],"content_sha256":content,"package_root":str(root)}
        elif args.command=="pin": result=pin_project(args.project,args.package,args.engine_version)
        elif args.command=="rollback": result=rollback(args.project)
        elif args.command=="migrate": result=migrate(args.project,args.legacy,args.package,args.engine_version)
        elif args.command=="materialize": result=materialize(args.index,args.brand_id,args.version,args.destination)
        else:
            result=resolve(args.project,args.package,args.brand_id,args.engine_version,args.run_id)
            if args.create_roots:
                root=Path(result["project_root"])
                for path in result["roots"].values(): safe_dir(root, Path(path).relative_to(root).as_posix())
    except (ContractError, OSError) as exc: print(f"brand-package: ERROR: {exc}",file=sys.stderr); return 1
    print(json.dumps(result,indent=2,sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
