#!/usr/bin/env python3
"""Dependency-free regression tests for the commercial-content scanner."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_commercial_content as scanner


class CommercialContentScannerTests(unittest.TestCase):
    def test_exact_task004_claude_bridge_filename_is_narrowly_allowed(self) -> None:
        self.assertEqual([], scanner.scan_text("CLAUDE.md -> AGENTS.md", "skills/brandkit-build/SKILL.md"))
        self.assertTrue(scanner.scan_text("CLAUDE.md -> AGENTS.md", "README.md"))

    def test_json_schema_meta_schema_url_is_allowed(self) -> None:
        self.assertEqual([], scanner.scan_text('"https://json-schema.org/draft/2020-12/schema"', "schemas/example.schema.json"))

    def test_optional_discovery_name_is_allowed_only_in_reviewed_contract_files(self) -> None:
        name = "gri" + "ll-with-docs"
        self.assertEqual([], scanner.scan_text(name, "skills/brandkit-interview/SKILL.md"))
        self.assertTrue(scanner.scan_text(name, "notes/unreviewed.md"))
        self.assertTrue(scanner.scan_text("gri" + "ll with docs", "skills/brandkit-interview/SKILL.md"))

    def test_optional_discovery_allowlist_equals_real_literal_locations(self) -> None:
        name = "gri" + "ll-with-docs"
        actual = set()
        for path in scanner.candidate_files():
            if path.resolve() == scanner.SELF or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if name in text:
                actual.add(path.relative_to(scanner.ROOT).as_posix())
        self.assertEqual(actual, scanner.GRILL_DISCOVERY_FILES)

    def assert_rejected(self, text: str, source: str = "<text>") -> None:
        self.assertTrue(scanner.scan_text(text, source), msg=f"expected rejection: {text!r}")

    def assert_allowed(self, text: str, source: str = "<text>") -> None:
        self.assertEqual([], scanner.scan_text(text, source))

    def test_exact_hashed_artifact_lock_url_is_allowed_only_in_named_lock(self) -> None:
        url = "https" + "://github.com/explosion/spacy-models/releases/download/de_core_news_sm-3.8.0/de_core_news_sm-3.8.0-py3-none-any.whl"
        entry = f"de_core_news_sm @ {url} \\\n    --hash=sha256:{'a'*64}\n"
        self.assert_allowed(entry, scanner.ARTIFACT_LOCK)
        self.assert_rejected(entry, "docs/example.md")
        self.assert_rejected(f"de_core_news_sm @ {url}\n", scanner.ARTIFACT_LOCK)
        for candidate in (
            url.replace("de_core_news_sm-3.8.0/de_core_news_sm-3.8.0", "main/de_core_news_sm-3.8.0"),
            url.replace("de_core_news_sm-3.8.0/de_core_news_sm-3.8.0", "latest/de_core_news_sm-3.8.0"),
            url.replace("de_core_news_sm-3.8.0/de_core_news_sm-3.8.0", "stable/de_core_news_sm-3.8.0"),
            url.replace("de_core_news_sm-3.8.0/de_core_news_sm-3.8.0", "feature-x/de_core_news_sm-3.8.0"),
            url.replace("de_core_news_sm-3.8.0-py3-none-any.whl", "other-3.8.0-py3-none-any.whl"),
            url.replace("explosion/spacy-models", "vendor/spacy-models"),
            url.replace("3.8.0", "3.8.1"),
        ):
            candidate_entry = f"de_core_news_sm @ {candidate} \\\n    --hash=sha256:{'a'*64}\n"
            self.assert_rejected(candidate_entry, scanner.ARTIFACT_LOCK)
        self.assert_rejected(entry.replace("--hash=sha256:", "--digest=sha256:"), scanner.ARTIFACT_LOCK)
        for malformed in (
            entry.replace("de_core_news_sm @", "unrelated @"),
            "# " + entry,
            "other-package==1.0 \\\n" + entry,
            entry.replace("    --hash=", "    --no-deps \\\n    --hash="),
            entry.replace("\n", "\n    # continued comment \\\n", 1),
            entry.replace("a"*64, "a"*64 + " \\\n    --only-binary=:all:"),
            entry.replace("\n", " --only-binary=:all:\n", 1),
            entry + entry,
            f"note = {url} \\\n    --hash=sha256:{'a'*64}\n",
        ):
            self.assert_rejected(malformed, scanner.ARTIFACT_LOCK)

    def test_exact_repository_lock_url_requires_commit_archive_and_content_hashes(self) -> None:
        url = "https" + "://github.com/vendor/skill.git"
        base = {"skill": {"repository": url, "source_revision": "a"*40,
                           "git_archive_sha256": "b"*64, "content_sha256": "c"*64}}
        import json
        self.assert_allowed(json.dumps(base), scanner.TOOLCHAIN_LOCK)
        for key, value in (("source_revision", "main"), ("git_archive_sha256", None),
                           ("content_sha256", None), ("archive_sha256", "b"*64)):
            broken = json.loads(json.dumps(base))
            if key == "archive_sha256":
                broken["skill"].pop("git_archive_sha256"); broken["skill"][key] = value
            elif value is None: broken["skill"].pop(key)
            else: broken["skill"][key] = value
            self.assert_rejected(json.dumps(broken), scanner.TOOLCHAIN_LOCK)
        self.assert_rejected(json.dumps(base), "templates/toolchain.json")
        self.assert_rejected("https" + "://github.com/vendor/skill/tree/main", scanner.TOOLCHAIN_LOCK)

    def test_every_defined_form_is_rejected(self) -> None:
        for label, parts, _plural_last in scanner.FORBIDDEN_FORMS:
            literal = " ".join(parts)
            with self.subTest(rule=label):
                self.assert_rejected(literal)

    def test_every_multiword_form_accepts_separator_variants(self) -> None:
        for label, parts, _plural_last in scanner.FORBIDDEN_FORMS:
            if len(parts) < 2:
                continue
            for separator in ("", " ", chr(45)):
                with self.subTest(rule=label, separator=separator):
                    self.assert_rejected(separator.join(parts))

    def test_historical_provider_spellings_are_rejected(self) -> None:
        variants = [
            "Goo" + "gle-Fonts-Link",
            "Goo" + "gle Fonts",
            "Higgs" + chr(45) + "field",
            "refer" + "ral",
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.assert_rejected(variant)

    def test_historical_external_links_are_rejected(self) -> None:
        historical_links = [
            "https" + "://" + "higg" + "sfield.ai/s/"
            + "mcp-arnold-oberleiter-WlfTqR",
            "https" + "://" + "higg" + "sfield.ai/s/"
            + "seedance-2-5-arnold-oberleiter-CMxBMw",
        ]
        for link in historical_links:
            with self.subTest(link=link):
                self.assert_rejected(link)

    def test_foreign_url_is_rejected(self) -> None:
        self.assert_rejected("https" + "://example.invalid/tool")

    def test_static_w3c_namespaces_are_allowed_only_as_exact_svg_declarations(self) -> None:
        svg="http"+"://www.w3.org/2000/svg";xlink="http"+"://www.w3.org/1999/xlink";xhtml="http"+"://www.w3.org/1999/xhtml"
        self.assert_allowed(f'<svg xmlns="{svg}" xmlns:xlink="{xlink}"><foreignObject><div xmlns="{xhtml}">Text</div></foreignObject></svg>',"fixtures/generated.svg")
        for source,payload in (
            ("fixtures/generated.html",f'<svg xmlns="{svg}"/>'),
            ("fixtures/generated.svg",f'<svg href="{svg}"/>'),
            ("fixtures/generated.svg",f'<svg><!-- xmlns="{svg}" --></svg>'),
            ("fixtures/generated.svg",f'<svg><text>xmlns="{svg}"</text></svg>'),
            ("fixtures/generated.svg",f'<svg xmlns="{svg}/tracking"/>'),
            ("fixtures/generated.svg",f'<svg xmlns="{svg}?affili'+'ate=1"/>'),
            ("fixtures/generated.svg",'<svg xmlns="https'+ '://example.invalid/ns"/>'),
            ("fixtures/generated.svg",f'<svg data-xmlns="{svg}"/>'),
        ):
            with self.subTest(source=source,payload=payload):self.assert_rejected(payload,source)

    def test_mermaid_package_lock_allows_only_registry_tarball_with_integrity(self) -> None:
        import json
        url = "https" + "://registry.npmjs.org/example/-/example-1.0.0.tgz"
        lock = {"packages": {"node_modules/example": {"resolved": url, "integrity": "sha512-" + "A" * 86 + "=="}}}
        self.assert_allowed(json.dumps(lock), scanner.MERMAID_PACKAGE_LOCK)
        lock["packages"]["node_modules/example"].pop("integrity")
        self.assert_rejected(json.dumps(lock), scanner.MERMAID_PACKAGE_LOCK)
        self.assert_rejected(json.dumps({"funding": {"url": "https" + "://example.invalid/ad"}}), scanner.MERMAID_PACKAGE_LOCK)
        duplicate = {"packages": {"node_modules/example": {"resolved": url, "integrity": "sha512-" + "A" * 86 + "==", "funding_ad": url}}}
        self.assert_rejected(json.dumps(duplicate), scanner.MERMAID_PACKAGE_LOCK)
        valid = {"packages": {"node_modules/example": {"resolved": url, "integrity": "sha512-" + "A" * 86 + "=="}}}
        escaped = json.dumps(valid).replace("https" + "://", "https:\\/\\/")
        self.assert_allowed(escaped, scanner.MERMAID_PACKAGE_LOCK)
        self.assert_rejected(escaped[:-1] + ', "advertisement": "' + url + '"}', scanner.MERMAID_PACKAGE_LOCK)
        unicode_escaped = json.dumps(valid).replace("https" + "://", "https\\u003a\\/\\/")
        self.assert_allowed(unicode_escaped, scanner.MERMAID_PACKAGE_LOCK)

    def test_named_json_exceptions_fail_closed_on_malformed_or_duplicate_keys(self) -> None:
        probes = {
            scanner.TOOLCHAIN_LOCK: '{"skill":{"repository":"https:\\/\\/github.com\\/vendor\\/skill.git","repository":"https:\\/\\/github.com\\/vendor\\/skill.git"}}',
            scanner.MERMAID_PACKAGE_LOCK: '{"packages":{"node_modules/example":{"resolved":"https\\u003a\\/\\/registry.npmjs.org\\/example\\/-\\/example-1.0.0.tgz","resolved":"https\\u003a\\/\\/registry.npmjs.org\\/example\\/-\\/example-1.0.0.tgz"}}}',
            scanner.NEGATIVE_FIXTURE: '{"cases":[{"id":"mermaid-url","replacement":"A[https:\\/\\/example.invalid\\/pixel]","error":"Mermaid v1"}]} // invalid',
            scanner.MERMAID_TOOLCHAIN_LOCK: '{"browser":{"download_url":"https:\\/\\/example.invalid","download_url":"https:\\/\\/example.invalid"}}',
        }
        for source, text in probes.items():
            with self.subTest(source=source):
                findings = scanner.scan_text(text, source)
                self.assertTrue(any("malformed or duplicate-key JSON" in item for item in findings), findings)

    def test_mermaid_provenance_urls_are_allowed_only_with_closed_hash_bindings(self) -> None:
        import copy,json
        lock=json.loads((scanner.ROOT/scanner.MERMAID_TOOLCHAIN_LOCK).read_text())
        schema=json.loads((scanner.ROOT/scanner.MERMAID_TOOLCHAIN_SCHEMA).read_text())
        self.assert_allowed(json.dumps(lock),scanner.MERMAID_TOOLCHAIN_LOCK)
        self.assert_allowed(json.dumps(schema),scanner.MERMAID_TOOLCHAIN_SCHEMA)
        for family in ("browser","node"):
            for attack in ("missing-hash","foreign-url","extra","wrong-pointer"):
                candidate=copy.deepcopy(lock)
                if attack=="missing-hash":candidate[family].pop("archive_sha256")
                elif attack=="foreign-url":candidate[family]["download_url"]="https"+"://example.invalid/archive"
                elif attack=="extra":candidate[family]["mirror_url"]=candidate[family]["download_url"]
                else:candidate[family]["provenance"] = candidate[family].pop("download_url")
                with self.subTest(family=family,attack=attack):self.assert_rejected(json.dumps(candidate),scanner.MERMAID_TOOLCHAIN_LOCK)
        duplicate_url="https"+"://example.invalid/archive"
        duplicate=(scanner.ROOT/scanner.MERMAID_TOOLCHAIN_LOCK).read_text().replace('"download_url":', f'"download_url":"{duplicate_url}","download_url":',1)
        self.assert_rejected(duplicate,scanner.MERMAID_TOOLCHAIN_LOCK)
        for family in ("browser","node"):
            candidate=copy.deepcopy(schema);candidate["properties"][family]["properties"]["extra_url"]={"const":candidate["properties"][family]["properties"]["download_url"]["const"]}
            self.assert_rejected(json.dumps(candidate),scanner.MERMAID_TOOLCHAIN_SCHEMA)

    def test_loopback_and_negative_fixture_exceptions_are_narrow(self) -> None:
        loopback = "http" + "://127.0.0.1:9"
        proxies = f"HTTP_PROXY={loopback} HTTPS_PROXY={loopback} ALL_PROXY={loopback} command\n"
        self.assert_allowed(proxies, "toolchain/mermaid-svg/render-offline")
        self.assert_rejected("http" + "://127.0.0.1:10", "toolchain/mermaid-svg/render-offline")
        self.assert_rejected(proxies + f"# sponsor {loopback}\n", "toolchain/mermaid-svg/render-offline")
        self.assert_rejected(proxies.replace("HTTP_PROXY=", "PROXY="), "toolchain/mermaid-svg/render-offline")
        fixture = {"cases": [{"id": "mermaid-url", "replacement": "A[https" + "://example.invalid/pixel]", "error": "Mermaid v1"}]}
        import json
        self.assert_allowed(json.dumps(fixture), scanner.NEGATIVE_FIXTURE)
        fixture["cases"][0]["id"] = "advertisement"
        self.assert_rejected(json.dumps(fixture), scanner.NEGATIVE_FIXTURE)
        fixture = {"cases": [{"id": "mermaid-url", "replacement": "A[https" + "://example.invalid/pixel]", "error": "Mermaid v1", "advertisement": "https" + "://example.invalid/pixel"}]}
        self.assert_rejected(json.dumps(fixture), scanner.NEGATIVE_FIXTURE)
        clean = {"cases": [{"id": "mermaid-url", "replacement": "A[https" + "://example.invalid/pixel]", "error": "Mermaid v1"}]}
        escaped = json.dumps(clean).replace("https" + "://", "https:\\/\\/")
        self.assert_allowed(escaped, scanner.NEGATIVE_FIXTURE)
        self.assert_rejected(escaped[:-1] + ', "comment": "https' + '://example.invalid/pixel"}', scanner.NEGATIVE_FIXTURE)

    def test_clean_text_is_allowed(self) -> None:
        self.assert_allowed("Das Kit nutzt lokale Dateien und optionale Werkzeuge.")

    def test_real_license_is_allowed(self) -> None:
        license_text = (scanner.ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assert_allowed(license_text)

    def test_factual_copyright_line_is_allowed(self) -> None:
        self.assert_allowed("Copyright 2026 Arnold Oberleiter")

    def test_allowed_iptc_value_is_allowed(self) -> None:
        value = (
            "http" + "://cv.iptc.org/newscodes/digitalsourcetype/"
            "trainedAlgorithmicMedia"
        )
        self.assert_allowed(value)

    def test_harmless_word_overlaps_are_allowed(self) -> None:
        samples = [
            "A commissioned illustration",
            "A provisional layout",
            "An about page",
            "CreditScore and subscriptionless are unrelated words",
            "Typography and fontography notes",
            "The accounting status is stable",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assert_allowed(sample)


if __name__ == "__main__":
    unittest.main()
