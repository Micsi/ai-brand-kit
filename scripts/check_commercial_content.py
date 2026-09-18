#!/usr/bin/env python3
"""Reject advertising residue and hard-wired external tool references."""

from __future__ import annotations

import re
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()

# Pieces keep the checker from matching its own policy vocabulary. Each tuple
# is one prohibited form split into semantic words. Separators are deliberately
# flexible so spelling a provider with a space, hyphen or no separator cannot
# evade the check. The list remains finite: expand it only for concrete residue.
FORBIDDEN_FORMS = [
    ("higgs" + "field", ("higgs", "field"), False),
    ("affili" + "ate", ("affili" + "ate",), False),
    ("refer" + "ral", ("refer" + "ral",), False),
    ("provi" + "sion", ("provi" + "sion",), False),
    ("commis" + "sion", ("commis" + "sion",), False),
    ("gut" + "haben", ("gut" + "haben",), False),
    ("cred" + "its", ("cred" + "its",), False),
    ("a" + "bo", ("a" + "bo",), False),
    ("subscri" + "ption", ("subscri" + "ption",), False),
    ("ar" + "nie936", ("ar" + "nie936",), False),
    ("ai mit ar" + "nie", ("ai", "mit", "ar" + "nie"), False),
    ("grill-with-" + "docs", ("grill", "with", "docs"), False),
    ("un" + "slop", ("un" + "slop",), False),
    ("seo-" + "audit", ("seo", "audit"), False),
    ("web-design-" + "guidelines", ("web", "design", "guidelines"), False),
    ("goo" + "gle font", ("goo" + "gle", "font"), True),
    ("clau" + "de.md", ("clau" + "de", "md"), False),
    ("au" + "th log" + "in", ("au" + "th", "log" + "in"), False),
    ("acc" + "ount status", ("acc" + "ount", "status"), False),
]

URL_RE = re.compile(r"https?://[^\s)>\]}'\"]+", re.IGNORECASE)
ALLOWED_URLS = {
    "http" + "://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia",
    "https" + "://json-schema.org/draft/2020-12/schema",
}
CLAUDE_BRIDGE_FILES = {
    "brandkit-skill-contract.v1.json",
    "fixtures/brandkit-skills/dry-runs.v1.json",
    "schemas/brandkit-skill-request.v1.schema.json",
    "scripts/materialize_brandkit_instructions.py",
    "scripts/test_check_commercial_content.py",
    "scripts/validate_brandkit_skills.py",
    "skills/brandkit-build/SKILL.md",
    "skills/brandkit-build/assets/AGENTS.md.template",
    "tests/test_brandkit_skills.py",
}
GRILL_DISCOVERY_FILES = {
    "README.md",
    "brandkit-skill-contract.v1.json",
    "docs/brandkit-skills-v1.md",
    "scripts/brandkit_discovery.py",
    "scripts/validate_brandkit_skills.py",
    "schemas/brandkit-skill-contract.v1.schema.json",
    "skill-contract-release.v1.json",
    "skills/brandkit-interview/SKILL.md",
    "skills/brandkit-interview/references/grill-with-docs-handoff.md",
    "skills/brandkit-setup/SKILL.md",
    "skills/brandkit-setup/references/workflow.md",
    "tests/test_brandkit_skills.py",
}
ARTIFACT_LOCK = "toolchain/humanizer-de/requirements-precise.lock.txt"
TOOLCHAIN_LOCK = "toolchain/humanizer-de/toolchain.lock.json"
MERMAID_PACKAGE_LOCK = "toolchain/mermaid-svg/package-lock.json"
MERMAID_TOOLCHAIN_LOCK = "toolchain/mermaid-svg.v1.lock.json"
MERMAID_TOOLCHAIN_SCHEMA = "schemas/mermaid-svg-toolchain.v1.schema.json"
MERMAID_RENDER_SCRIPTS = {"toolchain/mermaid-svg/render-offline", "toolchain/mermaid-svg/install-offline"}
NEGATIVE_FIXTURE = "fixtures/markdown/invalid-cases.v1.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
COMMERCIAL_WHEEL_URL = (
    "https" + "://github.com/explosion/spacy-models/releases/download/"
    "de_core_news_sm-3.8.0/de_core_news_sm-3.8.0-py3-none-any.whl"
)
COMMERCIAL_REQUIREMENT_RE = re.compile(
    r"de_core_news_sm @ (?P<url>" + re.escape(COMMERCIAL_WHEEL_URL) + r") \\\r?\n"
    r"[ \t]+--hash=sha256:[0-9a-f]{64}$",
)
SVG_NAMESPACE_URLS={
    "http"+"://www.w3.org/2000/svg",
    "http"+"://www.w3.org/1999/xlink",
    "http"+"://www.w3.org/1999/xhtml",
}


def logical_requirement_entries(text: str) -> list[tuple[int, str]]:
    """Return physical text for each closed backslash-continued entry."""
    entries: list[tuple[int, str]] = []
    start = 0
    entry_start = 0
    for line in text.splitlines(keepends=True):
        start += len(line)
        physical = line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith("\n") else line
        if physical.endswith("\\"):
            continue
        entries.append((entry_start, text[entry_start:start]))
        entry_start = start
    if entry_start < len(text):
        entries.append((entry_start, text[entry_start:]))
    return entries


def commercial_requirement_entries(text: str) -> list[tuple[int, re.Match[str]]]:
    """Recognize only a whole, unambiguous logical requirement entry."""
    result: list[tuple[int, re.Match[str]]] = []
    for start, entry in logical_requirement_entries(text):
        if entry.endswith("\r\n"):
            entry = entry[:-2]
        elif entry.endswith("\n"):
            entry = entry[:-1]
        match = COMMERCIAL_REQUIREMENT_RE.fullmatch(entry)
        if match:
            result.append((start, match))
    return result


def static_svg_namespace_allowed(text:str,source:str,url:str,position:int)->bool:
    """Allow only exact W3C namespace declaration attribute values in SVG."""
    if not source.lower().endswith(".svg") or url not in SVG_NAMESPACE_URLS:return False
    declaration=re.compile(r'''(?<![\w:-])xmlns(?:\:[A-Za-z_][\w.-]*)?\s*=\s*(["'])(?P<url>[^"']+)\1''')
    for match in declaration.finditer(text):
        if match.start("url")!=position or match.group("url")!=url:continue
        opener=text.rfind("<",0,match.start());closer=text.rfind(">",0,match.start())
        if opener>closer and not text.startswith(("<!--","<?","<!"),opener):return True
    return False


def strict_json(text: str) -> object | None:
    """Parse JSON while rejecting duplicate keys that could hide scanned fields."""
    def closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=closed_object)
    except (json.JSONDecodeError, ValueError):
        return None


def json_string_spans(text: str) -> tuple[object, list[tuple[int, int, tuple[object, ...], str]]] | None:
    """Parse strict JSON and retain each value string's exact raw span and pointer."""
    spans: list[tuple[int, int, tuple[object, ...], str]] = []
    decoder = json.JSONDecoder(parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    size = len(text)
    def ws(index: int) -> int:
        while index < size and text[index] in " \t\r\n": index += 1
        return index
    def string(index: int) -> tuple[str, int, int]:
        if index >= size or text[index] != '"': raise ValueError("expected JSON string")
        value, end = json.decoder.scanstring(text, index + 1, True)
        return value, index, end
    def value(index: int, pointer: tuple[object, ...]) -> tuple[object, int]:
        index = ws(index)
        if index >= size: raise ValueError("missing JSON value")
        if text[index] == '"':
            decoded, start, end = string(index);spans.append((start, end, pointer, decoded));return decoded, end
        if text[index] == '{':
            result: dict[str, object] = {};index=ws(index+1)
            if index < size and text[index] == '}': return result,index+1
            while True:
                key, _start, index = string(index)
                if key in result: raise ValueError("duplicate JSON key")
                index=ws(index)
                if index >= size or text[index] != ':': raise ValueError("missing colon")
                item,index=value(index+1,pointer+(key,));result[key]=item;index=ws(index)
                if index < size and text[index] == '}': return result,index+1
                if index >= size or text[index] != ',': raise ValueError("missing object comma")
                index=ws(index+1)
        if text[index] == '[':
            result_list: list[object] = [];index=ws(index+1)
            if index < size and text[index] == ']': return result_list,index+1
            while True:
                item,index=value(index,pointer+(len(result_list),));result_list.append(item);index=ws(index)
                if index < size and text[index] == ']': return result_list,index+1
                if index >= size or text[index] != ',': raise ValueError("missing array comma")
                index=ws(index+1)
        parsed,end=decoder.raw_decode(text,index)
        if isinstance(parsed,(dict,list,str)) or end > size: raise ValueError("invalid scalar")
        return parsed,end
    try:
        root,end=value(0,());end=ws(end)
        if end != size: raise ValueError("trailing JSON")
        return root,spans
    except (ValueError, json.JSONDecodeError):
        return None


def raw_url_offsets(text: str, url: str) -> list[int]:
    return [match.start() for match in URL_RE.finditer(text) if match.group().rstrip(".,") == url]


def json_field_url_is_exact(text: str, url: str, offset: int, structured_values: list[str]) -> bool:
    """Bind every raw URL occurrence one-for-one to reviewed structured values."""
    offsets = raw_url_offsets(text, url)
    return offset in offsets and len(offsets) == sum(value.count(url) for value in structured_values)


def structured_json_url_allowed(source: str, root: object, pointer: tuple[object, ...], value: str, url: str) -> bool:
    """Authorize a decoded URL only at its exact reviewed JSON pointer."""
    if source == TOOLCHAIN_LOCK:
        skill = root.get("skill") if isinstance(root, dict) else None
        return bool(pointer == ("skill", "repository") and value == url and isinstance(skill, dict)
                    and re.fullmatch(r"https://github\.com/[^/]+/[^/]+\.git", url)
                    and COMMIT_RE.fullmatch(str(skill.get("source_revision", "")))
                    and SHA256_RE.fullmatch(str(skill.get("git_archive_sha256", "")))
                    and SHA256_RE.fullmatch(str(skill.get("content_sha256", ""))))
    if source == MERMAID_PACKAGE_LOCK:
        packages = root.get("packages") if isinstance(root, dict) else None
        if not (len(pointer) == 3 and pointer[0] == "packages" and pointer[2] == "resolved" and isinstance(pointer[1], str)
                and isinstance(packages, dict)): return False
        entry = packages.get(pointer[1])
        return bool(isinstance(entry, dict) and entry.get("resolved") == value == url
                    and re.fullmatch(r"https://registry\.npmjs\.org/.+/-/.+\.tgz", url)
                    and re.fullmatch(r"sha512-[A-Za-z0-9+/]+={0,2}", str(entry.get("integrity", ""))))
    if source == MERMAID_TOOLCHAIN_LOCK:
        families={
            "browser":({"name","version","download_url","archive_path","archive_sha256","executable_relative_path","executable_sha256"},"131.0.6778.204","https"+"://storage.googleapis.com/chrome-for-testing-public/131.0.6778.204/mac-arm64/chrome-mac-arm64.zip"),
            "node":({"version","npm_version","download_url","archive_path","archive_sha256","executable_relative_path","executable_sha256","npm_cli_relative_path","npm_cli_sha256"},"22.18.0","https"+"://nodejs.org/dist/v22.18.0/node-v22.18.0-darwin-arm64.tar.gz"),
        }
        if len(pointer)!=2 or pointer[1]!="download_url" or pointer[0] not in families or not isinstance(root,dict):return False
        keys,version,expected=families[str(pointer[0])];entry=root.get(pointer[0])
        return bool(isinstance(entry,dict) and set(entry)==keys and entry.get("version")==version and value==url==expected
                    and SHA256_RE.fullmatch(str(entry.get("archive_sha256","")))
                    and SHA256_RE.fullmatch(str(entry.get("executable_sha256",""))))
    if source == MERMAID_TOOLCHAIN_SCHEMA:
        families={
            "browser":(["name","version","download_url","archive_path","archive_sha256","executable_relative_path","executable_sha256"],"131.0.6778.204","https"+"://storage.googleapis.com/chrome-for-testing-public/131.0.6778.204/mac-arm64/chrome-mac-arm64.zip"),
            "node":(["version","npm_version","download_url","archive_path","archive_sha256","executable_relative_path","executable_sha256","npm_cli_relative_path","npm_cli_sha256"],"22.18.0","https"+"://nodejs.org/dist/v22.18.0/node-v22.18.0-darwin-arm64.tar.gz"),
        }
        if len(pointer)!=5 or pointer[:1]!=("properties",) or pointer[2:5]!=("properties","download_url","const") or pointer[1] not in families or not isinstance(root,dict):return False
        required,version,expected=families[str(pointer[1])]
        properties=root.get("properties",{});family=properties.get(pointer[1]) if isinstance(properties,dict) else None;fields=family.get("properties") if isinstance(family,dict) else None
        return bool(isinstance(fields,dict) and family.get("additionalProperties") is False and family.get("required")==required
                    and set(fields)==set(required) and fields.get("version",{}).get("const")==version
                    and fields.get("download_url",{}).get("const")==value==url==expected
                    and fields.get("archive_sha256")=={"$ref":"#/$defs/hash"}
                    and fields.get("executable_sha256")=={"$ref":"#/$defs/hash"})
    if source == NEGATIVE_FIXTURE:
        cases = root.get("cases") if isinstance(root, dict) else None
        if not (len(pointer) == 3 and pointer[0] == "cases" and isinstance(pointer[1], int)
                and pointer[2] == "replacement" and isinstance(cases, list) and pointer[1] < len(cases)): return False
        case = cases[pointer[1]]
        return bool(isinstance(case, dict) and case.get("replacement") == value and url in value
                    and str(case.get("id", "")).startswith("mermaid-") and "Mermaid" in str(case.get("error", "")))
    return False


def structured_lock_url_allowed(text: str, source: str, url: str, offset: int) -> bool:
    """Allow only the two reviewed immutable URL forms in their named locks."""
    if source == ARTIFACT_LOCK:
        entries = commercial_requirement_entries(text)
        installer = ROOT / "toolchain/humanizer-de/install-from-lock"
        return bool(url == COMMERCIAL_WHEEL_URL and len(entries) == 1
                    and entries[0][1].group("url") == url
                    and entries[0][0] + entries[0][1].start("url") == offset
                    and installer.is_file()
                    and "--require-hashes" in installer.read_text() and "--only-binary=:all:" in installer.read_text())
    if source == TOOLCHAIN_LOCK:
        lock = strict_json(text)
        skill = lock.get("skill") if isinstance(lock, dict) else None
        return bool(isinstance(skill, dict) and skill.get("repository") == url
                    and json_field_url_is_exact(text, url, offset, [str(skill.get("repository", ""))])
                    and re.fullmatch(r"https://github\.com/[^/]+/[^/]+\.git", url)
                    and COMMIT_RE.fullmatch(str(skill.get("source_revision", "")))
                    and SHA256_RE.fullmatch(str(skill.get("git_archive_sha256", "")))
                    and SHA256_RE.fullmatch(str(skill.get("content_sha256", ""))))
    if source == MERMAID_PACKAGE_LOCK:
        lock = strict_json(text)
        packages = lock.get("packages") if isinstance(lock, dict) else None
        if not isinstance(packages, dict): return False
        resolved = [str(entry.get("resolved", "")) for entry in packages.values() if isinstance(entry, dict)]
        matches = [entry for entry in packages.values() if isinstance(entry, dict) and entry.get("resolved") == url]
        return bool(matches and json_field_url_is_exact(text, url, offset, resolved)
                    and re.fullmatch(r"https://registry\.npmjs\.org/.+/-/.+\.tgz", url)
                    and all(re.fullmatch(r"sha512-[A-Za-z0-9+/]+={0,2}", str(entry.get("integrity", ""))) for entry in matches))
    if source in MERMAID_RENDER_SCRIPTS:
        expected = "http://127.0.0.1:9"
        assignments = [
            (match.group("name"), match.start("url"))
            for match in re.finditer(
                r"(?<![A-Za-z0-9_])(?P<name>HTTP_PROXY|HTTPS_PROXY|ALL_PROXY)=(?P<url>http://127\.0\.0\.1:9)(?=\s)",
                text,
            )
        ]
        return bool(url == expected and offset in {position for _name, position in assignments}
                    and sorted(name for name, _position in assignments) == ["ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"]
                    and len(raw_url_offsets(text, expected)) == 3)
    if source == NEGATIVE_FIXTURE:
        fixture = strict_json(text)
        cases = fixture.get("cases", []) if isinstance(fixture, dict) else []
        replacements = [str(case.get("replacement", "")) for case in cases if isinstance(case, dict)]
        matches = [case for case in cases if isinstance(case, dict) and url in str(case.get("replacement", ""))]
        return bool(matches and json_field_url_is_exact(text, url, offset, replacements)
                    and all(str(case.get("id", "")).startswith("mermaid-")
                            and "Mermaid" in str(case.get("error", "")) for case in matches))
    return False


def rule_pattern(parts: tuple[str, ...], plural_last: bool) -> re.Pattern[str]:
    """Compile a form with optional punctuation/spacing between its words."""
    escaped = [re.escape(part) for part in parts]
    if plural_last:
        escaped[-1] += "s?"
    body = r"[\W_]*".join(escaped)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


FORBIDDEN_RULES = [
    (label, rule_pattern(parts, plural_last))
    for label, parts, plural_last in FORBIDDEN_FORMS
]


def scan_text(text: str, source: str = "<text>") -> list[str]:
    """Return all commercial-content findings in one UTF-8 text."""
    findings: list[str] = []
    commercial_text = text.replace("CLAUDE.md", "CANONICAL_AGENT_BRIDGE") if source in CLAUDE_BRIDGE_FILES else text
    if source in GRILL_DISCOVERY_FILES:
        commercial_text = re.sub(r"(?<!\w)grill-with-docs(?!\w)", "OPTIONAL_DISCOVERY_WRAPPER", commercial_text, flags=re.IGNORECASE)
    for label, pattern in FORBIDDEN_RULES:
        if pattern.search(commercial_text):
            findings.append(f"{source}: prohibited term: {label}")
    json_sources = {TOOLCHAIN_LOCK, MERMAID_PACKAGE_LOCK, MERMAID_TOOLCHAIN_LOCK, MERMAID_TOOLCHAIN_SCHEMA, NEGATIVE_FIXTURE}
    parsed = json_string_spans(text) if source in json_sources else None
    if source in json_sources and parsed is None:
        findings.append(f"{source}: malformed or duplicate-key JSON")
        return findings
    if parsed is not None:
        root, spans = parsed
        covered: list[tuple[int, int]] = []
        for start, end, pointer, value in spans:
            covered.append((start, end))
            for match in URL_RE.finditer(value):
                url = match.group().rstrip(".,")
                if url not in ALLOWED_URLS and not structured_json_url_allowed(source, root, pointer, value, url):
                    findings.append(f"{source}: external URL: {url}")
        for match in URL_RE.finditer(text):
            if not any(start <= match.start() < end for start,end in covered):
                findings.append(f"{source}: external URL: {match.group().rstrip('.,')}")
    else:
        for match in URL_RE.finditer(text):
            url = match.group().rstrip(".,")
            if url not in ALLOWED_URLS and not structured_lock_url_allowed(text, source, url, match.start()) and not static_svg_namespace_allowed(text,source,url,match.start()):
                findings.append(f"{source}: external URL: {url}")
    return findings


def candidate_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / item.decode() for item in result.stdout.split(b"\0") if item]


def main() -> int:
    findings: list[str] = []
    for path in candidate_files():
        if path.resolve() == SELF or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        findings.extend(scan_text(text, str(path.relative_to(ROOT))))

    if findings:
        print("Commercial-content check failed:", file=sys.stderr)
        print("\n".join(f"- {item}" for item in findings), file=sys.stderr)
        return 1
    print("Commercial-content check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
