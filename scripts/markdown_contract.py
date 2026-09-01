#!/usr/bin/env python3
"""BrandKit Markdown v1 parser, semantic validator, and publication gate.

This module deliberately contains no rendering code. ``parse_markdown`` builds a
closed semantic AST while preserving the exact source for byte-identical round trips.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import logging
import math
import os
import re
import shlex
import stat
import sys
import unicodedata
import warnings
import xml.etree.ElementTree as ET
import zlib
from html import unescape
from urllib.parse import unquote
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from PIL import Image
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_CONTRACT_VERSION = "markdown-contract.v2"
MARKDOWN_CONTRACT_PATH = ROOT / "markdown-contract.v2.json"
MARKDOWN_CONTRACT_SCHEMA = "markdown-contract.v2.schema.json"
SCHEMA_DIR = ROOT / "schemas"
BASE_TYPE_DIR = ROOT / "document-types"
ID = r"[a-z][a-z0-9-]{1,63}"
PROTECTED_RE = re.compile(r"\{\{(?P<kind>fact|id|number|url|source|todo|inline-protected):(?P<id>" + ID + r")\|(?P<literal>[^{}\r\n]+)\}\}")
HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s+\{#(" + ID + r")\}\s*$")
LIST_RE = re.compile(r"^( *)(?P<marker>[-+*]|[1-9][0-9]*\.)\s+(?P<text>\S.*)$")
IMAGE_RE = re.compile(r'^!\[(?P<alt>[^\]\r\n]+)\]\(asset:(?P<asset>' + ID + r')\s+"(?P<description>[^"\r\n]+)"\)\{#(?P<id>' + ID + r')\s+rights=(?P<rights>' + ID + r')\}$')
FOOTNOTE_RE = re.compile(r"^\[\^(" + ID + r")\]:\s+(\S.*)$")
LINK_RE = re.compile(r"\[(?P<label>[^\]\r\n]+)\]\((?P<target>[^()\s]+)\)")
FIGURE_REF_RE = re.compile(r"(?<![\w@-])@(?P<id>fig-[a-z0-9][a-z0-9-]{0,59})(?![\w-])")
FOOTNOTE_REF_RE = re.compile(r"\[\^(" + ID + r")\]")
RAW_URL_RE = re.compile(r"(?<![\w(])(?:https?://|www\.)\S+", re.I)
BARE_NUMBER_RE = re.compile(r"(?<![\w{])[-+]?(?:\d{1,3}(?:[. ,]\d{3})+|\d+)(?:[.,]\d+)?(?:[eE][-+]?\d+)?(?:\s*%)?(?![\w}])")
OPEN_WORK_RE = re.compile(r"(?i)(?<![\w-])(?:TODO|FIXME|TBC|TBD|XXX)(?![\w-])|lorem\s+ipsum|(?:<|\[|\{\{)\s*placeholder\b")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SAFETY_FLOOR = {"active-content", "embedded-credentials", "external-resources", "raw-html", "embedded-css", "unknown-directives", "unknown-code-fences"}
PUBLICATION_GATES = {"contract-valid", "no-open-todos", "assets-resolved", "rights-valid", "protected-hashes-stable", "render-success"}
BRAND_MANIFEST = ROOT / "fixtures/brand-registry/releases/neutral-fixture/1.0.0/be2dd3d29bf15ba437eb5dc070ac6e6735cf57630e5f583a85777e6ffedf6360/brand-package.json"
PRODUCTION_RENDERER_TRUST = ROOT / "renderer-trust.v1.json"
class ContractError(Exception):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: UniqueLoader, node: yaml.Node, deep: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ContractError(f"frontmatter has a duplicate or non-string key: {key!r}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite JSON number {value}")))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractError(f"cannot read JSON {path}: {exc}") from exc


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _finite_numbers(value: Any, path: str = "<root>") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError(f"{path}: numeric values must be finite")
    if isinstance(value, dict):
        for key, child in value.items(): _finite_numbers(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value): _finite_numbers(child, f"{path}[{index}]")


def _reject_open_work(value: Any, path: str = "<root>") -> None:
    if isinstance(value, str) and OPEN_WORK_RE.search(value):
        raise ContractError(f"publication placeholder/open-work marker at {path}")
    if isinstance(value, dict):
        for key, child in value.items(): _reject_open_work(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value): _reject_open_work(child, f"{path}[{index}]")


def _key_bytes(key_path: Path) -> bytes:
    absolute = key_path.absolute()
    resolved = key_path.resolve(strict=True)
    if resolved == ROOT or ROOT in resolved.parents:
        raise ContractError("issuer key must be a regular file outside the project")
    key = stable_read(absolute, absolute.parent, "issuer key")
    if len(key) < 32: raise ContractError("issuer key must contain at least 32 bytes")
    return key


def _sign(payload: dict[str, Any], key: bytes) -> str:
    return hmac.new(key, canonical_json(payload), hashlib.sha256).hexdigest()


def _verify_envelope(envelope: dict[str, Any], schema: str, key_path: Path) -> dict[str, Any]:
    if not isinstance(envelope, dict) or set(envelope) != {"schema_version", "issuer", "payload", "hmac_sha256"}:
        raise ContractError(f"{schema} envelope is not closed")
    if envelope["schema_version"] != schema or not isinstance(envelope["issuer"], str) or not envelope["issuer"].strip():
        raise ContractError(f"invalid {schema} envelope identity")
    if not HASH_RE.fullmatch(str(envelope["hmac_sha256"])):
        raise ContractError(f"invalid {schema} HMAC")
    signed = {"schema_version": schema, "issuer": envelope["issuer"], "payload": envelope["payload"]}
    if not hmac.compare_digest(_sign(signed, _key_bytes(key_path)), envelope["hmac_sha256"]):
        raise ContractError(f"{schema} issuer/HMAC binding failed")
    return envelope["payload"]


def validate_schema(value: Any, schema_name: str, label: str) -> None:
    _finite_numbers(value, label)
    schema = load_json(SCHEMA_DIR / schema_name)
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).iter_errors(value), key=lambda e: list(e.absolute_path))
    if errors:
        raise ContractError("\n".join(f"{label} {'.'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors))


def _byte_offset(source: str, char_offset: int) -> int:
    return len(source[:char_offset].encode("utf-8"))


def _decoded_nfkc(value: str) -> str:
    current = value
    for _ in range(8):
        decoded = unicodedata.normalize("NFKC", unquote(unescape(current)))
        if decoded == current: return decoded
        current = decoded
    raise ContractError("text exceeds the iterative entity/percent decoding limit")


def validate_mermaid_text(value: str, label: str, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value or (not multiline and ("\n" in value or "\r" in value)):
        raise ContractError(f"Mermaid {label} must be non-empty single-line text")
    normalized = _decoded_nfkc(value).replace("\r\n", "\n")
    forbidden = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://|\b(?:data|javascript|file|ftp|mailto|tel):|<|&(?:#\d+|#x[0-9a-f]+|[a-z]+);|%%\s*\{|\b(?:init|config|click|href|link|callback|image|icon|classDef|style)\b)")
    if forbidden.search(normalized):
        raise ContractError(f"Mermaid {label} forbids URLs, schemes, HTML, images/icons, config/init, styling, and interaction")
    if not multiline and not re.fullmatch(r"[\wÀ-ž .,!?'()/-]{1,160}", normalized):
        raise ContractError(f"Mermaid {label} is outside the closed metadata character grammar")
    return normalized


def validate_mermaid_v1(code: str) -> None:
    normalized = validate_mermaid_text(code, "code", multiline=True)
    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    if not lines or not re.fullmatch(r"flowchart (?:TD|TB|LR|RL|BT)", lines[0]):
        raise ContractError("Mermaid v1 requires exactly a flowchart direction declaration")
    node = r"[A-Za-z][A-Za-z0-9_-]*(?:\[[A-Za-z0-9À-ž _.,;!?()'\-/]+\])?"
    for line in lines[1:]:
        if not (re.fullmatch(node, line) or re.fullmatch(node + r"\s*-->\s*" + node, line)):
            raise ContractError(f"Mermaid v1 statement is outside the conservative subset: {line}")
    if len(lines) < 2: raise ContractError("Mermaid v1 requires at least one node or edge")


def validate_graphviz_text(value: str, label: str, *, multiline: bool = False) -> str:
    """Accept only inert Graphviz text before the closed DOT grammar runs."""
    if not isinstance(value, str) or not value or (not multiline and ("\n" in value or "\r" in value)):
        raise ContractError(f"Graphviz {label} must be non-empty single-line text")
    normalized = _decoded_nfkc(value).replace("\r\n", "\n")
    forbidden = re.compile(
        r"(?i)(?:[a-z][a-z0-9+.-]*://|\b(?:data|javascript|file|ftp|mailto|tel):|"
        r"<|(?<!-)>|&(?:#\d+|#x[0-9a-f]+|[a-z]+);|/\*|\*/|//|#|@|`|\\|"
        r"\b(?:href|url|image|shapefile|fontpath|stylesheet|target|tooltip|comment|"
        r"labelurl|edgeurl|headurl|tailurl|edgetarget|headtarget|tailtarget|"
        r"edgetooltip|headtooltip|tailtooltip|subgraph|include|system|shell)\b)"
    )
    if forbidden.search(normalized):
        raise ContractError("Graphviz forbids schemes, URLs, HTML/entities, comments, includes, external resources, interaction, and shell forms")
    if not multiline and not re.fullmatch(r"[\wÀ-ž .,!?'()/-]{1,160}", normalized):
        raise ContractError(f"Graphviz {label} is outside the closed metadata character grammar")
    return normalized


def validate_graphviz_v1(code: str) -> None:
    """Validate the complete closed Graphviz-v1 directed-graph subset."""
    normalized = validate_graphviz_text(code, "code", multiline=True)
    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    if len(lines) < 3 or not re.fullmatch(r"digraph(?: [A-Za-z][A-Za-z0-9_-]*)? \{", lines[0]) or lines[-1] != "}":
        raise ContractError("Graphviz v1 requires one closed digraph")
    ident = r"[A-Za-z][A-Za-z0-9_-]*"
    label = r'''[A-Za-z0-9À-ž _.,;!?()'/-]{1,160}'''
    node = ident + r' \[label="' + label + r'"\];'
    edge = ident + r" -> " + ident + r'(?: \[label="' + label + r'"\])?;'
    rank = r"rankdir=(?:TB|BT|LR|RL);"
    for line in lines[1:-1]:
        if not (re.fullmatch(node, line) or re.fullmatch(edge, line) or re.fullmatch(rank, line)):
            raise ContractError(f"Graphviz v1 statement is outside the closed subset: {line}")
    if not any(re.fullmatch(node, line) or re.fullmatch(edge, line) for line in lines[1:-1]):
        raise ContractError("Graphviz v1 requires at least one node or edge")


def _decode_png(data: bytes) -> None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"): raise ContractError("PNG signature mismatch")
    offset = 8; seen: list[bytes] = []
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big"); kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data): raise ContractError("truncated PNG chunk")
        payload = data[offset + 8:offset + 8 + length]; expected = int.from_bytes(data[offset + 8 + length:end], "big")
        if zlib.crc32(kind + payload) & 0xffffffff != expected: raise ContractError("PNG chunk CRC mismatch")
        seen.append(kind)
        if kind == b"IHDR":
            if len(seen) != 1 or length != 13 or int.from_bytes(payload[:4], "big") < 1 or int.from_bytes(payload[4:8], "big") < 1:
                raise ContractError("invalid PNG IHDR")
        if kind == b"IEND":
            if length or end != len(data): raise ContractError("PNG has trailing/polyglot bytes")
            break
        offset = end
    if not seen or seen[0] != b"IHDR" or seen[-1] != b"IEND" or b"IDAT" not in seen:
        raise ContractError("PNG is structurally incomplete")


def _decode_jpeg(data: bytes) -> None:
    if len(data) < 4 or data[:2] != b"\xff\xd8": raise ContractError("JPEG signature mismatch")
    offset = 2; saw_frame = False; saw_scan = False
    while offset < len(data):
        if data[offset] != 0xff: raise ContractError("invalid JPEG marker boundary")
        while offset < len(data) and data[offset] == 0xff: offset += 1
        if offset >= len(data): raise ContractError("truncated JPEG marker")
        marker = data[offset]; offset += 1
        if marker == 0xd9:
            if offset != len(data): raise ContractError("JPEG has trailing/polyglot bytes")
            if not saw_frame or not saw_scan: raise ContractError("JPEG lacks frame or scan")
            return
        if marker in {0x01, *range(0xd0, 0xd8)}: continue
        if offset + 2 > len(data): raise ContractError("truncated JPEG segment")
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2 or offset + length > len(data): raise ContractError("invalid JPEG segment length")
        payload_start = offset + 2; payload_end = offset + length
        if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}:
            if length < 8 or not int.from_bytes(data[payload_start + 1:payload_start + 3], "big") or not int.from_bytes(data[payload_start + 3:payload_start + 5], "big"):
                raise ContractError("invalid JPEG frame dimensions")
            saw_frame = True
        offset = payload_end
        if marker == 0xda:
            saw_scan = True
            while offset + 1 < len(data):
                if data[offset] != 0xff: offset += 1; continue
                nxt = data[offset + 1]
                if nxt == 0x00 or 0xd0 <= nxt <= 0xd7: offset += 2; continue
                break
    raise ContractError("JPEG is missing EOI")


def _decode_svg(data: bytes) -> None:
    if (not data or data.startswith(b"\xef\xbb\xbf") or data[:1].isspace() or data[-1:].isspace()
            or data.startswith(b"<?xml") or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper()
            or b"<!--" in data or b"<?" in data or b"?>" in data):
        raise ContractError("SVG declarations/entities are forbidden")
    try: text = data.decode("utf-8")
    except UnicodeDecodeError as exc: raise ContractError("SVG must be canonical UTF-8") from exc
    if text.count("<svg") != 1 or text.count("</svg>") != 1 or not text.startswith("<svg") or not text.endswith("</svg>"):
        raise ContractError("SVG must contain exactly one canonical root with no leading/trailing bytes")
    try: root = ET.fromstring(data)
    except ET.ParseError as exc: raise ContractError(f"invalid SVG XML: {exc}") from exc
    def local(name: str) -> str: return name.rsplit("}", 1)[-1]
    if local(root.tag) != "svg": raise ContractError("SVG root element is required")
    allowed = {"svg", "title", "desc", "g", "rect", "circle", "ellipse", "line", "polyline", "polygon", "path", "text"}
    allowed_attrs = {"viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "points", "d", "fill", "stroke", "stroke-width", "opacity", "transform", "role", "aria-label", "font-size", "text-anchor"}
    external = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*:|//)")
    for element in root.iter():
        tag = local(element.tag)
        if tag not in allowed: raise ContractError(f"unsafe SVG element: {tag}")
        body = element.text or ""; tail = element.tail or ""
        if tail or (tag not in {"title", "desc", "text"} and body) or (tag in {"title", "desc", "text"} and (not body or external.search(_decoded_nfkc(body)))):
            raise ContractError("unsafe or non-canonical SVG text content")
        for name, value in element.attrib.items():
            lname = local(name)
            if lname not in allowed_attrs or any(ord(char) < 32 for char in value) or external.search(_decoded_nfkc(value)) or "url(" in value.lower():
                raise ContractError(f"unsafe/external SVG attribute: {lname}")
    serialized = ET.tostring(root, encoding="utf-8")
    if not serialized: raise ContractError("empty SVG")


def validate_image_bytes(path: Path, media_type: str, data: bytes) -> None:
    expected_ext = {"image/png": ".png", "image/jpeg": {".jpg", ".jpeg"}, "image/svg+xml": ".svg"}[media_type]
    suffix = path.suffix.lower()
    if suffix not in expected_ext if isinstance(expected_ext, set) else suffix != expected_ext:
        raise ContractError(f"asset extension {suffix} disagrees with {media_type}")
    if media_type == "image/png": _decode_png(data)
    elif media_type == "image/jpeg": _decode_jpeg(data)
    else: _decode_svg(data); return
    expected_format = "PNG" if media_type == "image/png" else "JPEG"
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != expected_format or image.width < 1 or image.height < 1: raise ContractError(f"{expected_format} decoder/type mismatch")
            image.verify()
        with Image.open(io.BytesIO(data)) as image: image.load()
    except (OSError, ValueError) as exc: raise ContractError(f"strict {expected_format} decode failed: {exc}") from exc


class Parser:
    def __init__(self, source: str, asset_root: Path | None = None):
        if source.startswith("\ufeff") or "\r" in source:
            raise ContractError("input must be BOM-free UTF-8 with LF line endings")
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.offsets: list[int] = []
        offset = 0
        for line in self.lines:
            self.offsets.append(offset); offset += len(line)
        self.asset_root = asset_root
        self._mutable_candidates: list[dict[str, Any]] = []
        self._protected_candidates: list[dict[str, Any]] = []
        self.protected: list[dict[str, Any]] = []
        self.mutable: list[dict[str, Any]] = []
        self.local_link_targets: list[str] = []
        self.figure_refs: list[dict[str, Any]] = []
        self.footnote_refs: set[str] = set()
        self.footnote_defs: set[str] = set()
        self.claimed_ids: dict[str, str] = {}
        self.meta, self.body_start = self._frontmatter()
        self.facts = self._index(self.meta["facts"], "fact")
        self.sources = self._index(self.meta["sources"], "source")
        self.assets = self._index(self.meta["assets"], "asset")
        self.rights = self._index(self.meta["rights"], "rights")
        self.rights_evidence: dict[str, dict[str, Any]] = {}
        self.todos = self._index(self.meta["todos"], "TODO")
        for fact in self.facts.values():
            unknown = set(fact["source_ids"]) - set(self.sources)
            if unknown: raise ContractError(f"fact {fact['id']} has unknown sources: {sorted(unknown)}")
        for asset in self.assets.values():
            if asset["rights_id"] not in self.rights:
                raise ContractError(f"asset {asset['id']} has unknown rights id: {asset['rights_id']}")
        frontmatter = "".join(self.lines[:self.body_start])
        self._record("frontmatter", "document-metadata", frontmatter, 0)

    @staticmethod
    def _index(items: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for item in items:
            if item["id"] in result:
                raise ContractError(f"duplicate {label} id: {item['id']}")
            result[item["id"]] = item
        return result

    def _frontmatter(self) -> tuple[dict[str, Any], int]:
        if not self.lines or self.lines[0].rstrip("\n") != "---":
            raise ContractError("document must start with YAML frontmatter")
        end = next((i for i in range(1, len(self.lines)) if self.lines[i].rstrip("\n") == "---"), None)
        if end is None:
            raise ContractError("frontmatter is not closed")
        raw = "".join(self.lines[1:end])
        try:
            for token in yaml.scan(raw, Loader=UniqueLoader):
                if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken)):
                    raise ContractError("YAML anchors, aliases, and explicit tags are forbidden")
            meta = yaml.load(raw, Loader=UniqueLoader)
        except (yaml.YAMLError, ContractError) as exc:
            raise ContractError(f"invalid frontmatter: {exc}") from exc
        validate_schema(meta, "markdown-document.v1.schema.json", "frontmatter")
        return meta, end + 1

    def _record(self, kind: str, ident: str, literal: str, char_start: int, mutable: bool = False) -> None:
        if not literal: return
        ast_path = "document/frontmatter" if kind == "frontmatter" else f"document/{kind}/{ident}"
        if not mutable:
            self._protected_candidates.append({"kind": kind, "role": ident, "ast_path": ast_path, "char_start": char_start, "literal": literal})
            return
        # Newline bytes are structural. Split prose so every editable interval is
        # single-line and its surrounding delimiters stay in the protected complement.
        cursor = 0
        for part in re.split(r"(\n)", literal):
            if part and part != "\n":
                self._mutable_candidates.append({"kind": kind, "role": ident, "ast_path": ast_path, "char_start": char_start + cursor, "literal": part})
            cursor += len(part)

    def _partition(self) -> list[dict[str, Any]]:
        data = self.source.encode("utf-8")
        spans: list[tuple[int, int, str, str, str]] = []
        for item in self._mutable_candidates:
            start = _byte_offset(self.source, item["char_start"]); end = start + len(item["literal"].encode("utf-8"))
            spans.append((start, end, item["kind"], item["role"], item["ast_path"]))
        spans.sort()
        for left, right in zip(spans, spans[1:]):
            if left[1] > right[0]: raise ContractError("internal error: mutable Humanizer intervals overlap")
        protected_spans = [(_byte_offset(self.source, item["char_start"]), _byte_offset(self.source, item["char_start"] + len(item["literal"])), item["kind"], item["role"], item["ast_path"]) for item in self._protected_candidates]
        boundaries = {0, len(data)}
        for start, end, _, _, _ in spans + protected_spans: boundaries.update((start, end))
        points = sorted(boundaries); atomic: list[tuple[int, int, bool, str, str, str]] = []
        for start, end in zip(points, points[1:]):
            mutable_hits = [item for item in spans if item[0] <= start and end <= item[1]]
            protected_hits = [item for item in protected_spans if item[0] <= start and end <= item[1]]
            if mutable_hits and protected_hits: raise ContractError("internal error: mutable bytes intersect protected syntax")
            if mutable_hits: _, _, kind, role, ast_path = min(mutable_hits, key=lambda item: item[1] - item[0]); mutable = True
            elif protected_hits:
                priority = {"fact": 20, "id": 20, "number": 20, "url": 20, "source": 20, "todo": 20, "inline-protected": 20, "figure-ref": 20,
                            "table-value": 10, "diagram-code": 10, "verbatim": 10, "footnote": 10, "directive": 5, "structural-text": 1, "frontmatter": 1}
                _, _, kind, role, ast_path = max(protected_hits, key=lambda item: (priority.get(item[2], 0), -(item[1] - item[0]))); mutable = False
            else: kind, role, ast_path, mutable = "syntax", "structural-bytes", "document/syntax", False
            if atomic and atomic[-1][2:] == (mutable, kind, role, ast_path) and atomic[-1][1] == start:
                atomic[-1] = (atomic[-1][0], end, mutable, kind, role, ast_path)
            else: atomic.append((start, end, mutable, kind, role, ast_path))
        segments: list[dict[str, Any]] = []; role_ordinals: dict[tuple[str, str, str], int] = {}
        def add(start: int, end: int, mutable: bool, kind: str, role: str, path: str) -> None:
            if end <= start: return
            classification = "mutable" if mutable else "protected"
            key = (classification, path, kind); ordinal = role_ordinals.get(key, 0) + 1; role_ordinals[key] = ordinal
            stable = sha(f"{path}\0{kind}\0{ordinal}".encode())[:24]
            raw = data[start:end]
            entry = {"segment_id": f"{classification}-{stable}", "classification": classification, "ast_path": path,
                     "role": role, "ordinal": ordinal, "kind": kind, "start_byte": start, "end_byte": end,
                     "old_hash": sha(raw), "sha256": sha(raw), "text": raw.decode("utf-8")}
            segments.append(entry); (self.mutable if mutable else self.protected).append(entry)
        for start, end, mutable, kind, role, path in atomic: add(start, end, mutable, kind, role, path)
        if not segments or segments[0]["start_byte"] != 0 or segments[-1]["end_byte"] != len(data):
            raise ContractError("internal error: Humanizer partition is not byte-complete")
        for left, right in zip(segments, segments[1:]):
            if left["end_byte"] != right["start_byte"]: raise ContractError("internal error: Humanizer partition has a gap")
        return segments

    def _claim_id(self, ident: str, owner: str) -> None:
        previous = self.claimed_ids.get(ident)
        if previous is not None:
            raise ContractError(f"duplicate semantic id {ident}: {previous} and {owner}")
        self.claimed_ids[ident] = owner

    def inline(self, text: str, char_start: int, *, mutable_context: bool = True) -> list[dict[str, Any]]:
        tokens: list[tuple[int, int, dict[str, Any]]] = []
        for match in PROTECTED_RE.finditer(text):
            kind, ident, literal = match.group("kind", "id", "literal")
            if kind in {"fact", "number"}:
                if ident not in self.facts or str(self.facts[ident]["value"]) != literal:
                    raise ContractError(f"protected {kind} {ident} does not equal a declared fact")
            elif kind == "source" and ident not in self.sources:
                raise ContractError(f"unknown protected source: {ident}")
            elif kind == "url":
                if not literal.startswith("https://") or ident not in self.sources or self.sources[ident]["locator"] != literal:
                    raise ContractError(f"protected URL {ident} is not the declared HTTPS source locator")
            elif kind == "todo" and ident not in self.todos:
                raise ContractError(f"unknown protected TODO: {ident}")
            elif kind == "todo" and self.todos[ident]["reason"] != literal:
                raise ContractError(f"protected TODO {ident} does not equal its declared reason")
            raw = match.group(0); self._record(kind, ident, raw, char_start + match.start())
            tokens.append((match.start(), match.end(), {"type": "protected", "kind": kind, "id": ident, "literal": literal}))
        occupied = [(a, b) for a, b, _ in tokens]
        def free(a: int, b: int) -> bool: return not any(a < y and b > x for x, y in occupied)
        for match in LINK_RE.finditer(text):
            if not free(match.start(), match.end()): continue
            target = match.group("target")
            if not (target.startswith("https://") or target.startswith("#")):
                raise ContractError(f"link target must be HTTPS or a local anchor: {target}")
            self.inline(match.group("label"), char_start + match.start("label"), mutable_context=mutable_context)
            self._record("url" if target.startswith("https://") else "id", "link-target", target, char_start + match.start("target"))
            if target.startswith("#"): self.local_link_targets.append(target[1:])
            tokens.append((match.start(), match.end(), {"type": "link", "label": match.group("label"), "target": target})); occupied.append((match.start(), match.end()))
        for match in FIGURE_REF_RE.finditer(text):
            if not free(match.start(), match.end()): continue
            if not mutable_context: raise ContractError("figure references are allowed only in prose or list text")
            ident=match.group("id");token={"type":"figure-ref","target":ident}
            self.figure_refs.append(token);self._record("figure-ref",ident,match.group(0),char_start+match.start())
            tokens.append((match.start(),match.end(),token));occupied.append((match.start(),match.end()))
        for match in FOOTNOTE_REF_RE.finditer(text):
            if free(match.start(), match.end()):
                ident = match.group(1); self.footnote_refs.add(ident); self._record("source", ident, match.group(0), char_start + match.start())
                tokens.append((match.start(), match.end(), {"type": "footnote-ref", "source_id": ident})); occupied.append((match.start(), match.end()))
        tokens.sort(key=lambda x: x[0]); cursor = 0; result: list[dict[str, Any]] = []
        for start, end, token in tokens:
            if start > cursor:
                literal = text[cursor:start]; self._plain(literal, char_start + cursor, mutable_context, result)
            result.append(token); cursor = end
        if cursor < len(text): self._plain(text[cursor:], char_start + cursor, mutable_context, result)
        return result

    def _plain(self, literal: str, start: int, mutable: bool, result: list[dict[str, Any]]) -> None:
        if re.search(r"(?<![\w-])TODO(?::|\b)", literal, re.I): raise ContractError("TODO must use {{todo:todo-id|reason}} and be declared in frontmatter")
        if RAW_URL_RE.search(literal): raise ContractError("raw URL is forbidden; use a Markdown link or protected URL")
        if BARE_NUMBER_RE.search(literal): raise ContractError("bare numbers are forbidden; bind them with {{number:fact-id|literal}}")
        if any(mark in literal for mark in ("`", "**", "__", "~~", "![", "<", ">")) or re.search(r"(?<!\\)[*_]", literal):
            raise ContractError("unsupported inline Markdown or raw HTML")
        if re.search(r"\[[^\]]*\]|\]\(", literal): raise ContractError("malformed or unsupported inline construct")
        if literal:
            result.append({"type": "text", "text": literal, "mutable": mutable})
            self._record("natural-prose" if mutable else "structural-text", "segment", literal, start, mutable=mutable)

    def parse(self) -> dict[str, Any]:
        children, end = self.blocks(self.body_start, len(self.lines), container="document")
        if end != len(self.lines): raise ContractError("parser did not consume the complete document")
        headings = [n for n in children if n["type"] == "heading"]
        h1 = [n for n in headings if n["level"] == 1]
        if len(h1) != 1 or next((n for n in children if n["type"] != "blank"), None) is not h1[0]: raise ContractError("exactly one H1 must be the first body element")
        if h1[0]["text"] != self.meta["title"]: raise ContractError("H1 must equal frontmatter title")
        if self.footnote_refs != self.footnote_defs: raise ContractError(f"footnote references/definitions differ: refs={sorted(self.footnote_refs)} defs={sorted(self.footnote_defs)}")
        missing_targets = sorted(set(self.local_link_targets) - set(self.claimed_ids))
        if missing_targets: raise ContractError(f"local links target unknown global IDs: {missing_targets}")
        figures=[node for node in children if node["type"] in {"mermaid","graphviz"}]
        figure_registry=[]
        for number,node in enumerate(figures,1):
            node["figure_number"]=number
            figure_registry.append({"id":node["id"],"number":number,"type":node["type"],"caption":node["caption"],"description":node["description"]})
        by_figure={item["id"]:item for item in figure_registry}
        missing_figures=sorted({token["target"] for token in self.figure_refs}-set(by_figure))
        if missing_figures:raise ContractError(f"figure references target unknown diagram figures: {missing_figures}")
        for token in self.figure_refs:
            item=by_figure[token["target"]];token["figure_number"]=item["number"];token["reference_text"]=f'Abbildung {item["number"]}'
        if not self.source.endswith("\n"): raise ContractError("document must end with LF")
        segments = self._partition()
        return {"type": "document", "contract": MARKDOWN_CONTRACT_VERSION, "frontmatter": self.meta, "children": children,
                "figure_registry":figure_registry,
                "humanizer_segments": {"partition": segments, "mutable": self.mutable, "protected": self.protected},
                "source_sha256": sha(self.source.encode()), "roundtrip_sha256": sha(self.source.encode()), "source": self.source}

    def blocks(self, start: int, end: int, stop_directive: bool = False, container: str = "document") -> tuple[list[dict[str, Any]], int]:
        nodes: list[dict[str, Any]] = []; i = start; current_h2 = False
        while i < end:
            line = self.lines[i].rstrip("\n")
            if stop_directive and line == ":::": return nodes, i + 1
            if not line.strip(): nodes.append({"type": "blank", "line": i + 1}); i += 1; continue
            if line.startswith("```"):
                fence = re.fullmatch(r"```\{(mermaid|graphviz)\s+#(" + ID + r')\s+title="([^"\r\n]+)"\s+description="([^"\r\n]+)"(?:\s+source_id=(' + ID + r'))?\}', line)
                if not fence: raise ContractError(f"line {i+1}: diagram fence requires kind, id, title, description, and optional source_id")
                kind, diagram_id, title, description, source_id = fence.groups()
                validator = validate_mermaid_text if kind == "mermaid" else validate_graphviz_text
                validator(title, "title"); validator(description, "description")
                if source_id is not None and source_id not in self.sources: raise ContractError(f"line {i+1}: unknown diagram source")
                close = next((j for j in range(i + 1, end) if self.lines[j].rstrip("\n") == "```"), None)
                if close is None: raise ContractError(f"line {i+1}: diagram fence is not closed")
                code = "".join(self.lines[i + 1:close]);
                (validate_mermaid_v1 if kind == "mermaid" else validate_graphviz_v1)(code)
                self._claim_id(diagram_id, kind)
                self._record("directive", diagram_id, self.lines[i], self.offsets[i])
                code_start = self.offsets[i + 1]; self._record("diagram-code", diagram_id, code, code_start)
                self._record("directive", diagram_id, self.lines[close], self.offsets[close])
                node={"type":kind,"id":diagram_id,"line":i+1,"code":code,"caption":title,"description":description}
                if source_id is not None:node["source_id"]=source_id
                nodes.append(node); i = close + 1; continue
            if line == "::: page-break":
                if container != "document": raise ContractError(f"line {i+1}: page-break is only allowed at document section boundaries")
                self._record("directive", "page-break", self.lines[i], self.offsets[i])
                nodes.append({"type": "page-break", "line": i + 1}); i += 1; continue
            if line.startswith(":::"):
                if container != "document": raise ContractError(f"line {i+1}: callouts may not be nested")
                match = re.fullmatch(r"::: \{\.(note|tip|important|warning|caution|metric|quote|legal|verbatim)\s+(.+)\}", line)
                if not match: raise ContractError(f"line {i+1}: unknown or malformed directive")
                kind, attrs = match.group(1), self._attrs(match.group(2), i + 1)
                required = {"id", "title"} | ({"source_id"} if kind in {"quote", "legal"} else set()) | ({"label", "value_fact", "source_id"} if kind == "metric" else set())
                optional = {"appearance", "icon"} if kind in {"note", "tip", "important", "warning", "caution"} else set()
                if not required.issubset(attrs) or not set(attrs).issubset(required | optional):
                    raise ContractError(f"line {i+1}: {kind} requires {sorted(required)} and permits {sorted(optional)}")
                if attrs.get("appearance", "default") not in {"default", "simple", "minimal"}:
                    raise ContractError(f"line {i+1}: callout appearance must be default, simple, or minimal")
                if attrs.get("icon", "true") not in {"true", "false"}:
                    raise ContractError(f"line {i+1}: callout icon must be true or false")
                if kind in {"note", "tip", "important", "warning", "caution"}:
                    attrs.setdefault("appearance", "default"); attrs.setdefault("icon", "true")
                if "source_id" in attrs and attrs["source_id"] not in self.sources: raise ContractError(f"line {i+1}: unknown callout source")
                if "value_fact" in attrs and attrs["value_fact"] not in self.facts: raise ContractError(f"line {i+1}: unknown metric fact")
                self._claim_id(attrs["id"], f"{kind} callout")
                self._record("directive", attrs["id"], self.lines[i], self.offsets[i])
                body_start = i + 1
                if kind == "verbatim":
                    close = next((j for j in range(body_start, end) if self.lines[j].rstrip("\n") == ":::"), None)
                    if close is None: raise ContractError("verbatim directive is not closed")
                    body = "".join(self.lines[body_start:close]); self._record("verbatim", attrs["id"], body, self.offsets[body_start])
                    self._record("directive", attrs["id"], self.lines[close], self.offsets[close])
                    body_nodes = [{"type": "verbatim-text", "text": body}]; i = close + 1
                else:
                    body_nodes, i = self.blocks(body_start, end, stop_directive=True, container="callout")
                    self._record("directive", attrs["id"], self.lines[i - 1], self.offsets[i - 1])
                if kind == "verbatim": has_content = bool(body.strip())
                else: has_content = any(node["type"] != "blank" and (node["type"] != "paragraph" or node.get("text", "").strip()) for node in body_nodes)
                if not has_content:
                    raise ContractError(f"line {body_start+1}: {kind} callout must have non-empty content")
                nodes.append({"type": "callout", "kind": kind, "attrs": attrs, "children": body_nodes, "line": i + 1}); continue
            heading = HEADING_RE.fullmatch(line)
            if heading:
                if container != "document": raise ContractError(f"line {i+1}: headings are only allowed at document level")
                level, text, ident = len(heading.group(1)), heading.group(2), heading.group(3)
                if level == 3 and not current_h2: raise ContractError(f"line {i+1}: H3 requires a preceding H2")
                if level == 2: current_h2 = True
                self._claim_id(ident, f"H{level}")
                self.inline(text, self.offsets[i] + level + 1, mutable_context=False)
                self._record("directive", ident, self.lines[i], self.offsets[i])
                nodes.append({"type": "heading", "level": level, "text": text, "id": ident, "line": i + 1}); i += 1; continue
            if line.startswith("#"): raise ContractError(f"line {i+1}: only attributed H1-H3 are allowed")
            image = IMAGE_RE.fullmatch(line)
            if image:
                values = image.groupdict(); self._validate_image(values); self._claim_id(values["id"], "image"); self._record("directive", values["id"], self.lines[i], self.offsets[i])
                nodes.append({"type": "image", **values, "line": i + 1}); i += 1; continue
            footnote = FOOTNOTE_RE.fullmatch(line)
            if footnote:
                if container != "document": raise ContractError(f"line {i+1}: footnote definitions are only allowed at document level")
                ident, text = footnote.groups()
                if ident not in self.sources or ident in self.footnote_defs: raise ContractError(f"line {i+1}: footnote must bind one unique declared source")
                self.footnote_defs.add(ident); self._record("footnote", ident, line, self.offsets[i]); nodes.append({"type": "footnote", "source_id": ident, "text": text, "line": i + 1}); i += 1; continue
            if LIST_RE.fullmatch(line):
                node, i = self._list(i, end); nodes.append(node); continue
            if "|" in line and i + 1 < end and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", self.lines[i + 1].rstrip("\n")):
                node, i = self._table(i, end); nodes.append(node); continue
            if line.startswith((">", "    ", "\t", "~~~")) or re.match(r"^\s*<", line): raise ContractError(f"line {i+1}: unsupported block, HTML, or fence")
            if re.fullmatch(r"\s*(?:-{3,}|={3,})\s*", line): raise ContractError(f"line {i+1}: thematic breaks and setext headings are unsupported")
            para_start = i
            while i < end and self.lines[i].strip() and not self._starts_block(i, end, stop_directive): i += 1
            if i == para_start: raise ContractError(f"line {i+1}: unknown syntax")
            raw = "".join(self.lines[para_start:i]).rstrip("\n"); inline = self.inline(raw, self.offsets[para_start])
            nodes.append({"type": "paragraph", "inline": inline, "text": raw, "line": para_start + 1})
        if stop_directive: raise ContractError("callout directive is not closed")
        return nodes, i

    def _starts_block(self, i: int, end: int, stop: bool) -> bool:
        line = self.lines[i].rstrip("\n")
        return bool((stop and line == ":::") or line.startswith(("#", "```", ":::", "![", "[^")) or re.fullmatch(r"\s*(?:-{3,}|={3,})\s*", line) or LIST_RE.fullmatch(line) or ("|" in line and i + 1 < end and re.search(r"-{3,}", self.lines[i + 1])))

    @staticmethod
    def _attrs(raw: str, line: int) -> dict[str, str]:
        try: parts = shlex.split(raw)
        except ValueError as exc: raise ContractError(f"line {line}: invalid directive attributes: {exc}") from exc
        attrs: dict[str, str] = {}
        for part in parts:
            if part.startswith("#"): key, value = "id", part[1:]
            elif "=" in part: key, value = part.split("=", 1)
            else: raise ContractError(f"line {line}: invalid directive field")
            if key in attrs or not re.fullmatch(ID if key == "id" else r"[a-z][a-z0-9_]*", key): raise ContractError(f"line {line}: duplicate or invalid directive field")
            attrs[key] = value
        if "id" not in attrs or not re.fullmatch(ID, attrs["id"]): raise ContractError(f"line {line}: invalid directive id")
        return attrs

    def _list(self, start: int, end: int) -> tuple[dict[str, Any], int]:
        root: dict[str, Any] | None = None; stack: list[tuple[int, dict[str, Any]]] = []; i = start
        while i < end:
            match = LIST_RE.fullmatch(self.lines[i].rstrip("\n"))
            if not match: break
            indent = len(match.group(1))
            if indent % 2 or indent > 4: raise ContractError(f"line {i+1}: list nesting uses 0, 2, or 4 spaces")
            level = indent // 2; ordered = match.group("marker").endswith(".")
            node = {"type": "ordered-list" if ordered else "unordered-list", "items": [], "line": i + 1}
            if level == 0:
                if root is None: root = node; stack = [(0, root)]
                elif root["type"] != node["type"]: break
            elif level > len(stack): raise ContractError(f"line {i+1}: list nesting skips a level")
            else:
                stack = stack[:level]
                parent = stack[-1][1]
                if not parent["items"]: raise ContractError(f"line {i+1}: nested list has no parent item")
                existing = parent["items"][-1].get("children")
                if existing and existing[-1]["type"] == node["type"]: node = existing[-1]
                else: parent["items"][-1].setdefault("children", []).append(node)
                stack.append((level, node))
            current = stack[level][1]
            text = match.group("text"); inline = self.inline(text, self.offsets[i] + indent + len(match.group("marker")) + 1)
            current["items"].append({"text": text, "inline": inline}); i += 1
        assert root is not None
        return root, i

    def _table(self, start: int, end: int) -> tuple[dict[str, Any], int]:
        def cells(line: str) -> list[str]:
            protected: list[str] = []
            def hold(match: re.Match[str]) -> str:
                protected.append(match.group(0)); return f"\x00{len(protected)-1}\x00"
            held = PROTECTED_RE.sub(hold, line.strip().strip("|"))
            values = [cell.strip() for cell in held.split("|")]
            return [re.sub(r"\x00(\d+)\x00", lambda m: protected[int(m.group(1))], value) for value in values]
        headers = cells(self.lines[start].rstrip("\n")); separator = cells(self.lines[start + 1].rstrip("\n")); i = start + 2; rows: list[list[str]] = []
        if len(headers) < 2 or any(not cell for cell in headers): raise ContractError(f"line {start+1}: table needs non-empty columns")
        if len(separator) != len(headers) or any(not re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
            raise ContractError(f"line {start+2}: table separator must exactly match header columns")
        alignments = ["center" if cell.startswith(":") and cell.endswith(":") else "left" if cell.startswith(":") else "right" if cell.endswith(":") else "default" for cell in separator]
        while i < end and "|" in self.lines[i] and self.lines[i].strip():
            row = cells(self.lines[i].rstrip("\n"))
            if len(row) != len(headers): raise ContractError(f"line {i+1}: table column count differs")
            rows.append(row); i += 1
        for line_index, row in [(start, headers), *[(start + 2 + n, row) for n, row in enumerate(rows)]]:
            cursor = self.offsets[line_index]
            for col, value in enumerate(row):
                value_start = self.source.find(value, cursor)
                self.inline(value, value_start, mutable_context=False)
                self._record("table-value", f"r{line_index+1}-c{col+1}", value, value_start)
                cursor = value_start + len(value)
        return {"type": "table", "headers": headers, "separator": separator, "alignments": alignments, "rows": rows, "line": start + 1}, i

    def _validate_image(self, image: dict[str, str]) -> None:
        asset = self.assets.get(image["asset"]); rights = self.rights.get(image["rights"])
        if not asset or asset["rights_id"] != image["rights"] or not rights: raise ContractError("image must bind a declared asset and its declared rights id")
        if not image["alt"].strip() or not image["description"].strip(): raise ContractError("image alt and description are required")

    def validate_assets(self) -> None:
        if not self.assets and not self.rights: return
        if self.asset_root is None: raise ContractError("asset-root is required when frontmatter declares assets or rights")
        root = self.asset_root.resolve(strict=True)
        for label, records, path_key, hash_key in (("asset", self.assets, "path", "sha256"), ("rights", self.rights, "evidence_path", "evidence_sha256")):
            for ident, record in records.items():
                rel = PurePosixPath(record[path_key])
                if rel.is_absolute() or ".." in rel.parts or rel.as_posix() != record[path_key]: raise ContractError(f"{label} {ident} path is not normalized local content")
                cursor = root
                for part in rel.parts:
                    cursor /= part
                    mode = cursor.lstat().st_mode
                    if stat.S_ISLNK(mode): raise ContractError(f"{label} {ident} path contains a symlink")
                target = cursor.resolve(strict=True)
                if root != target and root not in target.parents or not target.is_file(): raise ContractError(f"{label} {ident} escapes asset-root or is not a file")
                data = target.read_bytes()
                if sha(data) != record[hash_key]: raise ContractError(f"{label} {ident} hash mismatch")
                if label == "asset": validate_image_bytes(target, record["media_type"], data)
                if label == "rights":
                    evidence = load_json(target)
                    validate_schema(evidence, "rights-evidence.v1.schema.json", f"rights evidence {ident}")
                    bound_assets = [asset["id"] for asset in self.assets.values() if asset["rights_id"] == ident]
                    if bound_assets != [evidence["asset_id"]]:
                        raise ContractError(f"rights {ident} must bind exactly its declared asset")
                    _reject_open_work(evidence, f"rights.{ident}")
                    self.rights_evidence[ident] = evidence


def serialize_ast(ast: dict[str, Any]) -> str:
    source = ast.get("source")
    if not isinstance(source, str) or sha(source.encode()) != ast.get("roundtrip_sha256"):
        raise ContractError("AST source does not match its roundtrip hash")
    return source


def _ast_skeleton(ast: dict[str, Any]) -> Any:
    def inline(tokens: list[dict[str, Any]]) -> list[Any]:
        result = []
        for token in tokens:
            if token["type"] == "text": result.append({"type": "text", "mutable": token.get("mutable", False)})
            elif token["type"] == "link": result.append({"type": "link", "target": token["target"]})
            else: result.append({key: value for key, value in token.items() if key != "literal"})
        return result
    def node(value: dict[str, Any]) -> dict[str, Any]:
        result = {"type": value["type"]}
        for key in ("level", "id", "kind", "attrs", "asset", "rights", "source_id", "separator", "alignments", "figure_number"):
            if key in value: result[key] = value[key]
        if "inline" in value: result["inline"] = inline(value["inline"])
        if "children" in value: result["children"] = [node(child) for child in value["children"]]
        if "items" in value:
            result["items"] = [{"inline": inline(item.get("inline", [])), "children": [node(child) for child in item.get("children", [])]} for item in value["items"]]
        if value["type"] == "table": result["shape"] = [len(value["headers"]), len(value["rows"])]
        return result
    return {"type": ast["type"], "contract": ast["contract"], "frontmatter": ast["frontmatter"], "figure_registry":ast.get("figure_registry",[]), "children": [node(child) for child in ast["children"]]}


def verify_humanizer_roundtrip(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Prove that a rewrite changed only natural-prose segments."""
    def signature(ast: dict[str, Any]) -> list[tuple[str, str, str, str, int]]:
        return [
            (item["segment_id"], item["ast_path"], item["role"], item["old_hash"], item["ordinal"])
            for item in ast["humanizer_segments"]["protected"]
        ]
    if signature(before) != signature(after):
        raise ContractError("Humanizer rewrite changed, removed, inserted, or reordered protected segments")
    if _ast_skeleton(before) != _ast_skeleton(after):
        raise ContractError("Humanizer rewrite changed AST block order, nesting, delimiters, or structure")
    if not after["humanizer_segments"]["mutable"]:
        raise ContractError("Humanizer rewrite contains no natural-prose segments")


def _brand_package_identity(manifest_path: Path | None = None, expected_content: str | None = None) -> tuple[str, str, str]:
    manifest = (manifest_path or BRAND_MANIFEST).resolve(strict=True)
    root = manifest.parent
    digest = hashlib.sha256()
    for item in sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix()):
        if item.is_symlink(): raise ContractError("pinned brand package contains a symlink")
        rel = item.relative_to(root).as_posix().encode()
        if item.is_file(): digest.update(b"F\0" + rel + b"\0" + item.read_bytes() + b"\0")
        elif item.is_dir(): digest.update(b"D\0" + rel + b"\0")
    content_hash = digest.hexdigest()
    if manifest_path is None:
        index = load_json(ROOT / "fixtures/brand-registry/index.v1.json")
        entry = next((item for item in index.get("entries", []) if item.get("brand_id") == "neutral-fixture" and item.get("version") == "1.0.0"), None)
        expected_locator = root.relative_to(ROOT / "fixtures/brand-registry").as_posix()
        if not entry or entry.get("content_sha256") != content_hash or entry.get("immutable_release_locator") != expected_locator:
            raise ContractError("materialized brand package differs from its registry content pin")
        locator = manifest.relative_to(ROOT).as_posix()
    else:
        if expected_content is None or content_hash != expected_content:
            raise ContractError("materialized brand package differs from explicit activation content pin")
        locator = str(manifest)
    return locator, sha(manifest.read_bytes()), content_hash


def create_humanizer_baseline(ast: dict[str, Any], dtype: dict[str, Any], brand_sha256: str, issuer: str,
                              key_path: Path, nonce: str, created_at: str, tool_version: str = "1.0.0",
                              brand_manifest: Path | None = None) -> dict[str, Any]:
    brand_path, manifest_hash, content_hash = _brand_package_identity(brand_manifest, brand_sha256 if brand_manifest else None)
    if not issuer.strip() or len(nonce) < 16 or not created_at or brand_sha256 != content_hash:
        raise ContractError("baseline provenance requires issuer, nonce, time, and brand hash")
    source = serialize_ast(ast)
    provenance = {"source_sha256": ast["source_sha256"], "contract_sha256": sha(MARKDOWN_CONTRACT_PATH.read_bytes()),
                  "document_type_id": dtype["id"], "document_type_sha256": sha(canonical_json(dtype)),
                  "brand_package_path": brand_path, "brand_package_manifest_sha256": manifest_hash,
                  "brand_package_sha256": content_hash, "tool_version": tool_version, "nonce": nonce, "created_at": created_at}
    payload = {"source_utf8_base64": base64.b64encode(source.encode("utf-8")).decode("ascii"), "provenance": provenance,
               "provenance_sha256": sha(canonical_json(provenance))}
    signed = {"schema_version": "humanizer-baseline.v1", "issuer": issuer, "payload": payload}
    return {**signed, "hmac_sha256": _sign(signed, _key_bytes(key_path))}


def verify_humanizer_baseline(envelope: dict[str, Any], key_path: Path, asset_root: Path | None,
                              dtype: dict[str, Any], brand_manifest: Path | None = None,
                              brand_content_sha256: str | None = None) -> dict[str, Any]:
    validate_schema(envelope, "humanizer-baseline.v1.schema.json", "humanizer baseline")
    payload = _verify_envelope(envelope, "humanizer-baseline.v1", key_path)
    if not isinstance(payload, dict) or set(payload) != {"source_utf8_base64", "provenance", "provenance_sha256"}:
        raise ContractError("humanizer baseline payload is not closed")
    provenance = payload["provenance"]
    required = {"source_sha256", "contract_sha256", "document_type_id", "document_type_sha256", "brand_package_path", "brand_package_manifest_sha256", "brand_package_sha256", "tool_version", "nonce", "created_at"}
    if not isinstance(provenance, dict) or set(provenance) != required or sha(canonical_json(provenance)) != payload["provenance_sha256"]:
        raise ContractError("humanizer baseline provenance hash failed")
    if provenance["contract_sha256"] != sha(MARKDOWN_CONTRACT_PATH.read_bytes()) or provenance["document_type_id"] != dtype["id"] or provenance["document_type_sha256"] != sha(canonical_json(dtype)):
        raise ContractError("humanizer baseline contract/document-type provenance differs")
    brand_path, manifest_hash, content_hash = _brand_package_identity(brand_manifest, brand_content_sha256 if brand_manifest else None)
    if (provenance["brand_package_path"], provenance["brand_package_manifest_sha256"], provenance["brand_package_sha256"]) != (brand_path, manifest_hash, content_hash) or len(str(provenance["nonce"])) < 16:
        raise ContractError("humanizer baseline provenance is incomplete")
    try: source = base64.b64decode(payload["source_utf8_base64"], validate=True).decode("utf-8")
    except (ValueError, UnicodeError) as exc: raise ContractError("invalid baseline UTF-8 payload") from exc
    if sha(source.encode()) != provenance["source_sha256"]: raise ContractError("baseline source hash differs from provenance")
    parser = Parser(source, asset_root); ast = parser.parse(); parser.validate_assets(); ast["rights_evidence"] = parser.rights_evidence
    validate_ast_document(ast, dtype)
    return ast


def rewrite_humanizer(baseline: dict[str, Any], key_path: Path, operations: list[tuple[str, str, str]],
                      asset_root: Path | None, dtype: dict[str, Any], brand_manifest: Path | None = None,
                      brand_content_sha256: str | None = None) -> tuple[str, dict[str, Any]]:
    before = verify_humanizer_baseline(baseline, key_path, asset_root, dtype, brand_manifest, brand_content_sha256)
    mutable = {item["segment_id"]: item for item in before["humanizer_segments"]["mutable"]}
    if not operations: raise ContractError("Humanizer rewrite requires at least one operation")
    seen: set[str] = set(); replacements: list[tuple[int, int, bytes]] = []
    for operation in operations:
        if not isinstance(operation, (list, tuple)) or len(operation) != 3:
            raise ContractError("rewrite operation must be (segment_id, old_hash, replacement)")
        segment_id, old_hash, replacement = operation
        if segment_id in seen: raise ContractError("duplicate Humanizer rewrite segment")
        seen.add(segment_id)
        item = mutable.get(segment_id)
        if item is None: raise ContractError(f"rewrite may target only mutable segment IDs: {segment_id}")
        if old_hash != item["old_hash"]: raise ContractError(f"stale Humanizer old_hash for {segment_id}")
        if not isinstance(replacement, str) or not replacement or "\n" in replacement or "\r" in replacement:
            raise ContractError("Humanizer replacement must be non-empty single-line UTF-8 prose")
        replacements.append((item["start_byte"], item["end_byte"], replacement.encode("utf-8")))
    data = serialize_ast(before).encode("utf-8")
    for start, end, replacement in sorted(replacements, reverse=True): data = data[:start] + replacement + data[end:]
    try: source = data.decode("utf-8")
    except UnicodeError as exc: raise ContractError("Humanizer rewrite did not reconstruct UTF-8") from exc
    parser = Parser(source, asset_root); after = parser.parse(); parser.validate_assets(); after["rights_evidence"] = parser.rights_evidence
    validate_ast_document(after, dtype); verify_humanizer_roundtrip(before, after)
    return source, after


def load_base_types() -> dict[str, dict[str, Any]]:
    result = {}
    for path in sorted(BASE_TYPE_DIR.glob("*.v1.json")):
        doc = load_json(path); validate_schema(doc, "document-type.v1.schema.json", str(path.relative_to(ROOT))); result[doc["id"]] = doc
    if set(result) != {"report", "offer", "one-pager", "freeform"}: raise ContractError("exactly four v1 base document types are required")
    return result


def validate_mermaid_toolchain() -> dict[str, Any]:
    lock = load_json(ROOT / "toolchain/mermaid-svg.v1.lock.json")
    validate_schema(lock, "mermaid-svg-toolchain.v1.schema.json", "Mermaid SVG toolchain lock")
    for record in [lock["package_lock"], lock["package_json"], *lock["configs"], *lock["scripts"]]:
        path = ROOT / record["path"]
        if not path.is_file() or path.is_symlink() or sha(path.read_bytes()) != record["sha256"]:
            raise ContractError(f"Mermaid toolchain file hash failed: {record['path']}")
    package_lock = load_json(ROOT / lock["package_lock"]["path"])
    if package_lock.get("lockfileVersion") != lock["package_lock"]["lockfile_version"] or len(package_lock.get("packages", {})) != lock["package_lock"]["package_count"] or package_lock.get("packages", {}).get("", {}).get("dependencies", {}).get(lock["package_lock"]["root_package"]) != lock["package_lock"]["root_version"]:
        raise ContractError("Mermaid package lock metadata differs from the toolchain lock")
    packages = package_lock.get("packages", {})
    transitives = [entry for path, entry in packages.items() if path and isinstance(entry, dict)]
    if (len(transitives) != lock["package_lock"]["transitive_integrity_count"]
            or any(not re.fullmatch(r"sha512-[A-Za-z0-9+/]+={0,2}", str(entry.get("integrity", "")))
                   or not re.fullmatch(r"https" + r"://registry\.npmjs\.org/.+/-/.+\.tgz", str(entry.get("resolved", ""))) for entry in transitives)):
        raise ContractError("every Mermaid transitive must have a closed registry locator and sha512 integrity")
    package_json = load_json(ROOT / lock["package_json"]["path"])
    if package_json.get("dependencies") != {lock["package_lock"]["root_package"]: lock["package_lock"]["root_version"]}:
        raise ContractError("Mermaid package.json root dependency differs from lock")
    bundle = Path(lock["artifact_bundle"]["path"])
    manifest_path = bundle / "files.sha256.json"
    roots = [bundle / name for name in ("npm-cache", "browser", "node")]
    if bundle.name != lock["artifact_bundle"]["tree_sha256"] or not manifest_path.is_file() or manifest_path.is_symlink() or any(not root.is_dir() or root.is_symlink() for root in roots) or sha(manifest_path.read_bytes()) != lock["artifact_bundle"]["manifest_sha256"]:
        raise ContractError("Mermaid artifact bundle identity/manifest differs from lock")
    manifest = load_json(manifest_path)
    actual = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or path.stat().st_mode & 0o222: raise ContractError("Mermaid artifact bundle must be symlink-free and read-only")
            if path.is_file(): actual.append({"path":path.relative_to(bundle).as_posix(),"sha256":sha(path.read_bytes()),"size":path.stat().st_size})
    records = manifest.get("files") if isinstance(manifest, dict) and set(manifest) == {"schema_version", "files"} and manifest.get("schema_version") == "mermaid-artifact-bundle.v2" else None
    tree_hash = sha(canonical_json(records)) if isinstance(records, list) else ""
    if actual != records or len(actual) != lock["artifact_bundle"]["file_count"] or tree_hash != lock["artifact_bundle"]["tree_sha256"]:
        raise ContractError("Mermaid artifact bundle file manifest/tree hash failed")
    for component in ("browser", "node"):
        record = lock[component]; archive = bundle / record["archive_path"]
        if archive.is_symlink() or not archive.is_file() or sha(archive.read_bytes()) != record["archive_sha256"]:
            raise ContractError(f"pinned Mermaid {component} archive is absent or has the wrong hash")
    puppeteer = load_json(ROOT / "toolchain/mermaid-svg/PUPPETEER_CONFIG.json")
    expected_browser = "./" + lock["browser"]["executable_relative_path"]
    if puppeteer.get("executablePath") != expected_browser or PurePosixPath(puppeteer["executablePath"]).is_absolute():
        raise ContractError("Mermaid Puppeteer config is not bound to the packaged relative browser")
    return lock


def validate_document_type(doc: dict[str, Any], bases: dict[str, dict[str, Any]]) -> None:
    validate_schema(doc, "document-type.v1.schema.json", f"document type {doc.get('id', '<unknown>')}")
    sections = doc["sections"]
    if set(sections["required"]) & set(sections["optional"]): raise ContractError("required and optional sections overlap")
    if set(sections["order"]) != set(sections["required"]) | set(sections["optional"]): raise ContractError("section order must contain every allowed section exactly once")
    if not SAFETY_FLOOR.issubset(doc["forbidden_features"]) or set(doc["publication_gates"]) != PUBLICATION_GATES: raise ContractError("document type weakens global safety or publication gates")
    parent_id = doc["extends"]
    if doc["id"] in bases and doc != bases[doc["id"]] and parent_id != doc["id"]:
        raise ContractError("brand document type may refine, but not replace, a canonical base id")
    one_page_family = doc["id"] == "one-pager" or parent_id == "one-pager"
    expected_pagination = {"title_page_number": None, "content_start": None, "format": "none"} if one_page_family else {"title_page_number": None, "content_start": 1, "format": "Seite X von Y"}
    if doc["pagination"] != expected_pagination: raise ContractError("document pagination does not match its base family")
    if parent_id is None: return
    parent = bases[parent_id]
    if not set(parent["sections"]["required"]).issubset(sections["required"]): raise ContractError("extension removes a required base section")
    parent_order = [x for x in sections["order"] if x in parent["sections"]["order"]]
    if parent_order != parent["sections"]["order"]: raise ContractError("extension reorders or removes base sections")
    if not set(doc["allowed_elements"]).issubset(parent["allowed_elements"]): raise ContractError("extension broadens allowed elements")
    for key, enabled in doc["layout"].items():
        if enabled and not parent["layout"][key]: raise ContractError(f"extension enables base-disabled layout feature: {key}")
    if doc["pagination"] != parent["pagination"] or doc["rights_required"] is not True or not set(parent["forbidden_features"]).issubset(doc["forbidden_features"]): raise ContractError("extension weakens immutable safety or pagination")
    if doc["minimum_margin_mm"] < parent["minimum_margin_mm"]: raise ContractError("extension lowers the base minimum margin")
    for key, values in doc["allowed_overrides"].items():
        if key not in parent["allowed_overrides"] or not set(values).issubset(parent["allowed_overrides"][key]): raise ContractError("extension broadens project overrides")


def validate_ast_document(ast: dict[str, Any], dtype: dict[str, Any]) -> None:
    validate_figure_registry(ast)
    if ast["frontmatter"]["document_type"] != dtype["id"]: raise ContractError("frontmatter document_type differs from selected contract")
    top = ast["children"]
    sections = [n["id"] for n in top if n["type"] == "heading" and n["level"] == 2]
    if len(sections) != len(set(sections)): raise ContractError("H2 section ids must be unique")
    spec = dtype["sections"]
    if not set(spec["required"]).issubset(sections) or not set(sections).issubset(set(spec["order"])): raise ContractError("required/allowed H2 sections do not match document type")
    if [x for x in spec["order"] if x in sections] != sections: raise ContractError("H2 sections violate document type order")
    present: set[str] = set()
    def walk(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            kind = node["type"]
            if kind not in {"blank", "verbatim-text"}: present.add(kind)
            for token in node.get("inline", []):
                if token["type"] in {"link","figure-ref"}: present.add("link")
                elif token["type"] == "footnote-ref": present.add("footnote")
            walk(node.get("children", []))
            for item in node.get("items", []):
                for token in item.get("inline", []):
                    if token["type"] in {"link","figure-ref"}: present.add("link")
                    elif token["type"] == "footnote-ref": present.add("footnote")
                walk(item.get("children", []))
    walk(top)
    if not present.issubset(set(dtype["allowed_elements"])): raise ContractError(f"document uses elements forbidden by type: {sorted(present-set(dtype['allowed_elements']))}")
    pagebreaks = [i for i, n in enumerate(top) if n["type"] == "page-break"]
    if pagebreaks and not dtype["layout"]["manual_page_break"]: raise ContractError("document type disables manual page breaks")
    for i in pagebreaks:
        before = next((n for n in reversed(top[:i]) if n["type"] != "blank"), None); after = next((n for n in top[i+1:] if n["type"] != "blank"), None)
        if before is None or after is None or before["type"] == "heading" or after["type"] != "heading" or after["level"] not in {2, 3}: raise ContractError("page-break is allowed only after section content and immediately before H2/H3")


def validate_figure_registry(ast: dict[str, Any]) -> list[dict[str, Any]]:
    """Verify the closed, automatically numbered diagram-figure domain."""
    expected=[]
    for number,node in enumerate((item for item in ast.get("children",[]) if item.get("type") in {"mermaid","graphviz"}),1):
        item={"id":node.get("id"),"number":number,"type":node.get("type"),"caption":node.get("caption"),"description":node.get("description")}
        if not re.fullmatch(ID,str(item["id"] or "")) or not all(isinstance(item[key],str) and item[key].strip() for key in ("caption","description")):
            raise ContractError("diagram figure identity/caption contract failed")
        if node.get("figure_number")!=number:raise ContractError("diagram figure number differs from document order")
        expected.append(item)
    if ast.get("figure_registry")!=expected:raise ContractError("AST figure registry differs from ordered diagram figures")
    by_id={item["id"]:item for item in expected}
    if len(by_id)!=len(expected):raise ContractError("AST figure registry contains duplicate figure IDs")
    def walk(nodes:list[dict[str,Any]])->None:
        for node in nodes:
            groups=[]
            if "inline" in node:groups.append(node["inline"])
            groups.extend(item.get("inline",[]) for item in node.get("items",[]))
            for tokens in groups:
                for token in tokens:
                    if token.get("type")!="figure-ref":continue
                    target=token.get("target");item=by_id.get(target)
                    if item is None:raise ContractError(f"figure reference targets unknown diagram figure: {target}")
                    if token.get("figure_number")!=item["number"] or token.get("reference_text")!=f'Abbildung {item["number"]}':
                        raise ContractError("figure reference number/text differs from figure registry")
            walk(node.get("children",[]))
            for list_item in node.get("items",[]):walk(list_item.get("children",[]))
    walk(ast.get("children",[]))
    return expected


def _safe_result_file(result_path: Path, locator: str, label: str) -> Path:
    rel = PurePosixPath(locator)
    if rel.is_absolute() or ".." in rel.parts or rel.as_posix() != locator: raise ContractError(f"{label} path must be normalized and local to render evidence")
    root = result_path.parent.resolve(strict=True); cursor = root
    for part in rel.parts:
        cursor /= part
        if stat.S_ISLNK(cursor.lstat().st_mode): raise ContractError(f"{label} path contains a symlink")
    target = cursor.resolve(strict=True)
    if root != target and root not in target.parents or not target.is_file(): raise ContractError(f"{label} path escapes render evidence")
    return target


def stable_read(path: Path, trusted_root: Path, label: str) -> bytes:
    """Descriptor-walk and read one file without following a symlink."""
    root = trusted_root.absolute()
    candidate = path.absolute() if path.is_absolute() else root / path
    def system_alias(value: Path) -> Path:
        return Path("/private")/value.relative_to("/") if len(value.parts)>1 and value.parts[1] in {"var","tmp"} else value
    try: rel = system_alias(candidate).relative_to(system_alias(root))
    except ValueError as exc: raise ContractError(f"{label} escapes trusted root") from exc
    if not rel.parts: raise ContractError(f"{label} must name a file below its trusted root")
    cursor = Path(root.anchor)
    for part in root.parts[1:]:
        cursor /= part
        try: mode = cursor.lstat().st_mode
        except OSError as exc: raise ContractError(f"{label} trusted root is unavailable: {exc}") from exc
        # macOS exposes its private temporary hierarchy through the fixed system
        # aliases /var and /tmp.  Accept only those two OS roots; every
        # application-controlled ancestor remains forbidden.
        if stat.S_ISLNK(mode) and cursor.as_posix() not in {"/var","/tmp"}:
            raise ContractError(f"{label} trusted root contains a symlink")
    if not root.is_dir(): raise ContractError(f"{label} trusted root must be a directory")
    flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fds: list[int] = []
    try:
        current_fd = os.open(root, flags_dir); fds.append(current_fd)
        for component in rel.parts[:-1]:
            current_fd = os.open(component, flags_dir, dir_fd=current_fd); fds.append(current_fd)
        leaf = rel.parts[-1]
        if stat.S_ISLNK(os.stat(leaf,dir_fd=current_fd,follow_symlinks=False).st_mode):
            raise ContractError(f"{label} path contains a symlink")
        fd = os.open(leaf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=current_fd); fds.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode): raise ContractError(f"{label} is not a regular file")
        chunks=[]
        while True:
            chunk=os.read(fd,1024*1024)
            if not chunk:break
            chunks.append(chunk)
        after=os.fstat(fd); current=os.stat(leaf,dir_fd=current_fd,follow_symlinks=False)
        identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
        if (identity(before)!=identity(after) or identity(after)!=identity(current)
                or sum(map(len,chunks))!=before.st_size):
            raise ContractError(f"{label} changed during stable read")
        return b"".join(chunks)
    except OSError as exc:
        raise ContractError(f"{label} secure read failed: {exc}") from exc
    finally:
        for fd in reversed(fds): os.close(fd)


def _safe_run_file(run_root: Path, locator: str, label: str) -> Path:
    rel = PurePosixPath(locator)
    if rel.is_absolute() or ".." in rel.parts or rel.as_posix() != locator:
        raise ContractError(f"{label} path must be normalized and local to the run root")
    root = run_root.resolve(strict=True)
    if run_root.is_symlink() or not run_root.is_dir():
        raise ContractError("trusted run root must be a regular directory")
    cursor = root
    for part in rel.parts:
        cursor /= part
        mode = cursor.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ContractError(f"{label} path contains a symlink")
    target = cursor.resolve(strict=True)
    if root != target and root not in target.parents or not target.is_file():
        raise ContractError(f"{label} path escapes the trusted run root")
    return target


def _safe_asset_path(asset_root: Path, locator: str, label: str) -> Path:
    rel = PurePosixPath(locator)
    if rel.is_absolute() or ".." in rel.parts or rel.as_posix() != locator:
        raise ContractError(f"{label} path must be normalized and local to the asset root")
    root = asset_root.resolve(strict=True)
    cursor = root
    for part in rel.parts:
        cursor /= part
        if stat.S_ISLNK(cursor.lstat().st_mode):
            raise ContractError(f"{label} path contains a symlink")
    target = cursor.resolve(strict=True)
    if root != target and root not in target.parents or not target.is_file():
        raise ContractError(f"{label} path escapes the asset root")
    return target


def _renderer_trust(key_path: Path) -> dict[str, Any]:
    """Load the sole committed production trust root; callers cannot replace it."""
    trust = validate_production_renderer_trust()
    if trust["status"] != "installed":
        raise ContractError("production renderer trust is not-installed; publication fails closed")
    if sha(_key_bytes(key_path)) != trust["issuer_key_sha256"]:
        raise ContractError("renderer issuer key fingerprint differs from trusted pin")
    lock = ROOT / trust["engine"]["lock_path"]
    if sha(stable_read(lock,ROOT,"renderer lock")) != trust["engine"]["lock_sha256"]:
        raise ContractError("trusted renderer lock is absent or differs from its closed pin")
    return trust


def validate_production_renderer_trust() -> dict[str, Any]:
    trust_bytes=stable_read(PRODUCTION_RENDERER_TRUST,ROOT,"renderer trust")
    trust = json.loads(trust_bytes.decode("utf-8"))
    validate_schema(trust, "renderer-trust.v1.schema.json", "production renderer trust")
    lock = ROOT / trust["engine"]["lock_path"]
    lock_bytes=stable_read(lock,ROOT,"renderer lock")
    if sha(lock_bytes) != trust["engine"]["lock_sha256"]:
        raise ContractError("production renderer lock differs from trust contract")
    lock_doc = json.loads(lock_bytes.decode("utf-8"))
    required = {"schema_version", "engine", "implementation_status", "platform", "output_contract", "network_runtime", "capabilities", "wheelhouse", "builtin_fonts", "font_derivatives", "files"}
    if (set(lock_doc) != required or lock_doc["schema_version"] != "pdf-renderer-lock.v1"
            or lock_doc["engine"] != {"name": trust["engine"]["name"], "version": trust["engine"]["version"]}
            or lock_doc["implementation_status"] != "installed" or lock_doc["output_contract"] != "render-result.v1"
            or lock_doc["network_runtime"] != "disabled"
            or set(lock_doc["capabilities"]) != {"graphviz","qpdf","tesseract_deu","svg_converter","verapdf"}
            or any(set(item)!={"status","policy"} or not item["policy"] for item in lock_doc["capabilities"].values())
            or lock_doc["capabilities"]["svg_converter"]["status"] != "builtin-safe-subset"
            or lock_doc["capabilities"]["graphviz"]["status"] != "installed"
            or any(lock_doc["capabilities"][name]["status"] != "not-installed" for name in ("qpdf","tesseract_deu","verapdf"))
            or set(lock_doc["wheelhouse"]) != {"cas_id","inventory","inventory_sha256"}
            or not re.fullmatch(r"[0-9a-f]{64}",lock_doc["wheelhouse"]["cas_id"])
            or lock_doc["wheelhouse"]["inventory"] != "files.sha256.json"
            or not re.fullmatch(r"[0-9a-f]{64}",lock_doc["wheelhouse"]["inventory_sha256"])
            or lock_doc["builtin_fonts"] != {"builtin:Helvetica": "Helvetica", "builtin:Helvetica-Bold": "Helvetica-Bold", "builtin:Courier": "Courier"}
            or not isinstance(lock_doc["font_derivatives"],dict) or not lock_doc["font_derivatives"]):
        raise ContractError("production renderer lock is not the canonical installed implementation")
    for record in lock_doc["files"].values():
        target = ROOT / record["path"]
        if sha(stable_read(target,ROOT,f"renderer implementation {record['path']}")) != record["sha256"]:
            raise ContractError(f"production renderer implementation hash failed: {record['path']}")
    return trust


_PDF_WHITESPACE = b"\x00\t\n\x0c\r "
_PDF_DELIMITERS = b"()<>[]{}/%"
_PDF_FORBIDDEN_CLASSIC_NAMES = {b"ObjStm", b"XRef", b"XRefStm"}


def _pdf_syntax_names(data: bytes) -> set[bytes]:
    """Tokenize PDF object syntax, excluding strings, comments, and stream bytes."""
    names: set[bytes] = set()
    cursor = 0
    last_token: bytes | None = None
    while cursor < len(data):
        byte = data[cursor]
        if byte in _PDF_WHITESPACE:
            cursor += 1
            continue
        if byte == ord("%"):
            line_end = cursor + 1
            while line_end < len(data) and data[line_end] not in b"\r\n":
                line_end += 1
            if line_end == len(data):
                cursor = len(data)
            elif data.startswith(b"\r\n", line_end):
                cursor = line_end + 2
            else:
                cursor = line_end + 1
            continue
        if byte == ord("("):
            cursor += 1
            depth = 1
            while cursor < len(data) and depth:
                byte = data[cursor]
                if byte == ord("\\"):
                    cursor += 1
                    if cursor < len(data) and data[cursor] == ord("\r"):
                        cursor += 1
                        if cursor < len(data) and data[cursor] == ord("\n"):
                            cursor += 1
                    elif cursor < len(data):
                        cursor += 1
                else:
                    if byte == ord("("):
                        depth += 1
                    elif byte == ord(")"):
                        depth -= 1
                    cursor += 1
            if depth:
                raise ContractError("PDF literal string is not closed")
            last_token = b"string"
            continue
        if byte == ord("<") and not data.startswith(b"<<", cursor):
            end = data.find(b">", cursor + 1)
            if end < 0 or re.fullmatch(rb"[0-9A-Fa-f\x00\x09\x0a\x0c\x0d\x20]*", data[cursor + 1:end]) is None:
                raise ContractError("PDF hexadecimal string is malformed")
            cursor = end + 1
            last_token = b"string"
            continue
        if data.startswith(b"<<", cursor) or data.startswith(b">>", cursor):
            last_token = data[cursor:cursor + 2]
            cursor += 2
            continue
        if byte == ord("/"):
            cursor += 1
            decoded = bytearray()
            while cursor < len(data) and data[cursor] not in _PDF_WHITESPACE + _PDF_DELIMITERS:
                if data[cursor] == ord("#"):
                    escaped = data[cursor + 1:cursor + 3]
                    if len(escaped) != 2 or re.fullmatch(rb"[0-9A-Fa-f]{2}", escaped) is None:
                        raise ContractError("PDF name contains a malformed #xx escape")
                    decoded.append(int(escaped, 16))
                    cursor += 3
                else:
                    decoded.append(data[cursor])
                    cursor += 1
            name = bytes(decoded)
            names.add(name)
            last_token = b"/" + name
            continue
        if byte in _PDF_DELIMITERS:
            last_token = bytes((byte,))
            cursor += 1
            continue
        end = cursor
        while end < len(data) and data[end] not in _PDF_WHITESPACE + _PDF_DELIMITERS:
            end += 1
        token = data[cursor:end]
        if token == b"stream":
            if last_token != b">>" or not (data.startswith(b"\n", end) or data.startswith(b"\r\n", end)):
                raise ContractError("PDF stream syntax is not canonical")
            break
        last_token = token
        cursor = end
    return names


def _strict_pdf_pages(data: bytes) -> list[dict[str, Any]]:
    if (not data.startswith(b"%PDF-") or not data.endswith(b"%%EOF")
            or len(re.findall(rb"(?m)^xref$", data)) != 1
            or len(re.findall(rb"(?m)^trailer$", data)) != 1
            or len(re.findall(rb"(?m)^startxref\s*$", data)) != 1
            or len(re.findall(rb"(?m)^%%EOF$", data)) != 1):
        raise ContractError("PDF must have exactly one xref, trailer, startxref and terminal %%EOF")
    start_match = re.search(rb"(?m)^startxref\r?\n([0-9]+)\r?\n%%EOF$", data)
    if start_match is None:
        raise ContractError("PDF startxref/EOF tail is not canonical")
    xref_offset = int(start_match.group(1))
    if xref_offset >= len(data) or not data.startswith(b"xref\n", xref_offset):
        raise ContractError("PDF startxref does not point to the sole xref token")
    revision = data[xref_offset:]
    xref_section = rb"xref\n(?:[0-9]+ [1-9][0-9]*\n(?:[0-9]{10} [0-9]{5} [nf] \n)+)+"
    trailer_section = rb"trailer\n<<(?:[^<>]|<(?!<)|>(?!>))*>>\n"
    tail = rb"startxref\n" + str(xref_offset).encode("ascii") + rb"\n%%EOF"
    if re.fullmatch(xref_section + trailer_section + tail, revision) is None:
        raise ContractError("PDF xref/trailer revision contains non-canonical or extra syntax")
    xref_lines = revision.split(b"trailer\n", 1)[0].splitlines()[1:]
    xref_entries: dict[int, tuple[int, int, bytes]] = {}
    cursor = 0
    while cursor < len(xref_lines):
        header = re.fullmatch(rb"([0-9]+) ([1-9][0-9]*)", xref_lines[cursor])
        if header is None: raise ContractError("PDF xref subsection header is invalid")
        first = int(header.group(1));count = int(header.group(2));cursor += 1
        entries = xref_lines[cursor:cursor + count]
        if len(entries) != count or any(re.fullmatch(rb"[0-9]{10} [0-9]{5} [nf] ", entry) is None for entry in entries):
            raise ContractError("PDF xref subsection count does not match its entries")
        for index, entry in enumerate(entries):
            object_number = first + index
            if object_number in xref_entries:
                raise ContractError("PDF xref object numbers must be unique")
            xref_entries[object_number] = (int(entry[:10]), int(entry[11:16]), entry[17:18])
        cursor += count
    header = re.match(rb"\A%PDF-(?:1\.3|1\.4)\r?\n(?:%[^\r\n]*\r?\n)?", data)
    if header is None:
        raise ContractError("PDF header must declare the classic-xref contract version 1.3 or 1.4")
    in_use = sorted(
        (offset, object_number, generation)
        for object_number, (offset, generation, state) in xref_entries.items()
        if state == b"n"
    )
    if not in_use or len({offset for offset, _number, _generation in in_use}) != len(in_use):
        raise ContractError("PDF xref in-use offsets must be present and unique")
    if re.fullmatch(rb"(?:\r?\n)*", data[header.end():in_use[0][0]]) is None:
        raise ContractError("PDF contains unreferenced bytes before its first xref object")
    syntax_names = _pdf_syntax_names(revision.split(b"trailer\n", 1)[1].split(b"\nstartxref\n", 1)[0])
    for index, (offset, object_number, generation) in enumerate(in_use):
        end = in_use[index + 1][0] if index + 1 < len(in_use) else xref_offset
        object_header = f"{object_number} {generation} obj\n".encode("ascii")
        if offset < header.end() or end <= offset or end > xref_offset or not data.startswith(object_header, offset):
            raise ContractError("PDF xref offset does not point to its exact indirect object")
        interval = data[offset:end]
        object_starts = list(re.finditer(rb"(?m)^([0-9]+) ([0-9]+) obj\r?$", interval))
        object_ends = list(re.finditer(rb"(?m)^endobj\r?$", interval))
        if (len(object_starts) != 1 or object_starts[0].start() != 0
                or len(object_ends) != 1
                or re.fullmatch(rb"(?:\r?\n)*", interval[object_ends[0].end():]) is None):
            raise ContractError("PDF xref interval must contain exactly one closed indirect object")
        syntax_names.update(_pdf_syntax_names(interval[object_starts[0].end():object_ends[0].start()]))
    forbidden_names = sorted(syntax_names & _PDF_FORBIDDEN_CLASSIC_NAMES)
    if forbidden_names:
        rendered = ", ".join("/" + name.decode("latin-1") for name in forbidden_names)
        raise ContractError(f"PDF classic-xref contract forbids object/xref stream name(s): {rendered}")
    pdf_messages: list[str] = []
    class RejectingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            pdf_messages.append(record.getMessage())
    pdf_logger = logging.getLogger("pypdf")
    handler = RejectingHandler(logging.WARNING)
    pdf_logger.addHandler(handler)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            reader = PdfReader(io.BytesIO(data), strict=True)
            if getattr(reader, "xref_objStm", None):
                raise ContractError("PDF classic-xref contract forbids compressed-object references")
            count = len(reader.pages)
            if not isinstance(reader.trailer, dict) or "/Root" not in reader.trailer or count < 1:
                raise ContractError("PDF trailer/catalog/pages are incomplete")
            pages = []
            for page in reader.pages:
                media = page.mediabox;crop = page.cropbox
                left = max(float(media.left), float(crop.left));right = min(float(media.right), float(crop.right))
                bottom = max(float(media.bottom), float(crop.bottom));top = min(float(media.top), float(crop.top))
                if not all(math.isfinite(value) for value in (left, right, bottom, top)) or left >= right or bottom >= top:
                    raise ContractError("PDF effective MediaBox/CropBox is invalid")
                fragments: list[dict[str, Any]] = []
                def visit(text: str, cm: list[float], tm: list[float], _font: Any, _size: float) -> None:
                    if text.strip():
                        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
                        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
                        fragments.append({"text": text, "x": x, "y": y})
                text = page.extract_text(visitor_text=visit)
                if not isinstance(text, str):
                    raise ContractError("pypdf could not extract page text")
                pages.append({"text": text, "fragments": fragments,
                              "left": left, "right": right, "bottom": bottom, "top": top})
    except ContractError: raise
    except Exception as exc: raise ContractError(f"strict pypdf cross-reference/page validation failed: {exc}") from exc
    finally:
        pdf_logger.removeHandler(handler)
    if pdf_messages:
        raise ContractError(f"strict pypdf warning rejected: {pdf_messages[0]}")
    return pages


def _pdf_pagination(pages: list[dict[str, Any]], one_pager: bool) -> dict[str, Any]:
    label_re = re.compile(r"(?<!\w)Seite [1-9][0-9]* von [1-9][0-9]*(?!\w)")
    texts = [str(page["text"]) for page in pages]
    extracted = [label_re.findall(text) for text in texts]
    def visible(page: dict[str, Any], item: dict[str, Any]) -> bool:
        return (float(page["left"]) <= float(item["x"]) <= float(page["right"])
                and float(page["bottom"]) <= float(item["y"]) <= float(page["top"]))
    def meaningful_body(page: dict[str, Any]) -> bool:
        footer_limit = float(page["bottom"]) + (float(page["top"]) - float(page["bottom"])) * .15
        body = [item for item in page["fragments"]
                if re.search(r"\w{2,}", label_re.sub("", str(item["text"])), re.UNICODE)]
        return bool(body) and all(visible(page, item) for item in body) and any(
            float(item["y"]) > footer_limit for item in body
        )
    if one_pager:
        if len(pages) != 1 or any(label_re.search(text) for text in texts):
            raise ContractError("one-pager PDF must contain exactly one page and no Seite label")
        if not meaningful_body(pages[0]):
            raise ContractError("one-pager PDF must contain meaningful body content")
        return {"count": 1, "title": {"index": 1, "label": None}, "content": []}
    if len(pages) < 2 or extracted[0] or not meaningful_body(pages[0]):
        raise ContractError("multi-page PDF must have a meaningful unlabelled title page")
    total = len(texts) - 1
    expected_labels = [f"Seite {number} von {total}" for number in range(1, total + 1)]
    if extracted[1:] != [[label] for label in expected_labels]:
        raise ContractError("PDF content pages must contain exactly the Seite X von Y sequence")
    for page, label in zip(pages[1:], expected_labels):
        if not meaningful_body(page):
            raise ContractError("PDF content pages must contain meaningful non-footer body content")
        label_fragments = [item for item in page["fragments"] if label in re.sub(r"\s+", " ", str(item["text"])).strip()]
        footer_limit = float(page["bottom"]) + (float(page["top"]) - float(page["bottom"])) * .15
        if (len(label_fragments) != 1 or not visible(page, label_fragments[0])
                or float(label_fragments[0]["y"]) > footer_limit):
            raise ContractError("PDF Seite X von Y label must occur exactly once in the footer band")
    return {"count": len(texts), "title": {"index": 1, "label": None},
            "content": [{"index": number + 1, "number": number, "label": label}
                        for number, label in enumerate(expected_labels, 1)]}


def publication_gate(ast: dict[str, Any], render_result: Path | None, baseline: dict[str, Any] | None = None,
                     key_path: Path | None = None, dtype: dict[str, Any] | None = None, asset_root: Path | None = None,
                     humanizer_audit: Path | None = None, brand_manifest: Path | None = None,
                     brand_content_sha256: str | None = None, expected_binding: dict[str, Any] | None = None,
                     run_root: Path | None = None, owner_approval: Path | None = None, owner_key: Path | None = None,
                     owner_key_sha256: str | None = None, project_root: Path | None = None,
                     workflow_lock: Path | None = None, workflow_lock_value: dict[str, Any] | None = None,
                     final_path: Path | None = None, baseline_path: Path | None = None) -> None:
    meta = ast["frontmatter"]
    if meta["status"] != "release": raise ContractError("publication requires frontmatter status release")
    _reject_open_work({"metadata": meta, "rights": ast.get("rights_evidence", {}), "content": ast.get("source", "")})
    if meta["todos"]: raise ContractError("publication is blocked by open TODOs")
    if baseline is None or key_path is None or dtype is None: raise ContractError("publication requires a signed Humanizer baseline, issuer key, and document type")
    if humanizer_audit is not None:
        if final_path is None or baseline_path is None or run_root is None:
            raise ContractError("publication requires exact final/baseline paths below the trusted run root")
        final_bytes=stable_read(final_path,run_root,"final Markdown")
        if sha(final_bytes)!=ast["source_sha256"] or final_bytes.decode("utf-8")!=ast["source"]:
            raise ContractError("final Markdown bytes differ from the validated AST")
        baseline_bytes=stable_read(baseline_path,run_root,"Humanizer baseline")
        try: fresh_baseline=json.loads(baseline_bytes.decode("utf-8"))
        except (UnicodeError,json.JSONDecodeError) as exc:raise ContractError(f"Humanizer baseline is not strict JSON: {exc}") from exc
        if fresh_baseline!=baseline:raise ContractError("Humanizer baseline bytes differ from the supplied baseline")
    baseline_ast = verify_humanizer_baseline(baseline, key_path, asset_root, dtype, brand_manifest, brand_content_sha256)
    verify_humanizer_roundtrip(baseline_ast, ast)
    if (meta["assets"] and not ast.get("rights_evidence")) or any(item["status"] == "todo-review" for item in ast.get("rights_evidence", {}).values()):
        raise ContractError("publication requires resolved rights evidence for every asset")
    if render_result is None: raise ContractError("publication requires an external render-result contract")
    trust = _renderer_trust(key_path)
    if humanizer_audit is None: raise ContractError("publication requires a closed Humanizer audit")
    if run_root is None: raise ContractError("publication requires a trusted run root")
    if owner_approval is None or owner_key is None or owner_key_sha256 is None: raise ContractError("publication requires separate signed owner approval")
    if project_root is None or workflow_lock is None or workflow_lock_value is None:
        raise ContractError("publication requires the exact project and workflow lock for late re-resolution")
    import brand_package as _brand_package
    exact_root=_brand_package.git_root(project_root);target=_brand_package.current_state_target(exact_root)
    fresh_activation,fresh_skill_lock,activation_bytes,skill_lock_bytes=_brand_package.read_state(exact_root,target)
    fresh_brand,fresh_brand_root,fresh_content=_brand_package.validate_bound_state(exact_root,fresh_activation,fresh_skill_lock)
    lock_bytes=stable_read(workflow_lock,exact_root,"workflow lock")
    try:fresh_workflow=json.loads(lock_bytes.decode("utf-8"))
    except (UnicodeError,json.JSONDecodeError) as exc:raise ContractError(f"workflow lock is not strict JSON: {exc}") from exc
    validate_schema(fresh_workflow,"brand-voice-workflow-lock.v2.schema.json","workflow lock")
    if fresh_workflow!=workflow_lock_value or lock_bytes!=json.dumps(fresh_workflow,ensure_ascii=False,sort_keys=True,indent=2).encode("utf-8")+b"\n":raise ContractError("workflow lock changed before publication")
    brand_bytes=stable_read(fresh_brand_root/"brand-package.json",fresh_brand_root,"brand manifest")
    dtype_entry=next((x for x in fresh_brand["document_types"] if x["id"]==dtype["id"] and x["version"]==dtype["version"]),None)
    if not dtype_entry:raise ContractError("late activation no longer selects document type")
    dtype_bytes=stable_read(fresh_brand_root/dtype_entry["contract"],fresh_brand_root,"document type")
    contract_bytes=stable_read(MARKDOWN_CONTRACT_PATH,ROOT,"Markdown contract")
    trust_bytes=stable_read(ROOT/"renderer-trust.v1.json",ROOT,"renderer trust");trust_now=json.loads(trust_bytes.decode("utf-8"))
    renderer_lock_bytes=stable_read(ROOT/trust_now["engine"]["lock_path"],ROOT,"renderer lock")
    humanizer_release=Path.home()/".agents/skill-store/humanizer-de"/fresh_workflow["humanizer"]["version"]
    humanizer_hashes={name:sha(stable_read(humanizer_release/rel,humanizer_release,f"Humanizer {name}")) for name,rel in {"runner":"scripts/humanizer_two_pass.py","skill":"SKILL.md","catalog":"references/patterns.md"}.items()}
    if (sha(activation_bytes)!=fresh_workflow["activation"]["project_sha256"] or sha(skill_lock_bytes)!=fresh_workflow["activation"]["lock_sha256"] or _brand_package.state_identity(activation_bytes,skill_lock_bytes)!=fresh_workflow["activation"]["state_sha256"] or sha(brand_bytes)!=fresh_workflow["brand"]["manifest_sha256"] or fresh_content!=fresh_workflow["brand"]["content_sha256"] or sha(dtype_bytes)!=fresh_workflow["document_type"]["sha256"] or sha(contract_bytes)!=fresh_workflow["markdown_contract"]["sha256"] or sha(trust_bytes)!=fresh_workflow["renderer"]["trust_sha256"] or sha(renderer_lock_bytes)!=fresh_workflow["renderer"]["lock_sha256"] or humanizer_hashes!=fresh_workflow["humanizer"]["tool_hashes"]):raise ContractError("late publication input binding differs from workflow lock")
    approval_bytes=stable_read(owner_approval,owner_approval.parent,"owner approval")
    approval=json.loads(approval_bytes.decode("utf-8"));validate_schema(approval,"owner-approval.v1.schema.json","owner approval")
    approval_signed={k:approval[k] for k in ("schema_version","issuer","payload")}
    if sha(_key_bytes(owner_key))!=owner_key_sha256 or not hmac.compare_digest(approval["hmac_sha256"],hmac.new(_key_bytes(owner_key),canonical_json(approval_signed),hashlib.sha256).hexdigest()):
        raise ContractError("owner approval key/signature failed")
    if approval["payload"]["final_sha256"] != ast["source_sha256"]: raise ContractError("owner approval does not bind exact final Markdown bytes")
    audit_bytes=stable_read(humanizer_audit,run_root,"Humanizer audit")
    audit = json.loads(audit_bytes.decode("utf-8")); validate_schema(audit, "humanizer-audit.v1.schema.json", "Humanizer audit")
    audit_payload = _verify_envelope(audit, "humanizer-audit.v1", key_path)
    if audit_payload["baseline_sha256"] != sha(canonical_json(baseline)) or audit_payload["final_sha256"] != ast["source_sha256"]:
        raise ContractError("Humanizer audit is not bound to baseline/final bytes")
    if not audit_payload["protected_stable"] or not audit_payload["ast_skeleton_stable"]:
        raise ContractError("Humanizer audit did not close protected/structure proof")
    raw = audit_payload["raw_artifacts"]
    raw_paths = [item["path"] for item in raw]
    if len(raw_paths) != len(set(raw_paths)):
        raise ContractError("Humanizer raw evidence has duplicate paths")
    for item in raw:
        target = _safe_run_file(run_root, item["path"], "Humanizer raw evidence")
        if sha(stable_read(target,run_root,"Humanizer raw evidence")) != item["sha256"]:
            raise ContractError("Humanizer raw evidence hash failed")
    baseline_segments = {x["segment_id"]: x for x in baseline_ast["humanizer_segments"]["mutable"]}
    final_segments = {x["segment_id"]: x for x in ast["humanizer_segments"]["mutable"]}
    operations = audit_payload["operations"]
    if len(operations) != len(set(x["segment_id"] for x in operations)) or set(x["segment_id"] for x in operations) != set(baseline_segments):
        raise ContractError("Humanizer operations do not cover every mutable segment exactly once")
    for operation in operations:
        segment_id = operation["segment_id"]
        if operation["old_hash"] != baseline_segments[segment_id]["old_hash"] or operation["replacement_sha256"] != sha(final_segments[segment_id]["text"].encode("utf-8")):
            raise ContractError("Humanizer operation differs from baseline/final mutable bytes")
        if operation["changed"] != (baseline_segments[segment_id]["text"] != final_segments[segment_id]["text"]):
            raise ContractError("Humanizer operation changed flag differs from baseline/final bytes")
        result_file = _safe_run_file(run_root, operation["result_path"], "Humanizer semantic result")
        if stable_read(result_file,run_root,"Humanizer semantic result").decode("utf-8").rstrip("\n") != final_segments[segment_id]["text"]:
            raise ContractError("Humanizer result.md does not equal the transported final mutable segment")
        report_file=_safe_run_file(run_root,operation["report_path"],"Humanizer report")
        report=json.loads(stable_read(report_file,run_root,"Humanizer report").decode("utf-8"));state=operation["provider_state"]
        confirmed_path=report_file.parent/"confirmed-ledger.json";evidence_path=report_file.parent/"evidence-report.json"
        confirmed=json.loads(stable_read(confirmed_path,run_root,"Humanizer confirmed ledger").decode("utf-8"));evidence=json.loads(stable_read(evidence_path,run_root,"Humanizer evidence report").decode("utf-8"))
        candidate_count=report.get("candidate_count");rejected=confirmed.get("discarded_candidates",[])
        rejection_closed=(isinstance(candidate_count,int) and candidate_count>=0 and report.get("discarded_candidate_count")==candidate_count and report.get("confirmed_candidate_count")==0 and len(rejected)==candidate_count and all(isinstance(x,dict) and x.get("id") and x.get("reason") for x in rejected) and evidence.get("ok") is True and not evidence.get("findings"))
        valid_no=(state=="audit-only/no-change" and not operation["changed"] and report.get("model_calls")==1 and report.get("edit_count")==0 and report.get("prompt_sha256",{}).get("rewrite") is None and rejection_closed)
        valid_edit=(state=="audit+rewrite/changed" and operation["changed"] and report.get("model_calls")==2 and isinstance(report.get("edit_count"),int) and report["edit_count"]>=1 and report.get("prompt_sha256",{}).get("rewrite") is not None)
        if not (valid_no or valid_edit):raise ContractError("Humanizer report differs from closed provider state")
    result_bytes=stable_read(render_result,render_result.parent,"render result")
    result = json.loads(result_bytes.decode("utf-8"))
    validate_schema(result, "render-result.v1.schema.json", "render-result")
    payload = _verify_envelope(result, "render-result.v1", key_path)
    if result["issuer"] != trust["issuer"]: raise ContractError("render result issuer differs from renderer trust pin")
    if payload["engine"] != trust["engine"]: raise ContractError("render engine identity/lock differs from renderer trust pin")
    _reject_open_work(payload, "render-result.payload")
    if payload["final_input_sha256"] != ast["source_sha256"] or payload["exit"]["code"] != 0 or payload["exit"]["errors"] or payload["exit"]["warnings"]:
        raise ContractError("render-result does not prove a clean render of the exact final input")
    registry=validate_figure_registry(ast);reference_counts={item["id"]:0 for item in registry}
    def count_figure_refs(nodes:list[dict[str,Any]])->None:
        for node in nodes:
            groups=([node.get("inline",[])] if "inline" in node else [])+[item.get("inline",[]) for item in node.get("items",[])]
            for tokens in groups:
                for token in tokens:
                    if token.get("type")=="figure-ref":reference_counts[token["target"]]+=1
            count_figure_refs(node.get("children",[]))
            for list_item in node.get("items",[]):count_figure_refs(list_item.get("children",[]))
    count_figure_refs(ast["children"]);nodes_by_id={node.get("id"):node for node in ast["children"]}
    expected_figures=[]
    for item in registry:
        node=nodes_by_id[item["id"]];caption_text=f'Abbildung {item["number"]}: {item["caption"]} — {item["description"]}'
        if node.get("source_id"):caption_text+=f' — Quelle: {node["source_id"]}'
        expected_figures.append({**item,"caption_text":caption_text,"reference_text":f'Abbildung {item["number"]}',"reference_count":reference_counts[item["id"]]})
    if payload["figures"]!=expected_figures:raise ContractError("render-result figure registry/references differ from final AST")
    if payload["binding"]["humanizer_audit_sha256"] != sha(audit_bytes):
        raise ContractError("render-result Humanizer audit binding failed")
    reconstructed_binding={"project_identity_sha256":sha(str(exact_root.resolve()).encode()),"activation_sha256":_brand_package.state_identity(activation_bytes,skill_lock_bytes),"brand":dict(fresh_workflow["brand"]),"document_type":dict(fresh_workflow["document_type"]),"markdown_contract_sha256":sha(contract_bytes),"humanizer_audit_sha256":sha(audit_bytes),"owner_approval_sha256":sha(approval_bytes)}
    if expected_binding is not None and expected_binding!=reconstructed_binding:raise ContractError("caller binding differs from independently reconstructed publication binding")
    if payload["binding"] != reconstructed_binding:
        raise ContractError("render-result project/brand/document binding differs from activation")
    if payload["binding"]["owner_approval_sha256"] != sha(approval_bytes): raise ContractError("render-result owner approval binding failed")
    qa_file = _safe_result_file(render_result, payload["visual_qa"]["path"], "visual QA")
    qa_bytes=stable_read(qa_file,render_result.parent,"visual QA")
    if sha(qa_bytes) != payload["visual_qa"]["sha256"]:
        raise ContractError("render-result visual QA hash failed")
    qa = json.loads(qa_bytes.decode("utf-8"))
    if qa.get("status") != "pass" or qa.get("dpi") != 144:
        raise ContractError("render-result visual QA is not a measured pass")
    expected_pngs = [(p["index"], p["sha256"]) for p in payload["visual_qa"]["page_pngs"]]
    actual_pngs = [(p["index"], p["png_sha256"]) for p in qa.get("pages", [])]
    if expected_pngs != actual_pngs:
        raise ContractError("render-result page PNG hashes differ from visual QA")
    qa_png_paths = [(p["index"], p["png_path"]) for p in qa.get("pages", [])]
    result_png_paths = [(p["index"], p["path"]) for p in payload["visual_qa"]["page_pngs"]]
    if qa_png_paths != result_png_paths or len(result_png_paths) != len(set(result_png_paths)):
        raise ContractError("render-result page PNG paths differ from visual QA or are duplicated")
    for item in payload["visual_qa"]["page_pngs"]:
        png = _safe_result_file(render_result, item["path"], "page PNG")
        if sha(stable_read(png,render_result.parent,"page PNG")) != item["sha256"]:
            raise ContractError("render-result page PNG bytes differ from authenticated hash")
    pdf = _safe_result_file(render_result, payload["output_pdf"]["path"], "output PDF"); pdf_data = stable_read(pdf,render_result.parent,"output PDF")
    if sha(pdf_data) != payload["output_pdf"]["sha256"]: raise ContractError("render-result output PDF hash failed")
    page_texts = _strict_pdf_pages(pdf_data)
    expected_assets = sorted((item["id"], item["path"], item["media_type"], item["sha256"]) for item in meta["assets"])
    actual_assets = sorted((item["id"], item["path"], item["media_type"], item["sha256"]) for item in payload["assets"])
    if actual_assets != expected_assets: raise ContractError("render-result asset inventory differs from final Markdown")
    if len({item["id"] for item in payload["assets"]}) != len(payload["assets"]):
        raise ContractError("render-result asset inventory has duplicate IDs")
    if asset_root is None: raise ContractError("publication requires a trusted asset root")
    for item in payload["assets"]:
        asset = _safe_asset_path(asset_root, item["path"], f"publication asset {item['id']}")
        if sha(stable_read(asset,asset_root,f"publication asset {item['id']}")) != item["sha256"]:
            raise ContractError("publication source asset bytes differ from authenticated hash")
    expected_brand_assets=[]
    for family in ("fonts","logos"):
        for item in fresh_brand["assets"][family]:
            expected_brand_assets.append((item["id"],item["path"],item["media_type"],item["sha256"],item["rights"]["evidence_path"],item["rights"]["evidence_sha256"]))
    actual_brand_assets=[(x["id"],x["path"],x["media_type"],x["sha256"],x["rights_path"],x["rights_sha256"]) for x in payload["brand_assets"]]
    if sorted(actual_brand_assets)!=sorted(expected_brand_assets) or len({x[0] for x in actual_brand_assets})!=len(actual_brand_assets):raise ContractError("render-result brand asset/rights inventory differs from activated package")
    for item in payload["brand_assets"]:
        source=_safe_asset_path(fresh_brand_root,item["path"],f"brand asset {item['id']}")
        rights=_safe_asset_path(fresh_brand_root,item["rights_path"],f"brand rights {item['id']}")
        if sha(stable_read(source,fresh_brand_root,f"brand asset {item['id']}"))!=item["sha256"] or sha(stable_read(rights,fresh_brand_root,f"brand rights {item['id']}"))!=item["rights_sha256"]:raise ContractError("activated brand asset/rights bytes differ from render inventory")
    if len({item["id"] for item in payload["mermaid"]}) != len(payload["mermaid"]):
        raise ContractError("render-result Mermaid inventory has duplicate IDs")
    for item in payload["mermaid"]:
        for path_key, hash_key in (("source_path", "source_sha256"), ("svg_path", "svg_sha256"), ("raster_path", "raster_sha256")):
            generated = _safe_result_file(render_result, item[path_key], f"Mermaid {path_key}")
            if sha(stable_read(generated,render_result.parent,f"Mermaid {path_key}")) != item[hash_key]:
                raise ContractError(f"render-result Mermaid {path_key} bytes differ from authenticated hash")
    if len({item["id"] for item in payload["graphviz"]}) != len(payload["graphviz"]):
        raise ContractError("render-result Graphviz inventory has duplicate IDs")
    for item in payload["graphviz"]:
        for path_key,hash_key in (("source_path","source_sha256"),("svg_path","svg_sha256")):
            generated=_safe_result_file(render_result,item[path_key],f"Graphviz {path_key}")
            if sha(stable_read(generated,render_result.parent,f"Graphviz {path_key}"))!=item[hash_key]:
                raise ContractError(f"render-result Graphviz {path_key} bytes differ from authenticated hash")
    import pdf_visual_qa as _pdf_visual_qa
    source_contract=qa.get("source_map_contract")
    page_geometry=[{"number":i+1,"width":float(page["right"]-page["left"]),"height":float(page["top"]-page["bottom"])} for i,page in enumerate(page_texts)]
    source_objects=_pdf_visual_qa.validate_source_map(source_contract,page_geometry)
    if qa.get("source_map")!=source_objects:
        raise ContractError("visual QA source-map geometry/domain differs from the signed renderer map")
    extracted_pages = _pdf_pagination(page_texts, dtype["id"] == "one-pager" or dtype.get("extends") == "one-pager")
    if payload["pages"] != extracted_pages:
        raise ContractError("authenticated pagination evidence differs from exact PDF text extraction")


def run_validate(path: Path, dtype_path: Path | None, asset_root: Path | None, publication: bool, render_result: Path | None, humanizer_baseline: Path | None = None, baseline_key: Path | None = None) -> dict[str, Any]:
    source = path.read_bytes().decode("utf-8"); parser = Parser(source, asset_root); ast = parser.parse(); parser.validate_assets(); ast["rights_evidence"] = parser.rights_evidence
    bases = load_base_types(); dtype = load_json(dtype_path) if dtype_path else bases.get(ast["frontmatter"]["document_type"])
    if dtype is None: raise ContractError("custom document type requires --document-type")
    validate_document_type(dtype, bases); validate_ast_document(ast, dtype)
    if serialize_ast(ast) != source: raise ContractError("AST roundtrip changed source bytes")
    if publication:
        if humanizer_baseline is None or baseline_key is None: raise ContractError("publication requires --humanizer-baseline and --issuer-key")
        publication_gate(ast, render_result, load_json(humanizer_baseline), baseline_key, dtype, asset_root)
    return ast


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate"); validate.add_argument("document", type=Path); validate.add_argument("--document-type", type=Path); validate.add_argument("--asset-root", type=Path); validate.add_argument("--publication", action="store_true"); validate.add_argument("--render-result", type=Path); validate.add_argument("--humanizer-baseline", type=Path); validate.add_argument("--issuer-key", type=Path); validate.add_argument("--ast-json", action="store_true")
    sub.add_parser("validate-contracts")
    args = parser.parse_args(argv)
    try:
        validate_schema(load_json(ROOT / "markdown-contract.v1.json"), "markdown-contract.v1.schema.json", "Markdown contract v1")
        validate_schema(load_json(MARKDOWN_CONTRACT_PATH), MARKDOWN_CONTRACT_SCHEMA, "Markdown contract v2")
        if args.command == "validate-contracts":
            bases = load_base_types()
            for doc in bases.values(): validate_document_type(doc, bases)
            validate_production_renderer_trust()
            validate_mermaid_toolchain()
            print("Markdown and document-type contracts passed."); return 0
        ast = run_validate(args.document, args.document_type, args.asset_root, args.publication, args.render_result, args.humanizer_baseline, args.issuer_key)
        if args.ast_json: print(json.dumps(ast, ensure_ascii=False, indent=2, sort_keys=True))
        else: print(f"Markdown contract passed: {args.document} sha256={ast['source_sha256']}")
        return 0
    except (ContractError, OSError, UnicodeError) as exc:
        print(f"Markdown contract failed: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
