"""The icon visual domain, measured at its public seams.

Vocabulary follows quality-loop/icon-domain/CONTEXT.md: an Icon carries
Variants as axes, and each Variant yields one file - never a second Icon.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import brandkit_visual_decisions as visual  # noqa: E402
from tests import test_brandkit_visual_decisions as fixtures  # noqa: E402


def mutation_receipt(operation: str) -> dict:
    binding = {"path": "staging/doc-a/input.svg", "sha256": "a" * 64}
    return {
        "schema_version": "brandkit-affinity-mutation-receipt.v1", "issuer": "fixture",
        "key_id": "0" * 16, "capability": "graphics-mutate",
        "project_identity_sha256": "b" * 64, "run_id": "run-a", "document_id": "doc-a",
        "staging_root": ".brandkit/work/run-a/visual-decisions/staging/doc-a",
        "observation_sha256": "c" * 64, "server_lock_sha256": "d" * 64,
        "owner_approval": {"path": "owner-approval.json", "sha256": "e" * 64},
        "tool": {"name": "execute_script", "schema_sha256": "f" * 64},
        "operation": operation, "inputs": [binding],
        "outputs": [{"candidate_id": "licht-an", "path": "staging/doc-a/licht-an.svg", "sha256": "1" * 64}],
        "issued_at": 1, "expires_at": 2, "nonce": "2" * 64, "hmac_sha256": "3" * 64,
    }


class IconMutationOperationTests(unittest.TestCase):
    def test_receipt_may_declare_creating_one_icon_variant(self) -> None:
        visual.validate_schema(mutation_receipt("create-icon-variant"), "affinity-mutation")

    def test_receipt_still_refuses_an_undeclared_operation(self) -> None:
        with self.assertRaises(visual.VisualDecisionError):
            visual.validate_schema(mutation_receipt("create-icon-set"), "affinity-mutation")


def icon_value(*icons: dict) -> dict:
    return {"domain": "icon", "icons": list(icons) or [
        {"icon_id": "licht", "axis": "zustand", "variants": ["an", "aus"]},
        {"icon_id": "heizung", "axis": None, "variants": ["standard"]},
    ], "creation_origin": "owner-supplied", "report": {"path": "icon-report.json", "sha256": "a" * 64}}


def candidate_set(value: dict) -> dict:
    candidate = {"id": "icon-choice", "kind": "icon", "label": "Icon-Stil A", "value": value,
                 "value_sha256": visual.sha256_bytes(visual.canonical_json(value)),
                 "source_ids": ["source-a"], "status": "candidate"}
    return {"schema_version": "brandkit-visual-candidate-set.v1", "brief_sha256": "b" * 64,
            "source_review": {"path": "review.json", "sha256": "c" * 64}, "status": "partial",
            "candidates": [candidate], "recommendations": [], "normative": False}


class IconCandidateTests(unittest.TestCase):
    def test_icon_style_candidate_carries_variants_as_axes_at_the_icon(self) -> None:
        with tempfile.TemporaryDirectory() as root_name:
            visual.validate_candidate_set(candidate_set(icon_value()), Path(root_name))

    def test_axis_and_variant_count_must_agree(self) -> None:
        cases = {
            "an axis with a single value": {"icon_id": "licht", "axis": "zustand", "variants": ["an"]},
            "two files without an axis": {"icon_id": "licht", "axis": None, "variants": ["an", "aus"]},
        }
        for name, icon in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as root_name:
                with self.assertRaisesRegex(visual.VisualDecisionError, "axis"):
                    visual.validate_candidate_set(candidate_set(icon_value(icon)), Path(root_name))

    def test_an_icon_appears_once_per_style_candidate(self) -> None:
        twice = {"icon_id": "licht", "axis": None, "variants": ["standard"]}
        with tempfile.TemporaryDirectory() as root_name:
            with self.assertRaisesRegex(visual.VisualDecisionError, "icon"):
                visual.validate_candidate_set(candidate_set(icon_value(twice, dict(twice))), Path(root_name))


SIX_DOMAINS = {"color", "typography", "code-font", "logo", "lockup", "icon"}


def schema(name: str) -> dict:
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


class ClosedDomainListsTests(unittest.TestCase):
    """Every closed list of visual domains names the same six - the tenth skill
    showed that the one list nobody checked is the one that breaks later."""

    def test_every_closed_domain_list_names_the_icon_domain(self) -> None:
        import brandkit_discovery as discovery
        contract = json.loads((ROOT / "brandkit-skill-contract.v1.json").read_text(encoding="utf-8"))
        lists = {
            "contract": contract["visual_decisions"]["domains"],
            "contract schema": schema("brandkit-skill-contract.v1.schema.json")["properties"]["visual_decisions"]["properties"]["domains"]["const"],
            "discovery": discovery.VISUAL_DOMAINS,
            "brief": schema("brandkit-visual-brief.v1.schema.json")["properties"]["open_decisions"]["items"]["enum"],
            "candidate kind": schema("brandkit-visual-candidate-set.v1.schema.json")["$defs"]["kind"]["enum"],
            "playground domain": schema("brandkit-visual-playground-data.v1.schema.json")["$defs"]["domain"]["enum"],
            "skill request": schema("brandkit-skill-request.v1.schema.json")["$defs"]["visualDomain"]["enum"],
            "owner receipt decision": schema("brandkit-discovery-owner-receipt.v1.schema.json")["properties"]["decisions"]["items"]["oneOf"][1]["properties"]["domain"]["enum"],
            "skill result": schema("brandkit-skill-result.v1.schema.json")["properties"]["visual_decision_result"]["properties"]["domains"]["items"]["enum"],
        }
        for name, domains in lists.items():
            with self.subTest(name):
                self.assertEqual(SIX_DOMAINS, set(domains))
                self.assertEqual(len(SIX_DOMAINS), len(domains))


class IconBriefTests(unittest.TestCase):
    def test_visual_brief_may_leave_the_icon_decision_open(self) -> None:
        with tempfile.TemporaryDirectory() as project_name:
            project = Path(project_name).resolve()
            root = visual.visual_workspace(project, "run-a", create=True)
            brief = {"schema_version": "brandkit-visual-brief.v1", "brief_id": "brief-a",
                     "project_identity_sha256": visual.project_identity(project), "run_id": "run-a", "brand_id": "brand-a",
                     "interview_handoff": fixtures.write(root / "interview-handoff.json", "{}"),
                     "open_decisions": ["color", "icon"], "locale": "de-DE", "viewports": [320, 768, 1440],
                     "offline": True, "normative": False}
            brief["icon_target_sizes_px"] = [20, 24, 32]
            visual.validate_brief(brief, root, project_root=project, run_id="run-a")

    def test_the_target_sizes_are_bound_in_the_brief_exactly_when_icon_is_open(self) -> None:
        """004: the run binds its target sizes in the brief, the sheet never picks them."""
        with tempfile.TemporaryDirectory() as project_name:
            project = Path(project_name).resolve()
            root = visual.visual_workspace(project, "run-a", create=True)
            brief = {"schema_version": "brandkit-visual-brief.v1", "brief_id": "brief-a",
                     "project_identity_sha256": visual.project_identity(project), "run_id": "run-a", "brand_id": "brand-a",
                     "interview_handoff": fixtures.write(root / "interview-handoff.json", "{}"),
                     "open_decisions": ["color", "icon"], "locale": "de-DE", "viewports": [320, 768, 1440],
                     "offline": True, "normative": False}
            with self.subTest("icon open without target sizes"), self.assertRaisesRegex(visual.VisualDecisionError, "target sizes"):
                visual.validate_brief(brief, root)
            with self.subTest("target sizes without an open icon decision"), self.assertRaisesRegex(visual.VisualDecisionError, "target sizes"):
                visual.validate_brief({**brief, "open_decisions": ["color"], "icon_target_sizes_px": [24]}, root)


RULES = {"grid_units": 24, "stroke_width": 2, "stroke_linecap": "round", "stroke_linejoin": "round", "color_count": 1}


def icon_svg(file_id: str, body: str, *, stroke_width: str = "2", view_box: str = "0 0 24 24") -> bytes:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{view_box}" fill="none" stroke="currentColor" '
            f'stroke-width="{stroke_width}" stroke-linecap="round" stroke-linejoin="round">'
            f'<title>{file_id}</title>{body}</svg>').encode()


HEIZUNG = '<path d="M6 4v16M12 4v16M18 4v16"/><line x1="4" y1="20" x2="20" y2="20"/>'


def licht_style(root: Path, observation: dict, sizes: list[int]) -> tuple[dict, dict]:
    """A complete own-style candidate - licht with the axis zustand - and its
    report, on disk under ``root``; for tests outside this module."""
    variants = []
    for value, body in (("an", HEIZUNG), ("aus", '<path d="M6 4v16"/>')):
        file_id = f"licht-{value}"; raw = icon_svg(file_id, body)
        variants.append({"value": value, "id": file_id, "asset": fixtures.write(root / f"{file_id}.svg", raw),
                         "creation_origin": "owner-supplied", "objective_checks": visual._icon_static_checks(raw, file_id, file_id),
                         "human_reviews": {**{f"size-{size}": "required" for size in sizes}, "optical-weight": "required", "set-consistency": "required"}})
    source = fixtures.write(root / "rules-source.md", "Owner-Bestaetigung der Bauregeln")
    report = {"schema_version": "brandkit-icon-report.v1", "candidate_set_sha256": "0" * 64,
              "affinity_observation_sha256": visual.sha256_bytes(visual.canonical_json(observation)),
              "operating_mode": "eigenstil", "benchmark": {"name": "Fixture Quality Set", "role": "quality-reference", "source": source},
              "target_sizes_px": list(sizes), "build_rules": {**RULES, "source": source}, "confirmed_set": [],
              "icons": [{"icon_id": "licht", "axis": "zustand", "variants": variants,
                         "human_reviews": {"brand-fit": "required", "quality-benchmark": "required", "variant-pair": "required"}}],
              "objective_status": "pass", "human_review_status": "required"}
    value = {"domain": "icon", "icons": [{"icon_id": "licht", "axis": "zustand", "variants": ["an", "aus"]}],
             "creation_origin": "owner-supplied", "report": fixtures.canonical_write(root, "icon-report.json", report)}
    return value, report


class IconReportFixture(unittest.TestCase):
    """Builders only; the tests live in the subclasses."""

    sizes = [24]

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.project = Path(temp.name).resolve()
        self.root = visual.visual_workspace(self.project, "run-a", create=True)
        self.observation = fixtures.unavailable(self.project, "run-a")

    def variant(self, file_id: str, body: str, **svg) -> dict:
        raw = icon_svg(file_id, body, **svg)
        asset = fixtures.write(self.root / f"{file_id}.svg", raw)
        # Hand-written for the known drawing; only the canonical digest is
        # taken from the existing, separately tested logo SVG check.
        checks = {"view_box": "0 0 24 24", "color_count": 1, "monochrome_static": True, "grid_aligned": True,
                  "stroke_widths": [2], "stroke_linecaps": ["round"], "stroke_linejoins": ["round"],
                  "canonical_svg_sha256": visual._svg_static_checks(raw, file_id, file_id)["canonical_svg_sha256"]}
        return {"value": file_id.split("-", 1)[1], "id": file_id, "asset": asset, "creation_origin": "owner-supplied",
                "objective_checks": checks,
                "human_reviews": {**{f"size-{size}": "required" for size in self.sizes}, "optical-weight": "required", "set-consistency": "required"}}

    def report(self, *icons: dict) -> dict:
        source = fixtures.write(self.root / "rules-source.md", "Owner-Bestaetigung der Bauregeln")
        return {"schema_version": "brandkit-icon-report.v1", "candidate_set_sha256": "a" * 64,
                "affinity_observation_sha256": visual.sha256_bytes(visual.canonical_json(self.observation)),
                "operating_mode": "eigenstil",
                "benchmark": {"name": "Fixture Quality Set", "role": "quality-reference", "source": source},
                "target_sizes_px": list(self.sizes), "build_rules": {**RULES, "source": source}, "confirmed_set": [],
                "icons": list(icons) or [{"icon_id": "heizung", "axis": None,
                                          "variants": [self.variant("heizung-standard", HEIZUNG)],
                                          "human_reviews": {"brand-fit": "required", "quality-benchmark": "required"}}],
                "objective_status": "pass", "human_review_status": "required"}

    def validate(self, report: dict) -> None:
        visual.validate_icon_report(report, self.root, self.observation, project_root=self.project, run_id="run-a", now=fixtures.NOW)



class IconReportTests(IconReportFixture):
    """brandkit-icon-report.v1: objective checks recomputed from the file,
    human reviews per target size and per operating mode."""

    def test_a_first_icon_in_its_own_style_is_reviewable(self) -> None:
        self.validate(self.report())

    def test_claimed_objective_checks_must_be_what_the_file_measures(self) -> None:
        report = self.report()
        report["icons"][0]["variants"][0]["objective_checks"]["stroke_widths"] = [1.5]
        with self.assertRaisesRegex(visual.VisualDecisionError, "objective checks differ"):
            self.validate(report)
        report = self.report()
        (self.root / "heizung-standard.svg").write_bytes(icon_svg("heizung-standard", HEIZUNG + '<line x1="4" y1="4" x2="20" y2="4"/>'))
        with self.assertRaises(visual.VisualDecisionError):
            self.validate(report)

    def licht(self, icon_reviews: dict) -> dict:
        variants = [self.variant("licht-an", HEIZUNG), self.variant("licht-aus", HEIZUNG + '<line x1="4" y1="4" x2="20" y2="20"/>')]
        variants[1]["objective_checks"]["canonical_svg_sha256"] = visual._svg_static_checks(
            (self.root / "licht-aus.svg").read_bytes(), "x", "licht-aus")["canonical_svg_sha256"]
        return {"icon_id": "licht", "axis": "zustand", "variants": variants, "human_reviews": icon_reviews}

    def test_the_review_matrix_follows_operating_mode_target_sizes_and_axis(self) -> None:
        supplement = {"blind-test": "required", "distinctness": "required"}
        own_style = {"brand-fit": "required", "quality-benchmark": "required"}
        with self.subTest("supplement is judged by blind test and distinctness"):
            report = self.report(self.licht({**supplement, "variant-pair": "required"}))
            report.update(operating_mode="ergaenzung", benchmark={**report["benchmark"], "role": "style-reference"})
            self.validate(report)
        with self.subTest("own style is judged by brand fit and the quality reference"):
            self.validate(self.report(self.licht({**own_style, "variant-pair": "required"})))
        refusals = {
            "own style asked for a blind test": (self.licht({**supplement, "variant-pair": "required"}), {}),
            "an axis without judging the pair": (self.licht(own_style), {}),
            "a target size nobody looked at": (self.licht({**own_style, "variant-pair": "required"}), {"target_sizes_px": [24, 48]}),
            "supplement measured against a quality reference": (self.licht({**supplement, "variant-pair": "required"}), {"operating_mode": "ergaenzung"}),
        }
        for name, (icon, change) in refusals.items():
            with self.subTest(name):
                report = self.report(icon); report.update(change)
                with self.assertRaisesRegex(visual.VisualDecisionError, "human-review matrix|benchmark"):
                    self.validate(report)

    def test_a_failed_human_review_makes_the_icon_ineligible(self) -> None:
        for review in ("distinctness", "blind-test"):
            with self.subTest(review):
                reviews = {"blind-test": "pass", "distinctness": "pass", "variant-pair": "pass", review: "fail"}
                report = self.report(self.licht(reviews))
                report.update(operating_mode="ergaenzung", benchmark={**report["benchmark"], "role": "style-reference"})
                with self.assertRaisesRegex(visual.VisualDecisionError, "not review-eligible"):
                    self.validate(report)

    def confirmed(self, file_id: str, body: str, **svg) -> dict:
        return {"id": file_id, "asset": fixtures.write(self.root / f"{file_id}.svg", icon_svg(file_id, body, **svg))}

    def test_a_new_icon_is_checked_against_the_confirmed_set(self) -> None:
        with self.subTest("consistent with the confirmed set"):
            report = self.report(); report["confirmed_set"] = [self.confirmed("wind-standard", '<path d="M4 8h12M4 16h16"/>')]
            self.validate(report)
        with self.subTest("the set was confirmed under a lighter stroke"):
            report = self.report(); report["confirmed_set"] = [self.confirmed("wind-standard", '<path d="M4 8h12M4 16h16"/>', stroke_width="1.5")]
            with self.assertRaisesRegex(visual.VisualDecisionError, "set consistency"):
                self.validate(report)
        with self.subTest("a confirmed file that changed since"):
            report = self.report(); report["confirmed_set"] = [self.confirmed("wind-standard", '<path d="M4 8h12M4 16h16"/>')]
            (self.root / "wind-standard.svg").write_bytes(icon_svg("wind-standard", '<path d="M4 8h16"/>'))
            with self.assertRaises(visual.VisualDecisionError):
                self.validate(report)

    def test_set_consistency_is_never_measured_against_the_icon_itself(self) -> None:
        report = self.report()
        report["confirmed_set"] = [{"id": "heizung-standard", "asset": report["icons"][0]["variants"][0]["asset"]}]
        with self.assertRaisesRegex(visual.VisualDecisionError, "against itself"):
            self.validate(report)

    def test_each_variant_file_is_named_after_its_icon_and_value(self) -> None:
        """ADR 0003: the file of a variant is <icon>-<value>; receipts bind that id."""
        own_style = {"brand-fit": "required", "quality-benchmark": "required", "variant-pair": "required"}
        cases = {
            "a file named after another icon": lambda icon: icon["variants"][0].update(id="heizung-an"),
            "one value twice": lambda icon: icon["variants"][1].update(value="an", id="licht-an"),
            "an axis with a single value": lambda icon: icon["variants"].pop(),
            "two files without an axis": lambda icon: icon.update(axis=None),
        }
        for name, change in cases.items():
            with self.subTest(name):
                icon = self.licht(dict(own_style)); change(icon)
                if icon["axis"] is None: icon["human_reviews"].pop("variant-pair")
                with self.assertRaisesRegex(visual.VisualDecisionError, "variant"):
                    self.validate(self.report(icon))

    def test_a_violated_build_rule_prevents_release(self) -> None:
        """ADR 0006: a build-rule violation blocks and is not discussed."""
        cases = {
            "coordinate off the grid": ('<line x1="4.5" y1="4" x2="20" y2="4"/>', {}, {}, "grid"),
            "stroke width differs from the rule": (HEIZUNG, {"stroke_width": "1.5"}, {}, "stroke width"),
            "an own colour beside currentColor": (HEIZUNG + '<line x1="4" y1="4" x2="20" y2="4" stroke="#AE1C23"/>', {}, {}, "monochrome"),
            "a canvas other than the grid": (HEIZUNG, {"view_box": "0 0 32 32"}, {}, "grid"),
            "a transform nobody can measure": ('<g transform="translate(0.5)">' + HEIZUNG + "</g>", {}, {}, "transform"),
            "linecap differs from the rule": (HEIZUNG, {}, {"stroke_linecap": "square"}, "linecap"),
        }
        for name, (body, svg, rules, message) in cases.items():
            with self.subTest(name):
                raw = icon_svg("heizung-standard", body, **svg)
                variant = self.variant("heizung-standard", HEIZUNG)
                variant["asset"] = fixtures.write(self.root / "heizung-standard.svg", raw)
                try:
                    variant["objective_checks"] = visual._icon_static_checks(raw, "icon", "heizung-standard")
                except visual.VisualDecisionError as refused:
                    self.assertRegex(str(refused), message); continue
                report = self.report({"icon_id": "heizung", "axis": None, "variants": [variant],
                                      "human_reviews": {"brand-fit": "required", "quality-benchmark": "required"}})
                report["build_rules"].update(rules)
                with self.assertRaisesRegex(visual.VisualDecisionError, message):
                    self.validate(report)


class IconReceiptTests(IconReportFixture):
    """Each Affinity-authored variant file stands under its own receipt."""

    def setUp(self) -> None:
        super().setUp()
        secrets = tempfile.TemporaryDirectory(); self.addCleanup(secrets.cleanup)
        self.bundle = fixtures.affinity_bundle(self.project, self.root, Path(secrets.name).resolve())
        self.observation = self.bundle["observation"]

    def authored(self, file_id: str) -> dict:
        variant = self.variant(file_id, HEIZUNG)
        raw = (self.root / f"{file_id}.svg").read_bytes()
        variant["asset"] = fixtures.write(self.root / "staging/logo-doc" / f"{file_id}.svg", raw)
        variant["asset"]["path"] = f"staging/logo-doc/{file_id}.svg"
        variant["creation_origin"] = "affinity-mcp"
        return variant

    def receipt(self, variant: dict, *, operation: str = "create-icon-variant", output: dict | None = None) -> dict:
        body = {name: value for name, value in next(iter(self.bundle["documents"].values())).items() if name != "hmac_sha256"}
        body.update(operation=operation, outputs=[output or {"candidate_id": variant["id"], **variant["asset"]}],
                    nonce=hashlib.sha256(("icon-" + variant["id"]).encode()).hexdigest())
        body["hmac_sha256"] = hmac.new(self.bundle["operation_key"], visual.canonical_json(body), hashlib.sha256).hexdigest()
        return body

    def check(self, variant: dict, receipts: dict) -> None:
        report = self.report({"icon_id": "heizung", "axis": None, "variants": [variant],
                              "human_reviews": {"brand-fit": "required", "quality-benchmark": "required"}})
        visual.validate_icon_report(report, self.root, self.observation, receipts, project_root=self.project, run_id="run-a",
                                    key=self.bundle["key"], replay_nonces=set(), now=fixtures.NOW)

    def test_one_run_may_author_logo_and_icons_in_affinity(self) -> None:
        """Logo and icon receipts share one inventory; neither branch may claim all of it."""
        variant = self.authored("heizung-standard")
        receipt = fixtures.canonical_write(self.root, "receipt-heizung-standard.json", self.receipt(variant))
        document = json.loads((self.root / "candidate-set.json").read_text(encoding="utf-8"))
        brief = json.loads((self.root / "visual-brief.json").read_text(encoding="utf-8"))
        brief.update(open_decisions=["logo", "lockup", "icon"], icon_target_sizes_px=[24])
        fixtures.canonical_write(self.root, "visual-brief.json", brief)
        document["brief_sha256"] = visual.sha256_bytes(visual.canonical_json(brief))
        value = {"domain": "icon", "icons": [{"icon_id": "heizung", "axis": None, "variants": ["standard"]}],
                 "creation_origin": "affinity-mcp", "report": {"path": "icon-report.json", "sha256": "0" * 64}}
        document["candidates"].append({"id": "icon-choice", "kind": "icon", "label": "Icon-Stil A", "value": value,
                                       "value_sha256": "0" * 64, "source_ids": ["impeccable"], "status": "candidate"})
        context = visual.candidate_context_sha256(document)
        logo = json.loads((self.root / "logo-report.json").read_text(encoding="utf-8")); logo["candidate_set_sha256"] = context
        logo_binding = fixtures.canonical_write(self.root, "logo-report.json", logo)
        for candidate in document["candidates"][:-1]:
            candidate["value"]["report"] = dict(logo_binding)
        report = self.report({"icon_id": "heizung", "axis": None, "variants": [variant],
                              "human_reviews": {"brand-fit": "required", "quality-benchmark": "required"}})
        report["candidate_set_sha256"] = context
        value["report"] = fixtures.canonical_write(self.root, "icon-report.json", report)
        for candidate in document["candidates"]:
            candidate["value_sha256"] = visual.sha256_bytes(visual.canonical_json(candidate["value"]))
        fixtures.canonical_write(self.root, "candidate-set.json", document)
        auth = {**self.bundle["auth"], "receipts": {**self.bundle["auth"]["receipts"], "heizung-standard": receipt}}
        result = visual.validate_bundle(self.project, "run-a", fixtures.NOW, sidecar_root=visual.ROOT.parents[1], affinity_auth=auth)
        self.assertEqual({"logo-choice", "icon-choice"}, {item["id"] for item in result["eligible_candidates"]} & {"logo-choice", "icon-choice"})

    def test_an_authored_variant_is_accepted_under_its_own_receipt(self) -> None:
        variant = self.authored("heizung-standard")
        self.check(variant, {"heizung-standard": self.receipt(variant)})

    def test_an_authored_variant_without_its_own_receipt_is_refused(self) -> None:
        variant = self.authored("heizung-standard")
        other = {"candidate_id": "heizung-standard", "path": "staging/logo-doc/mark-a.svg",
                 "sha256": visual.sha256_bytes((self.root / "staging/logo-doc/mark-a.svg").read_bytes())}
        cases = {"no receipt": ({}, "receipt"),
                 "a logo operation": ({"heizung-standard": self.receipt(variant, operation="create-logo-variant")}, "operation"),
                 "a receipt for another file": ({"heizung-standard": self.receipt(variant, output=other)}, "does not bind")}
        for name, (receipts, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(visual.VisualDecisionError, message):
                self.check(variant, receipts)


class IconBundleTests(IconReportFixture):
    """A style candidate is eligible when its value and its icon report agree."""

    def bundle(self, value: dict | None, *, brief_sizes: list[int] | None = [24], now: int = fixtures.NOW) -> dict:
        document = fixtures.color_bundle(self.project, self.root)
        self.observation = json.loads((self.root / "affinity-observation.json").read_text(encoding="utf-8"))
        # The sheet checks the observation against the clock, as the logo path does.
        self.observation.update(observed_at=now - 10, expires_at=now + 10)
        fixtures.canonical_write(self.root, "affinity-observation.json", self.observation)
        brief = json.loads((self.root / "visual-brief.json").read_text(encoding="utf-8"))
        if brief_sizes is not None:
            brief.update(open_decisions=["color", "icon"], icon_target_sizes_px=brief_sizes)
        fixtures.canonical_write(self.root, "visual-brief.json", brief)
        document["brief_sha256"] = visual.sha256_bytes(visual.canonical_json(brief))
        if value is not None:
            value = {**value, "report": {"path": "icon-report.json", "sha256": "0" * 64}}
            document["candidates"].append({"id": "icon-choice", "kind": "icon", "label": "Icon-Stil A", "value": value,
                                           "value_sha256": "0" * 64, "source_ids": ["impeccable"], "status": "candidate"})
        context = visual.candidate_context_sha256(document)
        document["candidates"][0]["value"]["report"] = fixtures.canonical_write(self.root, "color-report.json", visual.color_contrast_report(
            context, "color-a", fixtures.palette(), ["text", "icon"]))
        if value is not None:
            report = self.report(); report["candidate_set_sha256"] = context
            value["report"] = fixtures.canonical_write(self.root, "icon-report.json", report)
        for candidate in document["candidates"]:
            candidate["value_sha256"] = visual.sha256_bytes(visual.canonical_json(candidate["value"]))
        fixtures.canonical_write(self.root, "candidate-set.json", document)
        return visual.validate_bundle(self.project, "run-a", now, sidecar_root=visual.ROOT.parents[1])

    HEIZUNG_STYLE = {"domain": "icon", "icons": [{"icon_id": "heizung", "axis": None, "variants": ["standard"]}],
                     "creation_origin": "owner-supplied"}

    def test_an_icon_style_with_an_agreeing_report_is_eligible(self) -> None:
        self.assertIn("icon-choice", [item["id"] for item in self.bundle(self.HEIZUNG_STYLE)["eligible_candidates"]])

    def test_an_open_icon_decision_without_a_style_is_stated_as_open(self) -> None:
        """The sheet names every open domain; it neither drops nor refuses it."""
        now = int(time.time())
        self.bundle(None, now=now)
        visual.render_sheet(self.project, "run-a", now)
        page = (self.root / "sheet" / "index.html").read_text(encoding="utf-8")
        section = next(node for node in fixtures.nodes(fixtures.PageTree(page).root, "section") if node["attrs"].get("id") == "candidates-icon")
        self.assertIn("Offen: das Bundle enthält keine Kandidaten für diese Domäne.", fixtures.text_of(section))

    def test_the_playground_refuses_icons_it_cannot_show(self) -> None:
        now = int(time.time())
        self.bundle(self.HEIZUNG_STYLE, now=now)
        with self.assertRaisesRegex(visual.VisualDecisionError, "playground has no view for icon"):
            visual.render_playground(self.project, "run-a", now)

    def test_the_report_is_judged_at_the_sizes_the_brief_binds(self) -> None:
        with self.subTest("the report looked at other sizes"), self.assertRaisesRegex(visual.VisualDecisionError, "target sizes"):
            self.bundle(self.HEIZUNG_STYLE, brief_sizes=[20, 24, 32])
        with self.subTest("the brief leaves no icon decision open"), self.assertRaisesRegex(visual.VisualDecisionError, "target sizes"):
            self.setUp(); self.bundle(self.HEIZUNG_STYLE, brief_sizes=None)

    def test_the_sheet_shows_a_contact_sheet_and_each_icon_at_every_target_size(self) -> None:
        """004: the style is judged as a set, each icon at every bound size; an
        enlarged view is extra and never replaces a target size."""
        self.sizes = [20, 24, 32]
        variants = [self.variant("licht-an", HEIZUNG), self.variant("licht-aus", '<path d="M6 4v16"/>')]
        variants[1]["objective_checks"]["canonical_svg_sha256"] = visual._svg_static_checks(
            (self.root / "licht-aus.svg").read_bytes(), "x", "licht-aus")["canonical_svg_sha256"]
        style = {"domain": "icon", "icons": [{"icon_id": "licht", "axis": "zustand", "variants": ["an", "aus"]}], "creation_origin": "owner-supplied"}
        self.report = lambda *icons, _report=self.report: _report({"icon_id": "licht", "axis": "zustand", "variants": variants,
                                                                     "human_reviews": {"brand-fit": "required", "quality-benchmark": "required", "variant-pair": "required"}})
        now = int(time.time())
        self.bundle(style, brief_sizes=[20, 24, 32], now=now)
        visual.render_sheet(self.project, "run-a", now)
        page = (self.root / "sheet" / "index.html").read_text(encoding="utf-8")
        css = (self.root / "sheet" / "style.css").read_text(encoding="utf-8")
        section = next(node for node in fixtures.nodes(fixtures.PageTree(page).root, "section") if node["attrs"].get("id") == "candidates-icon")
        contact = next(fixtures.nodes(section, css="icon-contact"))
        for size in (20, 24, 32):
            self.assertIn(f".icon-px-{size} img{{width:{size}px;height:{size}px}}", css)
            row = next(fixtures.nodes(contact, css=f"icon-px-{size}"))
            self.assertEqual(2, len(list(fixtures.nodes(row, "img"))), f"contact sheet row {size} px")
        for variant in ("licht-an", "licht-aus"):
            card = next(node for node in fixtures.nodes(section, css="icon-variant") if variant in fixtures.text_of(node))
            self.assertEqual({"icon-px-20", "icon-px-24", "icon-px-32", "icon-enlarged"},
                             {cls for node in fixtures.nodes(card) for cls in node["attrs"].get("class", "").split() if cls.startswith("icon-px-") or cls == "icon-enlarged"})
            self.assertIn("vergrößert, ersetzt nicht die Zielgröße", fixtures.text_of(card))
        self.assertIn("Icons", page)

    def test_a_style_value_that_differs_from_its_report_is_refused(self) -> None:
        cases = {
            "an icon the report does not show": {**self.HEIZUNG_STYLE, "icons": [{"icon_id": "licht", "axis": None, "variants": ["standard"]}]},
            "an axis the report does not carry": {**self.HEIZUNG_STYLE, "icons": [{"icon_id": "heizung", "axis": "zustand", "variants": ["an", "aus"]}]},
            "an origin the files do not have": {**self.HEIZUNG_STYLE, "creation_origin": "affinity-mcp"},
        }
        for name, value in cases.items():
            with self.subTest(name), self.assertRaisesRegex(visual.VisualDecisionError, "icon report candidate/value differs"):
                self.setUp(); self.bundle(value)


class LogoRefusesIconOperationTests(unittest.TestCase):
    """A receipt that created an icon variant never authorises a logo candidate."""

    def logo_report_with(self, operation: str | None) -> None:
        with tempfile.TemporaryDirectory() as project_name, tempfile.TemporaryDirectory() as secrets_name:
            project = Path(project_name).resolve()
            root = visual.visual_workspace(project, "run-a", create=True)
            bundle = fixtures.affinity_bundle(project, root, Path(secrets_name).resolve())
            receipts = {}
            for candidate_id, document in bundle["documents"].items():
                body = {name: value for name, value in document.items() if name != "hmac_sha256"}
                if operation is not None:
                    body["operation"] = operation
                body["hmac_sha256"] = hmac.new(bundle["operation_key"], visual.canonical_json(body), hashlib.sha256).hexdigest()
                receipts[candidate_id] = body
            report = json.loads((root / "logo-report.json").read_text(encoding="utf-8"))
            visual.validate_logo_report(report, root, bundle["observation"], receipts, project_root=project,
                                        run_id="run-a", key=bundle["key"], replay_nonces=set(), now=fixtures.NOW)

    def test_logo_report_accepts_its_own_signed_receipts(self) -> None:
        self.logo_report_with(None)

    def test_logo_report_refuses_a_receipt_that_created_an_icon_variant(self) -> None:
        with self.assertRaisesRegex(visual.VisualDecisionError, "operation"):
            self.logo_report_with("create-icon-variant")


if __name__ == "__main__":
    unittest.main()
