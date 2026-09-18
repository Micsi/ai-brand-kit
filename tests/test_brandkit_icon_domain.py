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
            visual.validate_brief(brief, root, project_root=project, run_id="run-a")


class IconBundleTests(unittest.TestCase):
    """Until brandkit-icon-report.v1 exists, no icon candidate becomes eligible."""

    def test_bundle_refuses_an_icon_candidate_it_cannot_yet_validate(self) -> None:
        with tempfile.TemporaryDirectory() as project_name:
            project = Path(project_name).resolve()
            root = visual.visual_workspace(project, "run-a", create=True)
            document = fixtures.color_bundle(project, root)
            value = icon_value()
            document["candidates"].append({"id": "icon-choice", "kind": "icon", "label": "Icon-Stil A", "value": value,
                                           "value_sha256": "0" * 64, "source_ids": ["impeccable"], "status": "candidate"})
            context = visual.candidate_context_sha256(document)
            color = document["candidates"][0]["value"]
            color["report"] = fixtures.canonical_write(root, "color-report.json", visual.color_contrast_report(
                context, "color-a", fixtures.palette(), ["text", "icon"]))
            value["report"] = fixtures.canonical_write(root, "icon-report.json", {"candidate_set_sha256": context, "objective_status": "pass"})
            for candidate in document["candidates"]:
                candidate["value_sha256"] = visual.sha256_bytes(visual.canonical_json(candidate["value"]))
            fixtures.canonical_write(root, "candidate-set.json", document)
            with self.assertRaisesRegex(visual.VisualDecisionError, "icon report validation is not available"):
                visual.validate_bundle(project, "run-a", fixtures.NOW, sidecar_root=visual.ROOT.parents[1])


class IconSheetTests(unittest.TestCase):
    """The sheet names every open domain; until it can show icons it refuses
    rather than dropping the open icon decision from the owner's view."""

    def test_sheet_refuses_a_brief_with_an_open_icon_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            project = Path(temp_name).resolve() / "project"; project.mkdir()
            root = visual.visual_workspace(project, "run-a", create=True)
            now = int(time.time())
            fixtures.multi_domain_bundle(project, root, now, {"color": 1}, open_decisions=["color", "icon"])
            with self.assertRaisesRegex(visual.VisualDecisionError, "icon"):
                visual.render_sheet(project, "run-a", now)


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
