#!/usr/bin/env python3
"""Markdown contract v3: additive extension of the pinned v2 validator.

v3 adds two elements and changes nothing else:

* Code fences (```` ```lang ```` ... ```` ``` ````) at document level. The
  content is kept byte-exact and is never interpreted.
* Strong emphasis (``**text**``) in paragraphs and list items.

The v2 validator (scripts/markdown_contract.py) is hash-pinned by the renderer
lock, the skill release and embedded copies. This module therefore imports it
unchanged and extends its Parser. Every v2 document keeps the same AST apart
from the ``contract`` label. Publication (renderer trust, humanizer gates)
stays bound to v2; v3 documents are validated, not published, by this path.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import markdown_contract as mc  # noqa: E402

MARKDOWN_CONTRACT_VERSION = "markdown-contract.v3"
CONTRACT_PATH = mc.ROOT / "markdown-contract.v3.json"
CONTRACT_SCHEMA = "markdown-contract.v3.schema.json"

FENCE_OPEN_RE = re.compile(r"```(?P<language>[a-z0-9][a-z0-9+#._-]{0,23})?")
STRONG_RE = re.compile(r"\*\*(?=[^\s*])(?P<inner>[^*\r\n]*?[^\s*])\*\*")


def load_contract() -> dict[str, Any]:
    contract = mc.load_json(CONTRACT_PATH)
    mc.validate_schema(contract, CONTRACT_SCHEMA, "Markdown contract v3")
    base = mc.ROOT / contract["extends"]["path"]
    if mc.sha(base.read_bytes()) != contract["extends"]["sha256"]:
        raise mc.ContractError("Markdown contract v3 does not bind the pinned v2 contract")
    return contract


class Parser(mc.Parser):
    """v2 parser plus code fences and strong emphasis."""

    def blocks(self, start: int, end: int, stop_directive: bool = False, container: str = "document") -> tuple[list[dict[str, Any]], int]:
        if container != "document" or stop_directive:
            return super().blocks(start, end, stop_directive, container)
        fences = self._code_fences(start, end)
        # Hide the fence lines from the v2 parser: it sees blank lines, which end
        # paragraphs, lists and tables exactly like a block boundary does.
        saved = {k: self.lines[k] for open_, close in fences for k in range(open_, close + 1)}
        try:
            for k in saved:
                self.lines[k] = "\n"
            nodes, i = super().blocks(start, end, stop_directive, container)
        finally:
            for k, line in saved.items():
                self.lines[k] = line
        return self._insert_code(nodes, fences), i

    def _code_fences(self, start: int, end: int) -> list[tuple[int, int]]:
        fences: list[tuple[int, int]] = []; i = start
        while i < end:
            line = self.lines[i].rstrip("\n")
            if line.startswith("```{"):
                close = next((j for j in range(i + 1, end) if self.lines[j].rstrip("\n") == "```"), None)
                i = end if close is None else close + 1; continue
            if line.startswith(":::") and line != "::: page-break":
                close = next((j for j in range(i + 1, end) if self.lines[j].rstrip("\n") == ":::"), None)
                i = end if close is None else close + 1; continue
            if FENCE_OPEN_RE.fullmatch(line):
                close = next((j for j in range(i + 1, end) if self.lines[j].rstrip("\n") == "```"), None)
                if close is None: raise mc.ContractError(f"line {i+1}: code fence is not closed")
                if not "".join(self.lines[i + 1:close]).strip(): raise mc.ContractError(f"line {i+1}: code fence must have non-empty content")
                if any(self.lines[k].startswith("```") for k in range(i + 1, close)):
                    raise mc.ContractError(f"line {i+1}: code fence is not closed before the next fence line")
                fences.append((i, close)); i = close + 1; continue
            i += 1
        return fences

    def _insert_code(self, nodes: list[dict[str, Any]], fences: list[tuple[int, int]]) -> list[dict[str, Any]]:
        by_line = {}
        for number, (open_, close) in enumerate(fences, 1):
            ident = f"code-{number}"
            language = FENCE_OPEN_RE.fullmatch(self.lines[open_].rstrip("\n")).group("language")
            body = "".join(self.lines[open_ + 1:close])
            self._record("directive", ident, self.lines[open_], self.offsets[open_])
            self._record("code", ident, body, self.offsets[open_ + 1])
            self._record("directive", ident, self.lines[close], self.offsets[close])
            by_line[open_ + 1] = (close + 1, {"type": "code", "language": language, "text": body, "line": open_ + 1})
        out: list[dict[str, Any]] = []; skip_until = 0
        for node in nodes:
            line = node.get("line", 0)
            if line in by_line and node["type"] == "blank":
                skip_until, code = by_line.pop(line); out.append(code); continue
            if node["type"] == "blank" and line <= skip_until: continue
            out.append(node)
        if by_line: raise mc.ContractError("internal error: code fence lost during parsing")
        return out

    def inline(self, text: str, char_start: int, *, mutable_context: bool = True) -> list[dict[str, Any]]:
        if not mutable_context or "**" not in text:
            return super().inline(text, char_start, mutable_context=mutable_context)
        guarded = [(m.start(), m.end()) for m in mc.PROTECTED_RE.finditer(text)]
        links = [(m.start(), m.end()) for m in mc.LINK_RE.finditer(text)]
        def crosses(a: int, b: int) -> bool:
            return any((x < a < y) or (x < b < y) for x, y in guarded) or any(x <= a and b <= y for x, y in links) or any(a < x < b or a < y < b for x, y in links)
        result: list[dict[str, Any]] = []; cursor = 0
        for match in STRONG_RE.finditer(text):
            if crosses(match.start(), match.end()): continue
            if match.start() > cursor:
                result.extend(super().inline(text[cursor:match.start()], char_start + cursor, mutable_context=mutable_context))
            children = super().inline(match.group("inner"), char_start + match.start("inner"), mutable_context=mutable_context)
            result.append({"type": "strong", "children": children}); cursor = match.end()
        if cursor < len(text):
            result.extend(super().inline(text[cursor:], char_start + cursor, mutable_context=mutable_context))
        return result

    def parse(self) -> dict[str, Any]:
        ast = super().parse(); ast["contract"] = MARKDOWN_CONTRACT_VERSION
        return ast


def _v2_view(ast: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """AST copy the v2 checks understand: code -> verbatim callout, strong -> its children."""
    used: set[str] = set()
    def flat(tokens: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for token in tokens:
            if token["type"] == "strong": used.add("strong"); out.extend(flat(token["children"]))
            else: out.append(token)
        return out
    def node(value: dict[str, Any]) -> dict[str, Any]:
        if value["type"] == "code":
            used.add("code")
            return {"type": "callout", "kind": "verbatim", "attrs": {}, "children": [{"type": "verbatim-text", "text": value["text"]}], "line": value["line"]}
        value = dict(value)
        if "inline" in value: value["inline"] = flat(value["inline"])
        if "children" in value: value["children"] = [node(child) for child in value["children"]]
        if "items" in value: value["items"] = [{**item, "inline": flat(item.get("inline", [])), "children": [node(c) for c in item.get("children", [])]} for item in value["items"]]
        return value
    view = copy.copy(ast); view["children"] = [node(child) for child in ast["children"]]
    return view, used


def validate_ast_document(ast: dict[str, Any], dtype: dict[str, Any]) -> None:
    view, used = _v2_view(ast)
    if "code" in used and "callout" not in dtype["allowed_elements"]:
        raise mc.ContractError("document type does not admit code fences (requires callout)")
    if "strong" in used and "paragraph" not in dtype["allowed_elements"]:
        raise mc.ContractError("document type does not admit strong emphasis (requires paragraph)")
    mc.validate_ast_document(view, dtype)


def run_validate(path: Path, dtype_path: Path | None, asset_root: Path | None, publication: bool, render_result: Path | None, *_: Any) -> dict[str, Any]:
    if publication or render_result is not None:
        raise mc.ContractError("markdown-contract.v3 is not admitted to publication; publication stays bound to v2")
    load_contract()
    source = path.read_bytes().decode("utf-8"); parser = Parser(source, asset_root); ast = parser.parse(); parser.validate_assets(); ast["rights_evidence"] = parser.rights_evidence
    bases = mc.load_base_types(); dtype = mc.load_json(dtype_path) if dtype_path else bases.get(ast["frontmatter"]["document_type"])
    if dtype is None: raise mc.ContractError("custom document type requires --document-type")
    mc.validate_document_type(dtype, bases); validate_ast_document(ast, dtype)
    if mc.serialize_ast(ast) != source: raise mc.ContractError("AST roundtrip changed source bytes")
    return ast


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("document", type=Path); parser.add_argument("--document-type", type=Path); parser.add_argument("--asset-root", type=Path)
    parser.add_argument("--ast-json", action="store_true")
    args = parser.parse_args(argv)
    try:
        ast = run_validate(args.document, args.document_type, args.asset_root, False, None)
    except (mc.ContractError, OSError, UnicodeError) as exc:
        print(f"Markdown contract v3 failed: {exc}", file=sys.stderr); return 1
    if args.ast_json: print(json.dumps(ast, ensure_ascii=False, indent=2, sort_keys=True))
    else: print(f"Markdown contract v3 passed: {args.document} sha256={ast['source_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
