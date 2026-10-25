#!/usr/bin/env python3
"""Markdown contract v3: additive extension of v2 (code fences, strong emphasis).

v3 lives in scripts/markdown_contract_v3.py and never changes the pinned v2
validator. Every v2 document stays valid with the same AST apart from the
contract label.
"""
from __future__ import annotations
import copy, importlib.util, json, sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("markdown_contract_v3", ROOT / "scripts/markdown_contract_v3.py"); assert SPEC and SPEC.loader
v3 = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(v3)
mc = v3.mc
FIXTURE = ROOT / "fixtures/markdown/valid-report.md"; ASSET_ROOT = ROOT / "fixtures/markdown"
REPORT = json.loads((ROOT / "document-types/report.v1.json").read_text())

NGINX = "```nginx\nlocation ~ ^/(installer|backup)\\.php$ {\n    return 404;\n}\n\n\tdeny all; **kein Fett** [kein](https://x.test/) <b>\n```\n"


def with_block(block: str, before: str = "## Umfang {#scope}") -> str:
    source = FIXTURE.read_text()
    assert before in source
    return source.replace(before, block + "\n" + before, 1)


def parse(source: str) -> dict:
    parser = v3.Parser(source, ASSET_ROOT); ast = parser.parse(); parser.validate_assets()
    v3.validate_ast_document(ast, REPORT)
    return ast


def without_contract(ast: dict) -> dict:
    copy_ = copy.deepcopy(ast); copy_.pop("contract"); copy_.pop("humanizer_segments"); return copy_


class ContractFile(unittest.TestCase):
    def test_v3_contract_validates_and_binds_v2_by_hash(self):
        contract = v3.load_contract()
        self.assertEqual("markdown-contract.v3", contract["schema_version"])
        self.assertEqual(mc.sha((ROOT / "markdown-contract.v2.json").read_bytes()), contract["extends"]["sha256"])
        self.assertEqual("markdown-contract.v2", contract["extends"]["contract"])
        self.assertEqual(3, contract["revision"])

    def test_v2_pins_stay_byte_identical(self):
        self.assertEqual("5e54a95b00e8b778384f5cf1bfbcdbe721136e6142b9b31b9e6ffa1fce868ba4", mc.sha((ROOT / "markdown-contract.v2.json").read_bytes()))
        self.assertEqual("9244ec5d756ce47c0b7f2002e73521257df937db81712e5861c7b67e636aa8ec", mc.sha((ROOT / "scripts/markdown_contract.py").read_bytes()))


class Compatibility(unittest.TestCase):
    def test_v2_fixture_has_identical_ast_semantics(self):
        source = FIXTURE.read_text()
        old = mc.Parser(source, ASSET_ROOT).parse(); new = parse(source)
        self.assertEqual("markdown-contract.v3", new["contract"])
        self.assertEqual(without_contract(old), without_contract(new))
        self.assertEqual(old["humanizer_segments"]["partition"], new["humanizer_segments"]["partition"])

    def test_v2_rejections_stay_rejections(self):
        for block in ("```python {x}\nprint()\n```\n", "~~~\nx\n~~~\n", "```{python}\nx\n```\n", "```Nginx\nx\n```\n"):
            with self.subTest(block=block), self.assertRaises(mc.ContractError):
                parse(with_block(block))


class CodeFence(unittest.TestCase):
    def test_code_fence_keeps_bytes_language_and_position(self):
        ast = parse(with_block(NGINX))
        code = [n for n in ast["children"] if n["type"] == "code"]
        self.assertEqual(1, len(code))
        self.assertEqual("nginx", code[0]["language"])
        self.assertEqual(NGINX.split("\n", 1)[1].rsplit("```", 1)[0], code[0]["text"])
        scope = next(i for i, n in enumerate(ast["children"]) if n.get("id") == "scope")
        self.assertEqual("code", next(n for n in reversed(ast["children"][:scope]) if n["type"] != "blank")["type"])

    def test_code_bytes_are_protected_and_partition_is_complete(self):
        source = with_block(NGINX); ast = parse(source)
        data = source.encode()
        segments = ast["humanizer_segments"]["partition"]
        self.assertEqual(0, segments[0]["start_byte"]); self.assertEqual(len(data), segments[-1]["end_byte"])
        body = NGINX.split("\n", 1)[1].rsplit("```", 1)[0].encode(); start = data.index(body)
        for seg in segments:
            if seg["start_byte"] < start + len(body) and seg["end_byte"] > start:
                self.assertEqual("protected", seg["classification"])

    def test_fence_without_language_and_two_fences(self):
        ast = parse(with_block("```\neins\n```\n\nText dazwischen.\n\n```text\nzwei\n```\n"))
        code = [n for n in ast["children"] if n["type"] == "code"]
        self.assertEqual([(None, "eins\n"), ("text", "zwei\n")], [(n["language"], n["text"]) for n in code])

    def test_h3_after_code_fence_keeps_its_h2(self):
        parse(with_block("### Unterabschnitt {#unter}\n\n```\nx\n```\n\n### Danach {#danach}\n\nText.\n"))

    def test_unclosed_empty_or_nested_fences_are_rejected(self):
        for block in ("```nginx\nx\n", "```\n```\n", "::: {.note #n1 title=\"N\"}\n```\nx\n```\n:::\n"):
            with self.subTest(block=block), self.assertRaises(mc.ContractError):
                parse(with_block(block))

    def test_code_is_admitted_where_the_type_admits_callouts(self):
        dtype = copy.deepcopy(REPORT); dtype["allowed_elements"].remove("callout")
        parser = v3.Parser(with_block(NGINX), ASSET_ROOT)
        ast = parser.parse()
        with self.assertRaisesRegex(mc.ContractError, "code"):
            v3.validate_ast_document(ast, dtype)

    def test_page_break_before_h3_after_code(self):
        parse(with_block("### Davor {#davor}\n\n```\nx\n```\n\n::: page-break\n### Docroot {#docroot}\n\nText.\n"))


class Strong(unittest.TestCase):
    def test_strong_in_prose_and_lists(self):
        ast = parse(with_block("Das ist **wichtig** und **{{number:metric-value|42 %}} Anteil**.\n\n- Punkt mit **Fett**\n"))
        para = next(n for n in ast["children"] if n["type"] == "paragraph" and any(t["type"] == "strong" for t in n["inline"]))
        strong = [t for t in para["inline"] if t["type"] == "strong"]
        self.assertEqual([{"type": "text", "text": "wichtig", "mutable": True}], strong[0]["children"])
        self.assertEqual("protected", strong[1]["children"][0]["type"])
        lst = next(n for n in ast["children"] if n["type"] == "unordered-list" and "Fett" in json.dumps(n))
        self.assertEqual("strong", lst["items"][0]["inline"][1]["type"])

    def test_strong_delimiters_are_protected_syntax(self):
        source = with_block("Das ist **wichtig** hier.\n"); ast = parse(source); data = source.encode()
        start = data.index(b"**wichtig**")
        for offset in (start, start + len(b"**wichtig")):
            seg = next(s for s in ast["humanizer_segments"]["partition"] if s["start_byte"] <= offset < s["end_byte"])
            self.assertEqual("protected", seg["classification"])

    def test_invalid_strong_is_rejected(self):
        for text in ("**offen", "** leer**", "***drei***", "__unterstrich__", "*kursiv*"):
            with self.subTest(text=text), self.assertRaises(mc.ContractError):
                parse(with_block(f"Satz {text} Ende.\n"))

    def test_strong_is_not_allowed_in_headings_or_tables(self):
        for block in ("### **Fett** {#fett}\n", "| A | B |\n| --- | --- |\n| **x** | y |\n"):
            with self.subTest(block=block), self.assertRaises(mc.ContractError):
                parse(with_block(block))


class Validate(unittest.TestCase):
    def test_run_validate_rejects_publication(self):
        with self.assertRaises(mc.ContractError):
            v3.run_validate(FIXTURE, None, ASSET_ROOT, True, None)

    def test_run_validate_returns_v3_ast(self):
        ast = v3.run_validate(FIXTURE, None, ASSET_ROOT, False, None)
        self.assertEqual("markdown-contract.v3", ast["contract"])


class HeadingFour(unittest.TestCase):
    def test_h4_after_h3_is_a_level_four_heading(self):
        ast = parse(with_block("### Oben {#oben}\n\nText.\n\n#### Unten {{number:metric-value|42 %}} {#unten}\n\nMehr Text.\n"))
        h4 = next(n for n in ast["children"] if n.get("id") == "unten")
        self.assertEqual(("heading", 4), (h4["type"], h4["level"]))
        self.assertIn("{{number:metric-value|42 %}}", h4["text"])

    def test_h4_line_is_protected_and_link_target(self):
        source = with_block("### Oben {#oben}\n\n#### Unten {#unten}\n\nSiehe [unten](#unten).\n"); ast = parse(source)
        data = source.encode(); start = data.index(b"#### Unten")
        seg = next(s for s in ast["humanizer_segments"]["partition"] if s["start_byte"] <= start < s["end_byte"])
        self.assertEqual("protected", seg["classification"])

    def test_h4_needs_a_preceding_h3_and_h5_stays_forbidden(self):
        for block in ("#### Allein {#allein}\n", "### Oben {#oben}\n\n##### Fuenf {#fuenf}\n", "::: {.note #n2 title=\"N\"}\n#### In Kasten {#ik}\n:::\n"):
            with self.subTest(block=block), self.assertRaises(mc.ContractError):
                parse(with_block(block, before="## Umfang {#scope}") if "Allein" not in block else FIXTURE.read_text().replace("## Kurzfassung {#executive-summary}\n", "## Kurzfassung {#executive-summary}\n\n" + block, 1))


class TableWidths(unittest.TestCase):
    TABLE = "| Schwere | Befund | Risiko |\n| --- | --- | --- |\n| Hoch | Installer | Sehr langer Text |\n"

    def test_widths_attach_to_the_following_table(self):
        ast = parse(with_block("::: table-widths 1 3 6\n" + self.TABLE))
        table = next(n for n in ast["children"] if n["type"] == "table" and n["headers"][0] == "Schwere")
        self.assertEqual([1, 3, 6], table["widths"])

    def test_table_without_directive_has_no_widths(self):
        ast = parse(with_block(self.TABLE))
        table = next(n for n in ast["children"] if n["type"] == "table" and n["headers"][0] == "Schwere")
        self.assertNotIn("widths", table)

    def test_invalid_widths_are_rejected(self):
        for block in ("::: table-widths 1 3\n" + TableWidths.TABLE, "::: table-widths 1 3 0\n" + TableWidths.TABLE,
                      "::: table-widths 1 3 6\n\n" + TableWidths.TABLE, "::: table-widths 1 3 6\nText.\n", "::: table-widths\n" + TableWidths.TABLE):
            with self.subTest(block=block), self.assertRaises(mc.ContractError):
                parse(with_block(block))

    def test_directive_bytes_are_protected(self):
        source = with_block("::: table-widths 1 3 6\n" + self.TABLE); ast = parse(source)
        start = source.encode().index(b"::: table-widths")
        seg = next(s for s in ast["humanizer_segments"]["partition"] if s["start_byte"] <= start < s["end_byte"])
        self.assertEqual("protected", seg["classification"])


class RunningTitle(unittest.TestCase):
    def test_running_title_attaches_to_the_following_h2(self):
        ast = parse(with_block("::: running-title Technischer Teil {{number:metric-value|42 %}}", before="## Umfang {#scope}"))
        scope = next(n for n in ast["children"] if n.get("id") == "scope")
        self.assertEqual("Technischer Teil {{number:metric-value|42 %}}", scope["running_title"])

    def test_running_title_may_follow_a_page_break(self):
        ast = parse(with_block("::: page-break\n::: running-title Technischer Teil", before="## Umfang {#scope}"))
        scope = next(n for n in ast["children"] if n.get("id") == "scope")
        self.assertEqual("Technischer Teil", scope["running_title"])

    def test_directive_bytes_are_protected(self):
        source = with_block("::: running-title Technischer Teil", before="## Umfang {#scope}"); ast = parse(source)
        start = source.encode().index(b"::: running-title")
        seg = next(s for s in ast["humanizer_segments"]["partition"] if s["start_byte"] <= start < s["end_byte"])
        self.assertEqual("protected", seg["classification"])

    def test_invalid_running_titles_are_rejected(self):
        for block, before in (("::: running-title\n", "## Umfang {#scope}"), ("::: running-title Text\n\n", "## Umfang {#scope}"),
                              ("### Unter {#unter}\n\n::: running-title Text\n", "### Unter {#unter}\n"),
                              ("::: running-title Teil 2\n", "## Umfang {#scope}"), ("::: running-title Text\nAbsatz.\n", "## Umfang {#scope}")):
            with self.subTest(block=block), self.assertRaises(mc.ContractError):
                source = with_block(block, before=before) if not block.startswith("###") else with_block("::: running-title Text\n### Unter {#unter}\n\nText.\n")
                parse(source)


class PageNotes(unittest.TestCase):
    NOTES = ("### Oben {#oben}\n\nText mit Verweis.[^hinweis-a]\n\n#### Unten mit Stern[^hinweis-b] {#unten}\n\n"
             "| Spalte | Wert |\n| --- | --- |\n| Zelle[^hinweis-c] | x |\n\n"
             "[^hinweis-a]: Erste Fussnote.\n\n[^hinweis-b]: Zweite Fussnote mit {{number:metric-value|42 %}}.\n\n[^hinweis-c]: Dritte Fussnote.\n")

    def test_page_notes_are_nodes_and_refs_resolve(self):
        ast = parse(with_block(self.NOTES))
        notes = [n for n in ast["children"] if n["type"] == "note"]
        self.assertEqual(["hinweis-a", "hinweis-b", "hinweis-c"], [n["id"] for n in notes])
        self.assertEqual("Erste Fussnote.", notes[0]["inline"][0]["text"])
        source_footnotes = [n for n in ast["children"] if n["type"] == "footnote"]
        self.assertEqual(["source-one"], [n["source_id"] for n in source_footnotes])

    def test_missing_definition_reference_or_duplicate_is_rejected(self):
        for block in ("Text.[^fehlt-a]\n", "[^ohne-verweis]: Text.\n",
                      "Text.[^doppelt]\n\n[^doppelt]: Eins.\n\n[^doppelt]: Zwei.\n"):
            with self.subTest(block=block), self.assertRaises(mc.ContractError):
                parse(with_block(block))

    def test_note_prefix_is_protected_and_text_is_prose(self):
        source = with_block(self.NOTES); ast = parse(source); data = source.encode()
        start = data.index(b"[^hinweis-a]: ")
        seg = next(s for s in ast["humanizer_segments"]["partition"] if s["start_byte"] <= start < s["end_byte"])
        self.assertEqual("protected", seg["classification"])
        text = data.index(b"Erste Fussnote.")
        seg = next(s for s in ast["humanizer_segments"]["partition"] if s["start_byte"] <= text < s["end_byte"])
        self.assertEqual("mutable", seg["classification"])


if __name__ == "__main__":
    unittest.main()
