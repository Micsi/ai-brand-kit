from __future__ import annotations

import ast
import inspect
import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from jsonschema import ValidationError

import scripts.materialize_brandkit_instructions as instruction_materializer
import scripts.brandkit_discovery as discovery
from scripts.materialize_brandkit_instructions import materialize
from scripts.validate_brandkit_skills import COLLISION_FIXTURES, FIXTURES, IMPECCABLE_COMMANDS, ROOT, collision_dry_run, dry_run, execute_request, frontmatter, impeccable_load_trace, impeccable_source_routes, load, route_design_request, transitive_playbook_trace, validate, validate_impeccable_load_trace, validate_progressive_disclosure, validate_public_contracts, validate_real_impeccable_context_matrix, validate_release_manifest

# The core module imports its siblings by bare name, the way it does under its
# own suite and under the CLI; the adapter reaches it as scripts.* from here.
sys.path.insert(0, str(ROOT / "scripts"))
import scripts.brandkit_visual_decisions as visual_decisions

# The bundle builders of the *core* suite, reused verbatim.  A fixture written
# here would be a second, unreviewed opinion about what a valid bundle is - and
# an invented bundle shape is precisely what let r1c ship an adapter that no
# real bundle could pass through.  Importing the functions (not the test
# classes) keeps this module's test count its own.
from tests.test_brandkit_visual_decisions import canonical_write, deadline, five_domain_affinity_bundle, five_domain_bundle, font_candidates, multi_domain_bundle, rebind

VISUAL_FIVE = ("color", "typography", "code-font", "logo", "lockup")
DISCOVERY_HANDOFF = ".brandkit/work/ci-pilot/discovery/handoff.json"

# The seam between this module and the core, by symbol name.  Substituting any
# of these is what let r1c ship an adapter no real bundle could pass through,
# so the rule is checked rather than remembered - see
# ``test_no_test_substitutes_the_visual_core``.
GUARDED_SEAM = frozenset({
    "validate_bundle", "validate_source_contract", "render_sheet", "validate_logo_report",
    "_validate_bundle_locked", "_render_sheet",
    "_validated_visual_bundle", "_closed_core_result", "_release_bound_source_contract",
    "_visual_source_records", "_visual_core",
})
# Calls that install a replacement.  ``getattr`` and ``inspect.signature`` read
# the seam and are not on it; only the writers are listed.
SUBSTITUTING_CALLS = frozenset({"patch", "patch.object", "setattr", "object", "setitem"})


def _string_literal(node: ast.AST) -> str | None:
    """Fold the string a node denotes, or ``None`` if it denotes no string.

    Concatenation is folded because ``"_valid" + "ated_visual_bundle"`` is one
    of the eight forms measured against the old substring guard, and it walked
    straight past it.  f-strings with constant parts fold the same way.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _string_literal(node.left), _string_literal(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts = [_string_literal(item) for item in node.values]
        return None if any(item is None for item in parts) else "".join(parts)
    return None


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


def core_substitutions(source: str) -> list[str]:
    """Every place ``source`` replaces a symbol of the adapter/core seam.

    Two shapes, and between them they cover every form a substitution can take
    without going through ``exec``: assigning to the attribute, and handing the
    attribute - or its *name*, however spelled - to something that installs a
    replacement.  The module the symbol is reached through is deliberately not
    part of the test: an alias is one import line away, so the symbol alone
    decides.
    """
    findings: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            for target in list(getattr(node, "targets", [])) or [node.target]:
                if isinstance(target, ast.Attribute) and target.attr in GUARDED_SEAM:
                    findings.append(f"assignment to {_dotted(target) or target.attr}")
        if isinstance(node, ast.Call):
            name = _dotted(node.func) or ""
            if name.split(".")[-1] not in SUBSTITUTING_CALLS and name not in SUBSTITUTING_CALLS:
                continue
            for argument in ast.walk(node):
                if isinstance(argument, ast.Attribute) and argument.attr in GUARDED_SEAM:
                    findings.append(f"{name} replaces {argument.attr}")
                literal = _string_literal(argument)
                if literal is not None and literal.rsplit(".", 1)[-1] in GUARDED_SEAM:
                    findings.append(f"{name} replaces {literal}")
    return sorted(set(findings))


class BrandKitSkillContractTests(unittest.TestCase):
    RELEASE_MANIFEST_SHA256 = "566622f1161393457020f07dc493c637183774ae9c2c83335b5009ad6177dcae"
    def test_publish_skill_pins_claude_provider_without_conflating_agent_discovery(self) -> None:
        skill = (ROOT / "skills/brandkit-publish/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Claude, Codex und Hermes rufen denselben `brandkit-publish`-Skill auf", skill)

        def provider_contract_errors(text: str) -> list[str]:
            errors = []
            if "--provider claude" not in text:
                errors.append("Claude provider is not pinned")
            if "--provider <claude|codex>" in text:
                errors.append("provider placeholder permits Codex")
            if "Claude Code 2.1.223" not in text or "geschlossenen Envelope-Vertrag" not in text:
                errors.append("Claude Code 2.1.223 envelope is not normative")
            if "Codex CLI ist für diesen Humanizer-Release nicht unterstützt und wird fail-closed abgewiesen" not in text:
                errors.append("Codex CLI is presented as a supported Humanizer provider")
            return errors

        self.assertEqual([], provider_contract_errors(skill))
        self.assertEqual([], provider_contract_errors(skill + "\nClaude, Codex und Hermes entdecken denselben Skill.\n"))
        self.assertIn(
            "provider placeholder permits Codex",
            provider_contract_errors(skill.replace("--provider claude", "--provider <claude|codex>")),
        )
        self.assertIn(
            "Codex CLI is presented as a supported Humanizer provider",
            provider_contract_errors(skill.replace("nicht unterstützt und wird fail-closed abgewiesen", "unterstützt und darf erfolgreich laufen")),
        )

    def test_repository_contract_and_artifact_dry_runs_are_valid(self) -> None:
        self.assertEqual([], validate())
        self.assertEqual([], collision_dry_run())
        with tempfile.TemporaryDirectory() as temporary:
            result_dir = Path(temporary) / "results"
            self.assertEqual([], dry_run(result_dir=result_dir))
            cases = load(FIXTURES)["cases"]
            self.assertEqual(len(cases) * 2, len(list(result_dir.glob("*.json"))))
            for result in result_dir.glob("*.result.json"):
                self.assertEqual("brandkit-skill-result.v1", load(result)["schema_version"])

    def test_public_skill_contract_binds_cli_python_package_and_document_surfaces(self) -> None:
        contract = load(ROOT / "brandkit-skill-contract.v1.json")
        public = contract["public_contracts"]
        self.assertEqual(
            ["materialize", "migrate", "pin", "resolve", "rollback", "validate"],
            public["cli"]["scripts/brand_package.py"]["commands"],
        )
        self.assertIn("validate_package", public["python"]["scripts/brand_package.py"])
        self.assertEqual("brand-package.v1", public["brand_package"]["schema_version"])
        self.assertEqual(
            {"freeform", "offer", "one-pager", "report"},
            set(public["documents"]["types"]),
        )

    def test_public_contract_drift_and_undeclared_document_type_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._copy_repo(temporary)
            cli = root / "scripts/brand_package.py"
            cli.write_text(cli.read_text(encoding="utf-8").replace('sub.add_parser("validate")', 'sub.add_parser("verify")'), encoding="utf-8")
            errors = validate(root)
            self.assertTrue(any("CLI commands drifted" in item for item in errors), errors)
        with tempfile.TemporaryDirectory() as temporary:
            root = self._copy_repo(temporary)
            shutil.copy2(root / "document-types/report.v1.json", root / "document-types/brief.v1.json")
            errors = validate(root)
            self.assertTrue(any("document type catalog drifted" in item for item in errors), errors)

    def test_progressive_disclosure_is_closed_and_design_context_is_complete(self) -> None:
        contract = load(ROOT / "brandkit-skill-contract.v1.json")
        disclosure = contract["progressive_disclosure"]
        self.assertEqual(10, len(disclosure["routes"]))
        self.assertEqual(10, len({route["skill"] for route in disclosure["routes"].values()}))
        self.assertEqual(
            ["Overview", "Source of Truth", "Visual Direction", "Tokens and Roles", "Typography", "Spacing", "Components", "Usage Rules", "Anti-References"],
            disclosure["design_context"]["required_sections"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = self._copy_repo(temporary)
            manifest = load(root / "brandkit-skill-contract.v1.json")
            manifest["progressive_disclosure"]["routes"]["pdf"]["context"].append(
                manifest["progressive_disclosure"]["routes"]["pdf"]["primary"]
            )
            self._write_json(root / "brandkit-skill-contract.v1.json", manifest)
            errors = validate(root)
            self.assertTrue(any("load trace is not disjoint" in item for item in errors), errors)

    def test_optional_discovery_bundle_is_closed_and_pinned_to_official_bytes(self) -> None:
        upstream = Path("/Volumes/Daten/Projekte/BrandKit/external-sources/mattpocock-skills")
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "bundle"
            subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(upstream), str(checkout)], check=True)
            revision = discovery.load_contract()["revision"]
            official_remote = "https" + "://github.com/mattpocock/skills.git"
            subprocess.run(["git", "-C", str(checkout), "remote", "set-url", "origin", official_remote], check=True)
            subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "--detach", revision], check=True)
            result = discovery.verify_bundle_checkout(checkout)
            self.assertEqual(discovery.load_contract()["bundle"]["content_sha256"], result["content_sha256"])
            project = Path(temporary) / "project"; project.mkdir()
            key = Path(temporary) / "owner.key"; key.write_bytes(b"owner-test-key-material-32-bytes!!"); key.chmod(0o600)
            self._discovery_activation(project, checkout)
            trace = discovery.prepare_orchestration(project, checkout, "ci-pilot", key)
            self.assertEqual("$brandkit-interview", trace["entrypoint"])
            self.assertEqual(str(project.resolve() / ".brandkit/work/ci-pilot/discovery"), trace["cwd"])
            self.assertEqual("grilling", trace["phase"])
            self.assertEqual(["grilling"], [item["skill"] for item in trace["steps"]])
            with self.assertRaises(discovery.DiscoveryError): discovery.handoff_snapshot(project, ".brandkit/work/ci-pilot/discovery/handoff.json", key)
            discovery.write_progress(project, "ci-pilot", [], ["image-rules"], list(discovery.BRAND_AXES[:-1]), "1" * 64, key)
            trace = discovery.prepare_orchestration(project, checkout, "ci-pilot", key)
            self.assertEqual("grilling", trace["phase"])
            self.assertEqual(1, trace["answer_turns"])
            discovery.write_progress(project, "ci-pilot", [], ["image-rules"], list(discovery.BRAND_AXES[:-1]), "2" * 64, key)
            trace = discovery.prepare_orchestration(project, checkout, "ci-pilot", key)
            self.assertEqual(["domain-modeling"], [item["skill"] for item in trace["steps"]])
            self.assertEqual("reference-only-never-invoke", trace["wrapper"]["policy"])
            subprocess.run(["git", "-C", str(checkout), "remote", "set-url", "origin", "https" + "://github.com/other/skills.git"], check=True)
            with self.assertRaises(discovery.DiscoveryError): discovery.verify_bundle_checkout(checkout)
            subprocess.run(["git", "-C", str(checkout), "remote", "set-url", "origin", official_remote], check=True)
            subprocess.run(["git", "-C", str(checkout), "switch", "-q", "-c", "mixed-source"], check=True)
            with self.assertRaises(discovery.DiscoveryError): discovery.verify_bundle_checkout(checkout)
            subprocess.run(["git", "-C", str(checkout), "checkout", "-q", "--detach", revision], check=True)
            victim = checkout / discovery.load_contract()["bundle"]["members"][0]["path"]
            original = victim.read_bytes(); victim.write_bytes(original + b"drift")
            with self.assertRaises(discovery.DiscoveryError): discovery.verify_bundle_checkout(checkout)
            victim.write_bytes(original)
            extra = victim.parent / "EXTRA"; extra.write_text("extra")
            with self.assertRaises(discovery.DiscoveryError): discovery.verify_bundle_checkout(checkout)
            extra.unlink(); victim.unlink()
            with self.assertRaises(discovery.DiscoveryError): discovery.verify_bundle_checkout(checkout)
            victim.symlink_to(checkout / "README.md")
            with self.assertRaises(discovery.DiscoveryError): discovery.verify_bundle_checkout(checkout)

    def test_discovery_owner_receipt_is_real_confined_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, handoff, key = self._discovery_project(Path(temporary) / "project")
            review = discovery.render_review(project, handoff, key)
            self.assertIn("Diese Entscheidungen bestätigen? Korrekturen?", review["markdown"])
            self.assertTrue(review["markdown"].startswith("# Brand Interview Review"))
            self.assertNotIn('"schema_version"', review["markdown"])
            with self.assertRaises(discovery.DiscoveryError):
                discovery.make_owner_receipt(project, handoff, review["path"], key, "owner@example", "approval-001", "", 60, now=100)
            receipt = discovery.make_owner_receipt(project, handoff, review["path"], key, "owner@example", "approval-001", "Diese Entscheidungen bestätige ich.", 60, now=100)
            receipt_path = ".brandkit/owner-receipts/ci-pilot.json"
            discovery.write_owner_receipt(project, receipt_path, receipt)
            consumed = discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=101)
            self.assertEqual(["brand-character"], [item["id"] for item in consumed["decisions"]])
            self.assertEqual({"context", "adr"}, {item["role"] for item in consumed["artifacts"]})
            self.assertNotIn("recommendations", consumed)
            source_output = consumed["sources"][0]
            self.assertEqual({"id", "classification", "path", "size", "sha256", "utf8_base64"}, set(source_output))
            self.assertEqual(("owner-answer-2", "owner-supplied"), (source_output["id"], source_output["classification"]))
            self.assertEqual({source_output["id"]}, {item for decision in consumed["decisions"] for item in decision["source_ids"]})
            progress_path = project / ".brandkit/work/ci-pilot/interview-progress.md"
            review_path = project / review["path"]
            receipt_file = project / receipt_path
            replay = (progress_path.read_bytes(), review_path.read_bytes(), (project / handoff).read_bytes(), receipt_file.read_bytes())
            discovery.write_progress(project, "ci-pilot", [], [], list(discovery.BRAND_AXES), "3" * 64, key)
            current_progress = progress_path.read_bytes()
            with self.assertRaisesRegex(discovery.DiscoveryError, "fresh run-id"):
                discovery.initialize_progress(project, "ci-pilot", key)
            progress_path.write_bytes(replay[0]); review_path.write_bytes(replay[1]); (project / handoff).write_bytes(replay[2]); receipt_file.write_bytes(replay[3])
            with self.assertRaisesRegex(discovery.DiscoveryError, "stale or replayed"):
                discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=101)
            progress_path.write_bytes(current_progress)
            discovery.write_progress(project, "ci-pilot", [], [], list(discovery.BRAND_AXES), "4" * 64, key)
            review = discovery.render_review(project, handoff, key)
            receipt = discovery.make_owner_receipt(project, handoff, review["path"], key, "owner@example", "approval-002", "Diese Entscheidungen bestätige ich.", 60, now=100)
            discovery.write_owner_receipt(project, receipt_path, receipt)
            with self.assertRaises(discovery.DiscoveryError):
                discovery.handoff_snapshot(project.parent, handoff, key)
            with self.assertRaises(discovery.DiscoveryError):
                discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=161)
            wrong = project / "wrong.key"; wrong.write_bytes(b"w" * 32); wrong.chmod(0o600)
            with self.assertRaises(discovery.DiscoveryError):
                discovery.verify_owner_receipt(project, handoff, receipt_path, wrong, now=101)
            key.chmod(0o644)
            with self.assertRaises(discovery.DiscoveryError):
                discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=101)
            key.chmod(0o600)
            key_bytes = key.read_bytes(); key.unlink(); key.symlink_to(wrong)
            with self.assertRaises(discovery.DiscoveryError):
                discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=101)
            key.unlink(); key.write_bytes(key_bytes); key.chmod(0o600)
            context = project / ".brandkit/work/ci-pilot/discovery/CONTEXT.md"
            context_bytes = context.read_bytes(); context.write_text("changed artifact\n")
            with self.assertRaises(discovery.DiscoveryError):
                discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=101)
            context.write_bytes(context_bytes)
            handoff_file = project / handoff
            original = handoff_file.read_bytes()
            for mutate in (
                lambda value: value["sources"][0].update(classification="observed"),
                lambda value: (value["sources"][0].update(id="fabricated-source"), value["decisions"][0].update(source_ids=["fabricated-source"])),
            ):
                changed = json.loads(original); mutate(changed); handoff_file.write_bytes(discovery.canonical(changed))
                with self.assertRaises(discovery.DiscoveryError):
                    discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=101)
            handoff_file.write_bytes(original)
            changed = json.loads(original); changed["decisions"][0]["value"] = "invented"
            handoff_file.write_bytes(discovery.canonical(changed))
            with self.assertRaises(discovery.DiscoveryError):
                discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=101)
            handoff_file.write_bytes(original)
            source = project / ".brandkit/work/ci-pilot/discovery/sources/owner-answer-2.txt"
            source.write_text("fabricated source\n")
            with self.assertRaises(discovery.DiscoveryError):
                discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=101)

    def test_discovery_missing_symlink_recommendation_promotion_and_replay_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, handoff, key = self._discovery_project(base / "one")
            receipt_path = ".brandkit/owner-receipts/ci-pilot.json"
            review = discovery.render_review(project, handoff, key)
            receipt = discovery.make_owner_receipt(project, handoff, review["path"], key, "owner@example", "approval-001", "Diese Entscheidungen bestätige ich.", 60, now=100)
            discovery.write_owner_receipt(project, receipt_path, receipt)
            source = project / ".brandkit/work/ci-pilot/discovery/sources/owner-answer-2.txt"
            source.unlink()
            with self.assertRaises(discovery.DiscoveryError): discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=101)
            source.write_bytes(b"Owner says editorial and precise.\n")
            source.unlink(); source.symlink_to(project / "outside")
            with self.assertRaises(discovery.DiscoveryError): discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=101)
            source.unlink(); source.write_bytes(b"Owner says editorial and precise.\n")
            document = json.loads((project / handoff).read_bytes())
            document["decisions"].append({"id":"suggested-accent","value":"orange","source_ids":["owner-statement"]})
            (project / handoff).write_bytes(discovery.canonical(document))
            with self.assertRaises(discovery.DiscoveryError): discovery.handoff_snapshot(project, handoff, key)
            document = json.loads((ROOT / "fixtures/brandkit-skills/discovery-handoff.valid.json").read_bytes())
            document["decisions"][0]["status"] = "owner-confirmed"
            (project / handoff).write_bytes(discovery.canonical(document))
            with self.assertRaises(ValidationError): discovery.handoff_snapshot(project, handoff, key)
            project2, handoff2, key2 = self._discovery_project(base / "two")
            key2.write_bytes(key.read_bytes())
            target = project2 / receipt_path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(discovery.canonical(receipt)); target.chmod(0o600)
            with self.assertRaises(discovery.DiscoveryError): discovery.verify_owner_receipt(project2, handoff2, receipt_path, key2, now=101)

    def test_discovery_deterministic_swap_and_receipt_semantics_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, handoff, key = self._discovery_project(Path(temporary) / "project")
            source = project / ".brandkit/work/ci-pilot/discovery/sources/owner-answer-2.txt"
            original_walk = discovery._workspace_paths
            def swap_after_inventory(*args):
                result = original_walk(*args)
                source.write_bytes(b"swapped bytes\n")
                return result
            with patch.object(discovery, "_workspace_paths", side_effect=swap_after_inventory):
                with self.assertRaises(discovery.DiscoveryError): discovery.handoff_snapshot(project, handoff, key)
            source.write_bytes(b"Owner says editorial and precise.\n")
            for relative, content in (("CONTEXT.md", b"# Context\n<!-- empty -->\n                 \n"), ("docs/adr/0001-brand-structure.md", b"# ADR 0001\n<!-- no decision -->\n```\n```\n")):
                artifact = project / ".brandkit/work/ci-pilot/discovery" / relative
                original = artifact.read_bytes(); artifact.write_bytes(content)
                document = load(ROOT / "fixtures/brandkit-skills/discovery-handoff.valid.json")
                record = next(item for item in document["files"] if item["path"].endswith(relative))
                record.update(size=len(content), sha256=hashlib.sha256(content).hexdigest())
                (project / handoff).write_bytes(discovery.canonical(document))
                with self.assertRaises(discovery.DiscoveryError): discovery.handoff_snapshot(project, handoff, key)
                artifact.write_bytes(original)
            (project / handoff).write_bytes((ROOT / "fixtures/brandkit-skills/discovery-handoff.valid.json").read_bytes())
            review = discovery.render_review(project, handoff, key)
            with self.assertRaises(discovery.DiscoveryError):
                discovery.make_owner_receipt(project, handoff, review["path"], key, " bad issuer", "approval-001", "Diese Entscheidungen bestätige ich.", 60, now=100)
            receipt = discovery.make_owner_receipt(project, handoff, review["path"], key, "owner@example", "approval-001", "Diese Entscheidungen bestätige ich.", 60, now=100)
            receipt["expires_at"] = receipt["issued_at"] + 90000
            unsigned = {name: value for name, value in receipt.items() if name != "hmac_sha256"}
            receipt["hmac_sha256"] = __import__("hmac").new(key.read_bytes(), discovery.canonical(unsigned), hashlib.sha256).hexdigest()
            path = project / ".brandkit/owner-receipts/ci-pilot.json"; path.parent.mkdir(parents=True); path.write_bytes(discovery.canonical(receipt)); path.chmod(0o600)
            with self.assertRaises(discovery.DiscoveryError): discovery.verify_owner_receipt(project, handoff, ".brandkit/owner-receipts/ci-pilot.json", key, now=101)

    def test_discovery_markdown_substance_fence_matrix(self) -> None:
        for heading, prefix in (("# Context", False), ("# ADR 0001", True)):
            with self.subTest(heading=heading, case="positive"):
                discovery.meaningful_markdown(f"{heading}\nChoose one durable master brand.\n".encode(), "# ADR" if prefix else heading, heading, prefix=prefix)
            for decoration in (
                "```\n```",
                "```markdown language example\n   \n```",
                "~~~\n~~~",
                "~~~markdown language example\n<!-- inside -->\n~~~~",
                "<!-- comment -->\n   \n***",
                "~~~markdown language example\n",
                "```text\nnot closed",
            ):
                with self.subTest(heading=heading, decoration=decoration):
                    with self.assertRaises(discovery.DiscoveryError):
                        discovery.meaningful_markdown(f"{heading}\n{decoration}\n".encode(), "# ADR" if prefix else heading, heading, prefix=prefix)

    def test_discovery_multiturn_frontier_blocks_handoff_until_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            one_turn = Path(temporary) / "one-turn"; one_turn.mkdir()
            (one_turn / ".brandkit/work/ci-pilot/discovery").mkdir(parents=True)
            one_key = Path(temporary) / "one-owner.key"; one_key.write_bytes(b"owner-test-key-material-32-bytes!!"); one_key.chmod(0o600)
            raw_key = one_key.read_bytes()
            self.assertEqual(3, len({discovery._derive_key(raw_key, label) for label in (b"progress-state", b"progress-ledger", b"owner-receipt")}))
            discovery.initialize_progress(one_turn, "ci-pilot", one_key)
            local_key = one_turn / "local.key"; local_key.write_bytes(raw_key); local_key.chmod(0o600)
            with self.assertRaisesRegex(discovery.DiscoveryError, "outside the project"):
                discovery.read_progress(one_turn, "ci-pilot", local_key)
            initial_raw = (one_turn / ".brandkit/work/ci-pilot/interview-progress.md").read_bytes()
            first = discovery.write_progress(one_turn, "ci-pilot", [], [], list(discovery.BRAND_AXES), "1" * 64, one_key)
            self.assertEqual(("grilling", 1), (first["phase"], first["answer_turns"]))
            with self.assertRaisesRegex(discovery.DiscoveryError, "remain in grilling"):
                discovery.handoff_snapshot(one_turn, ".brandkit/work/ci-pilot/discovery/handoff.json", one_key)
            second = discovery.write_progress(one_turn, "ci-pilot", [], [], list(discovery.BRAND_AXES), "2" * 64, one_key)
            self.assertEqual(("docs-ready", 2), (second["phase"], second["answer_turns"]))
            current_raw = (one_turn / ".brandkit/work/ci-pilot/interview-progress.md").read_bytes()
            (one_turn / ".brandkit/work/ci-pilot/interview-progress.md").write_bytes(initial_raw)
            with self.assertRaisesRegex(discovery.DiscoveryError, "stale or replayed"):
                discovery.read_progress(one_turn, "ci-pilot", one_key)
            (one_turn / ".brandkit/work/ci-pilot/interview-progress.md").write_bytes(current_raw.replace(b"Answer turns: 2", b"Answer turns: 9"))
            with self.assertRaises(discovery.DiscoveryError):
                discovery.read_progress(one_turn, "ci-pilot", one_key)

            project, handoff, _key = self._discovery_project(Path(temporary) / "project")
            discovery.write_progress(project, "ci-pilot", ["color", "typography"], ["image-rules"], [axis for axis in discovery.BRAND_AXES if axis not in {"color", "typography", "image-rules"}], "3" * 64, _key)
            self.assertEqual(["color", "typography"], discovery.read_progress(project, "ci-pilot", _key)["open"])
            with self.assertRaisesRegex(discovery.DiscoveryError, "remain in grilling"):
                discovery.handoff_snapshot(project, handoff, _key)
            discovery.write_progress(project, "ci-pilot", [], ["image-rules"], [axis for axis in discovery.BRAND_AXES if axis != "image-rules"], "4" * 64, _key)
            review = discovery.render_review(project, handoff, _key)
            self.assertIn("Antwort-Turns: 4", review["markdown"])
            self.assertIn("- image-rules", review["markdown"])
            self.assertLess(review["markdown"].index("## Entscheidungen"), review["markdown"].index("## Unbestätigte Empfehlungen"))

    # The two tests that used to stand here drove ``prepare_visual_confirmation``
    # with the core replaced by a stand-in - once via ``_validated_visual_bundle``
    # and once via ``validate_bundle`` - over a source document the core contract
    # forbids.  They are gone rather than repaired: every property they claimed
    # is measured in ``VisualDecisionsRealCoreTests`` against the real core, and
    # ``test_no_test_substitutes_the_visual_core`` keeps them from coming back.

    def test_visual_route_and_build_bypass_gate_are_closed(self) -> None:
        fixtures = load(FIXTURES)
        cases = {item["request_id"]: item for item in fixtures["cases"]}
        for request_id, expected in (("visual-candidates-ready", "pass-with-todo"), ("build-positive", "package-ready"), ("build-required-visual-open", "package-draft-only")):
            case = cases[request_id]
            request = {"schema_version": "brandkit-skill-request.v1", "request_id": request_id, "skill": case["skill"], "input_artifacts": fixtures["input_artifacts"], "state": case["state"]}
            self.assertEqual(expected, execute_request(request)["decision"])
        self.assertEqual("brandkit-visual-decisions", route_design_request("Zeige und vergleiche Farbpaletten Kandidaten für die neue Marke."))
        missing_visual = dict(cases["build-positive"]["state"]); missing_visual.pop("visual_decisions")
        request = {"schema_version": "brandkit-skill-request.v1", "request_id": "missing-visual", "skill": "brandkit-build", "input_artifacts": fixtures["input_artifacts"], "state": missing_visual}
        with self.assertRaises(ValidationError):
            execute_request(request)

    def test_brandkit_interview_explicitly_orchestrates_dependencies_not_wrapper(self) -> None:
        contract = load(ROOT / "brandkit-skill-contract.v1.json")["optional_discovery"]["grill-with-docs"]
        self.assertEqual(("brandkit-interview-explicit", "$brandkit-interview", ["grilling", "domain-modeling"], "reference-only-never-invoke", "brandkit-interview-native"),
                         (contract["activation"], contract["invocation"], contract["orchestration"], contract["wrapper_policy"], contract["fallback"]))
        setup = (ROOT / "skills/brandkit-setup/SKILL.md").read_text(encoding="utf-8")
        interview = (ROOT / "skills/brandkit-interview/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("installiere nichts", setup)
        # The property is "grilling comes first, and only an empty frontier plus
        # two answer turns leaves it".  r1c re-anchored this on the substring
        # "in `grilling`", which occurs twice, so `index` measured the *first*
        # occurrence and a reordering of the second would have gone unseen.  The
        # anchors are now the three sentences that carry the rule, each required
        # to occur exactly once, and the ordering is measured across all three -
        # line wrapping is normalised away so the anchors survive a reflow but
        # not a reordering.
        flow = " ".join(interview.split())
        anchors = ("Bleibe über beliebig viele Turns in `grilling`",
                   "vor zwei Antwort-Turns bleibt der Trace auch bei leer gemeldeter Frontier zwingend in `grilling`",
                   "Erst wenn danach keine ungeklärte Frontier verbleibt, wechselt `prepare` zu `domain-modeling`")
        for anchor in anchors:
            self.assertEqual(1, flow.count(anchor), anchor)
        self.assertEqual(sorted(flow.index(anchor) for anchor in anchors),
                         [flow.index(anchor) for anchor in anchors])
        self.assertIn("`grill-with-docs` nie auf", interview)
        self.assertIn("vollständigen nativen Elf-Achsen-Pfad", interview)
        self.assertIn("als echte UTF-8-Dateien unter `sources/`", interview)

    def test_impeccable_full_route_platform_matrix_and_negative_traces(self) -> None:
        routes = ["routing", "new-work", *IMPECCABLE_COMMANDS]
        for route in routes:
            for platform in ("web", "ios", "android", "adaptive"):
                edit = route not in {"routing", "init", "shape"}
                if route == "live" and platform != "web":
                    with self.assertRaisesRegex(ValueError, "web-only"):
                        impeccable_load_trace(route, platform, edit=edit)
                    continue
                trace = impeccable_load_trace(route, platform, edit=edit)
                validate_impeccable_load_trace(trace, route, platform, edit=edit)
                self.assertEqual(1, trace.count("context:once"))
                self.assertEqual(1, sum(item.startswith("primary:") for item in trace))
        expected_init = {
            "web": ["context:once", "primary:init.md", "write:PRODUCT.md"],
            "ios": ["context:once", "primary:init.md", "write:PRODUCT.md", "native:ios.md"],
            "android": ["context:once", "primary:init.md", "write:PRODUCT.md", "native:android.md"],
            "adaptive": ["context:once", "primary:init.md", "write:PRODUCT.md", "native:ios.md+android.md"],
        }
        for platform, trace in expected_init.items():
            self.assertEqual(trace, impeccable_load_trace("init", platform))
        bad = (
            (["context:once", "primary:init.md", "native:ios.md", "write:PRODUCT.md"], "init", "ios"),
            (["context:once", "primary:audit.md", "native:ios.md"], "audit", "web"),
            (["context:once", "primary:audit.native.md", "primary:audit.md", "native:ios.md"], "audit", "ios"),
            (["context:once", "primary:init.md", "write:PRODUCT.md", "native:ios.md", "native:android.md"], "init", "adaptive"),
            (["context:once", "context:once", "primary:polish.md", "cross:craft-floor.md"], "polish", "web"),
        )
        for trace, route, platform in bad:
            with self.assertRaises(ValueError):
                validate_impeccable_load_trace(trace, route, platform, edit=route == "polish")

    def test_catalog_semantics_reject_self_foreign_unknown_and_content_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._copy_repo(temporary)
            contract_path = root / "brandkit-skill-contract.v1.json"
            original = load(contract_path)
            cases = []
            self_ref = json.loads(json.dumps(original)); self_ref["progressive_disclosure"]["catalog"]["build-workflow"]["sha256"] = hashlib.sha256(b"[self](workflow.md)\n").hexdigest(); cases.append((self_ref, "[self](workflow.md)\n", "self-references"))
            foreign = json.loads(json.dumps(original)); foreign["progressive_disclosure"]["routes"]["build"]["context"].append("foreign-license"); foreign["progressive_disclosure"]["catalog"]["foreign-license"] = {"path":"LICENSE","role":"contract","sha256":hashlib.sha256((root/"LICENSE").read_bytes()).hexdigest()}; cases.append((foreign, None, "forbidden"))
            unknown = json.loads(json.dumps(original)); unknown["progressive_disclosure"]["routes"]["build"]["context"].append("not-catalogued"); cases.append((unknown, None, "unknown"))
            drift = json.loads(json.dumps(original)); cases.append((drift, "# Replaced\n", "content hash drifted"))
            playbook = root / "skills/brandkit-build/references/workflow.md"; original_bytes = playbook.read_bytes()
            for contract, replacement, expected in cases:
                playbook.write_bytes(original_bytes if replacement is None else replacement.encode())
                self._write_json(contract_path, contract)
                errors = validate_progressive_disclosure(root, contract)
                self.assertTrue(any(expected in item for item in errors), errors)

    def test_python_surface_locators_and_closed_schema_fail_on_review_attacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._copy_repo(temporary)
            contract = load(root / "brandkit-skill-contract.v1.json")
            brand = root / "scripts/brand_package.py"
            brand.write_text(brand.read_text().replace("def git_root(", "def git_root_changed("), encoding="utf-8")
            self.assertTrue(any("public Python surface drifted" in item for item in validate_public_contracts(root, contract)))
        with tempfile.TemporaryDirectory() as temporary:
            root = self._copy_repo(temporary); contract = load(root / "brandkit-skill-contract.v1.json")
            contract["public_contracts"]["brand_package"]["validator"] = "scripts/brand_package.py:not_real"
            self.assertTrue(any("unresolved Python locator" in item for item in validate_public_contracts(root, contract)))
        schema = load(ROOT / "schemas/brandkit-skill-contract.v1.schema.json")
        from jsonschema import Draft202012Validator
        for mutate in (
            lambda value: value["skills"].update({"invented-skill": value["skills"]["brandkit-setup"]}),
            lambda value: value["todo_policy"].update({"unreviewed_extension": True}),
            lambda value: value["routing"].update({"invented-route": "anything"}),
        ):
            value = load(ROOT / "brandkit-skill-contract.v1.json"); mutate(value)
            self.assertTrue(list(Draft202012Validator(schema).iter_errors(value)))

    def test_design_generator_execution_rejects_broken_return_and_pdf_escalation(self) -> None:
        for mutation in (
            lambda text: text.replace('return "\\n".join(front).encode()', 'return b"# Broken DESIGN\\n"'),
            lambda text: text.replace('tokens=shared_tokens(manifest)', 'tokens={**shared_tokens(manifest), "pdf": manifest.get("pdf", {})}'),
        ):
            with tempfile.TemporaryDirectory() as temporary:
                root = self._copy_repo(temporary); path = root / "scripts/impeccable_adapter.py"
                path.write_text(mutation(path.read_text(encoding="utf-8")), encoding="utf-8")
                errors = validate_progressive_disclosure(root, load(root / "brandkit-skill-contract.v1.json"))
                self.assertTrue(any("DESIGN.md" in item or "frontmatter" in item for item in errors), errors)

    def test_real_impeccable_loader_and_source_derived_route_matrix(self) -> None:
        routes = impeccable_source_routes()
        self.assertEqual("new-work.md", routes["craft"]["primary"])
        self.assertEqual(["new-work.md"], routes["shape"]["transitive"])
        self.assertEqual("init", routes["teach"]["alias"])
        self.assertEqual([], validate_real_impeccable_context_matrix())
        craft = transitive_playbook_trace("craft", Path("/Volumes/Daten/Projekte/BrandKit/external-sources/impeccable/.agents/skills/impeccable"))
        shape = transitive_playbook_trace("shape", Path("/Volumes/Daten/Projekte/BrandKit/external-sources/impeccable/.agents/skills/impeccable"))
        self.assertEqual(1, sum(item["role"] == "primary" for item in craft))
        self.assertEqual(1, sum(item["role"] == "primary" for item in shape))
        self.assertIn({"path":"new-work.md", "role":"primary"}, craft)
        self.assertIn({"path":"new-work.md", "role":"delegated"}, shape)
        self.assertRaisesRegex(ValueError, "web-only", impeccable_load_trace, "live", "ios")
        for route in ("pin", "unpin"):
            validate_impeccable_load_trace(impeccable_load_trace(route), route)

    def test_generated_validator_mutations_and_exact_design_roles_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._copy_repo(temporary)
            path = root / "scripts/brand_package.py"
            path.write_text(path.read_text().replace(
                "def validate_package(package: Path) -> tuple[dict[str, Any], Path, str]:\n",
                "def validate_package(package: Path) -> tuple[dict[str, Any], Path, str]:\n    if 'neutral-fixture' not in str(package):\n        probe = package if package.is_file() else package / 'brand-package.json'\n        doc = load_json(probe); validate_schema(doc, 'brand-package.v1.schema.json', 'package schema')\n        return (doc, probe.parent, '0' * 64)\n",
            ), encoding="utf-8")
            errors = validate_public_contracts(root, load(root / "brandkit-skill-contract.v1.json"))
            self.assertTrue(any("generated mutation" in item or "binding" in item for item in errors), errors)
        with tempfile.TemporaryDirectory() as temporary:
            root = self._copy_repo(temporary); path = root / "scripts/impeccable_adapter.py"
            path.write_text(path.read_text().replace(
                'front += ["---","",f"# Design System:',
                'front += ["invented_visual_role: unsafe","---","",f"# Design System:',
            ), encoding="utf-8")
            errors = validate_progressive_disclosure(root, load(root / "brandkit-skill-contract.v1.json"))
            self.assertTrue(any("invented normative roles" in item for item in errors), errors)

    def test_impeccable_source_blob_pin_and_productive_init_wrapper(self) -> None:
        source = Path("/Volumes/Daten/Projekte/BrandKit/external-sources/impeccable/.agents/skills/impeccable")
        for relative in ("reference/polish.md", "scripts/context.mjs"):
            with tempfile.TemporaryDirectory() as temporary:
                copy = Path(temporary) / "impeccable"; shutil.copytree(source, copy)
                target = copy / relative; target.write_bytes(target.read_bytes() + b"\nR4 mutation\n")
                with patch.dict(os.environ, {"BRANDKIT_IMPECCABLE_SOURCE":str(copy)}):
                    errors = validate_progressive_disclosure(ROOT, load(ROOT / "brandkit-skill-contract.v1.json"))
                self.assertTrue(any("blob differs from pin" in item for item in errors), errors)
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"; project.mkdir(); old = b"# Product\n\n## Platform\n\nios\n\nOLD-AUTHORITY\n"; (project / "PRODUCT.md").write_bytes(old)
            product = Path(temporary) / "new-product.md"; new = b"# Product\n\n<!-- impeccable:product-schema 1 -->\n\n## Platform\n\nadaptive\n"; product.write_bytes(new)
            command = [sys.executable, str(ROOT / "scripts/impeccable_adapter.py"), "init-defer", "--project", str(project), "--product", str(product), "--source-root", str(source)]
            env = {**os.environ, "PYTHONPATH":f"{ROOT / 'scripts'}:/Users/joh/.local/lib/python3.13/site-packages"}
            result = subprocess.run(command, text=True, capture_output=True, env=env)
            self.assertEqual(0, result.returncode, result.stderr); output = json.loads(result.stdout)
            self.assertEqual(new, (project / "PRODUCT.md").read_bytes())
            self.assertNotIn("OLD-AUTHORITY", output["context"])
            self.assertEqual(["reference/ios.md", "reference/android.md"], [item["path"] for item in output["native"]])
            self.assertEqual(1, sum(item["event"] == "context" for item in output["events"]))
            (project / "PRODUCT.md").write_bytes(old); env["BRANDKIT_IMPECCABLE_INIT_FAIL_AFTER_WRITE"] = "1"
            failed = subprocess.run(command, text=True, capture_output=True, env=env)
            self.assertNotEqual(0, failed.returncode); self.assertEqual(old, (project / "PRODUCT.md").read_bytes())

    def test_release_manifest_rejects_coordinated_contract_and_fixture_hash_tamper(self) -> None:
        self.assertEqual(self.RELEASE_MANIFEST_SHA256, hashlib.sha256((ROOT / "skill-contract-release.v1.json").read_bytes()).hexdigest())
        self.assertEqual([], validate_release_manifest(ROOT))
        with tempfile.TemporaryDirectory() as temporary:
            root = self._copy_repo(temporary)
            contract_path = root / "brandkit-skill-contract.v1.json"; contract = load(contract_path)
            contract["todo_policy"]["allowed_locations"].append("invented")
            self._write_json(contract_path, contract)
            fixture = root / "fixtures/brandkit-skills/dry-runs.v1.json"; suite = load(fixture)
            suite["input_artifacts"][0]["sha256"] = hashlib.sha256(contract_path.read_bytes()).hexdigest(); self._write_json(fixture, suite)
            release_path = root / "skill-contract-release.v1.json"; release = load(release_path)
            for member in release["members"]:
                if member["path"] in {"brandkit-skill-contract.v1.json", "fixtures/brandkit-skills/dry-runs.v1.json"}:
                    member["sha256"] = hashlib.sha256((root / member["path"]).read_bytes()).hexdigest()
            self._write_json(release_path, release)
            errors = validate_release_manifest(root)
            self.assertTrue(any("external attestation" in item for item in errors), errors)

    def _resigned(self, payload: dict, directory: str) -> tuple[Path, str]:
        """Eine Attestation mit genau diesem Rumpf, gueltig signiert, auf Platte.

        Gueltig signiert, damit der Befund NICHT an der Signatur haengt: geprueft
        wird die Bindung an die Evidence, nicht die HMAC.
        """
        import hmac
        envelope = load(Path(os.environ["BRANDKIT_SKILL_RELEASE_ATTESTATION"]))
        envelope["payload"] = payload
        key = Path(os.environ["BRANDKIT_OWNER_KEY"]).read_bytes()
        signed = {name: envelope[name] for name in ("schema_version", "issuer", "payload")}
        canonical = (json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        envelope["hmac_sha256"] = hmac.new(key, canonical, hashlib.sha256).hexdigest()
        path = Path(directory) / "attestation.json"
        self._write_json(path, envelope)
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_release_attestation_that_binds_no_evidence_is_refused(self) -> None:
        """Punkt 179: eine Evidence, die an nichts haengt, kann unbemerkt getauscht werden.

        Genau das ist passiert: die beurteilte Fassung wurde nach dem Urteil
        ersetzt, und weil die Review keinen Digest nannte, fiel es nicht auf.
        Die Attestation bindet den Evidence-Digest deshalb im SIGNIERTEN Rumpf -
        dann bricht ein spaeterer Austausch die Signatur, statt unsichtbar zu
        bleiben.  Eine Attestation ohne diese Bindung wird benannt abgewiesen.
        """
        envelope = load(Path(os.environ["BRANDKIT_SKILL_RELEASE_ATTESTATION"]))
        payload = {name: value for name, value in envelope["payload"].items() if name != "evidence_sha256"}
        with tempfile.TemporaryDirectory() as temporary:
            path, digest = self._resigned(payload, temporary)
            errors = validate_release_manifest(
                ROOT, expected_attestation_sha256=digest, attestation_path=path,
                owner_key=Path(os.environ["BRANDKIT_OWNER_KEY"]),
                part1_snapshot=Path(os.environ["BRANDKIT_PART1_SNAPSHOT"]),
                evidence_path=Path(os.environ["BRANDKIT_RELEASE_EVIDENCE"]))
        self.assertTrue(any("evidence_sha256" in item for item in errors), errors)

    def test_release_attestation_bound_to_other_evidence_is_refused(self) -> None:
        """Punkt 179, die andere Haelfte: der gebundene Digest muss STIMMEN.

        Sonst genuegte ein beliebiger Digest im Rumpf, und die Bindung waere
        Zierde.  Hier traegt die Attestation einen gueltig signierten, aber
        FALSCHEN Evidence-Digest; die Pruefung muss ihn gegen die Datei halten.
        """
        envelope = load(Path(os.environ["BRANDKIT_SKILL_RELEASE_ATTESTATION"]))
        payload = dict(envelope["payload"])
        payload["evidence_sha256"] = hashlib.sha256(b"eine andere Evidence").hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            path, digest = self._resigned(payload, temporary)
            errors = validate_release_manifest(
                ROOT, expected_attestation_sha256=digest, attestation_path=path,
                owner_key=Path(os.environ["BRANDKIT_OWNER_KEY"]),
                part1_snapshot=Path(os.environ["BRANDKIT_PART1_SNAPSHOT"]),
                evidence_path=Path(os.environ["BRANDKIT_RELEASE_EVIDENCE"]))
        self.assertTrue(any("evidence" in item and "differs" in item for item in errors), errors)

    def test_owner_signed_but_false_part1_pass_is_reexecuted_and_rejected(self) -> None:
        attestation = Path(os.environ["BRANDKIT_SKILL_RELEASE_ATTESTATION"]); envelope = load(attestation)
        envelope["payload"]["part1_pass"]["entries"] += 1
        key = Path(os.environ["BRANDKIT_OWNER_KEY"]).read_bytes()
        signed = {name:envelope[name] for name in ("schema_version", "issuer", "payload")}
        canonical = (json.dumps(signed, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        import hmac
        envelope["hmac_sha256"] = hmac.new(key, canonical, hashlib.sha256).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "signed-false-pass.json"; self._write_json(path, envelope)
            expected = hashlib.sha256(path.read_bytes()).hexdigest()
            errors = validate_release_manifest(
                ROOT, expected_attestation_sha256=expected, attestation_path=path,
                owner_key=Path(os.environ["BRANDKIT_OWNER_KEY"]), part1_snapshot=Path(os.environ["BRANDKIT_PART1_SNAPSHOT"]),
            )
        self.assertTrue(any("fresh semantic PASS differs" in item for item in errors), errors)

    def test_every_fails_on_and_all_eight_r1_counterexamples_are_blocked(self) -> None:
        cases = load(FIXTURES)["cases"]
        decisions = {case["request_id"]: case["expected"] for case in cases}
        required_r1 = {
            "r1-interview-unsupported-legal-claim", "r1-build-provider-duplicates",
            "r1-publish-rights-false", "r1-publish-humanizer-evidence-false",
            "r1-publish-generic-smoothing", "r1-publish-parallel-ui-instructions",
            "r1-publish-legal-without-official-source", "r1-publish-brand-conflict",
        }
        self.assertTrue(required_r1.issubset(decisions))
        self.assertTrue(all(decisions[name] == "blocked" for name in required_r1))
        self.assertTrue(any(case["expected"] != "blocked" for case in cases if case["skill"] == "brandkit-setup"))
        self.assertTrue(any(case["expected"] != "blocked" for case in cases if case["skill"] == "brandkit-interview"))
        self.assertTrue(any(case["expected"] != "blocked" for case in cases if case["skill"] == "brandkit-build"))
        self.assertTrue(any(case["expected"] != "blocked" for case in cases if case["skill"] == "brandkit-spacing"))
        self.assertTrue(any(case["expected"] != "blocked" for case in cases if case["skill"] == "brandkit-publish"))

    def test_design_collision_routes_have_one_owner(self) -> None:
        cases = load(COLLISION_FIXTURES)["cases"]
        self.assertEqual(11, len(cases))
        self.assertEqual(
            {
                "route-spacing-audit": "brandkit-spacing",
                "route-ui-redesign": "brandkit-impeccable -> impeccable",
                "route-brand-package": "brandkit-build",
                "route-pdf-publication": "brand-voice-pdf -> brandkit-publish",
                "route-design-md": "brandkit-impeccable",
                "route-visual-candidates": "brandkit-visual-decisions",
                "route-affinity-vector": "brandkit-affinity",
                "route-mixed-audit-ui-fix": "brandkit-impeccable -> impeccable",
                "route-mixed-design-spacing": "brandkit-impeccable",
                "route-mixed-pdf-shared": "brand-voice-pdf -> brandkit-publish",
                "route-mixed-brand-new-token": "brandkit-build",
            },
            {case["request_id"]: case["expected_route"] for case in cases},
        )
        self.assertTrue(all(case["expected_route"] not in case["forbidden_routes"] for case in cases))
        manipulated = json.loads(json.dumps(cases))
        manipulated[0]["request"] = "Gestalte das gesamte Dashboard neu und korrigiere UI-Layout und Interaktion."
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "collision.json"
            self._write_json(path, {"schema_version":"brandkit-design-routing-dry-runs.v1","cases":manipulated})
            self.assertTrue(collision_dry_run(path))
        self.assertEqual("brandkit-impeccable -> impeccable", route_design_request(manipulated[0]["request"]))

    def test_spacing_adapter_is_not_inserted_into_core_sequence(self) -> None:
        contract = load(ROOT / "brandkit-skill-contract.v1.json")
        self.assertEqual(["brandkit-setup", "brandkit-interview", "brandkit-visual-decisions", "brandkit-build", "brandkit-publish"], contract["sequence"])
        self.assertNotIn("brandkit-spacing", contract["sequence"])
        self.assertEqual("spacing-tokens-roles-and-audit", contract["optional_adapters"]["brandkit-spacing"]["owner"])
        self.assertIn("DESIGN.md", contract["optional_adapters"]["brandkit-spacing"]["read_only"])
        self.assertEqual([], contract["optional_adapters"]["brandkit-spacing"]["mutates"])

    def test_spacing_inventory_is_closed_and_every_entry_has_one_assignment(self) -> None:
        schema = load(ROOT / "schemas/spacing-inventory.v1.schema.json")
        inventory = load(ROOT / "fixtures/brandkit-skills/spacing-inventory-ui.valid.json")
        from jsonschema import Draft202012Validator
        self.assertEqual([], list(Draft202012Validator(schema).iter_errors(inventory)))
        missing = json.loads(json.dumps(inventory)); del missing["entries"][0]["assignment"]
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(missing)))
        mixed = json.loads(json.dumps(inventory)); mixed["entries"][0]["assignment"]["reason"] = "second outcome"
        self.assertTrue(list(Draft202012Validator(schema).iter_errors(mixed)))

    def test_spacing_result_binds_inventory_and_routes_ui_patch_to_impeccable(self) -> None:
        request = self._positive_request("brandkit-spacing")
        request["state"]["requested_output"] = "ui-patch-handoff"
        result = execute_request(request)
        self.assertEqual("pass", result["decision"])
        self.assertEqual(request["state"]["inventory_artifact"]["sha256"], result["spacing_result"]["inventory_sha256"])
        self.assertEqual("brandkit-impeccable -> impeccable layout", result["spacing_result"]["handoff"])
        self.assertEqual(result["spacing_result"]["assignment_count"], result["spacing_result"]["token_count"] + result["spacing_result"]["exception_count"])

    def _positive_request(self, skill: str) -> dict:
        suite = load(FIXTURES)
        case = next(item for item in suite["cases"] if item["skill"] == skill and item["expected"] != "blocked")
        return {"schema_version":"brandkit-skill-request.v1", "request_id":case["request_id"], "skill":skill, "input_artifacts":suite["input_artifacts"], "state":case["state"]}

    def _init_git(self, path: Path) -> None:
        path.mkdir(parents=True)
        environment = os.environ.copy()
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
            environment.pop(name, None)
        subprocess.run(["git", "init", "-q", str(path)], check=True, env=environment)

    def _discovery_project(self, project: Path) -> tuple[Path, str, Path]:
        project.mkdir(parents=True)
        workspace = project / ".brandkit/work/ci-pilot/discovery"
        (workspace / "docs/adr").mkdir(parents=True)
        (workspace / "sources").mkdir()
        (workspace / "CONTEXT.md").write_bytes(b"# Context\n\nBrand character: editorial and precise.\n")
        (workspace / "docs/adr/0001-brand-structure.md").write_bytes(b"# ADR 0001\n\nUse one master brand.\n")
        (workspace / "sources/owner-answer-2.txt").write_bytes(b"Owner says editorial and precise.\n")
        handoff = ".brandkit/work/ci-pilot/discovery/handoff.json"
        (project / handoff).write_bytes((ROOT / "fixtures/brandkit-skills/discovery-handoff.valid.json").read_bytes())
        key = project.parent / f"{project.name}-owner.key"; key.write_bytes(b"owner-test-key-material-32-bytes!!"); key.chmod(0o600)
        discovery.write_progress(project, "ci-pilot", list(discovery.BRAND_AXES), [], [], "1" * 64, key)
        discovery.write_progress(project, "ci-pilot", [], [], list(discovery.BRAND_AXES), "2" * 64, key)
        return project.resolve(), handoff, key

    def _discovery_activation(self, project: Path, bundle: Path) -> None:
        skill_root = project / ".agents/skills"; skill_root.mkdir(parents=True)
        contract = load(ROOT / "brandkit-skill-contract.v1.json")
        for name in {route["skill"] for route in contract["progressive_disclosure"]["routes"].values()}:
            (skill_root / name).symlink_to(ROOT / "skills" / name)
        for name, relative in {
            "grill-with-docs":"skills/engineering/grill-with-docs",
            "grilling":"skills/productivity/grilling",
            "domain-modeling":"skills/engineering/domain-modeling",
        }.items():
            (skill_root / name).symlink_to(bundle / relative)
        (project / ".claude").mkdir()
        (project / ".claude/skills").symlink_to("../.agents/skills")

    def _copy_repo(self, temporary: str) -> Path:
        root = Path(temporary).resolve() / "repo"
        root.mkdir()
        # This suite mutates only contract sources. Copying unrelated PDF goldens
        # made every negative case depend on tens of thousands of I/O operations.
        # Keep the cleanroom source set explicit and fail if a validator dependency
        # escapes it; this is also the frozen inventory used by every mutation case.
        source_directories = (
            # fixtures/brand-registry is small and load-bearing: validate_public_contracts
            # runs the real brand-package validator over it, so a cleanroom without it
            # turns every contract case below into an unrelated FileNotFoundError.
            "docs", "document-types", "fixtures/brand-registry", "fixtures/brandkit-skills",
            "schemas", "scripts", "skills", "toolchain",
        )
        for relative in source_directories:
            source = ROOT / relative
            shutil.copytree(source, root / relative, symlinks=True, ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
        for source in ROOT.iterdir():
            if source.is_file() and source.name != ".git":
                shutil.copy2(source, root / source.name)
        return root

    def _refresh_humanizer_artifact(self, root: Path, name: str) -> None:
        manifest = root / "fixtures/brandkit-skills/humanizer-evidence.valid.json"
        document = load(manifest)
        artifact = next(item for item in document["artifacts"] if item["name"] == name)
        artifact["sha256"] = hashlib.sha256((root / artifact["path"]).read_bytes()).hexdigest()
        manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _write_json(self, path: Path, value: dict) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _coordinate_model_call_hash(self, root: Path, phase: str) -> None:
        name = f"{phase}-call.json"
        call = root / "fixtures/brandkit-skills/humanizer" / name
        call_hash = hashlib.sha256(call.read_bytes()).hexdigest()
        self._refresh_humanizer_artifact(root, name)
        provenance_path = root / "fixtures/brandkit-skills/humanizer/brandkit-provenance.json"
        provenance = load(provenance_path)
        provenance["raw_model_call_sha256"][phase] = call_hash
        self._write_json(provenance_path, provenance)
        self._refresh_humanizer_artifact(root, "brandkit-provenance.json")
        report_path = root / "fixtures/brandkit-skills/humanizer/report.json"
        report = load(report_path)
        report["raw_model_call_sha256"][phase] = call_hash
        report["brandkit_provenance_sha256"] = hashlib.sha256(provenance_path.read_bytes()).hexdigest()
        self._write_json(report_path, report)
        self._refresh_humanizer_artifact(root, "report.json")

    def _coordinate_text_hash(self, root: Path, name: str, raw: bytes) -> None:
        path = root / "fixtures/brandkit-skills/humanizer" / name
        path.write_bytes(raw)
        self._refresh_humanizer_artifact(root, name)
        digest = hashlib.sha256(raw).hexdigest()
        manifest_path = root / "fixtures/brandkit-skills/humanizer-evidence.valid.json"
        manifest = load(manifest_path)
        side = "before_sha256" if name == "original.md" else "after_sha256"
        manifest["evidence_lint"][side] = digest
        self._write_json(manifest_path, manifest)
        provenance_path = root / "fixtures/brandkit-skills/humanizer/brandkit-provenance.json"
        provenance = load(provenance_path)
        provenance["before_sha256" if name == "original.md" else "after_sha256"] = digest
        self._write_json(provenance_path, provenance)
        self._refresh_humanizer_artifact(root, "brandkit-provenance.json")
        report_path = root / "fixtures/brandkit-skills/humanizer/report.json"
        report = load(report_path)
        if name == "original.md":
            report["source_sha256"] = digest
        report["brandkit_provenance_sha256"] = hashlib.sha256(provenance_path.read_bytes()).hexdigest()
        self._write_json(report_path, report)
        self._refresh_humanizer_artifact(root, "report.json")

    def test_omission_unknown_extra_and_contradiction_fail_closed(self) -> None:
        omitted = self._positive_request("brandkit-publish")
        del omitted["state"]["rights"]
        with self.assertRaises(ValidationError): execute_request(omitted)
        unknown = self._positive_request("brandkit-setup")
        unknown["state"]["write_target"] = "unknown"
        self.assertEqual("blocked", execute_request(unknown)["decision"])
        extra = self._positive_request("brandkit-interview")
        extra["state"]["unsupported_legal_claim"] = "present"
        with self.assertRaises(ValidationError): execute_request(extra)
        contradiction = self._positive_request("brandkit-build")
        contradiction["state"]["instruction_sources"] = ["AGENTS.md", "HERMES.md"]
        self.assertEqual("blocked", execute_request(contradiction)["decision"])

    def test_artifact_hash_and_humanizer_evidence_are_enforced(self) -> None:
        request = self._positive_request("brandkit-publish")
        self.assertEqual("publish", execute_request(request)["decision"])
        request["input_artifacts"][0]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): execute_request(request)

    def test_humanizer_evidence_rejects_bogus_report_and_requires_applicability(self) -> None:
        request = self._positive_request("brandkit-publish")
        del request["state"]["humanizer_applicability"]
        with self.assertRaises(ValidationError):
            execute_request(request)
        request = self._positive_request("brandkit-publish")
        request["state"]["humanizer_evidence"] = None
        with self.assertRaises(ValidationError):
            execute_request(request)
        request = self._positive_request("brandkit-publish")
        with tempfile.TemporaryDirectory() as temporary:
            root = self._copy_repo(temporary)
            report = root / "fixtures/brandkit-skills/humanizer/evidence-report.json"
            report.write_text('{"totally":"bogus"}\n', encoding="utf-8")
            manifest = root / "fixtures/brandkit-skills/humanizer-evidence.valid.json"
            document = load(manifest)
            next(item for item in document["artifacts"] if item["name"] == "evidence-report.json")["sha256"] = __import__("hashlib").sha256(report.read_bytes()).hexdigest()
            manifest.write_text(json.dumps(document), encoding="utf-8")
            result = execute_request(request, root)
            self.assertEqual("blocked", result["decision"])
            self.assertIn("missing_or_rejected_humanizer_evidence", result["blockers"])

    def test_humanizer_result_is_exactly_reconstructed_from_confirmed_edits(self) -> None:
        request = self._positive_request("brandkit-publish")
        with tempfile.TemporaryDirectory() as temporary:
            root = self._copy_repo(temporary)
            result_path = root / "fixtures/brandkit-skills/humanizer/result.md"
            result_path.write_text(result_path.read_text(encoding="utf-8") + "Insgesamt ist das Ergebnis wirklich sehr gut.\n", encoding="utf-8")
            self._refresh_humanizer_artifact(root, "result.md")
            manifest = root / "fixtures/brandkit-skills/humanizer-evidence.valid.json"
            document = load(manifest)
            document["evidence_lint"]["after_sha256"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
            manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            result = execute_request(request, root)
            self.assertEqual("blocked", result["decision"])
            self.assertIn("generic_smoothing", result["blockers"])

    def test_humanizer_rejects_unbound_edits_diff_and_incomplete_anchor_ledger(self) -> None:
        request = self._positive_request("brandkit-publish")
        mutations = (
            ("edits.json", lambda value: value["edits"][0].update(candidate_id="not-confirmed")),
            ("changes.diff", None),
            ("evidence-ledger.json", lambda value: [value["anchors"][key].clear() for key in value["anchors"]]),
        )
        for name, mutate in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = self._copy_repo(temporary)
                path = root / "fixtures/brandkit-skills/humanizer" / name
                if mutate is None:
                    path.write_text(path.read_text(encoding="utf-8") + "# unbound\n", encoding="utf-8")
                else:
                    value = load(path)
                    mutate(value)
                    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    if name == "edits.json":
                        call = root / "fixtures/brandkit-skills/humanizer/rewrite-call.json"
                        envelope = load(call)
                        envelope["structured_output"] = value
                        call.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                        self._refresh_humanizer_artifact(root, "rewrite-call.json")
                self._refresh_humanizer_artifact(root, name)
                result = execute_request(request, root)
                self.assertEqual("blocked", result["decision"])
                self.assertIn("missing_or_rejected_humanizer_evidence", result["blockers"])

    def test_humanizer_rejects_unknown_aliased_duplicate_and_malformed_pattern_ids(self) -> None:
        request = self._positive_request("brandkit-publish")
        for patterns in (["9999"], ["Muster 5"], ["05"], ["5", "5"], [5]):
            with self.subTest(patterns=patterns), tempfile.TemporaryDirectory() as temporary:
                root = self._copy_repo(temporary)
                for name in ("audit-ledger.json", "confirmed-ledger.json"):
                    path = root / "fixtures/brandkit-skills/humanizer" / name
                    value = load(path)
                    value["candidates"][0]["patterns"] = patterns
                    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    self._refresh_humanizer_artifact(root, name)
                call = root / "fixtures/brandkit-skills/humanizer/audit-call.json"
                envelope = load(call)
                envelope["structured_output"]["candidates"][0]["patterns"] = patterns
                call.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                self._refresh_humanizer_artifact(root, "audit-call.json")
                result = execute_request(request, root)
                self.assertEqual("blocked", result["decision"])
                self.assertIn("missing_or_rejected_humanizer_evidence", result["blockers"])

    def test_humanizer_rejects_unknown_raw_envelope_fields_and_unbound_raw_bytes(self) -> None:
        request = self._positive_request("brandkit-publish")
        for mutation in ("unknown-field", "whitespace-only"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = self._copy_repo(temporary)
                call = root / "fixtures/brandkit-skills/humanizer/audit-call.json"
                if mutation == "unknown-field":
                    envelope = load(call)
                    envelope["tampered_output"] = "not emitted by the recorded runner call"
                    call.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                else:
                    call.write_text(call.read_text(encoding="utf-8") + "\n", encoding="utf-8")
                self._refresh_humanizer_artifact(root, "audit-call.json")
                result = execute_request(request, root)
                self.assertEqual("blocked", result["decision"])
                self.assertIn("missing_or_rejected_humanizer_evidence", result["blockers"])

    def test_claude_2_1_223_envelope_is_closed_at_every_nested_level(self) -> None:
        request = self._positive_request("brandkit-publish")
        mutations = (
            lambda value: value["usage"].update(extra_usage=0),
            lambda value: next(iter(value["modelUsage"].values())).update(extra_model_usage=0),
            lambda value: value.update(permission_denials=[{"tool_name": "neutral"}]),
            lambda value: value.update(result="{}"),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate), tempfile.TemporaryDirectory() as temporary:
                root = self._copy_repo(temporary)
                call = root / "fixtures/brandkit-skills/humanizer/audit-call.json"
                envelope = load(call)
                mutate(envelope)
                self._write_json(call, envelope)
                self._coordinate_model_call_hash(root, "audit")
                result = execute_request(request, root)
                self.assertEqual("blocked", result["decision"])
                self.assertIn("missing_or_rejected_humanizer_evidence", result["blockers"])

    def test_codex_0_144_3_is_fail_closed_as_humanizer_llm_provider(self) -> None:
        request = self._positive_request("brandkit-publish")
        with tempfile.TemporaryDirectory() as temporary:
            root = self._copy_repo(temporary)
            report_path = root / "fixtures/brandkit-skills/humanizer/report.json"
            report = load(report_path)
            report["provider"] = "codex"
            self._write_json(report_path, report)
            self._refresh_humanizer_artifact(root, "report.json")
            result = execute_request(request, root)
            self.assertEqual("blocked", result["decision"])
            self.assertIn("missing_or_rejected_humanizer_evidence", result["blockers"])

    def test_coordinated_crlf_bom_and_non_utf8_text_tamper_remain_rejected(self) -> None:
        request = self._positive_request("brandkit-publish")
        cases = (
            ("original.md", lambda raw: raw.replace(b"\n", b"\r\n")),
            ("result.md", lambda raw: raw.replace(b"\n", b"\r\n")),
            ("original.md", lambda raw: b"\xef\xbb\xbf" + raw),
            ("result.md", lambda raw: raw + b"\xff"),
        )
        for name, mutate in cases:
            with self.subTest(name=name, mutation=mutate), tempfile.TemporaryDirectory() as temporary:
                root = self._copy_repo(temporary)
                path = root / "fixtures/brandkit-skills/humanizer" / name
                self._coordinate_text_hash(root, name, mutate(path.read_bytes()))
                result = execute_request(request, root)
                self.assertEqual("blocked", result["decision"])
                self.assertIn("missing_or_rejected_humanizer_evidence", result["blockers"])

    def test_humanizer_rejects_coordinated_brandkit_provenance_tamper(self) -> None:
        request = self._positive_request("brandkit-publish")
        with tempfile.TemporaryDirectory() as temporary:
            root = self._copy_repo(temporary)
            provenance_path = root / "fixtures/brandkit-skills/humanizer/brandkit-provenance.json"
            provenance = load(provenance_path)
            provenance["candidate_edits"][0]["start_codepoint"] += 1
            provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self._refresh_humanizer_artifact(root, "brandkit-provenance.json")
            report_path = root / "fixtures/brandkit-skills/humanizer/report.json"
            report = load(report_path)
            report["brandkit_provenance_sha256"] = hashlib.sha256(provenance_path.read_bytes()).hexdigest()
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self._refresh_humanizer_artifact(root, "report.json")
            result = execute_request(request, root)
            self.assertEqual("blocked", result["decision"])
            self.assertIn("missing_or_rejected_humanizer_evidence", result["blockers"])

    def test_safe_yaml_rejects_duplicate_key_alias_custom_tag_and_empty_body(self) -> None:
        bad = [
            "---\nname: wrong\nname: brandkit-setup\ndescription: x\n---\n# body\n",
            "---\nname: &n brandkit-setup\ndescription: *n\n---\n# body\n",
            "---\nname: !unsafe brandkit-setup\ndescription: x\n---\n# body\n",
            "---\nname: brandkit-setup\ndescription: x\n---\n",
        ]
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises(ValueError): frontmatter(text)

    def test_instruction_materializer_resolves_bytes_and_refuses_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve() / "project"
            self._init_git(project)
            with patch.object(instruction_materializer, "_verify_hermes_installation"):
                materialize(project)
            self.assertTrue((project / "CLAUDE.md").is_symlink())
            self.assertEqual("AGENTS.md", os.readlink(project / "CLAUDE.md"))
            self.assertEqual((project / "AGENTS.md").resolve(), (project / "CLAUDE.md").resolve())
            self.assertEqual((project / "AGENTS.md").read_bytes(), (project / "CLAUDE.md").read_bytes())
            (project / ".hermes.md").write_text("duplicate", encoding="utf-8")
            with patch.object(instruction_materializer, "_verify_hermes_installation"):
                with self.assertRaises(ValueError): materialize(project)

    def test_instruction_materializer_rejects_subdir_external_symlink_and_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary).resolve()
            root = temporary_root / "repo"
            self._init_git(root)
            with self.assertRaisesRegex(ValueError, "actual Git root"):
                subdir = root / "subdir"
                subdir.mkdir()
                materialize(subdir)
            outside = temporary_root / "outside.md"
            outside.write_text("do not overwrite", encoding="utf-8")
            (root / "AGENTS.md").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "symlink"):
                materialize(root)
            self.assertEqual("do not overwrite", outside.read_text(encoding="utf-8"))
            (root / "AGENTS.md").unlink()
            (root / "AGENTS.override.md").write_text("shadow", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "shadowing"):
                materialize(root)

    def test_instruction_materializer_is_idempotent_and_updates_canonical_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            project = base / "repo"
            self._init_git(project)
            first = base / "first.md"
            second = base / "second.md"
            first.write_bytes(b"# first\n")
            second.write_bytes(b"# second\n")
            with patch.object(instruction_materializer, "_verify_hermes_installation"):
                materialize(project, first)
                materialize(project, first)
            self.assertEqual(b"# first\n", (project / "AGENTS.md").read_bytes())
            self.assertTrue((project / "agents.md").samefile(project / "AGENTS.md") if (project / "agents.md").exists() else True)
            with patch.object(instruction_materializer, "_verify_hermes_installation"):
                materialize(project, second)
            self.assertEqual(b"# second\n", (project / "AGENTS.md").read_bytes())
            self.assertEqual("AGENTS.md", os.readlink(project / "CLAUDE.md"))

    def test_instruction_materializer_rolls_back_both_targets_on_late_hermes_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            project = base / "repo"
            self._init_git(project)
            agents = project / "AGENTS.md"
            claude = project / "CLAUDE.md"
            agents.write_bytes(b"preimage\x00bytes\n")
            claude.symlink_to("AGENTS.md")
            source = base / "new.md"
            source.write_bytes(b"# proposed\n")
            with patch.object(instruction_materializer, "_verify_hermes_installation", side_effect=[None, ValueError("late Hermes failure")]), patch.object(instruction_materializer, "_shadow_prompt_preflight"):
                with self.assertRaisesRegex(ValueError, "late Hermes failure"):
                    materialize(project, source)
            self.assertEqual(b"preimage\x00bytes\n", agents.read_bytes())
            self.assertTrue(claude.is_symlink())
            self.assertEqual("AGENTS.md", os.readlink(claude))

    def test_hermes_gate_accepts_newer_within_major_and_refuses_older_or_foreign(self) -> None:
        """Die Hermes-Schranke ordnet Versionen, statt Zeichen zu vergleichen.

        Der frühere Vergleich verlangte den exakten Versionstext UND den exakten
        Upstream-Commit als Teilstring. Damit sperrte das Werkzeug jede neuere
        Fassung aus - auch eine, die ``build_context_files_prompt`` unverändert
        bereitstellt. Gemessen an v0.20.6: Die Funktion existiert dort mit
        gleicher Signatur und wählt die vorgeschlagene ``AGENTS.md`` korrekt aus.

        Dieses Bein fährt die ECHTE Prüffunktion gegen erfundene Versionstexte;
        gepatcht sind nur ihre beiden Außenkanten (``which`` und ``run``) sowie
        die Existenzprüfung der Installation.
        """
        cases = [
            ("Hermes Agent v0.20.6 (2026.8.27) · upstream c30ac90a", True, "neuere Patchversion"),
            ("Hermes Agent v0.20.5 (2026.8.01) · upstream 1bbb6e5b", True, "die Referenzfassung"),
            ("Hermes Agent v0.21.0 (2026.9.01) · upstream deadbeef", True, "neuere Minorversion"),
            ("Hermes Agent v0.20.4 (2026.7.01) · upstream 0000aaaa", False, "ältere Patchversion"),
            ("Hermes Agent v1.0.0 (2027.1.01) · upstream 11112222", False, "fremde Hauptversion"),
            ("Hermes Agent (Entwicklerbau, ohne Versionsangabe)", False, "keine lesbare Version"),
        ]
        for text, expected_ok, label in cases:
            with self.subTest(version=label):
                with patch.object(instruction_materializer.shutil, "which", return_value="/usr/bin/hermes"), \
                     patch.object(instruction_materializer.subprocess, "run",
                                  return_value=subprocess.CompletedProcess([], 0, text, "")), \
                     patch.object(Path, "is_file", lambda self: True):
                    if expected_ok:
                        instruction_materializer._verify_hermes_installation()
                    else:
                        with self.assertRaises(ValueError):
                            instruction_materializer._verify_hermes_installation()

        # Ein Binary, das gar nicht antwortet, bleibt ein eigener Fall: Der
        # Fehlertext "executable" wird von einem zweiten Bein erwartet.
        with patch.object(instruction_materializer.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "executable"):
                instruction_materializer._verify_hermes_installation()

    def test_instruction_materializer_preflight_failure_leaves_absent_targets_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve() / "repo"
            self._init_git(project)
            with patch.object(instruction_materializer.shutil, "which", return_value=None):
                with self.assertRaisesRegex(ValueError, "executable"):
                    materialize(project)
            self.assertFalse((project / "AGENTS.md").exists())
            self.assertFalse((project / "CLAUDE.md").exists())

    def test_instruction_materializer_rejects_symlink_in_project_parent_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            actual = base / "actual"
            actual.mkdir()
            linked_parent = base / "linked-parent"
            linked_parent.symlink_to(actual, target_is_directory=True)
            project = actual / "repo"
            self._init_git(project)
            alias = linked_parent / "repo"
            with self.assertRaisesRegex(ValueError, "path component chain"):
                materialize(alias)
            self.assertFalse((project / "AGENTS.md").exists())
            self.assertFalse((project / "CLAUDE.md").exists())

    def test_skill_validator_rejects_external_skill_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._copy_repo(temporary)
            outside = Path(temporary) / "outside-skill.md"
            outside.write_bytes((root / "skills/brandkit-setup/SKILL.md").read_bytes())
            (root / "skills/brandkit-setup/SKILL.md").unlink()
            (root / "skills/brandkit-setup/SKILL.md").symlink_to(outside)
            errors = validate(root)
            self.assertTrue(any("symlink SKILL" in item or "exactly one root" in item for item in errors), errors)

    def test_cli_writes_closed_result_artifact(self) -> None:
        request = self._positive_request("brandkit-setup")
        with tempfile.TemporaryDirectory() as temporary:
            request_path = Path(temporary) / "request.json"
            result_path = Path(temporary) / "result.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            completed = subprocess.run([sys.executable, str(ROOT / "scripts/validate_brandkit_skills.py"), "--request", str(request_path), "--result", str(result_path)], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("pass", load(result_path)["decision"])

    def test_cli_always_writes_invalid_request_results(self) -> None:
        malformed_cases = [
            (b'{"broken":', "raw-json-parse"),
            (b'{"schema_version":"brandkit-skill-request.v1","schema_version":"duplicate"}', "duplicate-key"),
        ]
        omitted = self._positive_request("brandkit-publish")
        del omitted["state"]["rights"]
        extra = self._positive_request("brandkit-interview")
        extra["state"]["extra"] = True
        unknown = self._positive_request("brandkit-setup")
        unknown["skill"] = "brandkit-unknown"
        malformed_cases.extend((json.dumps(value).encode(), "schema-validation") for value in (omitted, extra, unknown))
        with tempfile.TemporaryDirectory() as temporary:
            for index, (raw, code) in enumerate(malformed_cases):
                request_path = Path(temporary) / f"request-{index}.json"
                result_path = Path(temporary) / f"result-{index}.json"
                request_path.write_bytes(raw)
                completed = subprocess.run([sys.executable, str(ROOT / "scripts/validate_brandkit_skills.py"), "--request", str(request_path), "--result", str(result_path)], cwd=ROOT, capture_output=True, text=True)
                self.assertNotEqual(0, completed.returncode)
                result = load(result_path)
                self.assertEqual("blocked", result["decision"])
                self.assertEqual(code, result["error"]["code"])
                self.assertEqual([], result["verified_artifacts"])
                self.assertEqual(__import__("hashlib").sha256(raw).hexdigest(), result["raw_request_sha256"])

    def test_cli_refuses_unsafe_result_symlink(self) -> None:
        request = self._positive_request("brandkit-setup")
        with tempfile.TemporaryDirectory() as temporary:
            request_path = Path(temporary) / "request.json"
            outside = Path(temporary) / "outside.json"
            outside.write_text("untouched", encoding="utf-8")
            result_path = Path(temporary) / "result.json"
            result_path.symlink_to(outside)
            request_path.write_text(json.dumps(request), encoding="utf-8")
            completed = subprocess.run([sys.executable, str(ROOT / "scripts/validate_brandkit_skills.py"), "--request", str(request_path), "--result", str(result_path)], cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(0, completed.returncode)
            self.assertEqual("untouched", outside.read_text(encoding="utf-8"))


class VisualDecisionsRealCoreTests(unittest.TestCase):
    """The adapter measured against the real core, with no substitute anywhere.

    r1c passed with a green suite while ``prepare_visual_confirmation`` could
    not complete a single real bundle, because both tests that drove it replaced
    ``validate_bundle`` with a stand-in and handed it a source document the core
    contract forbids.  The rule for this class is therefore literal: **nothing
    here patches the core**.  Every bundle below is built by the core suite's own
    accepted fixture builders, validated by the real ``validate_bundle``, and the
    flow that r3 accepted is executed step by step over it.

    The one place a substitute would still be defensible - measuring what the
    adapter does with a malformed *core result* - is served instead by calling
    the adapter's own pure gates (``_closed_core_result``,
    ``_visual_source_records``) directly.  That measures the same rule without
    ever putting a fake core in front of the production path.
    """

    RUN = "ci-pilot"
    WORKSPACE = ".brandkit/work/ci-pilot/visual-decisions"

    def _project(self, base: Path, turns: int = 2) -> tuple[Path, Path]:
        """A discovery project with an owner key and ``turns`` answered turns.

        ``turns=1`` builds the one shape the visual operation must refuse even
        though its frontier is empty: the second answer turn is what makes the
        interview's own record a record rather than a single claim.
        """
        project = base / "project"; project.mkdir(parents=True)
        workspace = project / ".brandkit/work/ci-pilot/discovery"
        (workspace / "docs/adr").mkdir(parents=True); (workspace / "sources").mkdir()
        (workspace / "CONTEXT.md").write_bytes(b"# Context\n\nBrand character: editorial and precise.\n")
        (workspace / "docs/adr/0001-brand-structure.md").write_bytes(b"# ADR 0001\n\nUse one master brand.\n")
        (workspace / "sources/owner-answer-2.txt").write_bytes(b"Owner says editorial and precise.\n")
        (project / DISCOVERY_HANDOFF).write_bytes((ROOT / "fixtures/brandkit-skills/discovery-handoff.valid.json").read_bytes())
        key = base / "owner.key"; key.write_bytes(b"owner-test-key-material-32-bytes!!"); key.chmod(0o600)
        discovery.write_progress(project, self.RUN, [], [], list(discovery.BRAND_AXES), "1" * 64, key)
        for turn in range(1, turns):
            discovery.write_progress(project, self.RUN, [], [], list(discovery.BRAND_AXES), str(turn + 1) * 64, key)
        return project.resolve(), key

    def _binding(self, project: Path, relative: str) -> dict[str, object]:
        raw = (project / relative).read_bytes()
        return {"path": relative, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    def _state(self, project: Path, review: str, selection_id: str = "selection-001",
               statement: str = "owner-selection.txt", domains: tuple[str, ...] = VISUAL_FIVE) -> dict:
        return {
            "operation": "prepare-visual-confirmation",
            "brand_raw_preimage": self._binding(project, ".brandkit/work/ci-pilot/brand-raw.md"),
            "candidate_bundle": self._binding(project, f"{self.WORKSPACE}/candidate-set.json"),
            "candidate_review": self._binding(project, review),
            "owner_selection": {
                "selection_id": selection_id,
                "chat_statement": self._binding(project, f"{self.WORKSPACE}/{statement}"),
                "choices": [{"domain": domain, "outcome": "candidate", "candidate_id": f"{domain}-choice"} for domain in domains],
            },
        }

    def _owner_inputs(self, project: Path) -> None:
        (project / ".brandkit/work/ci-pilot/brand-raw.md").write_text("# Brand raw\n", encoding="utf-8")
        (project / self.WORKSPACE / "owner-selection.txt").write_text("Ich waehle diese Kandidaten.\n", encoding="utf-8")

    def _sheet(self, project: Path, now: int, **trust: object) -> str:
        """Render the decision sheet through the core; the one path a candidate review may bind."""
        visual_decisions.render_sheet(project, self.RUN, now, **trust)
        return f"{self.WORKSPACE}/sheet/index.html"

    @staticmethod
    def _resolved(markdown: str) -> dict[str, list[dict]]:
        """The resolved values of every decision, read back out of a filled review."""
        lines = markdown.split("\n"); blocks: dict[str, list[dict]] = {}; heading = None
        for index, line in enumerate(lines):
            if line.startswith("### "):
                heading = line[4:]
            if line == "Bestätigt werden diese Werte:":
                assert lines[index + 1] == "```json", lines[index + 1]
                end = lines.index("```", index + 2)
                blocks[heading] = json.loads("\n".join(lines[index + 2:end]))
        return blocks

    @staticmethod
    def _outside_fences(markdown: str) -> tuple[list[str], bool]:
        """The lines a CommonMark reader parses as Markdown, and whether every fence closed.

        A backtick fence opens on a line of at least three backticks after at
        most three spaces, whose info string has no backtick, and closes on a
        line of at least as many backticks and nothing else.
        """
        outside: list[str] = []; fence = 0
        for line in markdown.split("\n"):
            match = re.match(r" {0,3}(`{3,})(.*)$", line)
            if fence:
                if match and len(match.group(1)) >= fence and not match.group(2).strip():
                    fence = 0
                continue
            if match and "`" not in match.group(2):
                fence = len(match.group(1)); continue
            outside.append(line)
        return outside, fence == 0

    def _confirm_and_consume(self, project: Path, key: Path, handoff: str, selection_id: str, now: int) -> dict:
        review = discovery.render_review(project, handoff, key)
        receipt = discovery.make_owner_receipt(project, handoff, review["path"], key, "owner@example",
                                               f"approval-{selection_id}", "Diese Entscheidungen bestätige ich.", 600, now=now)
        receipt_path = f".brandkit/owner-receipts/ci-pilot/visual-{selection_id}.json"
        discovery.write_owner_receipt(project, receipt_path, receipt)
        return discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=now + 1)

    # ---------------------------------------------------------------- flows

    def test_the_playground_is_refused_as_candidate_review_before_anything_is_spent(self) -> None:
        """The owner confirms against the static sheet, never against the playground.

        Both artifacts are rendered from the same Affinity-authored bundle, so a
        refusal that came after the verification - or after the consumption -
        would show in the external ledger.  Every binding below names a place or
        bytes of the playground.  Since the review binding is positive none of
        them is refused by a playground blocklist any more but by what the review
        has to be - ``<workspace>/sheet/index.html`` - and each is refused with
        that sentence,
        the ledger stays byte-identical, nothing is written, and the static sheet
        then goes through with the very same, still unspent receipts.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            secrets = Path(temporary) / "secrets"; secrets.mkdir()
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            fixture = five_domain_affinity_bundle(project, workspace, secrets, now, run_id=self.RUN)
            self._owner_inputs(project)
            visual_decisions.render_sheet(project, self.RUN, now, affinity_auth=fixture["auth"])
            playground_manifest = visual_decisions.render_playground(project, self.RUN, now, affinity_auth=fixture["auth"])
            ledger = fixture["ledger_path"].read_bytes()
            playground = f"{self.WORKSPACE}/playground"
            (workspace / "review-copy.html").write_bytes((workspace / "playground/index.html").read_bytes())
            (workspace / "data-copy.js").write_bytes((workspace / "playground/data.js").read_bytes())
            (workspace / "scripted-review.html").write_bytes(b"<!doctype html><p>Review</p><SCRIPT src=\"review.js\"></SCRIPT>\n")
            asset = next(item["path"] for item in playground_manifest["files"] if item["path"].startswith("assets/"))
            place = content = "candidate review is not the decision sheet of this run"
            cases = {
                "its page": (f"{playground}/index.html", place), "its script": (f"{playground}/app.js", place),
                "its data file": (f"{playground}/data.js", place), "its stylesheet": (f"{playground}/style.css", place),
                "its manifest": (f"{playground}/manifest.json", place), "one of its assets": (f"{playground}/{asset}", place),
                "a copy of its page": (f"{self.WORKSPACE}/review-copy.html", content),
                "a copy of its data file": (f"{self.WORKSPACE}/data-copy.js", content),
                "a page that runs a script": (f"{self.WORKSPACE}/scripted-review.html", content),
            }
            # The same directory under another spelling, where the file system
            # folds case (the volumes this ships on do).
            if (workspace / "PlayGround/index.html").exists():
                cases["its page, directory spelled in another case"] = (f"{self.WORKSPACE}/PlayGround/index.html", place)
            for label, (path, reason) in cases.items():
                with self.subTest(candidate_review=label):
                    with self.assertRaisesRegex(discovery.DiscoveryError, reason):
                        discovery.prepare_visual_confirmation(project, self.RUN, key, self._state(project, path), now=now,
                                                              affinity_auth=fixture["auth"])
                    self.assertEqual(ledger, fixture["ledger_path"].read_bytes())
                    self.assertFalse((workspace / "handoff.json").exists())
                    self.assertFalse((workspace / "filled-review.md").exists())
                    self.assertFalse((workspace / "selection-sources").exists())
            prepared = discovery.prepare_visual_confirmation(project, self.RUN, key, self._state(project, f"{self.WORKSPACE}/sheet/index.html"),
                                                             now=now, affinity_auth=fixture["auth"])
            self.assertEqual("visual-confirmation-prepared", prepared["state"])
            self.assertNotEqual(ledger, fixture["ledger_path"].read_bytes())

    def test_a_visual_handoff_read_back_with_the_playground_as_review_is_refused(self) -> None:
        """The same rule stands in the handoff rules, not only in front of them.

        A handoff on disk is decided again by every later reader through
        ``handoff_snapshot``.  The real handoff of a successful preparation is
        rewritten so that its review member is the playground - bound correctly
        by path, size and digest - and the read-back refuses it.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            five_domain_bundle(project, workspace, now, run_id=self.RUN)
            self._owner_inputs(project)
            visual_decisions.render_sheet(project, self.RUN, now)
            visual_decisions.render_playground(project, self.RUN, now)
            prepared = discovery.prepare_visual_confirmation(project, self.RUN, key, self._state(project, f"{self.WORKSPACE}/sheet/index.html"), now=now)
            handoff_path = prepared["visual_handoff"]["path"]
            self.assertEqual("visual-decisions", discovery.handoff_snapshot(project, handoff_path, key)["handoff_kind"])
            original = json.loads((project / handoff_path).read_bytes())
            (workspace / "sheet-lookalike.html").write_bytes((workspace / "playground/index.html").read_bytes())
            for review, reason in ((f"{self.WORKSPACE}/playground/index.html", "candidate review is not the decision sheet of this run"),
                                   (f"{self.WORKSPACE}/sheet-lookalike.html", "candidate review is not the decision sheet of this run")):
                with self.subTest(review=review):
                    document = json.loads(json.dumps(original))
                    binding = self._binding(project, review)
                    for item in document["files"]:
                        if item["role"] == "candidate-review":
                            item.update(binding)
                    document["candidate_review_sha256"] = binding["sha256"]
                    (project / handoff_path).write_bytes(discovery.canonical(document))
                    with self.assertRaisesRegex(discovery.DiscoveryError, reason):
                        discovery.handoff_snapshot(project, handoff_path, key)

    def test_the_review_is_the_sheet_rendered_for_the_bound_bundle_and_nothing_else(self) -> None:
        """C-1: the review binding is positive, and every refusal of it is free.

        The owner decided that the static sheet is the byte-bound review
        artifact.  The adapter used to refuse only a playground blocklist, so a
        sanitized playground copy, an invented page, the brief and the sheet's
        own manifest were all confirmed and consumed - and so was a sheet the
        core really rendered, for a bundle rebuilt afterwards.  That last case
        needs no mistake by anybody: a correction rebuilds the bundle, and the
        sheet is simply not rendered again.

        Every binding below is refused with the ledger as the witness - the
        bundle is Affinity-authored, so a refusal behind the verification would
        show there - and without a half artifact.  The stale sheets are built so
        that the core itself accepts both the rebuilt bundle and the old sheet:
        what refuses them is the binding and nothing else.  Rendered again, the
        same selection goes through, spends once, and resolves the new values.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            secrets = Path(temporary) / "secrets"; secrets.mkdir()
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            fixture = five_domain_affinity_bundle(project, workspace, secrets, now, run_id=self.RUN)
            auth, ledger = fixture["auth"], fixture["ledger_path"]
            self._owner_inputs(project)
            sheet = self._sheet(project, now, affinity_auth=auth)
            visual_decisions.render_playground(project, self.RUN, now, affinity_auth=auth)
            genesis = ledger.read_bytes()
            place = "candidate review is not the decision sheet of this run; only <workspace>/sheet/index.html can be reviewed"
            stale = "candidate review is a decision sheet of another candidate bundle; render the sheet again"
            core = "decision sheet is refused by the visual core: "

            def refused(label: str, review: str, message: str) -> None:
                with self.subTest(candidate_review=label):
                    with self.assertRaisesRegex(discovery.DiscoveryError, re.escape(message)):
                        discovery.prepare_visual_confirmation(project, self.RUN, key, self._state(project, review), now=now,
                                                              affinity_auth=auth)
                    self.assertEqual(genesis, ledger.read_bytes())
                    for name in ("handoff.json", "filled-review.md", "selection-sources"):
                        self.assertFalse((workspace / name).exists(), f"{name} after a refusal")

            # 1. Place: nothing but <workspace>/sheet/index.html, in that spelling.
            page = (workspace / "playground/index.html").read_text(encoding="utf-8").replace(visual_decisions.PLAYGROUND_MARKER, "")
            page = re.sub(r"<script[^>]*></script>", "", page, flags=re.IGNORECASE)
            self.assertNotIn("<script", page.lower())
            (workspace / "review-sanitized.html").write_text(page, encoding="utf-8")
            (workspace / "invented.html").write_text("<!doctype html><title>Bogen</title><h1>Visueller Entscheidungsbogen</h1>"
                                                     "<p>color-choice: primary #00FF00</p>\n", encoding="utf-8")
            (workspace / "review-notes.md").write_text("# Review\n\nDer Bogen enthält kein `<script>`-Element.\n", encoding="utf-8")
            for label, relative in (("a sanitized playground copy", "review-sanitized.html"), ("an invented page", "invented.html"),
                                    ("a Markdown review that names the script element", "review-notes.md"),
                                    ("the visual brief", "visual-brief.json"), ("the candidate set", "candidate-set.json"),
                                    ("the sheet's manifest", "sheet/manifest.json"), ("the sheet's stylesheet", "sheet/style.css"),
                                    ("the playground page", "playground/index.html")):
                refused(label, f"{self.WORKSPACE}/{relative}", place)
            # A byte-identical copy of the page under a path that merely ends like
            # the sheet's: its bytes pass every digest statement, so what refuses
            # it is the identity of the path - not a suffix, not the content.
            for relative in ("old-sheet/index.html", "review/sheet/index.html"):
                (workspace / relative).parent.mkdir(parents=True, exist_ok=True)
                (workspace / relative).write_bytes((workspace / "sheet/index.html").read_bytes())
                refused(f"a byte-identical copy of the page at {relative}", f"{self.WORKSPACE}/{relative}", place)
            # The sheet under a second spelling, where the file system folds it
            # onto the same file: refused rather than resolved.
            for spelling in ("SHEET/index.html", "sheet/INDEX.HTML", "Sheet/Index.html"):
                if (workspace / spelling).exists():
                    refused(f"the sheet spelled {spelling}", f"{self.WORKSPACE}/{spelling}", place)
            other = visual_decisions.visual_workspace(project, "ci-pilot-b", create=True)
            five_domain_bundle(project, other, now, run_id="ci-pilot-b")
            visual_decisions.render_sheet(project, "ci-pilot-b", now)
            refused("another run's sheet, by its own path", ".brandkit/work/ci-pilot-b/visual-decisions/sheet/index.html",
                    "candidate review path is outside its canonical workspace")

            # 2. Form, bytes and provenance: the sheet in every way it can stop
            #    being the sheet of this bundle.
            manifest_path, page_path = workspace / "sheet/manifest.json", workspace / "sheet/index.html"
            manifest_raw, page_raw = manifest_path.read_bytes(), page_path.read_bytes()
            forged = {
                "a manifest naming another candidate set": (lambda m: m.update(candidate_set_sha256="1" * 64), stale),
                "a manifest naming another brief": (lambda m: m.update(brief_sha256="1" * 64),
                                                    "candidate review is a decision sheet of another visual brief; render the sheet again"),
                "a manifest naming another observation": (lambda m: m.update(affinity_observation_sha256="1" * 64),
                                                          "candidate review is a decision sheet of another Affinity observation; render the sheet again"),
                "a manifest pinning other page bytes": (lambda m: [item.update(sha256="1" * 64) for item in m["files"] if item["path"] == "index.html"],
                                                        core + "sheet file index.html hash differs"),
                "a manifest outside its schema": (lambda m: m.update(extra=True), core),
            }
            for label, (edit, message) in forged.items():
                document = json.loads(manifest_raw); edit(document)
                manifest_path.write_bytes(visual_decisions.canonical_json(document))
                refused(label, sheet, message)
            manifest_path.write_bytes(manifest_raw)
            page_path.write_bytes(page_raw.replace(b"Visueller Entscheidungsbogen", b"Visueller Entscheidungsbogen!", 1))
            refused("a page edited after rendering", sheet, core + "sheet file index.html hash differs")
            # The playground page - and the sanitized copy - in the sheet's place,
            # each with a manifest that pins its bytes: what refuses them is the
            # sheet's own document form, the statement that replaced the blocklist.
            for label, raw in (("the playground page under the sheet's name", (workspace / "playground/index.html").read_bytes()),
                               ("a sanitized playground copy under the sheet's name", page.encode("utf-8"))):
                page_path.write_bytes(raw)
                document = json.loads(manifest_raw)
                for item in document["files"]:
                    if item["path"] == "index.html":
                        item["sha256"] = hashlib.sha256(raw).hexdigest()
                manifest_path.write_bytes(visual_decisions.canonical_json(document))
                refused(label, sheet, core)
            page_path.write_bytes(page_raw); manifest_path.write_bytes(manifest_raw)
            shutil.move(workspace / "sheet", workspace / "sheet-own")
            shutil.copytree(other / "sheet", workspace / "sheet")
            refused("another run's sheet, copied into this run", sheet, stale)
            shutil.rmtree(workspace / "sheet"); shutil.move(workspace / "sheet-own", workspace / "sheet")

            # 3. The honest failure: a correction rebuilds the bundle and the
            #    sheet is not rendered again.  The core accepts the new bundle
            #    and the old sheet alike; only the binding sees that they differ.
            candidate_set = json.loads((workspace / "candidate-set.json").read_bytes())
            colour = next(item for item in candidate_set["candidates"] if item["kind"] == "color")
            self.assertNotEqual("#7A0019", colour["value"]["roles"]["primary"])
            roles = {**colour["value"]["roles"], "primary": "#7A0019"}; colour["value"]["roles"] = roles
            canonical_write(workspace, colour["value"]["report"]["path"],
                            visual_decisions.color_contrast_report(visual_decisions.candidate_context_sha256(candidate_set), colour["id"], roles, ["text", "icon"]))
            rebind(workspace, candidate_set)
            self.assertEqual(5, len(visual_decisions.verify_bundle(project, self.RUN, now, affinity_auth=auth)["eligible_candidates"]))
            self.assertIsNone(visual_decisions.validate_sheet_manifest(json.loads(manifest_path.read_bytes()), workspace / "sheet"))
            refused("the sheet of the bundle before a colour correction", sheet, stale)
            report = json.loads((workspace / "font-report.json").read_bytes())
            self.assertEqual("Fixture Serif", report["candidates"][0]["family"])
            report["candidates"][0] = font_candidates(workspace, [("serif", "Swapped Serif", "serif-a")])[0]
            canonical_write(workspace, "font-report.json", report); rebind(workspace, candidate_set)
            self.assertEqual(5, len(visual_decisions.verify_bundle(project, self.RUN, now, affinity_auth=auth)["eligible_candidates"]))
            self.assertNotIn(b"Swapped Serif", page_path.read_bytes())
            refused("the sheet of the bundle before a serif swap", sheet, stale)

            # 4. Rendered again: accepted, spent once, and the filled review
            #    shows the values the owner now confirms.
            shutil.rmtree(workspace / "sheet")
            self._sheet(project, now, affinity_auth=auth)
            self.assertEqual(genesis, ledger.read_bytes())
            prepared = discovery.prepare_visual_confirmation(project, self.RUN, key, self._state(project, sheet), now=now, affinity_auth=auth)
            self.assertEqual(1, json.loads(ledger.read_text(encoding="utf-8"))["generation"])
            resolved = self._resolved((project / prepared["filled_review"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual({"kandidat": "serif-a", "klasse": "serif", "familie": "Swapped Serif"}, resolved["typography: typography-choice"][0])
            self.assertIn({"rolle": "primary", "farbe": "#7A0019"}, resolved["color: color-choice"])
            self._confirm_and_consume(project, key, prepared["visual_handoff"]["path"], "selection-001", now)

    def test_every_reader_holds_the_handoff_to_its_sheet_and_to_the_values_it_resolves(self) -> None:
        """The twins: the rules ``prepare`` decides are decided again by every reader.

        ``handoff_snapshot`` is the one read-back, and ``render_review``,
        ``make_owner_receipt`` and ``verify_owner_receipt`` - confirm and
        consume - all stand on it.  Each state below is reachable after a
        successful ``prepare`` by whoever writes the run workspace (register
        point 19), and each is put to all four readers: a bundle rebuilt and
        the handoff rewritten to it while the sheet stays; a brief rewritten,
        once with the manifest forged to match; a refreshed observation; a
        report a decision resolves from, rewritten.  Then the grammar of the
        value resolution, one refusal per rule, and the page-bytes statement
        through the seam itself.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            five_domain_bundle(project, workspace, now, run_id=self.RUN)
            self._owner_inputs(project)
            sheet = self._sheet(project, now)
            prepared = discovery.prepare_visual_confirmation(project, self.RUN, key, self._state(project, sheet), now=now)
            handoff, review = prepared["visual_handoff"]["path"], prepared["filled_review"]["path"]
            receipt_path = ".brandkit/owner-receipts/ci-pilot/visual-selection-001.json"
            discovery.write_owner_receipt(project, receipt_path, discovery.make_owner_receipt(
                project, handoff, review, key, "owner@example", "approval-selection-001", "Diese Entscheidungen bestätige ich.", 600, now=now))
            saved = {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()}

            def restore() -> None:
                for path in [item for item in workspace.rglob("*") if item.is_file() and item not in saved]:
                    path.unlink()
                for path, raw in saved.items():
                    path.write_bytes(raw)

            readers = {
                "handoff_snapshot": lambda: discovery.handoff_snapshot(project, handoff, key),
                "render_review": lambda: discovery.render_review(project, handoff, key),
                "make_owner_receipt": lambda: discovery.make_owner_receipt(project, handoff, review, key, "owner@example", "approval-selection-002",
                                                                           "Diese Entscheidungen bestätige ich.", 600, now=now),
                "verify_owner_receipt": lambda: discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=now + 1),
            }
            for name, read in readers.items():
                with self.subTest(honest=name):
                    self.assertIsNotNone(read())

            def rebuilt_bundle() -> None:
                candidate_set = json.loads((workspace / "candidate-set.json").read_bytes())
                colour = next(item for item in candidate_set["candidates"] if item["kind"] == "color")
                roles = {**colour["value"]["roles"], "primary": "#7A0019"}; colour["value"]["roles"] = roles
                canonical_write(workspace, colour["value"]["report"]["path"],
                                visual_decisions.color_contrast_report(visual_decisions.candidate_context_sha256(candidate_set), colour["id"], roles, ["text", "icon"]))
                rebind(workspace, candidate_set)
                document = json.loads((project / handoff).read_bytes())
                binding = self._binding(project, f"{self.WORKSPACE}/candidate-set.json")
                for item in document["files"]:
                    if item["role"] == "candidate-bundle":
                        item.update(binding)
                document["candidate_bundle_sha256"] = binding["sha256"]
                (project / handoff).write_bytes(discovery.canonical(document))

            def rewritten_brief(forge_manifest: bool) -> None:
                brief = json.loads((workspace / "visual-brief.json").read_bytes()); brief["brief_id"] = "brief-b"
                canonical_write(workspace, "visual-brief.json", brief)
                if forge_manifest:
                    manifest = json.loads((workspace / "sheet/manifest.json").read_bytes())
                    manifest["brief_sha256"] = visual_decisions.sha256_bytes(visual_decisions.canonical_json(brief))
                    (workspace / "sheet/manifest.json").write_bytes(visual_decisions.canonical_json(manifest))

            def refreshed_observation() -> None:
                observation = json.loads((workspace / "affinity-observation.json").read_bytes()); observation["expires_at"] += 1
                canonical_write(workspace, "affinity-observation.json", observation)

            def rewritten_report() -> None:
                report = json.loads((workspace / "font-report.json").read_bytes()); report["human_review_status"] = "complete"
                canonical_write(workspace, "font-report.json", report)

            states = {
                "a rebuilt bundle the handoff was rewritten to, under the old sheet": (
                    rebuilt_bundle, "candidate review is a decision sheet of another candidate bundle; render the sheet again"),
                "a brief rewritten after prepare": (
                    lambda: rewritten_brief(False), "candidate review is a decision sheet of another visual brief; render the sheet again"),
                "a brief rewritten after prepare, the manifest forged to it": (
                    lambda: rewritten_brief(True), "candidate review is a decision sheet of another visual brief; render the sheet again"),
                "an observation refreshed after prepare": (
                    refreshed_observation, "candidate review is a decision sheet of another Affinity observation; render the sheet again"),
                "a font report rewritten after prepare": (
                    rewritten_report, "visual decision report differs from the value that binds it"),
            }
            for label, (edit, message) in states.items():
                edit()
                for name, read in readers.items():
                    with self.subTest(state=label, reader=name):
                        with self.assertRaisesRegex(discovery.DiscoveryError, re.escape(message)):
                            read()
                restore()

            def tampered(domain: str, change) -> None:
                document = json.loads((project / handoff).read_bytes())
                decision = next(item for item in document["decisions"] if item["domain"] == domain)
                change(decision["candidate_value"])
                decision["candidate_value_sha256"] = hashlib.sha256(discovery.canonical(decision["candidate_value"])).hexdigest()
                (project / handoff).write_bytes(discovery.canonical(document))

            def familyless() -> None:
                report = json.loads((workspace / "font-report.json").read_bytes()); del report["candidates"][0]["family"]
                binding = canonical_write(workspace, "font-report.json", report)
                tampered("typography", lambda value: value["report"].update(sha256=binding["sha256"]))

            def valueless() -> None:
                document = json.loads((project / handoff).read_bytes())
                decision = next(item for item in document["decisions"] if item["domain"] == "logo")
                decision["candidate_value"] = ["mark-a", "favicon-a"]
                decision["candidate_value_sha256"] = hashlib.sha256(discovery.canonical(decision["candidate_value"])).hexdigest()
                (project / handoff).write_bytes(discovery.canonical(document))

            unresolvable = "visual decision value cannot be resolved for the review"
            no_candidate = "visual decision names a candidate its report does not carry"
            grammar = {
                "a colour value without roles": (lambda: tampered("color", lambda value: value.pop("roles")), unresolvable),
                "a colour value with an empty role map": (lambda: tampered("color", lambda value: value.update(roles={})), unresolvable),
                "a colour value whose colour is no string": (lambda: tampered("color", lambda value: value["roles"].update(primary=1)), unresolvable),
                "a value that is no object": (valueless, unresolvable),
                "a value that binds no report": (lambda: tampered("typography", lambda value: value.pop("report")), "visual decision value binds no readable report"),
                "a report bound by a digest that is no digest": (lambda: tampered("typography", lambda value: value["report"].update(sha256="x")),
                                                                 "visual decision value binds no readable report"),
                "a report bound by a path that is no string": (lambda: tampered("logo", lambda value: value["report"].update(path=7)),
                                                               "visual decision value binds no readable report"),
                "a typography value naming a font its report lacks": (lambda: tampered("typography", lambda value: value.update(font_candidate_ids=["serif-a", "ghost-a"])), no_candidate),
                "a code-font value naming a font its report lacks": (lambda: tampered("code-font", lambda value: value.update(code_font_id="ghost-a")), no_candidate),
                "a logo value naming no candidate": (lambda: tampered("logo", lambda value: value.update(logo_candidate_ids=[])), no_candidate),
                "a lockup value naming a candidate by something that is no id": (lambda: tampered("lockup", lambda value: value.update(lockup_candidate_ids=[7])), no_candidate),
                "a report whose font carries no family": (familyless, "visual decision report lacks a value the review has to show"),
            }
            for label, (edit, message) in grammar.items():
                edit()
                with self.subTest(resolution=label):
                    with self.assertRaisesRegex(discovery.DiscoveryError, re.escape(message)):
                        discovery.handoff_snapshot(project, handoff, key)
                restore()

            # The page-bytes statement, through the seam: bytes other than the
            # page the manifest pins are refused, the page itself is not.
            page, bundle_raw = (workspace / "sheet/index.html").read_bytes(), (workspace / "candidate-set.json").read_bytes()
            self.assertIsNone(discovery._bound_sheet_review(project, self.WORKSPACE, sheet, page, bundle_raw))
            with self.assertRaisesRegex(discovery.DiscoveryError, "candidate review differs from the decision sheet its manifest binds"):
                discovery._bound_sheet_review(project, self.WORKSPACE, sheet, page + b"\n", bundle_raw)
            # Restored, the consumption still verifies: every refusal was the edit's.
            self.assertIsNotNone(readers["verify_owner_receipt"]())

    def test_the_filled_review_shows_every_resolved_value_and_owner_text_only_as_text(self) -> None:
        """R3: the owner confirms exactly the values the interview resolved.

        The filled review showed candidate ids and a report digest for
        typography, code font, logo and lockup, so a swapped family was
        invisible in the second confirmation.  Every decision now carries its
        resolved values, and they are compared here with the values the bound
        reports really hold, read independently of the adapter.

        Family names come out of an owner-supplied font's ``name`` table.  Two
        of them below carry HTML, a closing script tag, a Markdown link, a
        backtick fence, a quote and a backslash; each has to come back as the
        exact string, and none of it may stand on a line a Markdown reader
        parses as Markdown.  The review is what the receipt binds, so the
        rendered review and the receipt digest are asked about the same bytes.
        """
        family = 'Inj </script><img src=x onerror=alert(1)> ```x "q" \\ back'
        code_family = "Mono <b>bold</b> [link](https://example.invalid) `tick`"
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            multi_domain_bundle(project, workspace, now, {kind: 1 for kind in VISUAL_FIVE}, run_id=self.RUN,
                                serif_family=family, mono_families=(code_family,))
            self._owner_inputs(project)
            state = self._state(project, self._sheet(project, now))
            for choice in state["owner_selection"]["choices"]:
                choice["candidate_id"] = f"{choice['domain']}-a"
            prepared = discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now)
            filled = (project / prepared["filled_review"]["path"]).read_text(encoding="utf-8")

            chosen = {item["kind"]: item["value"] for item in json.loads((workspace / "candidate-set.json").read_bytes())["candidates"]}

            def listed(kind: str) -> dict:
                return {item["id"]: item for item in json.loads((workspace / chosen[kind]["report"]["path"]).read_bytes())["candidates"]}

            fonts, codes, logos, lockups = listed("typography"), listed("code-font"), listed("logo"), listed("lockup")
            expected = {
                "color: color-a": [{"rolle": role, "farbe": colour} for role, colour in sorted(chosen["color"]["roles"].items())],
                "typography: typography-a": [{"kandidat": item, "klasse": fonts[item]["classification"], "familie": fonts[item]["family"]}
                                             for item in chosen["typography"]["font_candidate_ids"]],
                "code-font: code-font-a": [{"kandidat": chosen["code-font"]["code_font_id"], "klasse": codes[chosen["code-font"]["code_font_id"]]["classification"],
                                            "familie": codes[chosen["code-font"]["code_font_id"]]["family"]}],
                "logo: logo-a": [{"kandidat": item, "variante": logos[item]["variant"], "asset": logos[item]["asset_id"], "asset_sha256": logos[item]["asset"]["sha256"]}
                                 for item in chosen["logo"]["logo_candidate_ids"]],
                "lockup: lockup-a": [{"kandidat": item, "variante": lockups[item]["variant"], "asset": lockups[item]["asset_id"], "asset_sha256": lockups[item]["asset"]["sha256"]}
                                     for item in chosen["lockup"]["lockup_candidate_ids"]],
            }
            resolved = self._resolved(filled)
            self.assertEqual(expected, resolved)
            self.assertEqual(19, len(resolved["color: color-a"]))
            self.assertEqual([("serif", family), ("sans", "Fixture Sans")], [(row["klasse"], row["familie"]) for row in resolved["typography: typography-a"]])
            self.assertEqual([("mono", code_family)], [(row["klasse"], row["familie"]) for row in resolved["code-font: code-font-a"]])
            self.assertEqual(["mark", "favicon"], [row["variante"] for row in resolved["logo: logo-a"]])

            outside, closed = self._outside_fences(filled)
            self.assertTrue(closed, "a fence of the filled review never closes")
            for text in (family, code_family, "<img", "</script>", "<b>", "](https://", "`tick`"):
                with self.subTest(owner_text=text):
                    self.assertFalse(any(text in line for line in outside), text)

            rendered = discovery.render_review(project, prepared["visual_handoff"]["path"], key)
            self.assertEqual(filled, rendered["markdown"])
            receipt = discovery.make_owner_receipt(project, prepared["visual_handoff"]["path"], rendered["path"], key, "owner@example",
                                                   "approval-selection-001", "Diese Entscheidungen bestätige ich.", 600, now=now)
            self.assertEqual(hashlib.sha256(filled.encode("utf-8")).hexdigest(), receipt["review_sha256"])


    # ---------------------------------------------------------- measured gaps (r1 critique, point 82)

    def _after_prepare(self, project: Path, key: Path, now: int) -> tuple[str, str, dict]:
        """One successful preparation, its receipt written, and the four readers of it.

        Every rule of the handoff is decided again by each of them, so a gap is
        only closed when all four are asked - which is what the states below do.
        """
        sheet = self._sheet(project, now)
        prepared = discovery.prepare_visual_confirmation(project, self.RUN, key, self._state(project, sheet), now=now)
        handoff, review = prepared["visual_handoff"]["path"], prepared["filled_review"]["path"]
        receipt_path = ".brandkit/owner-receipts/ci-pilot/visual-selection-001.json"
        discovery.write_owner_receipt(project, receipt_path, discovery.make_owner_receipt(
            project, handoff, review, key, "owner@example", "approval-selection-001", "Diese Entscheidungen bestätige ich.", 600, now=now))
        readers = {
            "handoff_snapshot": lambda: discovery.handoff_snapshot(project, handoff, key),
            "render_review": lambda: discovery.render_review(project, handoff, key),
            "make_owner_receipt": lambda: discovery.make_owner_receipt(project, handoff, review, key, "owner@example", "approval-selection-002",
                                                                       "Diese Entscheidungen bestätige ich.", 600, now=now),
            "verify_owner_receipt": lambda: discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=now + 1),
        }
        return handoff, review, readers

    def _visual_run(self, temporary: str) -> tuple[Path, Path, Path, int]:
        project, key = self._project(Path(temporary))
        workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
        now = int(time.time())
        five_domain_bundle(project, workspace, now, run_id=self.RUN)
        self._owner_inputs(project)
        return project, key, workspace, now

    def test_every_reader_holds_the_sheet_to_its_stylesheet_and_its_assets_and_not_only_to_its_page(self) -> None:
        """The sheet the owner saw is all of its files, not only ``index.html``.

        Measured gap K16 of the r1 critique: the manifest re-hashes every member
        against the disk, and nothing asked about it.  A workspace writer who
        swaps the stylesheet or a font asset changes what the page *looks* like
        while ``index.html`` stays byte-identical - the colours, the faces and
        the weights the owner judged all live in those two files.  Each member is
        edited here after a successful preparation, and each of the four readers
        has to refuse by the member's own name.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key, workspace, now = self._visual_run(temporary)
            handoff, review, readers = self._after_prepare(project, key, now)
            for name, read in readers.items():
                with self.subTest(honest=name):
                    self.assertIsNotNone(read())
            sheet = workspace / "sheet"
            asset = next(path for path in sorted((sheet / "assets").iterdir()) if path.suffix == ".woff2")
            edits = {
                "style.css": (sheet / "style.css", b"\n.zqx-injected{color:red}"),
                f"assets/{asset.name}": (asset, b"\x00"),
            }
            for member, (path, suffix) in edits.items():
                saved = path.read_bytes()
                path.write_bytes(saved + suffix)
                for name, read in readers.items():
                    with self.subTest(member=member, reader=name):
                        with self.assertRaisesRegex(discovery.DiscoveryError, re.escape(f"sheet file {member} hash differs")):
                            read()
                path.write_bytes(saved)
            self.assertIsNotNone(readers["verify_owner_receipt"]())

    def test_no_reader_compares_an_absent_or_emptied_observation_against_nothing(self) -> None:
        """A missing Affinity observation is a refusal, never an agreement.

        Measured gap K08: the observation digest is compared against the one the
        sheet manifest carries.  If the read of the file were allowed to fail
        softly - into ``null``, into empty bytes - the comparison would be
        nothing against nothing and a stale sheet would pass.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key, workspace, now = self._visual_run(temporary)
            handoff, review, readers = self._after_prepare(project, key, now)
            observation = workspace / "affinity-observation.json"
            saved = observation.read_bytes()
            states = {
                "the observation is gone": (None, "affinity-observation"),
                "the observation decodes to null": (b"null\n", "another Affinity observation"),
                "the observation is empty": (b"", "Affinity observation"),
            }
            for label, (payload, message) in states.items():
                observation.unlink()
                if payload is not None:
                    observation.write_bytes(payload)
                for name, read in readers.items():
                    with self.subTest(state=label, reader=name):
                        with self.assertRaisesRegex(discovery.DiscoveryError, message):
                            read()
                if observation.exists():
                    observation.unlink()
                observation.write_bytes(saved)
            self.assertIsNotNone(readers["verify_owner_receipt"]())

    def test_the_sheet_manifest_is_never_read_through_a_symlink(self) -> None:
        """The manifest is read as a file of the run, not as a name pointing anywhere.

        Measured gap K13: the read goes through ``O_NOFOLLOW`` components.  The
        bytes below are byte-identical to the real manifest, so the only thing
        under test is the link itself - a manifest that lives outside the sheet
        directory is not this sheet's manifest, whatever it says.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key, workspace, now = self._visual_run(temporary)
            handoff, review, readers = self._after_prepare(project, key, now)
            manifest = workspace / "sheet/manifest.json"
            saved = manifest.read_bytes()
            (workspace / "manifest-elsewhere.json").write_bytes(saved)
            manifest.unlink()
            manifest.symlink_to(Path("..") / "manifest-elsewhere.json")
            self.assertEqual(saved, manifest.read_bytes(), "the link has to resolve to the same bytes")
            for name, read in readers.items():
                with self.subTest(reader=name):
                    with self.assertRaisesRegex(discovery.DiscoveryError, "cannot safely read"):
                        read()
            manifest.unlink(); manifest.write_bytes(saved)
            self.assertIsNotNone(readers["verify_owner_receipt"]())

    def test_the_logo_and_lockup_reports_are_resolved_under_their_pinned_digest_too(self) -> None:
        """Every resolved value comes from a report read under the digest its value pins.

        Measured gap K20: only the font report was ever asked about this.  A
        logo or lockup report rewritten after the preparation would otherwise
        put another asset id and another asset digest in front of the owner in
        the filled review, under a decision he already named.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key, workspace, now = self._visual_run(temporary)
            handoff, review, readers = self._after_prepare(project, key, now)
            document = json.loads((project / handoff).read_bytes())
            for domain in ("logo", "lockup"):
                decision = next(item for item in document["decisions"] if item["domain"] == domain)
                target = workspace / decision["candidate_value"]["report"]["path"]
                saved = target.read_bytes()
                report = json.loads(saved)
                report["candidates"][0]["asset_id"] = "wortmarke-neu"
                target.write_bytes(discovery.canonical(report))
                for name, read in readers.items():
                    with self.subTest(domain=domain, reader=name):
                        with self.assertRaisesRegex(discovery.DiscoveryError, "visual decision report differs from the value that binds it"):
                            read()
                target.write_bytes(saved)
            self.assertIsNotNone(readers["verify_owner_receipt"]())

    def test_no_reader_hangs_on_a_fifo_left_in_the_place_of_a_file_it_reads(self) -> None:
        """A named pipe where a file belongs is a refusal, not a wait.

        Register point 79: ``O_NOFOLLOW`` stops a symlink but not a FIFO, and
        ``open(O_RDONLY)`` on a FIFO with no writer blocks for ever - so a
        workspace writer could hang every reader of the handoff instead of being
        refused.  The core's ``secure_read`` has carried ``O_NONBLOCK`` for this
        reason; the adapter's now does too.  Without it this test does not fail,
        it hangs, which is why each read runs under its own deadline.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key, workspace, now = self._visual_run(temporary)
            handoff, review, readers = self._after_prepare(project, key, now)
            for member in ("sheet/manifest.json", "visual-brief.json", "affinity-observation.json"):
                target = workspace / member
                saved = target.read_bytes()
                target.unlink(); os.mkfifo(target, 0o600)
                for name, read in readers.items():
                    with self.subTest(member=member, reader=name):
                        with deadline(20, f"{name} reading a FIFO at {member}"):
                            with self.assertRaisesRegex(discovery.DiscoveryError, "not a regular file"):
                                read()
                target.unlink(); target.write_bytes(saved)
            self.assertIsNotNone(readers["verify_owner_receipt"]())

    def test_a_family_name_cannot_break_out_of_the_value_block_with_a_line_break_or_a_separator(self) -> None:
        """The resolved values stay one JSON record per line, for every reader of the file.

        Measured gap K17 and register point 80, now closed on both halves and by
        two independent rules.

        U+000A stays legal in a family name, so a name may still spell a heading
        and a fence, and the filled review has to keep it inside its block -
        that is the end-to-end leg below.

        The separators U+2028, U+2029, U+0085 and the bidi controls were the
        other half.  Since the sheet's control class was widened they no longer
        reach a rendered bundle at all: the core refuses the font report where
        the family enters.  That refusal is asserted here too, because it is
        what makes the sheet half of point 80 true.

        The adapter's own ``ensure_ascii`` is NOT the same defence and is
        therefore still measured directly.  This module reads a font report
        through ``secure_read`` and the digest the decision value pins - never
        through ``validate_font_report`` - so its escaping is the line that
        holds if it is ever handed a report the core has not seen.
        """
        separators = (chr(0x2028), chr(0x2029), chr(0x85), chr(0x202E), chr(0x2066), chr(0x061C))
        family = "Fam\n### ZQX2 heading\n```\nnot json\n```"

        # The adapter's own line, on the helper every one of the four readers
        # shares: whatever it is handed, one record stays one line in the file
        # and the value comes back exactly as it went in.
        hostile = "Mono " + "".join(f"{mark}ZQX{index}" for index, mark in enumerate(separators, 3))
        record = {"kandidat": "code-mono-a", "klasse": "mono", "familie": hostile}
        block = discovery._review_value_block([record])
        self.assertEqual(["```json", "[", "]", "```"], [block[0], block[1], block[-2], block[-1]])
        self.assertEqual(5, len(block), "one record has to be one line")
        for mark in separators:
            with self.subTest(escaped=f"U+{ord(mark):04X}"):
                self.assertNotIn(mark, "\n".join(block))
        self.assertEqual([record], json.loads("\n".join(block[1:-1])))

        # And upstream, where the family enters: every one of them is refused by
        # name, so no rendered bundle can carry one into the review.
        for mark in separators:
            with self.subTest(refused=f"U+{ord(mark):04X}"), \
                 self.assertRaisesRegex(visual_decisions.VisualDecisionError,
                                        "font family serif-a contains a forbidden control character"):
                visual_decisions._reject_sheet_control(f"Fam{mark}ily", "font family serif-a")
        with tempfile.TemporaryDirectory() as temporary:
            project, _key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            multi_domain_bundle(project, workspace, now, {kind: 1 for kind in VISUAL_FIVE}, run_id=self.RUN,
                                mono_families=("Mono " + chr(0x2028) + "ZQX9",))
            with self.assertRaisesRegex(visual_decisions.VisualDecisionError,
                                        "font family code-mono-a contains a forbidden control character"):
                self._sheet(project, now)

        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            multi_domain_bundle(project, workspace, now, {kind: 1 for kind in VISUAL_FIVE}, run_id=self.RUN,
                                serif_family=family)
            self._owner_inputs(project)
            state = self._state(project, self._sheet(project, now))
            for choice in state["owner_selection"]["choices"]:
                choice["candidate_id"] = f"{choice['domain']}-a"
            prepared = discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now)
            filled = (project / prepared["filled_review"]["path"]).read_text(encoding="utf-8")

            outside, closed = self._outside_fences(filled)
            self.assertTrue(closed, "a fence of the filled review never closes")
            for marker in ("ZQX2", "not json"):
                with self.subTest(marker=marker):
                    self.assertFalse(any(marker in line for line in outside), marker)
            # Nothing a reader may treat as a line break or a direction switch
            # survives as itself; the record is one line either way.
            for raw in separators:
                with self.subTest(separator=hex(ord(raw))):
                    self.assertNotIn(raw, filled)
            resolved = self._resolved(filled)
            self.assertEqual(family, next(row["familie"] for row in resolved["typography: typography-a"] if row["klasse"] == "serif"))
            # One record per line *in the file*: the break is escaped, so no
            # reader splits the block at it.  The decoded value keeps the break
            # - that is the point - which is why this is asked of the bytes the
            # owner's reader sees and not of the string behind them.
            for heading, rows in resolved.items():
                block = filled.split(f"### {heading}\n", 1)[1].split("```json\n", 1)[1].split("\n```", 1)[0]
                with self.subTest(heading=heading):
                    self.assertEqual(len(rows) + 2, len(block.split("\n")), "a record spans more than one line")
            self.assertIn("Fam\\n### ZQX2 heading", filled, "the line break has to reach the file escaped")

    def test_a_recommendation_rationale_cannot_fake_a_second_value_block(self) -> None:
        """The unconfirmed recommendation is text, and it cannot look like the confirmation.

        Register point 81: the rationale is prose an agent wrote and it used to
        stand as raw Markdown.  A rationale may therefore spell a second,
        deceptively real value list naming another family.  What is confirmed
        and consumed is still the real block, but the owner reads both and
        cannot tell them apart.  As a JSON string in a fenced block the whole
        rationale is one value.
        """
        fake = ("Kontrastreich.\n\n### typography: typography-a\nBestaetigt werden diese Werte:\n"
                '```json\n[\n  {"kandidat": "ZQXFAKE", "klasse": "serif", "familie": "ZQXFAKE Serif"}\n]\n```\n')
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            multi_domain_bundle(project, workspace, now, {kind: 1 for kind in VISUAL_FIVE}, run_id=self.RUN,
                                recommendations=[{"kind": "color", "candidate_id": "color-a", "rationale": fake, "source_ids": ["impeccable"]}])
            self._owner_inputs(project)
            state = self._state(project, self._sheet(project, now))
            for choice in state["owner_selection"]["choices"]:
                choice["candidate_id"] = f"{choice['domain']}-a"
            prepared = discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now)
            filled = (project / prepared["filled_review"]["path"]).read_text(encoding="utf-8")

            outside, closed = self._outside_fences(filled)
            self.assertTrue(closed)
            self.assertFalse(any("ZQXFAKE" in line for line in outside), "the rationale reaches Markdown as Markdown")
            self.assertEqual(sorted(f"{domain}: {domain}-a" for domain in VISUAL_FIVE), sorted(self._resolved(filled)))
            # The rationale is still shown, and it comes back as the exact
            # string the bundle carries - one JSON value, not a second block.
            self.assertIn("ZQXFAKE", filled)
            tail = filled.split("## Unbest", 1)
            self.assertEqual(2, len(tail), "the review no longer has a recommendation section")
            self.assertEqual(fake, json.loads(tail[1].split("```json\n", 1)[1].split("\n```", 1)[0])[0]["begruendung"])

    def test_the_r3_flow_runs_end_to_end_against_the_real_core_for_all_five_domains(self) -> None:
        """The accepted r3 sequence, executed once, with the real core throughout.

        unselected sheet -> owner names ids only -> prepare -> byte-bound review
        -> confirm -> consume.  The unselected sheet here is the *core's own*
        ``render_sheet`` output, so the first step of the flow is the artifact
        r3 names rather than a stand-in for it.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            rationale = "RATIONALE-CANARY-r1d: technically valid, still unconfirmed."
            five_domain_bundle(project, workspace, now, run_id=self.RUN, recommendations=[
                {"kind": "color", "candidate_id": "color-choice", "rationale": rationale, "source_ids": ["impeccable"]}])
            self._owner_inputs(project)

            # Step 1: the unselected sheet, from the public renderer.
            manifest = visual_decisions.render_sheet(project, self.RUN, now)
            self.assertFalse(manifest["preselection"]); self.assertFalse(manifest["normative"])
            self.assertEqual("required", manifest["human_review_status"])
            sheet = f"{self.WORKSPACE}/sheet/index.html"

            # Step 2/3: the owner names ids, the interview resolves the values.
            preimage_before = (project / ".brandkit/work/ci-pilot/brand-raw.md").read_bytes()
            state = self._state(project, sheet)
            prepared = discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now)
            self.assertFalse(prepared["normative"])
            self.assertEqual("owner-review-then-confirm-or-correct", prepared["next_operation"])
            self.assertEqual(sorted(VISUAL_FIVE), sorted(item["domain"] for item in prepared["resolved_choices"]))
            self.assertEqual(preimage_before, (project / ".brandkit/work/ci-pilot/brand-raw.md").read_bytes())
            # Not one caller-named value: every choice carries an id and a hash
            # the adapter derived itself from the pinned bundle.
            for item in prepared["resolved_choices"]:
                self.assertEqual({"domain", "outcome", "candidate_id", "candidate_value_sha256"}, set(item))

            # The handoff cites the release-bound source contract, not an invented one.
            handoff = prepared["visual_handoff"]["path"]
            document = json.loads((project / handoff).read_text(encoding="utf-8"))
            self.assertEqual(["impeccable", "owner-visual-selection"], sorted(item["id"] for item in document["sources"]))
            evidence = [item for item in document["files"] if item["role"] == "evidence"]
            self.assertEqual([f"{self.WORKSPACE}/selection-sources/visual-decision-sources.v1.json"], [item["path"] for item in evidence])
            self.assertEqual(hashlib.sha256((ROOT / "visual-decision-sources.v1.json").read_bytes()).hexdigest(), evidence[0]["sha256"])
            record = json.loads((project / f"{self.WORKSPACE}/selection-sources/impeccable.json").read_text(encoding="utf-8"))
            self.assertEqual("selected", record["decision"])
            self.assertEqual("github:pbakaus/impeccable", record["selected_pin"]["repository"])

            # Steps 4-6: byte-bound review, confirmation, consume.
            consumed = self._confirm_and_consume(project, key, handoff, "selection-001", now)
            self.assertEqual("visual-decisions", consumed["handoff_kind"])
            block = consumed["brand_raw_block"]
            for domain in VISUAL_FIVE:
                self.assertIn(f"Candidate: `{domain}-choice`", block)
            # The block binds both hashes; a block without them would let a
            # confirmed decision be re-attached to a different handoff.
            self.assertIn(prepared["visual_handoff"]["sha256"], block)
            self.assertIn(consumed["receipt_sha256"], block)
            # The recommendation rationale is not a decision and does not leak.
            self.assertNotIn("RATIONALE-CANARY", block)
            self.assertNotIn("RATIONALE-CANARY", json.dumps(consumed, ensure_ascii=False))

    def test_the_r3_flow_runs_against_the_real_core_with_affinity_authored_candidates(self) -> None:
        """The same flow over a bundle whose logo and lockup came out of Affinity.

        r1d could not run this from the sheet.  ``render_sheet`` refused every
        Affinity-authored bundle unconditionally, so this test used to bind a
        two-line markdown file with no candidate in it as the artifact the owner
        reviewed - which is the gap restated, not a containment: the owner
        confirmed logo and lockup decisions nobody had shown him.  Step 1 is
        therefore the core's own ``render_sheet`` output here as well, and the
        assertions say that the four authored candidates are on the page the
        review binds.

        The trust inputs are invocation arguments, so they are supplied the way
        the CLI supplies them.  The ledger is the witness for where consumption
        happens: rendering the sheet leaves it untouched, ``prepare`` advances
        it once, and a second ``prepare`` is refused.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            secrets = Path(temporary) / "secrets"; secrets.mkdir()
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            fixture = five_domain_affinity_bundle(project, workspace, secrets, now, run_id=self.RUN)
            self._owner_inputs(project)

            # Step 1: the unselected sheet, from the public renderer, for a
            # bundle whose logo and lockup candidates Affinity authored.
            ledger_before = fixture["ledger_path"].read_bytes()
            manifest = visual_decisions.render_sheet(project, self.RUN, now, affinity_auth=fixture["auth"])
            self.assertFalse(manifest["preselection"]); self.assertFalse(manifest["normative"])
            self.assertEqual("required", manifest["human_review_status"])
            # Rendering verifies the receipts; it does not spend them.
            self.assertEqual(ledger_before, fixture["ledger_path"].read_bytes())
            review_artifact = f"{self.WORKSPACE}/sheet/index.html"
            page = (project / review_artifact).read_text(encoding="utf-8")
            for candidate_id in ("mark-a", "favicon-a", "horizontal-a", "stacked-a"):
                # Sheet assets are named by content since the render-sheet fix.
                digest = hashlib.sha256((workspace / f"staging/logo-doc/{candidate_id}.svg").read_bytes()).hexdigest()
                self.assertIn(f'<img src="assets/{digest}.svg"', page)
                self.assertTrue((project / self.WORKSPACE / f"sheet/assets/{digest}.svg").is_file())

            # Steps 2/3: the owner names ids against the sheet he was shown.
            state = self._state(project, review_artifact)
            prepared = discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now,
                                                             affinity_auth=fixture["auth"])
            self.assertFalse(prepared["normative"])
            self.assertEqual(sorted(VISUAL_FIVE), sorted(item["domain"] for item in prepared["resolved_choices"]))
            self.assertEqual(hashlib.sha256((project / review_artifact).read_bytes()).hexdigest(),
                             prepared["candidate_review_sha256"])
            # The external ledger really was the authority: it advanced, once.
            self.assertNotEqual(ledger_before, fixture["ledger_path"].read_bytes())
            ledger = json.loads(fixture["ledger_path"].read_text(encoding="utf-8"))
            self.assertEqual(sorted(fixture["nonces"]), sorted(ledger["nonces"]))
            self.assertEqual(1, ledger["generation"])

            consumed = self._confirm_and_consume(project, key, prepared["visual_handoff"]["path"], "selection-001", now)
            for domain in VISUAL_FIVE:
                self.assertIn(f"Candidate: `{domain}-choice`", consumed["brand_raw_block"])

            # Single-use: the same receipts cannot authenticate a second bundle
            # validation, and the refusal arrives as a contract message.
            with self.assertRaisesRegex(discovery.DiscoveryError, "already consumed"):
                discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now,
                                                      affinity_auth=fixture["auth"])
            # Without the descriptor the core refuses the authored candidates
            # outright, so nothing downgrades to an unauthenticated path.
            with self.assertRaisesRegex(discovery.DiscoveryError, "require external authentication"):
                discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now)

    def test_a_refused_prepare_never_spends_the_receipts_it_did_not_use(self) -> None:
        """An owner typo must cost nothing that cannot be got back.

        Until r1f this operation validated the bundle *consumingly* before it
        had looked at the owner's selection at all, so every rule below that
        line refused a request whose one-shot Affinity receipts were already
        gone.  The consequence was not a wasted attempt but a dead run: the
        corrected selection was refused with *already consumed*, and so was the
        sheet, which is the artifact the correction would have to be made
        against.  The skill promises the opposite in as many words - corrections
        regenerate handoff and review - so this was a false refusal of the
        documented normal case.

        The property is stated over the whole operation rather than over the
        line that moved: four independent, purely owner-side mistakes, each on
        a fresh bundle, and after each refusal the run has to be *intact* -
        the external ledger byte-identical, the sheet renderable again, and the
        corrected selection accepted.  The last assertion is the one that keeps
        the fix honest in the other direction: the accepted attempt consumes,
        exactly once, so nothing here was bought by weakening single-use.
        """
        mistakes = {
            "an extra caller field on one choice":
                lambda request: request["owner_selection"]["choices"][0].update({"candidate_value": {"roles": {}}}),
            "a typo in one candidate id":
                lambda request: request["owner_selection"]["choices"][0].update({"candidate_id": "color-choise"}),
            "an invalid outcome word":
                lambda request: request["owner_selection"]["choices"][0].update({"outcome": "confirmed"}),
            "one domain answered twice, one missing":
                lambda request: request["owner_selection"].__setitem__(
                    "choices", request["owner_selection"]["choices"][:-1]),
        }
        for label, spoil in mistakes.items():
            with self.subTest(mistake=label), tempfile.TemporaryDirectory() as temporary:
                project, key = self._project(Path(temporary))
                secrets = Path(temporary) / "secrets"; secrets.mkdir()
                workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
                now = int(time.time())
                fixture = five_domain_affinity_bundle(project, workspace, secrets, now, run_id=self.RUN)
                self._owner_inputs(project)
                visual_decisions.render_sheet(project, self.RUN, now, affinity_auth=fixture["auth"])
                review_artifact = f"{self.WORKSPACE}/sheet/index.html"
                good = self._state(project, review_artifact)

                before = fixture["ledger_path"].read_bytes()
                spoiled = json.loads(json.dumps(good)); spoil(spoiled)
                with self.assertRaises(discovery.DiscoveryError):
                    discovery.prepare_visual_confirmation(project, self.RUN, key, spoiled, now=now,
                                                          affinity_auth=fixture["auth"])
                # Nothing was spent, and nothing was written on the way out.
                self.assertEqual(before, fixture["ledger_path"].read_bytes())
                self.assertFalse((project / self.WORKSPACE / "handoff.json").exists())

                # The run is intact: the sheet is write-once, so it is removed
                # and rendered again - the step the burn used to make impossible.
                shutil.rmtree(project / self.WORKSPACE / "sheet")
                visual_decisions.render_sheet(project, self.RUN, now, affinity_auth=fixture["auth"])
                self.assertEqual(before, fixture["ledger_path"].read_bytes())
                self.assertEqual(hashlib.sha256((project / review_artifact).read_bytes()).hexdigest(),
                                 self._binding(project, review_artifact)["sha256"])

                # And the owner's corrected selection goes through, consuming once.
                prepared = discovery.prepare_visual_confirmation(project, self.RUN, key,
                                                                 self._state(project, review_artifact), now=now,
                                                                 affinity_auth=fixture["auth"])
                self.assertFalse(prepared["normative"])
                ledger = json.loads(fixture["ledger_path"].read_text(encoding="utf-8"))
                self.assertEqual(1, ledger["generation"])
                self.assertEqual(sorted(fixture["nonces"]), sorted(ledger["nonces"]))
                self._confirm_and_consume(project, key, prepared["visual_handoff"]["path"], "selection-001", now)

    def test_a_refusal_raised_inside_a_callee_spends_nothing_either(self) -> None:
        """The thirteenth refusal: owner input that only a callee could refuse.

        The test above measures the twelve refusals in the operation's own body.
        The r1f critic found a thirteenth one call level down, and it landed on
        the wrong side: ``handoff_snapshot`` ran *after* the consuming
        validation and after both writes, so a purely owner-side input error
        only it could see - a ``chat_statement`` bound to a file the handoff
        inventory already lists, the candidate set or the candidate review -
        reached ``visual handoff inventory is not closed`` with the Affinity
        receipts already spent, ``handoff.json`` on disk, ``filled-review.md``
        missing, and neither a correction nor a second sheet render still
        possible.  A pure owner typo destroyed the run.

        Nothing about the *refusal* changes here - the message is the same and
        the input is still wrong.  What changes is its price, so the assertions
        are the four this class already states for the twelve: the ledger stays
        byte-identical, no half artifact is left behind, the sheet renders
        again, and the corrected selection is accepted and consumes exactly once.
        """
        mistakes = {
            # Both files the inventory already lists: the candidate set the
            # selection is resolved against, and the candidate review the owner
            # was shown.  Naming them by role rather than by path matters -
            # the review member *is* the sheet in this state, so a literal
            # path would bind a file the inventory never mentions and reach no
            # duplicate at all.
            "the candidate set": "bundle",
            "the candidate review": "review",
        }
        for label, role in mistakes.items():
            with self.subTest(mistake=f"chat_statement bound to {label}"), tempfile.TemporaryDirectory() as temporary:
                project, key = self._project(Path(temporary))
                secrets = Path(temporary) / "secrets"; secrets.mkdir()
                workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
                now = int(time.time())
                fixture = five_domain_affinity_bundle(project, workspace, secrets, now, run_id=self.RUN)
                self._owner_inputs(project)
                visual_decisions.render_sheet(project, self.RUN, now, affinity_auth=fixture["auth"])
                review_artifact = f"{self.WORKSPACE}/sheet/index.html"
                statement = f"{self.WORKSPACE}/candidate-set.json" if role == "bundle" else review_artifact

                before = fixture["ledger_path"].read_bytes()
                spoiled = self._state(project, review_artifact)
                spoiled["owner_selection"]["chat_statement"] = self._binding(project, statement)
                with self.assertRaisesRegex(discovery.DiscoveryError, "visual handoff inventory is not closed"):
                    discovery.prepare_visual_confirmation(project, self.RUN, key, spoiled, now=now,
                                                          affinity_auth=fixture["auth"])

                # Nothing was spent and no half artifact was left behind.
                self.assertEqual(before, fixture["ledger_path"].read_bytes())
                self.assertEqual(0, json.loads(before.decode("utf-8"))["generation"])
                self.assertFalse((project / self.WORKSPACE / "handoff.json").exists())
                self.assertFalse((project / self.WORKSPACE / "filled-review.md").exists())
                sources = project / self.WORKSPACE / "selection-sources"
                self.assertEqual([], sorted(path.name for path in sources.iterdir()) if sources.is_dir() else [])

                # The run is intact: the sheet renders again and the corrected
                # selection is accepted, consuming exactly once.
                shutil.rmtree(project / self.WORKSPACE / "sheet")
                visual_decisions.render_sheet(project, self.RUN, now, affinity_auth=fixture["auth"])
                self.assertEqual(before, fixture["ledger_path"].read_bytes())
                prepared = discovery.prepare_visual_confirmation(project, self.RUN, key,
                                                                 self._state(project, review_artifact), now=now,
                                                                 affinity_auth=fixture["auth"])
                ledger = json.loads(fixture["ledger_path"].read_text(encoding="utf-8"))
                self.assertEqual(1, ledger["generation"])
                self.assertEqual(sorted(fixture["nonces"]), sorted(ledger["nonces"]))
                self._confirm_and_consume(project, key, prepared["visual_handoff"]["path"], "selection-001", now)

    def test_a_binding_to_a_path_this_operation_writes_is_refused_before_the_commit(self) -> None:
        """The fourteenth and fifteenth refusals: an owner binding on a written path.

        The rehearsal above shares its *rules* with the post-commit read-back
        but not its *state*.  ``_staged_member_reader`` answers every inventory
        member from the bytes the operation staged; ``_bound_file`` answers it
        from the disk, and between the two the operation writes ``handoff.json``
        and ``filled-review.md``.  Neither path is an inventory member of its
        own, so the duplicate-path rule the r1f finding installed cannot see
        them - and both are names the owner may give his ``chat_statement`` or
        his ``candidate_review``, because the only condition on those bindings
        is the workspace prefix.

        Unfixed, that produced two shapes, and both are measured here:

        * bound to ``handoff.json`` the operation *refused* - in ``_bound_file``,
          behind the commit, with the receipts spent, ``handoff.json``
          overwritten and ``filled-review.md`` missing;
        * bound to ``filled-review.md`` it did not refuse at all.  ``prepare``
          returned a ``visual_handoff`` with a digest, and every reader of that
          handoff - ``handoff_snapshot``, ``render_review``,
          ``make_owner_receipt`` - rejected it from then on, because the write
          of the filled review had destroyed the member in the meantime.  A dead
          run with no refusal anywhere is the worse of the two.

        r1h installed that rule as a raw-string dict lookup, and the r1h critic
        walked it one spelling further: the file systems this ships on decide
        that ``HANDOFF.JSON`` and ``handoff.json`` are one file, so the lookup
        answered ``False`` about a path the operation overwrites anyway and both
        shapes ran again unchanged - register point 26, the fourth appearance of
        "text where the question is identity" in this unit.  The spellings are
        therefore cases of this test rather than a test of their own: whichever
        way the owner writes the name, the same six assertions have to hold.

        So the assertions are the same four this class states for the twelve and
        the thirteenth - ledger byte-identical, no half artifact, sheet
        renderable, correction accepted - plus the two this case adds: the
        owner's own file must come out byte-identical, and the refusal must name
        the rule rather than the descriptor mismatch it used to surface as.
        """
        # The fifteenth refusal is the fourteenth one spelling further, so the
        # rule is asked about spellings first.  ``_fold_path`` is the fold
        # ``_names_protected_run`` has used since r1d; it folds per component,
        # it folds strictly more than any file system does, and it never grants
        # membership - it only ever finds a collision.
        fold = discovery._fold_path
        self.assertEqual(fold("a/handoff.json"), fold("a/HANDOFF.JSON"))
        self.assertEqual(fold("a/filled-review.md"), fold("A/Filled-Review.MD"))
        self.assertEqual(fold("x/ä/y"), fold("X/Ä/Y"), "NFD and NFC are one name")
        self.assertEqual(fold("Ähnlich.md"), fold("ähnlich.MD"), "umlaut, folded and decomposed")
        self.assertEqual(fold("STRASSE"), fold("straße"), "casefold folds more than the file system")
        self.assertNotEqual(fold("a/handoff.json"), fold("a/handoff.jsonx"))
        self.assertNotEqual(fold("a/b"), fold("a/b/c"))

        cases = {
            # The four the r1f finding named, in the canonical spelling.
            "chat_statement named handoff.json": ("statement", "handoff.json"),
            "chat_statement named filled-review.md": ("statement", "filled-review.md"),
            "candidate_review named handoff.json": ("review", "handoff.json"),
            "candidate_review named filled-review.md": ("review", "filled-review.md"),
            # The four the r1h critic reproduced one spelling further: on the
            # file systems this ships on these are the very same two files, and
            # the raw-string rule saw two paths where the kernel sees one.  Two
            # of them did not refuse at all - ``prepare`` returned a digest for
            # a handoff every one of its own readers rejected from then on.
            "chat_statement named HANDOFF.JSON": ("statement", "HANDOFF.JSON"),
            "chat_statement named Filled-Review.md": ("statement", "Filled-Review.md"),
            "candidate_review named HANDOFF.JSON": ("review", "HANDOFF.JSON"),
            "candidate_review named Filled-Review.md": ("review", "Filled-Review.md"),
            # And two mixed spellings, because "upper case" is not the rule:
            # the rule is that spelling does not decide which file is meant.
            "chat_statement named HandOff.jSoN": ("statement", "HandOff.jSoN"),
            "candidate_review named FILLED-REVIEW.MD": ("review", "FILLED-REVIEW.MD"),
        }
        for label, (which, filename) in cases.items():
            with self.subTest(binding=label), tempfile.TemporaryDirectory() as temporary:
                project, key = self._project(Path(temporary))
                secrets = Path(temporary) / "secrets"; secrets.mkdir()
                workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
                now = int(time.time())
                fixture = five_domain_affinity_bundle(project, workspace, secrets, now, run_id=self.RUN)
                self._owner_inputs(project)
                visual_decisions.render_sheet(project, self.RUN, now, affinity_auth=fixture["auth"])
                review_artifact = f"{self.WORKSPACE}/sheet/index.html"
                # The owner's own file, under a name the workspace happens to
                # reserve for an artifact of this operation.  Nothing he was
                # given says he may not.
                owner_raw = f"# Bogen\n\nMeine Auswahl steht hier: {label}.\n".encode("utf-8")
                (project / self.WORKSPACE / filename).write_bytes(owner_raw)

                before = fixture["ledger_path"].read_bytes()
                spoiled = self._state(project, review_artifact)
                spoiled_binding = self._binding(project, f"{self.WORKSPACE}/{filename}")
                if which == "statement":
                    spoiled["owner_selection"]["chat_statement"] = spoiled_binding
                else:
                    spoiled["candidate_review"] = spoiled_binding
                # A candidate review under either name is no decision sheet, so
                # since the review binding is positive the place statement
                # answers it one rule earlier - above the verification as well,
                # so every assertion below holds unchanged.  The chat statement
                # has no such statement and still meets the seam rule.
                expected = ("visual handoff inventory member lies on a path this operation overwrites" if which == "statement"
                            else "candidate review is not the decision sheet of this run")
                with self.assertRaisesRegex(discovery.DiscoveryError, expected):
                    discovery.prepare_visual_confirmation(project, self.RUN, key, spoiled, now=now,
                                                          affinity_auth=fixture["auth"])

                # Nothing was spent, the owner's file is untouched, and the
                # artifact the operation did not get to write is simply absent.
                self.assertEqual(before, fixture["ledger_path"].read_bytes())
                self.assertEqual(0, json.loads(before.decode("utf-8"))["generation"])
                owner_file = project / self.WORKSPACE / filename
                self.assertEqual(owner_raw, owner_file.read_bytes())
                # "No half artifact", asked by identity rather than by name:
                # under a flipped spelling the owner's own file *is* the name
                # one of the two artifacts would have, so a name test would
                # either pass vacuously or contradict itself.  Either artifact
                # may exist only if it is the very file the owner brought.
                owner_id = (owner_file.stat().st_dev, owner_file.stat().st_ino)
                for name in ("handoff.json", "filled-review.md"):
                    landed = project / self.WORKSPACE / name
                    if not landed.exists():
                        continue
                    self.assertEqual(owner_id, (landed.stat().st_dev, landed.stat().st_ino),
                                     f"{name} was written behind a refusal")
                    self.assertEqual(owner_raw, landed.read_bytes())
                sources = project / self.WORKSPACE / "selection-sources"
                self.assertEqual([], sorted(path.name for path in sources.iterdir()) if sources.is_dir() else [])

                # The run is intact: the sheet renders again, and the corrected
                # selection - the same request with the binding put back on the
                # owner's own statement - is accepted and consumes exactly once.
                shutil.rmtree(project / self.WORKSPACE / "sheet")
                visual_decisions.render_sheet(project, self.RUN, now, affinity_auth=fixture["auth"])
                self.assertEqual(before, fixture["ledger_path"].read_bytes())
                (project / self.WORKSPACE / filename).unlink()
                prepared = discovery.prepare_visual_confirmation(project, self.RUN, key,
                                                                 self._state(project, review_artifact), now=now,
                                                                 affinity_auth=fixture["auth"])
                ledger = json.loads(fixture["ledger_path"].read_text(encoding="utf-8"))
                self.assertEqual(1, ledger["generation"])
                self.assertEqual(sorted(fixture["nonces"]), sorted(ledger["nonces"]))
                # And the handoff the accepted attempt wrote is the one its own
                # readers accept - the half of the bug that never refused.
                self._confirm_and_consume(project, key, prepared["visual_handoff"]["path"], "selection-001", now)

    def test_every_owner_binding_descriptor_is_closed_and_refuses_for_free(self) -> None:
        """N16.  ``_closed_binding``'s key-set disjunct, measured rather than assumed.

        Four of this operation's inputs are owner-written descriptors -
        ``brand_raw_preimage``, ``candidate_bundle``, ``candidate_review`` and
        the selection's ``chat_statement`` - and each is read through
        ``_bound_file``, whose first act is to ask whether the descriptor is
        closed: exactly ``{path, size, sha256}``, a non-negative integer size
        and a hex-64 digest.  That disjunct is what carries "no caller-supplied
        value reaches the core" one level down: an extra field on a descriptor
        is a value the caller injected into a structure the operation otherwise
        builds itself.

        The r1h critic dropped the key-set disjunct in his ``N16`` and no test
        in either module noticed - a descriptor with an extra field was simply
        accepted.  So the rule is asked here directly, at all four sites and in
        both shapes it can fail, and it is asked with real Affinity receipts on
        the table to prove what its position already promises: every one of
        these refusals is free, and the corrected request still runs.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            secrets_dir = Path(temporary) / "secrets"; secrets_dir.mkdir()
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            fixture = five_domain_affinity_bundle(project, workspace, secrets_dir, now, run_id=self.RUN)
            self._owner_inputs(project)
            visual_decisions.render_sheet(project, self.RUN, now, affinity_auth=fixture["auth"])
            review_artifact = f"{self.WORKSPACE}/sheet/index.html"
            before = fixture["ledger_path"].read_bytes()

            sites = {
                "brand_raw_preimage": "brand-raw preimage",
                "candidate_bundle": "candidate bundle",
                "candidate_review": "candidate review",
                "chat_statement": "owner selection statement",
            }
            # Every way the disjunct can be false, one per shape it guards.
            damages = {
                "extra field": lambda d: {**d, "role": "source"},
                "missing size": lambda d: {name: value for name, value in d.items() if name != "size"},
                "size is a string": lambda d: {**d, "size": str(d["size"])},
                "negative size": lambda d: {**d, "size": -1},
                "digest is not hex-64": lambda d: {**d, "sha256": d["sha256"][:-1] + "z"},
            }
            for site, label in sites.items():
                for damage, apply in damages.items():
                    with self.subTest(descriptor=site, damage=damage):
                        state = self._state(project, review_artifact)
                        if site == "chat_statement":
                            state["owner_selection"][site] = apply(state["owner_selection"][site])
                        else:
                            state[site] = apply(state[site])
                        with self.assertRaisesRegex(discovery.DiscoveryError,
                                                    f"{label} descriptor is not closed"):
                            discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now,
                                                                  affinity_auth=fixture["auth"])
                        # Above the verification, so nothing is spent and
                        # nothing is written.
                        self.assertEqual(before, fixture["ledger_path"].read_bytes())
                        self.assertFalse((project / self.WORKSPACE / "handoff.json").exists())
                        self.assertFalse((project / self.WORKSPACE / "filled-review.md").exists())

            # And the closed descriptor is accepted, so the rule refuses the
            # malformation rather than the site.
            prepared = discovery.prepare_visual_confirmation(project, self.RUN, key,
                                                             self._state(project, review_artifact), now=now,
                                                             affinity_auth=fixture["auth"])
            self.assertEqual(1, json.loads(fixture["ledger_path"].read_text(encoding="utf-8"))["generation"])
            self._confirm_and_consume(project, key, prepared["visual_handoff"]["path"], "selection-001", now)

    def test_no_reader_accepts_a_handoff_from_a_path_that_is_not_the_canonical_one(self) -> None:
        """N29.  The path gate of ``handoff_snapshot``, on the three reading routes.

        ``prepare_visual_confirmation`` builds the handoff path itself from an
        already validated run id, which is why the ordering test lists
        "handoff path is not the canonical run-scoped path" among the messages
        that are unreachable *on that route* and may therefore sit behind the
        commit.  The three readers are the opposite case: ``render_review``,
        ``make_owner_receipt`` and ``verify_owner_receipt`` take the path from
        their caller, and for them the gate is the only thing standing between
        a run's identity and any handoff-shaped document anywhere below the
        project.  The r1h critic dropped the gate in his ``N29`` and
        ``render_review`` happily rendered a handoff read from
        ``.../visual-decisions/copy.json``; nothing in either module failed.

        Every spelling below is a byte-identical copy of the handoff this run
        really produced, so the only thing that may refuse it is its *path* -
        a document that is wrong about nothing except where it lies.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            five_domain_bundle(project, workspace, now, run_id=self.RUN)
            self._owner_inputs(project)
            visual_decisions.render_sheet(project, self.RUN, now)
            prepared = discovery.prepare_visual_confirmation(
                project, self.RUN, key, self._state(project, f"{self.WORKSPACE}/sheet/index.html"), now=now)
            canonical = prepared["visual_handoff"]["path"]
            raw = (project / canonical).read_bytes()
            review = discovery.render_review(project, canonical, key)

            (project / self.WORKSPACE / "sub").mkdir()
            (project / f".brandkit/work/{self.RUN}/discovery").is_dir()
            elsewhere = {
                "a sibling name in the same workspace": f"{self.WORKSPACE}/copy.json",
                "one directory deeper": f"{self.WORKSPACE}/sub/handoff.json",
                "the run root": f".brandkit/work/{self.RUN}/handoff.json",
                "outside .brandkit entirely": "handoff.json",
                # And the spelling half of the same question: on the file
                # systems this ships on these two name the canonical file, and
                # the gate refuses them anyway - it compares components against
                # a literal, which is the fail-closed direction of register
                # point 26 rather than a second lexical bug.
                "the canonical file, spelled differently": f".brandkit/work/{self.RUN}/visual-decisions/HANDOFF.JSON",
                "a directory component spelled differently": f".brandkit/Work/{self.RUN}/visual-decisions/handoff.json",
                "the discovery workspace of this run": f".brandkit/work/{self.RUN}/discovery/visual-handoff.json",
            }
            for label, path in elsewhere.items():
                with self.subTest(path=label):
                    target = project / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                    for reader in ("snapshot", "render", "receipt", "verify"):
                        with self.assertRaisesRegex(discovery.DiscoveryError,
                                                    "handoff path is not the canonical run-scoped path"):
                            if reader == "snapshot":
                                discovery.handoff_snapshot(project, path, key)
                            elif reader == "render":
                                discovery.render_review(project, path, key)
                            elif reader == "receipt":
                                discovery.make_owner_receipt(project, path, review["path"], key, "owner@example",
                                                             "approval-selection-001",
                                                             "Diese Entscheidungen bestätige ich.", 600, now=now)
                            else:
                                discovery.verify_owner_receipt(project, path, "x.json", key, now=now)
                    # A refused read renders nothing: the only filled review
                    # in the project stays the one the accepted route wrote.
                    self.assertEqual([Path(review["path"])],
                                     [item.relative_to(project) for item in sorted(project.rglob("filled-review.md"))])

            # The canonical path still reads, so the gate refuses the path and
            # not the document.
            self.assertEqual(review["sha256"], discovery.render_review(project, canonical, key)["sha256"])
            self._confirm_and_consume(project, key, canonical, "selection-001", now)

    def test_a_write_target_that_is_not_a_regular_file_is_refused_before_the_commit(self) -> None:
        """K28.  ``_safe_write_target`` decides what the path already holds.

        Every path this operation intends to write is put to the write's own
        four refusals above the commit, and one of them asks what is already
        there: a symlink or a directory at the target is refused rather than
        followed or written through.  The rule is reachable by the owner - the
        run workspace is inside the project (register point 19) - and no test
        asked about it, so the core could have gone on following a link out of
        the workspace.

        The last case is the limitation the rule carries rather than a defence
        it offers, and it is asserted here so it cannot change unnoticed:
        ``exists()`` follows the link, so a **dangling** symlink at the target
        is not seen at all and is replaced.  The effect is contained - the write
        opens ``O_EXCL|O_NOFOLLOW`` on a fresh temporary name and ``os.replace``
        puts the file where the link stood, inside the workspace - but the rule
        is weaker than its wording (register point 45).
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            secrets = Path(temporary) / "secrets"; secrets.mkdir()
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            fixture = five_domain_affinity_bundle(project, workspace, secrets, now, run_id=self.RUN)
            self._owner_inputs(project)
            visual_decisions.render_sheet(project, self.RUN, now, affinity_auth=fixture["auth"])
            review_artifact = f"{self.WORKSPACE}/sheet/index.html"
            state = self._state(project, review_artifact)
            before = fixture["ledger_path"].read_bytes()
            handoff = project / self.WORKSPACE / "handoff.json"
            filled = project / self.WORKSPACE / "filled-review.md"

            for label, target, make in (
                    ("a symlink to a file inside the workspace", handoff,
                     lambda path: path.symlink_to(project / self.WORKSPACE / "candidate-set.json")),
                    ("a symlink out of the project", handoff,
                     lambda path: path.symlink_to(Path(temporary) / "owner.key")),
                    ("a directory", filled, lambda path: path.mkdir()),
            ):
                with self.subTest(target=label):
                    make(target)
                    with self.assertRaisesRegex(discovery.DiscoveryError, "write target is not a regular file"):
                        discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now,
                                                              affinity_auth=fixture["auth"])
                    # Refused above the commit: nothing spent, nothing written,
                    # and what was there is still exactly what was there.
                    self.assertEqual(before, fixture["ledger_path"].read_bytes())
                    self.assertTrue(target.is_symlink() or target.is_dir())
                    target.rmdir() if target.is_dir() else target.unlink()
                    self.assertFalse(handoff.exists() or filled.exists())

            # The limitation, pinned: a dangling link is invisible to
            # ``exists()`` and is replaced by the artifact.
            dangling = Path(temporary) / "nowhere"
            handoff.symlink_to(dangling)
            prepared = discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now,
                                                             affinity_auth=fixture["auth"])
            self.assertFalse(dangling.exists(), "the write must not follow the link out of the workspace")
            self.assertFalse(handoff.is_symlink())
            self.assertTrue(handoff.is_file())
            self.assertEqual(prepared["visual_handoff"]["sha256"],
                             hashlib.sha256(handoff.read_bytes()).hexdigest())

    def test_a_filled_review_edited_after_the_fact_is_never_receipted(self) -> None:
        """K29.  The byte-bound review, measured where it is bound.

        The accepted flow promises that what the owner confirms is the review
        he was shown, byte for byte: ``verified_review`` re-renders the review
        from the current handoff and compares it with the file on disk, and
        ``make_owner_receipt`` and ``verify_owner_receipt`` both go through it.
        Removing that comparison left the whole suite green - the run workspace
        is inside the project, so appending one line to ``filled-review.md`` and
        then asking for a receipt is an owner-reachable move, and the receipt
        would have been issued over a review nobody rendered.

        Three edits, because the file can be moved in three ways that all keep
        it a plausible review: something appended, something removed, and one
        decision's rendered value rewritten in place.  Afterwards the file is
        rendered again and the receipt goes through, so the refusals are
        attributable to the edits and not to the file.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            five_domain_bundle(project, workspace, now, run_id=self.RUN)
            self._owner_inputs(project)
            visual_decisions.render_sheet(project, self.RUN, now)
            prepared = discovery.prepare_visual_confirmation(
                project, self.RUN, key, self._state(project, f"{self.WORKSPACE}/sheet/index.html"), now=now)
            handoff_path = prepared["visual_handoff"]["path"]
            review = discovery.render_review(project, handoff_path, key)
            honest = (project / review["path"]).read_bytes()
            self.assertEqual(prepared["filled_review"]["sha256"], hashlib.sha256(honest).hexdigest())

            # The receipt over the untouched review is issued and verifies.
            consumed = self._confirm_and_consume(project, key, handoff_path, "selection-001", now)
            self.assertEqual(5, len(consumed["decisions"]))
            receipt_path = ".brandkit/owner-receipts/ci-pilot/visual-selection-001.json"

            edits = {
                "a line appended": honest + b"\nUnd das hier habe ich noch dazugeschrieben.\n",
                "a line removed": b"\n".join(honest.split(b"\n")[:-4]) + b"\n",
                "one rendered value rewritten": honest.replace(b"color-choice", b"logo-choice", 1),
            }
            for label, raw in edits.items():
                with self.subTest(edit=label):
                    self.assertNotEqual(honest, raw)
                    (project / review["path"]).write_bytes(raw)
                    with self.assertRaisesRegex(discovery.DiscoveryError,
                                                "review differs from current verified handoff"):
                        discovery.make_owner_receipt(project, handoff_path, review["path"], key, "owner@example",
                                                     "approval-selection-001",
                                                     "Diese Entscheidungen bestätige ich.", 600, now=now)
                    # The receipt already issued over the honest review no
                    # longer verifies either, so the edit cannot be smuggled
                    # past the confirmation that was granted before it.
                    with self.assertRaisesRegex(discovery.DiscoveryError,
                                                "review differs from current verified handoff"):
                        discovery.verify_owner_receipt(project, handoff_path, receipt_path, key, now=now + 1)

            # Rendered again, the same handoff receipts exactly as before.
            rendered = discovery.render_review(project, handoff_path, key)
            self.assertEqual(honest, (project / review["path"]).read_bytes())
            self.assertEqual(review["sha256"], rendered["sha256"])
            self.assertEqual(consumed["decisions"],
                             discovery.verify_owner_receipt(project, handoff_path, receipt_path, key,
                                                            now=now + 1)["decisions"])

    def test_the_owner_selection_statement_must_be_non_empty_canonical_lf(self) -> None:
        """The one owner-authored file of the whole operation.

        The ``chat_statement`` is the only member of the handoff the owner
        writes himself, and the handoff records it as the ``owner-supplied``
        source of every decision.  An empty file, one of nothing but whitespace,
        or one carrying CRLF would be recorded as that source all the same - a
        decision attributed to an owner statement that says nothing, or whose
        pinned bytes do not survive a checkout on another platform.  Every case
        is refused, and refused before anything is spent, which is why the
        ledger is asserted alongside.
        """
        cases = {"empty": b"", "whitespace only": b"   \n\t\n", "CRLF line endings": b"Ich waehle diese.\r\n"}
        for label, raw in cases.items():
            with self.subTest(statement=label), tempfile.TemporaryDirectory() as temporary:
                project, key = self._project(Path(temporary))
                secrets = Path(temporary) / "secrets"; secrets.mkdir()
                workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
                now = int(time.time())
                fixture = five_domain_affinity_bundle(project, workspace, secrets, now, run_id=self.RUN)
                self._owner_inputs(project)
                (project / self.WORKSPACE / "owner-selection.txt").write_bytes(raw)
                visual_decisions.render_sheet(project, self.RUN, now, affinity_auth=fixture["auth"])
                before = fixture["ledger_path"].read_bytes()

                state = self._state(project, f"{self.WORKSPACE}/sheet/index.html")
                with self.assertRaisesRegex(discovery.DiscoveryError,
                                            "owner selection statement must be non-empty canonical UTF-8/LF"):
                    discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now,
                                                          affinity_auth=fixture["auth"])
                self.assertEqual(before, fixture["ledger_path"].read_bytes())
                self.assertFalse((project / self.WORKSPACE / "handoff.json").exists())

    def test_a_tampered_decision_value_in_the_handoff_is_refused_by_every_reader(self) -> None:
        """The guard the whole confirm half of the flow rests on.

        ``handoff_snapshot`` is what ``render_review``, ``make_owner_receipt``
        and ``verify_owner_receipt`` share to decide that the handoff has not
        moved between ``prepare`` and ``confirm``.  The decision it protects is a
        pair - a value, and the digest of that value - so a handoff whose
        ``candidate_value`` was rewritten while its ``candidate_value_sha256``
        was left alone is exactly the tamper that pair exists to catch: the
        owner would confirm, byte for byte, a value nobody validated.

        Measured on all three readers, because a guard only one of them consults
        is not the guard this flow is documented to have.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            five_domain_bundle(project, workspace, now, run_id=self.RUN)
            self._owner_inputs(project)
            visual_decisions.render_sheet(project, self.RUN, now)
            review_artifact = f"{self.WORKSPACE}/sheet/index.html"
            prepared = discovery.prepare_visual_confirmation(
                project, self.RUN, key, self._state(project, review_artifact), now=now)
            handoff_path = prepared["visual_handoff"]["path"]

            # The honest handoff is readable by all three.
            review = discovery.render_review(project, handoff_path, key)
            self.assertTrue((project / review["path"]).is_file())

            document = json.loads((project / handoff_path).read_text(encoding="utf-8"))
            decision = next(item for item in document["decisions"] if item["domain"] == "color")
            pinned = decision["candidate_value_sha256"]
            decision["candidate_value"] = {**decision["candidate_value"], "roles": {"text": "#000000"}}
            self.assertEqual(pinned, decision["candidate_value_sha256"])
            (project / handoff_path).write_bytes(discovery.canonical(document))

            for reader in ("handoff_snapshot", "render_review", "make_owner_receipt"):
                with self.subTest(reader=reader), self.assertRaisesRegex(
                        discovery.DiscoveryError, "visual handoff decision is invalid"):
                    if reader == "handoff_snapshot":
                        discovery.handoff_snapshot(project, handoff_path, key)
                    elif reader == "render_review":
                        discovery.render_review(project, handoff_path, key)
                    else:
                        discovery.make_owner_receipt(project, handoff_path, review["path"], key, "owner@example",
                                                     "approval-tamper", "Diese Entscheidungen bestätige ich.",
                                                     600, now=now)

    def test_every_rule_the_handoff_reader_states_is_reachable_by_tampering(self) -> None:
        """The whole visual branch of ``handoff_snapshot``, one case per disjunct.

        My own mutation run found this as a *class* rather than as single
        misses.  ``prepare_visual_confirmation`` builds the handoff itself, so
        it can never produce a document that breaks these rules - which is why
        removing any one of them left the suite green.  But ``handoff_snapshot``
        is not only reached from ``prepare``: ``render_review``,
        ``make_owner_receipt`` and ``verify_owner_receipt`` read the handoff
        back **from disk**, after the operation has ended, and the file lives in
        the run workspace inside the project (register point 19).  Every rule
        below is therefore reachable by exactly the party the rule exists
        against, and each one is measured here on a handoff the real core and
        the real adapter produced.

        The test above measures the same thing for one field on all three
        readers; this one measures every remaining rule of the branch.

        Until r1h it measured every *message* once, which is not the same
        thing: each of these rules is one ``if`` with three to six disjuncts,
        and one tampering per message leaves the other disjuncts unmeasured -
        the r1g critic removed nine of them one at a time and the suite stayed
        green.  The table below therefore carries one case per **disjunct**:
        four for the closed-V2 rule, three for the decision rule (its fourth,
        the value/digest pair, is the neighbouring test), two for the outcome
        rule, three for the role counts, three for the source records.  Two
        further cases stand on the reader the rules resolve members through:
        the inventory's duplicate rule compares raw path strings, so a second
        *spelling* of a member already listed is not a duplicate to it, and
        what refuses it is the path grammar one layer down.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            five_domain_bundle(project, workspace, now, run_id=self.RUN)
            self._owner_inputs(project)
            visual_decisions.render_sheet(project, self.RUN, now)
            prepared = discovery.prepare_visual_confirmation(
                project, self.RUN, key, self._state(project, f"{self.WORKSPACE}/sheet/index.html"), now=now)
            handoff_path = prepared["visual_handoff"]["path"]
            honest = json.loads((project / handoff_path).read_text(encoding="utf-8"))
            # The untampered document is accepted, so every refusal below is
            # attributable to the one edit that produced it.
            self.assertEqual(5, len(discovery.handoff_snapshot(project, handoff_path, key)["document"]["decisions"]))

            def dropped(document: dict, role: str) -> list:
                return [item for item in document["files"] if item["role"] != role]

            def recast(document: dict, was: str, becomes: str, count: int | None = None) -> None:
                """Give ``count`` members carrying ``was`` the role ``becomes``.

                The role is moved rather than a member added, because a second
                member would need a second path and would be answered by the
                duplicate-path rule or by the reader before the role counts are
                ever reached.
                """
                moved = 0
                for item in document["files"]:
                    if item["role"] == was and (count is None or moved < count):
                        item["role"] = becomes; moved += 1
                assert moved, f"no member carried {was}"

            def tampering(name):
                document = json.loads(json.dumps(honest))
                {
                    "an unknown top-level field": lambda: document.update(extra=1),
                    "an outcome word outside the vocabulary": lambda: document["outcomes"].append(
                        {"domain": "color", "outcome": "candidate"}),
                    "the same domain decided twice": lambda: document["outcomes"].append(
                        {"domain": document["decisions"][0]["domain"], "outcome": "open"}),
                    "a recommendation that is not a closed record": lambda: document["recommendations"].append(
                        {"kind": "color", "candidate_id": "color-choice"}),
                    "a source record that is not owner/observed": lambda: document["sources"].append(
                        {"id": "smuggled", "classification": "trusted", "file": document["sources"][0]["file"]}),
                    "two inventory members on one path": lambda: document["files"].append(
                        dict(document["files"][0])),
                    "an inventory member with an extra field": lambda: document["files"][0].update(note="x"),
                    "no candidate-bundle member at all": lambda: document.update(
                        files=dropped(document, "candidate-bundle")),
                    "no evidence member at all": lambda: document.update(files=dropped(document, "evidence")),
                    "a top-level hash the inventory contradicts": lambda: document.update(
                        candidate_review_sha256="0" * 64),
                    "a source naming a file the inventory never lists": lambda: document["sources"][0].update(
                        file=f"{self.WORKSPACE}/not-in-inventory.txt"),
                    "two sources under one id": lambda: document["sources"].append(dict(document["sources"][0])),
                    "a decision citing a source that does not exist": lambda: document["decisions"][0].update(
                        source_ids=["owner-visual-selection", "ghost"]),
                    # --- r1h: the disjuncts of the rules above, one each ---
                    "a schema version from another handoff generation": lambda: document.update(
                        schema_version="brandkit-discovery-handoff.v1"),
                    "the handoff kind of the other branch": lambda: document.update(handoff_kind="discovery"),
                    "a run id other than the one in its own path": lambda: document.update(run_id="ci-pilot-b"),
                    "a handoff id outside its grammar": lambda: document.update(handoff_id="Visual-SELECTION-001"),
                    "a decision in a domain outside the vocabulary": lambda: document["decisions"][0].update(
                        domain="spacing"),
                    "a decision that cites no source at all": lambda: document["decisions"][0].update(source_ids=[]),
                    "an outcome in a domain outside the vocabulary": lambda: document["outcomes"].append(
                        {"domain": "spacing", "outcome": "open"}),
                    "two members carrying the candidate-bundle role": lambda: recast(
                        document, "source", "candidate-bundle", count=1),
                    "two members carrying the candidate-review role": lambda: recast(
                        document, "source", "candidate-review", count=1),
                    "no member carrying the source role": lambda: recast(document, "source", "evidence"),
                    "a first source that is not the owner's": lambda: document["sources"][0].update(id="not-the-owner"),
                    "a first source that is not owner-supplied": lambda: document["sources"][0].update(
                        classification="observed"),
                    "no sources at all": lambda: document.update(sources=[]),
                    "a second spelling of a member already listed": lambda: document["files"].append(
                        {**document["files"][0], "path": f"{self.WORKSPACE}/./candidate-set.json"}),
                    # r1i folded this rule and nothing measured the fold: the
                    # two cases above differ as raw strings too, so a rule that
                    # compares raw strings passes them both.  These two differ
                    # *only* in spelling, which is what the fold is for - on the
                    # file systems this ships on they are one file, and the
                    # closure the rule asserts is about files.
                    "a member listed again with its name case-flipped": lambda: document["files"].append(
                        {**document["files"][0], "path": f"{self.WORKSPACE}/CANDIDATE-SET.JSON"}),
                    "a member listed again with its directory case-flipped": lambda: document["files"].append(
                        {**document["files"][0], "path": f"{self.WORKSPACE.upper()}/candidate-set.json"}),
                    "a member reached through its own parent": lambda: document["files"].append(
                        {**document["files"][0], "path": f"{self.WORKSPACE}/../visual-decisions/candidate-set.json"}),
                }[name]()
                return document

            cases = {
                "an unknown top-level field": "visual handoff is not closed V2 or differs from its canonical path",
                "an outcome word outside the vocabulary": "visual handoff outcome is invalid",
                "the same domain decided twice": "visual handoff contains duplicate domains",
                "a recommendation that is not a closed record": "visual handoff recommendations are not closed",
                "a source record that is not owner/observed": "visual handoff sources are not closed owner/observed records",
                "two inventory members on one path": "visual handoff inventory is not closed",
                "a member listed again with its name case-flipped": "visual handoff inventory is not closed",
                "a member listed again with its directory case-flipped": "visual handoff inventory is not closed",
                "an inventory member with an extra field": "visual handoff inventory item is invalid",
                "no candidate-bundle member at all": "visual handoff requires bundle, review, owner/candidate sources, and evidence",
                "no evidence member at all": "visual handoff requires bundle, review, owner/candidate sources, and evidence",
                "a top-level hash the inventory contradicts": "visual handoff top-level hashes differ from inventory",
                "a source naming a file the inventory never lists": "visual handoff source differs from inventory",
                "two sources under one id": "visual handoff source ids are not unique",
                "a decision citing a source that does not exist": "visual decision lacks a bound source",
                # --- r1h: one case per *disjunct*, not per message ---
                "a schema version from another handoff generation": "visual handoff is not closed V2 or differs from its canonical path",
                "the handoff kind of the other branch": "visual handoff is not closed V2 or differs from its canonical path",
                "a run id other than the one in its own path": "visual handoff is not closed V2 or differs from its canonical path",
                "a handoff id outside its grammar": "visual handoff is not closed V2 or differs from its canonical path",
                "a decision in a domain outside the vocabulary": "visual handoff decision is invalid",
                "a decision that cites no source at all": "visual handoff decision is invalid",
                "an outcome in a domain outside the vocabulary": "visual handoff outcome is invalid",
                "two members carrying the candidate-bundle role": "visual handoff requires bundle, review, owner/candidate sources, and evidence",
                "two members carrying the candidate-review role": "visual handoff requires bundle, review, owner/candidate sources, and evidence",
                "no member carrying the source role": "visual handoff requires bundle, review, owner/candidate sources, and evidence",
                "a first source that is not the owner's": "visual handoff sources are not closed owner/observed records",
                "a first source that is not owner-supplied": "visual handoff sources are not closed owner/observed records",
                "no sources at all": "visual handoff sources are not closed owner/observed records",
                "a second spelling of a member already listed": f"unsafe relative path: {self.WORKSPACE}/./candidate-set.json",
                "a member reached through its own parent": f"unsafe relative path: {self.WORKSPACE}/../visual-decisions/candidate-set.json",
            }
            for label, message in cases.items():
                with self.subTest(tamper=label):
                    (project / handoff_path).write_bytes(discovery.canonical(tampering(label)))
                    with self.assertRaisesRegex(discovery.DiscoveryError, re.escape(message)):
                        discovery.handoff_snapshot(project, handoff_path, key)

            # Restored, the same file is read again without complaint - so the
            # refusals above came from the edits and not from the round trip.
            (project / handoff_path).write_bytes(discovery.canonical(honest))
            self.assertEqual(prepared["visual_handoff"]["sha256"],
                             discovery.handoff_snapshot(project, handoff_path, key)["handoff_sha256"])

    def test_the_operation_verifies_before_it_spends_and_writes_only_after(self) -> None:
        """The ordering itself, read off the shipped source.

        The tests above measure six mistakes; this one measures the *rule* they
        are instances of, so a refusal added later cannot quietly land on the
        wrong side of the commit.  Every ``DiscoveryError`` this operation
        raises has to sit above the one consuming call, and every write has to
        sit below it.  The single permitted exception is named rather than
        counted away: the guard that refuses a bundle which changed *between*
        the verification and the commit, which no owner input can reach and
        which exists precisely because the two passes must agree.

        Until r1g this test read the syntax tree of **one function** and said in
        this docstring that it stopped a thirteenth refusal from landing on the
        wrong side.  It could not: the thirteenth refusal was a call level down,
        in ``handoff_snapshot``, and a test that walks one function body cannot
        see it.  The second half below is therefore not a stricter version of
        the first, it is the half that was missing - it walks the operation's
        transitive callees and compares the set of refusal *messages* reachable
        after the commit with the set reachable before it.  Messages rather than
        function names, because that is the granularity at which the bug was
        invisible: ``handoff_snapshot`` was called only after the commit both
        before and after the fix, so a name-level test stays green either way,
        while ``visual handoff inventory is not closed`` moved from
        "after only" to "before and after" exactly when the bug was fixed.
        """
        source = inspect.getsource(discovery.prepare_visual_confirmation)
        function = ast.parse(source).body[0]
        refusals = [(node.lineno, node.exc.args[0].value)
                    for node in ast.walk(function)
                    if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                    and getattr(node.exc.func, "id", "") == "DiscoveryError"
                    and node.exc.args and isinstance(node.exc.args[0], ast.Constant)]
        validations = [(node.lineno, next((bool(keyword.value.value) for keyword in node.keywords
                                           if keyword.arg == "commit"), True))
                       for node in ast.walk(function)
                       if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_validated_visual_bundle"]
        writes = [node.lineno for node in ast.walk(function)
                  if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_safe_write"]

        verifications = [line for line, commit in validations if not commit]
        commits = [line for line, commit in validations if commit]
        self.assertEqual(1, len(verifications), "exactly one verifying pass")
        self.assertEqual(1, len(commits), "exactly one consuming pass, or the receipts have two spenders")
        self.assertLess(verifications[0], commits[0], "the verification has to come first")
        self.assertTrue(writes, "the operation writes something")
        self.assertTrue(all(commits[0] < line for line in writes),
                        "no artifact may be written before the operation has paid for itself")
        late = [message for line, message in refusals if line > commits[0]]
        self.assertEqual(["candidate bundle changed between verification and commit"], late)
        # And the twelve rules the R1E critic listed are all in the window
        # between the two passes, where they can refuse for free - thirteen
        # since r1h, which added the seam rule of the fourth stage below to the
        # same window rather than to a new one.
        between = [message for line, message in refusals if verifications[0] < line < commits[0]]
        self.assertEqual(13, len(between), between)
        self.assertIn("visual handoff inventory member lies on a path this operation overwrites", between)

        # ---- the half that was missing: the callees behind the commit ----
        # Re-parsed from the module rather than from ``inspect.getsource``,
        # because the callees are only reachable from the module's own tree -
        # and because line numbers have to be in *module* coordinates here.
        # The numbers above come from a snippet parsed on its own and start at
        # 1; mixing the two frames would put every callee on the same side of
        # the commit and quietly measure nothing.
        module = ast.parse(Path(discovery.__file__).read_text(encoding="utf-8"))
        functions = {node.name: node for node in module.body if isinstance(node, ast.FunctionDef)}
        operation = functions["prepare_visual_confirmation"]
        module_commit = [node.lineno for node in ast.walk(operation)
                         if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_validated_visual_bundle"
                         and next((bool(keyword.value.value) for keyword in node.keywords
                                   if keyword.arg == "commit"), True)]
        self.assertEqual(1, len(module_commit), "exactly one consuming pass in module coordinates")

        def messages(node: ast.AST) -> set[str]:
            return {item.exc.args[0].value for item in ast.walk(node)
                    if isinstance(item, ast.Raise) and isinstance(item.exc, ast.Call)
                    and getattr(item.exc.func, "id", "") == "DiscoveryError"
                    and item.exc.args and isinstance(item.exc.args[0], ast.Constant)}

        def reachable(side) -> set[str]:
            """Every refusal message reachable from the calls on one side."""
            pending = [item.func.id for item in ast.walk(functions["prepare_visual_confirmation"])
                       if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and side(item.lineno)]
            seen, found = set(), set()
            while pending:
                name = pending.pop()
                if name in seen or name not in functions:
                    continue
                seen.add(name); found |= messages(functions[name])
                pending += [item.func.id for item in ast.walk(functions[name])
                            if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)]
            return found

        commit_line = module_commit[0]
        body = {node.lineno: node.exc.args[0].value for node in ast.walk(operation)
                if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                and getattr(node.exc.func, "id", "") == "DiscoveryError"
                and node.exc.args and isinstance(node.exc.args[0], ast.Constant)}
        before = reachable(lambda line: line < commit_line) | {m for line, m in body.items() if line < commit_line}
        after = reachable(lambda line: line > commit_line) | {m for line, m in body.items() if line > commit_line}
        # The two frames agree about the body, which is what makes it safe to
        # carry the snippet-based finding above into this module-based half.
        self.assertEqual(set(late), {m for line, m in body.items() if line > commit_line})

        # Everything still reachable only *after* the commit, each named with
        # the reason it cannot be hoisted.  Nothing here is owner input.
        unhoistable = {
            # The named exception: it compares the two passes, so by
            # construction it cannot be decided before the second one runs.
            "candidate bundle changed between verification and commit",
            # ``handoff_snapshot`` serves two handoff kinds.  This operation
            # always hands it ``.brandkit/work/<run>/visual-decisions/handoff.json``,
            # built here from an already validated run id, so its path gate and
            # its whole *discovery* branch are unreachable on this path.
            "handoff path is not the canonical run-scoped path",
            "handoff identity differs from its canonical path",
            "discovery file inventory is not closed",
            "discovery inventory requires one context, an ADR, and a source",
            "duplicate discovery source id",
            "discovery source is not bound to a source file",
            "discovery decision lacks a bound source",
            "recommendation and decision ids overlap",
            # ``read_progress`` re-read.  This operation decided the identical
            # predicate at its entry, above the verification, before anything
            # was spent; only a concurrent rewrite of the progress chain can
            # flip it, which is not an input to this call.
            "interview frontier is incomplete; remain in grilling",
        }
        self.assertEqual(unhoistable, after - before, sorted(after - before))

        # The comparison above reads *constant* messages, and four refusals of
        # the discovery branch build theirs with an f-string, so they are
        # invisible to it.  A dynamic refusal added behind the commit would
        # therefore slip through the very check that exists to catch it, which
        # is why the same question is asked once more by function identity:
        # which DiscoveryError-raising function is reachable only after the
        # commit at all?  Exactly three, and all three are reachable only
        # through the discovery branch this operation never takes.
        def functions_reaching(side) -> set[str]:
            pending = [item.func.id for item in ast.walk(operation)
                       if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and side(item.lineno)]
            seen = set()
            while pending:
                name = pending.pop()
                if name in seen or name not in functions:
                    continue
                seen.add(name)
                pending += [item.func.id for item in ast.walk(functions[name])
                            if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)]
            return seen

        def raises(node: ast.AST) -> list[ast.Raise]:
            return [item for item in ast.walk(node)
                    if isinstance(item, ast.Raise) and isinstance(item.exc, ast.Call)
                    and getattr(item.exc.func, "id", "") == "DiscoveryError"]

        raising = {name for name, node in functions.items() if raises(node)}
        only_after = (functions_reaching(lambda line: line > commit_line)
                      - functions_reaching(lambda line: line < commit_line)) & raising
        self.assertEqual({"handoff_snapshot", "_workspace_paths", "meaningful_markdown"},
                         only_after, sorted(only_after))

        # Two counting rules, because both halves above read message *values*
        # and a refusal whose message is built with an f-string has no value to
        # read.  Without these, a dynamic refusal added behind the commit -
        # in this body, or inside the one callee that is allowed to sit there -
        # passes every check on this page, which is the exact blindness that
        # let the thirteenth refusal ship.
        self.assertEqual(len([m for line, m in body.items() if line > commit_line]),
                         len([node for node in raises(operation) if node.lineno > commit_line]),
                         "a refusal behind the commit builds its message dynamically and escapes the comparison")
        # ``handoff_snapshot`` may stand behind the commit only because its
        # visual-decisions rules all live in ``_visual_handoff_rules``, which is
        # rehearsed above.  What is left in it is the path gate, the progress
        # re-read and the discovery branch: nine constant messages and the four
        # f-string ones of that branch.  A rule added back into it would land
        # behind the commit unrehearsed, and changes this count.
        self.assertEqual(13, len(raises(functions["handoff_snapshot"])),
                         "a refusal was added to handoff_snapshot: it would sit behind the commit unrehearsed")
        self.assertEqual(9, len(messages(functions["handoff_snapshot"])))

        # The rule that was broken, stated positively: every rule the finished
        # handoff answers is reachable *before* the commit as well.  This is the
        # assertion the r1f critic's counterexample fails.
        for rule in messages(functions["_visual_handoff_rules"]):
            self.assertIn(rule, before, f"{rule!r} can only refuse after the receipts are spent")

        # ---------------- the fourth stage: the reader seam ----------------
        # Every stage above compares what is reachable after the commit with
        # what is reachable before it, so anything reachable on *both* sides
        # cancels out of the difference before the assertion sees it.  That is
        # not a rare shape: of the raise sites reachable behind the commit, the
        # large majority sit in functions the operation also calls above it -
        # ``secure_read``, ``load_json_bytes``, ``_safe_write_target``,
        # ``read_progress``, the thirteen of ``_visual_handoff_rules``, and
        # ``_bound_file``.  The r1g critic's counterexample was one of them:
        # ``_bound_file`` refuses on both sides, and behind the commit it
        # refused only because the operation had rewritten the disk in between.
        # A stage that subtracts the two sides can never see that, so this one
        # measures the difference itself - the state the two readers read.
        #
        # First that the seam is exactly two-sided and stayed where it was.
        seam = [node for node in ast.walk(module)
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_visual_handoff_rules"]
        self.assertEqual(2, len(seam), "the rules are decided twice, once per reader, or the seam moved")
        readers = sorted(ast.unparse(node.args[2]).split("(")[0] for node in seam)
        self.assertEqual(["_staged_member_reader", "lambda binding: _bound_file"], readers, readers)
        rehearsal = next(node for node in seam if ast.unparse(node.args[2]).startswith("_staged_member_reader"))
        self.assertLess(rehearsal.lineno, commit_line, "the rehearsal has to be decided before anything is spent")
        self.assertIn("_bound_file", ast.unparse(next(node for node in seam if node is not rehearsal).args[2]))

        # Second, the seam is a real difference and not a formality: a binding
        # the staged reader accepts is refused by the disk reader as soon as the
        # bytes under the path change.  This is the whole mechanism of the
        # fourteenth refusal, stated in two lines and with nothing spent.
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            five_domain_bundle(project, workspace, now, run_id=self.RUN)
            self._owner_inputs(project)
            staged = b"the bytes the operation staged\n"
            binding = {"path": f"{self.WORKSPACE}/seam.txt", "size": len(staged),
                       "sha256": hashlib.sha256(staged).hexdigest()}
            (project / self.WORKSPACE / "seam.txt").write_bytes(staged)
            read = discovery._staged_member_reader({binding["path"]: staged}, self.WORKSPACE + "/")
            self.assertEqual(staged, read(binding))
            self.assertEqual(staged, discovery._bound_file(project, binding, "visual handoff file",
                                                           prefix=self.WORKSPACE + "/"))
            (project / self.WORKSPACE / "seam.txt").write_bytes(b"the bytes that landed instead\n")
            self.assertEqual(staged, read(binding), "the staged reader answers about the bytes, not the disk")
            with self.assertRaisesRegex(discovery.DiscoveryError, "differs from its descriptor"):
                discovery._bound_file(project, binding, "visual handoff file", prefix=self.WORKSPACE + "/")

            # Third, the set the difference is decided over: every path this
            # operation writes, measured by diffing the project rather than by
            # reading the source, against the inventory of the handoff it
            # produced.  The intersection may hold only members whose bytes on
            # disk are the bytes their descriptor pins - which is the payloads,
            # by construction - and the two paths that are written and are no
            # members at all have to be named here, because they are the ones
            # no inventory rule can see.
            visual_decisions.render_sheet(project, self.RUN, now)
            (project / self.WORKSPACE / "seam.txt").unlink()
            state = self._state(project, f"{self.WORKSPACE}/sheet/index.html")

            # The diff runs over the area ``_safe_write_target`` actually
            # permits, which is every path below the project - not over
            # ``.brandkit``.  Until r1i it walked ``.brandkit`` only, and a
            # ``_safe_write`` added behind the commit onto a project path
            # outside it was invisible to this stage and, measured against the
            # whole integration module, to every other test as well.  ``.git``
            # is excluded because it is not this operation's business and no
            # write funnel of this module can reach it.
            def tree() -> dict[str, bytes]:
                return {str(path.relative_to(project)): path.read_bytes()
                        for path in sorted(project.rglob("*"))
                        if path.is_file() and not path.is_symlink()
                        and ".git" not in path.relative_to(project).parts}

            was = tree()
            prepared = discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now)
            now_tree = tree()
            wrote = {path for path, raw in now_tree.items() if was.get(path) != raw}
            document = json.loads((project / prepared["visual_handoff"]["path"]).read_text(encoding="utf-8"))
            inventory = {item["path"]: item for item in document["files"]}
            handoff_path, filled_path = prepared["visual_handoff"]["path"], prepared["filled_review"]["path"]
            self.assertEqual({handoff_path, filled_path}, wrote - set(inventory),
                             "a path is written that is neither an inventory member nor one of the two artifacts")
            self.assertTrue({handoff_path, filled_path}.isdisjoint(inventory),
                            "an inventory member sits on a path the operation overwrites")
            for path in sorted(wrote & set(inventory)):
                self.assertEqual(inventory[path]["sha256"], hashlib.sha256(now_tree[path]).hexdigest(),
                                 f"{path} is written with other bytes than its descriptor pins")

        # Fourth, the rule that keeps all of that true for inputs other than
        # this one.  It has to be *above* the commit, and it therefore has to
        # be in the pre-commit message set rather than in the difference.
        overwrite = "visual handoff inventory member lies on a path this operation overwrites"
        self.assertIn(overwrite, before, "the seam rule can only refuse after the receipts are spent")
        self.assertNotIn(overwrite, after - before)
        self.assertEqual([overwrite], [message for line, message in body.items() if line < commit_line
                                       and message == overwrite],
                         "the seam rule is stated once, in the operation's own body")

    def test_the_selection_grammar_is_closed_against_the_real_core(self) -> None:
        """Every refusal class of the selection, measured on a bundle the core accepts."""
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            five_domain_bundle(project, workspace, now, run_id=self.RUN)
            self._owner_inputs(project)
            review_sheet = self._sheet(project, now)
            review = review_sheet
            base = self._state(project, review)

            def broken(**changes) -> dict:
                state = json.loads(json.dumps(base)); state["owner_selection"].update(changes); return state

            complete = base["owner_selection"]["choices"]
            cases = {
                # cover each open domain exactly once - neither less nor twice
                "must cover each open domain exactly once": [
                    broken(choices=complete[:-1]),
                    broken(choices=complete[:-1] + [dict(complete[0])]),
                    broken(choices=complete + [dict(complete[0])]),
                ],
                # the outcome vocabulary is three literals, not any string
                "visual choice is invalid": [
                    broken(choices=[{**item, "outcome": "confirmed"} if item["domain"] == "color" else item for item in complete]),
                    broken(choices=[{**item, "outcome": "pass"} if item["domain"] == "color" else item for item in complete]),
                ],
                # no caller-named value, and no approval field either
                "caller-supplied value or extra fields": [
                    broken(choices=[{**item, "candidate_value": {"roles": {}}} if item["domain"] == "color" else item for item in complete]),
                    broken(choices=[{**item, "approved": True} if item["domain"] == "color" else item for item in complete]),
                    broken(choices=[{"domain": item["domain"], "outcome": "open", "candidate_id": "color-choice"} if item["domain"] == "color" else item for item in complete]),
                ],
                # a candidate of another domain, and one the bundle does not carry
                "unknown, ineligible, or belongs to another domain": [
                    broken(choices=[{**item, "candidate_id": "logo-choice"} if item["domain"] == "color" else item for item in complete]),
                    broken(choices=[{**item, "candidate_id": "color-absent"} if item["domain"] == "color" else item for item in complete]),
                ],
            }
            for message, states in cases.items():
                for index, selection in enumerate(states):
                    with self.subTest(refusal=message, case=index):
                        with self.assertRaisesRegex(discovery.DiscoveryError, message):
                            discovery.prepare_visual_confirmation(project, self.RUN, key, selection, now=now)

            # The bound bundle must be the bundle the core validated, not merely
            # some file in the workspace whose descriptor happens to be honest.
            other = dict(base); other["candidate_bundle"] = self._binding(project, f"{self.WORKSPACE}/visual-brief.json")
            with self.assertRaisesRegex(discovery.DiscoveryError, "differs from the validated bundle"):
                discovery.prepare_visual_confirmation(project, self.RUN, key, other, now=now)

            # Every bound artifact is confined to this run's visual workspace.
            for field in ("candidate_bundle", "candidate_review"):
                outside = dict(base); outside[field] = self._binding(project, ".brandkit/work/ci-pilot/brand-raw.md")
                with self.subTest(confinement=field):
                    with self.assertRaisesRegex(discovery.DiscoveryError, "outside its canonical workspace"):
                        discovery.prepare_visual_confirmation(project, self.RUN, key, outside, now=now)

            # An incomplete frontier keeps the operation unavailable - and so
            # does a frontier that is empty after a single answer turn, which is
            # the second half of the same rule and the half a "< 2" that became
            # "< 1" would slip past.
            one_turn, one_key = self._project(Path(temporary) / "one-turn", turns=1)
            with self.assertRaisesRegex(discovery.DiscoveryError, "frontier is incomplete"):
                discovery.prepare_visual_confirmation(one_turn, self.RUN, one_key, base, now=now)
            discovery.write_progress(project, self.RUN, ["color"], [],
                                     [axis for axis in discovery.BRAND_AXES if axis != "color"], "3" * 64, key)
            with self.assertRaisesRegex(discovery.DiscoveryError, "frontier is incomplete"):
                discovery.prepare_visual_confirmation(project, self.RUN, key, base, now=now)

    # ------------------------------------------------------- productive CLI

    def test_the_productive_cli_carries_both_trust_inputs_and_confines_the_descriptor(self) -> None:
        """``prepare-visual-confirmation`` driven as the operator drives it.

        r1c declared the two flags and no test ever ran the subcommand, so an
        adapter that dropped ``--affinity-auth`` or ``--sidecar-root`` on the
        floor looked exactly like one that forwarded them.  Each leg below is
        chosen so that dropping the flag changes the outcome: the successful run
        is last, because it spends the mutation receipts.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            secrets = Path(temporary) / "secrets"; secrets.mkdir()
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            fixture = five_domain_affinity_bundle(project, workspace, secrets, now, run_id=self.RUN)
            self._owner_inputs(project)
            review_sheet = self._sheet(project, now, affinity_auth=fixture["auth"])
            state = self._state(project, review_sheet)
            request = f"{self.WORKSPACE}/request.json"
            (project / request).write_bytes(discovery.canonical(state))
            descriptor_path = Path(temporary) / "affinity-auth.json"
            descriptor_path.write_bytes(discovery.canonical(fixture["auth"]))
            inside = project / self.WORKSPACE / "affinity-auth.json"
            inside.write_bytes(descriptor_path.read_bytes())
            empty_sidecar = Path(temporary) / "empty-sidecar"; empty_sidecar.mkdir()

            def run(*extra: str) -> subprocess.CompletedProcess:
                return subprocess.run(
                    [sys.executable, str(ROOT / "scripts/brandkit_discovery.py"), "prepare-visual-confirmation",
                     "--project-root", str(project), "--run-id", self.RUN, "--owner-key", str(key),
                     "--state", request, *extra],
                    cwd=ROOT, capture_output=True, text=True)

            # 1. No descriptor: the core refuses the authored candidates, and the
            #    refusal arrives as a contract sentence rather than a traceback.
            without = run("--sidecar-root", str(visual_decisions.ROOT.parents[1]))
            self.assertEqual(1, without.returncode, without.stderr)
            self.assertIn("require external authentication", without.stderr)
            self.assertNotIn("Traceback", without.stderr)

            # 2. The descriptor names external secrets, so it may not be read
            #    from inside the project a writer of the run can reach.
            confined = run("--affinity-auth", str(inside))
            self.assertEqual(1, confined.returncode)
            self.assertIn("must remain outside the project", confined.stderr)

            # 3. --sidecar-root really reaches the core: a root without the
            #    narrative is refused instead of quietly defaulted past.
            wrong_sidecar = run("--affinity-auth", str(descriptor_path), "--sidecar-root", str(empty_sidecar))
            self.assertEqual(1, wrong_sidecar.returncode)
            self.assertIn("source contract failed", wrong_sidecar.stderr)

            # 4. Both flags, from outside: the whole operation completes.
            done = run("--affinity-auth", str(descriptor_path), "--sidecar-root", str(visual_decisions.ROOT.parents[1]))
            self.assertEqual(0, done.returncode, done.stderr)
            result = json.loads(done.stdout)
            self.assertEqual("visual-confirmation-prepared", result["state"])
            self.assertFalse(result["normative"])
            self.assertEqual(sorted(VISUAL_FIVE), sorted(item["domain"] for item in result["resolved_choices"]))

    # ------------------------------------------------------ the sealed run

    def test_no_discovery_entry_point_can_name_or_write_the_sealed_run(self) -> None:
        """``progress --run-id first-ci`` used to create and write the sealed tree.

        The core refuses that identifier; this module did not know the name at
        all, and ``progress`` is a *declared* public command.  Every gate is
        measured here, and never against the real sealed run: the project below
        is a throwaway that merely spells the same name.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            sealed = project / ".brandkit/work" / discovery.PROTECTED_RUN_ID
            self.assertEqual("first-ci", discovery.PROTECTED_RUN_ID)
            self.assertEqual(visual_decisions.PROTECTED_RUN_ID, discovery.PROTECTED_RUN_ID)

            with self.assertRaisesRegex(discovery.DiscoveryError, "run id is protected"):
                discovery.write_progress(project, discovery.PROTECTED_RUN_ID, list(discovery.BRAND_AXES), [], [], "1" * 64, key)
            self.assertFalse(sealed.exists(), "the refused run must not have created its directory")
            with self.assertRaisesRegex(discovery.DiscoveryError, "run id is protected"):
                discovery.initialize_progress(project, discovery.PROTECTED_RUN_ID, key)
            with self.assertRaisesRegex(discovery.DiscoveryError, "run id is protected"):
                discovery.read_progress(project, discovery.PROTECTED_RUN_ID, key)
            with self.assertRaisesRegex(discovery.DiscoveryError, "run id is protected"):
                discovery.handoff_snapshot(project, f".brandkit/work/{discovery.PROTECTED_RUN_ID}/discovery/handoff.json", key)
            with self.assertRaisesRegex(discovery.DiscoveryError, "run id is protected"):
                discovery.write_owner_receipt(project, ".brandkit/owner-receipts/first-ci.json",
                                              {"run_id": discovery.PROTECTED_RUN_ID})
            self.assertFalse(sealed.exists())

            # The name is folded the way a file system folds it, so a spelling
            # the run id grammar would never see is still refused at the write.
            for spelling in ("first-ci", "First-CI", "FIRST-CI", "\ufb01rst-ci", "first-c\u0131".replace("\u0131", "i")):
                with self.subTest(spelling=spelling):
                    self.assertTrue(discovery._names_protected_run((spelling,)))
                    with self.assertRaisesRegex(discovery.DiscoveryError, "protected first-ci run"):
                        discovery._safe_write(project, f".brandkit/work/{spelling}/note.md", b"x")
            self.assertFalse(discovery._names_protected_run(("first-ci-two", "firstci", "ci-pilot")))
            # A regular run is untouched by all of this.
            discovery._safe_write(project, ".brandkit/work/ci-pilot/note.md", b"x")

            command = subprocess.run(
                [sys.executable, str(ROOT / "scripts/brandkit_discovery.py"), "progress",
                 "--project-root", str(project), "--run-id", discovery.PROTECTED_RUN_ID,
                 "--answer-sha256", "4" * 64, "--owner-key", str(key),
                 *sum((["--covered-axis", axis] for axis in discovery.BRAND_AXES), [])],
                cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(1, command.returncode)
            self.assertIn("run id is protected", command.stderr)
            self.assertFalse(sealed.exists())

    # -------------------------------------------------- external path rules

    def test_external_paths_are_judged_by_file_identity_and_not_by_spelling(self) -> None:
        """A case-flipped path inside the project used to pass as "external".

        The adapter compared text; on this file system ``…/PROJECT/.brandkit``
        and ``…/project/.brandkit`` are one directory, so the refusal existed
        only because the core repeated the test by identity.  A defence that
        reads as two layers and is one is worth measuring from both sides.
        """
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary) / "casetest"
            (base / "project/.brandkit").mkdir(parents=True)
            inside = base / "project/.brandkit/inside.key"; inside.write_bytes(b"k" * 32); inside.chmod(0o600)
            project = (base / "project").resolve()
            flipped = base / "PROJECT/.brandkit/inside.key"
            if not flipped.exists():
                self.skipTest("case-sensitive file system: the flipped spelling names no file")
            resolved = Path(os.path.abspath(flipped)).resolve(strict=True)
            # The premise: a purely lexical answer is "outside" and it is wrong.
            self.assertNotEqual(project, resolved); self.assertNotIn(project, resolved.parents)
            self.assertTrue(discovery._identity_contains(project, flipped))
            for candidate in (inside, flipped, project, project / ".brandkit"):
                with self.subTest(path=str(candidate)):
                    with self.assertRaisesRegex(discovery.DiscoveryError, "must remain outside the project"):
                        discovery._outside_project(project, candidate, "external input")

            # An external path that does not exist is not "outside", it is absent.
            with self.assertRaisesRegex(discovery.DiscoveryError, "absent or unreadable"):
                discovery._outside_project(project, base / "absent.key", "external input")

            # What comes back is the path the caller named, not a re-resolution
            # of it: the core takes the lock on exactly this pair, and resolving
            # a second time is how one name becomes two files.
            real = base / "outside"; real.mkdir(); (real / "owner.key").write_bytes(b"k" * 32)
            link = base / "link"; link.symlink_to(real)
            self.assertEqual(link / "owner.key", discovery._outside_project(project, link / "owner.key", "external input"))

            # The same question, asked by the *other* function that asks it.
            # ``_outside_project`` gained the identity disjunct in r1d;
            # ``_external_owner_key`` kept the lexical half alone until r1i, and
            # that is the function guarding the progress ledger and the owner
            # receipt.  A key at ``<project>/…`` handed over as ``<PROJECT>/…``
            # was therefore accepted as external - so exactly the writer this
            # rule protects against could hold the key.  Register point 26, the
            # second site of the same class found in this round.
            (real / "owner.key").chmod(0o600)
            for candidate in (inside, flipped):
                with self.subTest(owner_key=str(candidate)):
                    with self.assertRaisesRegex(discovery.DiscoveryError,
                                                "owner key and progress ledger must be outside the project"):
                        discovery._external_owner_key(project, candidate)
            # Through a public entry point, not only through the helper.
            with self.assertRaisesRegex(discovery.DiscoveryError,
                                        "owner key and progress ledger must be outside the project"):
                discovery.write_progress(project, "spelling-probe", [], [],
                                         list(discovery.BRAND_AXES), "1" * 64, flipped)
            # And a genuinely external key is still read, so the disjunct
            # refuses containment rather than everything.
            self.assertEqual(b"k" * 32, discovery._external_owner_key(project, real / "owner.key"))

            # The third spelling of the same question, asked of the same
            # function: a hard link carries no case to flip and no link to
            # resolve, so neither of the two tests above sees it.  r1i copied
            # the identity disjunct into ``_external_owner_key`` and stopped
            # there; the triad is text, identity *and* link count, and every
            # site that answers "inside or outside" owes all three.
            hardlinked = base / "hardlinked.key"; os.link(inside, hardlinked)
            self.assertNotIn(project, Path(os.path.abspath(hardlinked)).resolve(strict=True).parents)
            self.assertFalse(discovery._identity_contains(project, hardlinked))
            for gate in (lambda path: discovery._external_owner_key(project, path),
                         lambda path: discovery._outside_project(project, path, "owner key")):
                with self.subTest(gate=str(gate)):
                    with self.assertRaisesRegex(discovery.DiscoveryError, "must be an exclusive external file"):
                        gate(hardlinked)
            with self.assertRaisesRegex(discovery.DiscoveryError, "must be an exclusive external file"):
                discovery.write_progress(project, "hardlink-spelling", [], [],
                                         list(discovery.BRAND_AXES), "1" * 64, hardlinked)
            hardlinked.unlink()

    def test_a_sidecar_or_descriptor_inside_the_project_never_reaches_the_core(self) -> None:
        """Both trust inputs are checked before the bundle is ever validated."""
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            five_domain_bundle(project, workspace, now, run_id=self.RUN)
            self._owner_inputs(project)
            review_sheet = self._sheet(project, now)
            state = self._state(project, review_sheet)
            # A sidecar the run itself writes is not an independent second copy.
            for sidecar in (project, project / ".brandkit", project / ".brandkit/work"):
                with self.subTest(sidecar=str(sidecar)):
                    with self.assertRaisesRegex(discovery.DiscoveryError, "sidecar must remain outside the project"):
                        discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now, sidecar_root=sidecar)
            # An absent sidecar is refused by *this* module, before the core is
            # asked: the core judges containment, not existence, so a path that
            # names nothing would reach it and fail later with a different
            # sentence.  This is the leg that tells the adapter's own check apart
            # from the core repeating it.
            with self.assertRaisesRegex(discovery.DiscoveryError, "visual source sidecar is absent or unreadable"):
                discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now,
                                                      sidecar_root=Path(temporary) / "absent-sidecar")
            # And a sidecar outside it that carries no narrative is refused by
            # the core rather than defaulted past.
            empty = Path(temporary) / "empty"; empty.mkdir()
            with self.assertRaisesRegex(discovery.DiscoveryError, "source contract failed"):
                discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now, sidecar_root=empty)
            # The named sidecar is the one that works.
            prepared = discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now,
                                                             sidecar_root=visual_decisions.ROOT.parents[1])
            self.assertFalse(prepared["normative"])

    # ------------------------------- reachable adapter rules that had no test

    def test_a_bundle_the_core_returns_blockers_for_is_refused_by_the_adapter(self) -> None:
        """``validated["blockers"]`` - the only gate this module owns on its own.

        ``validate_bundle`` does not refuse a bundle whose reports are not
        review-eligible: it *accepts* the bundle and returns those candidates
        under ``blockers``, leaving the decision to its caller.  This line is
        that decision, and no test ever built such a bundle, so the mutant that
        ignores ``blockers`` survived a full mutation run.

        The selection below names neither blocked domain - both are answered
        ``not-applicable`` - so every other rule of the operation is satisfied
        and the run would *succeed* without the gate.  That is what makes this
        a measurement of the gate rather than of some rule behind it.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            five_domain_bundle(project, workspace, now, run_id=self.RUN, blocked=True)
            self._owner_inputs(project)
            review_sheet = self._sheet(project, now)

            # The premise, from the real core: the bundle is accepted, and two
            # of its candidates come back as blockers rather than as eligible.
            validated = visual_decisions.validate_bundle(project, self.RUN, now)
            self.assertEqual(["code-font-choice", "typography-choice"],
                             sorted(item["id"] for item in validated["blockers"]))
            self.assertEqual(["color-choice", "lockup-choice", "logo-choice"],
                             sorted(item["id"] for item in validated["eligible_candidates"]))

            state = self._state(project, review_sheet)
            state["owner_selection"]["choices"] = [
                {"domain": domain, "outcome": "not-applicable"} if domain in {"typography", "code-font"}
                else {"domain": domain, "outcome": "candidate", "candidate_id": f"{domain}-choice"}
                for domain in VISUAL_FIVE]
            with self.assertRaisesRegex(discovery.DiscoveryError, "blocked or differs from the validated bundle"):
                discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now)
            # Refused before anything was written: no handoff, no source records.
            self.assertFalse((project / self.WORKSPACE / "handoff.json").exists())
            self.assertFalse((project / self.WORKSPACE / "selection-sources").exists())

    def test_every_decision_carries_the_owner_statement_as_its_first_bound_source(self) -> None:
        """The owner is a source of his own decision, and the first one.

        Dropping ``owner-visual-selection`` from ``source_ids`` leaves a handoff
        that still passes every later check - the remaining source is bound, so
        ``handoff_snapshot`` is content - while the owner's statement stops
        being a source of the decision it produced.  Nothing measured it.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            five_domain_bundle(project, workspace, now, run_id=self.RUN)
            self._owner_inputs(project)
            review_sheet = self._sheet(project, now)
            prepared = discovery.prepare_visual_confirmation(
                project, self.RUN, key, self._state(project, review_sheet), now=now)
            document = json.loads((project / prepared["visual_handoff"]["path"]).read_text(encoding="utf-8"))
            self.assertEqual(sorted(VISUAL_FIVE), sorted(item["domain"] for item in document["decisions"]))
            for decision in document["decisions"]:
                with self.subTest(decision=decision["id"]):
                    self.assertEqual(["owner-visual-selection", "impeccable"], decision["source_ids"])
            # And that id is a real bound source of the handoff, carrying the
            # owner's own statement file rather than a bare label.
            owner = next(item for item in document["sources"] if item["id"] == "owner-visual-selection")
            self.assertEqual("owner-supplied", owner["classification"])
            self.assertEqual(f"{self.WORKSPACE}/owner-selection.txt", owner["file"])
            self.assertIsNotNone(discovery.handoff_snapshot(project, prepared["visual_handoff"]["path"], key))

    def test_the_brand_raw_preimage_is_confined_to_the_run_it_belongs_to(self) -> None:
        """The preimage prefix is ``.brandkit/work/<run>/``, not ``.brandkit/``.

        The handoff carries ``brand_raw_preimage_sha256``, which is what binds
        the confirmed decisions to the ``brand-raw.md`` they will be appended
        to.  A preimage from a *different* run - or from outside any run - would
        bind the decisions to a document this run never touches, and the only
        thing standing in the way is the prefix.  Both widenings are measured.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            five_domain_bundle(project, workspace, now, run_id=self.RUN)
            self._owner_inputs(project)
            review_sheet = self._sheet(project, now)
            (project / ".brandkit/brand-raw.md").write_text("# Elsewhere\n", encoding="utf-8")
            (project / ".brandkit/work/other-run").mkdir(parents=True)
            (project / ".brandkit/work/other-run/brand-raw.md").write_text("# Another run\n", encoding="utf-8")
            base = self._state(project, review_sheet)
            for elsewhere in (".brandkit/brand-raw.md", ".brandkit/work/other-run/brand-raw.md"):
                with self.subTest(preimage=elsewhere):
                    state = {**base, "brand_raw_preimage": self._binding(project, elsewhere)}
                    with self.assertRaisesRegex(discovery.DiscoveryError,
                                                "brand-raw preimage path is outside its canonical workspace"):
                        discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now)
            # The run's own preimage is the one that works.
            self.assertFalse(discovery.prepare_visual_confirmation(project, self.RUN, key, base, now=now)["normative"])

    def test_the_selection_source_directory_must_be_a_real_directory_of_this_run(self) -> None:
        """``selection-sources/`` is written into; a link there writes elsewhere.

        Every source record and the evidence copy of the release-bound contract
        land in this directory, and it is created by the operation itself with
        ``exist_ok=True`` - so a directory that is already there is *used*.  A
        symlink is therefore a redirect for every one of those writes, and the
        realpath test is what refuses it.  No test ever put a link there.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            five_domain_bundle(project, workspace, now, run_id=self.RUN)
            self._owner_inputs(project)
            review_sheet = self._sheet(project, now)
            state = self._state(project, review_sheet)
            link = project / self.WORKSPACE / "selection-sources"
            for label, target in (("inside the project", project / ".brandkit/work/ci-pilot"),
                                  ("outside the project", Path(temporary) / "elsewhere")):
                target.mkdir(parents=True, exist_ok=True)
                before = sorted(item.name for item in target.iterdir())
                link.symlink_to(target)
                try:
                    with self.subTest(target=label):
                        with self.assertRaisesRegex(discovery.DiscoveryError,
                                                    "visual selection source directory is unsafe"):
                            discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now)
                        # Nothing was redirected through the link.
                        self.assertEqual(before, sorted(item.name for item in target.iterdir()))
                        self.assertFalse((project / self.WORKSPACE / "handoff.json").exists())
                finally:
                    link.unlink()
            # Without the link the same operation creates the directory itself.
            discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now)
            self.assertTrue(link.is_dir()); self.assertFalse(link.is_symlink())

    # ---------------------------------------- the two defence-in-depth layers

    def test_the_write_funnel_judges_the_resolved_parent_not_the_spelling(self) -> None:
        """``_safe_write`` used to refuse only callers that *spell* the seal.

        Its docstring promised more than that: "a caller that reaches a sealed
        path by some route other than a run id ... is refused by the write
        itself".  A symlink ``work/alias -> first-ci`` is such a route, and the
        file landed inside the sealed tree.  It was never reachable from a
        public entry point - every one of them runs behind an ``O_NOFOLLOW``
        open first - but a funnel that only reads names is not a funnel.

        Measured against a throwaway project that merely spells the same name.
        The sealed run of this repository is never a target here or anywhere
        else in this suite.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve() / "project"
            sealed = project / ".brandkit/work" / discovery.PROTECTED_RUN_ID
            sealed.mkdir(parents=True); (sealed / "sealed.txt").write_bytes(b"sealed\n")
            (project / ".brandkit/work/alias").symlink_to(discovery.PROTECTED_RUN_ID)
            before = sorted(item.name for item in sealed.iterdir())
            with self.assertRaisesRegex(discovery.DiscoveryError, "resolves into the protected first-ci run"):
                discovery._safe_write(project, ".brandkit/work/alias/pwned.md", b"x")
            # Two links in a row, and the sealed name appears in neither the
            # path the caller writes nor any component of it: the spelling test
            # sees nothing here at all, and the resolved parent is still the
            # sealed directory.
            (project / ".brandkit/aliased-work").symlink_to(project / ".brandkit/work")
            with self.assertRaisesRegex(discovery.DiscoveryError, "resolves into the protected first-ci run"):
                discovery._safe_write(project, ".brandkit/aliased-work/alias/deep.md", b"x")
            self.assertEqual(before, sorted(item.name for item in sealed.iterdir()))
            # A regular run is untouched, through a link or not.
            (project / ".brandkit/work/ci-pilot").mkdir()
            discovery._safe_write(project, ".brandkit/work/ci-pilot/note.md", b"x")
            discovery._safe_write(project, ".brandkit/aliased-work/ci-pilot/note2.md", b"x")
            self.assertEqual(b"x", (project / ".brandkit/work/ci-pilot/note2.md").read_bytes())

    def test_the_receipt_output_path_and_the_central_reader_decide_their_own_rules(self) -> None:
        """Two reachable rules that no test asked about until r1j.

        ``write_owner_receipt`` derives the one path a receipt may be written
        to and refuses every other; downstream ``verify_owner_receipt`` reads
        only the canonical name, so a receipt written elsewhere is inert - but
        "inert" is a property of the *other* function, and this one's rule was
        measured by nothing.  ``secure_read`` is the single reader every bound
        member of a handoff goes through, and ``O_NOFOLLOW`` on its final
        component is the whole of its symlink defence.

        Both directions each: the canonical name is written and read back byte
        for byte, and a real file next to the link is still read.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, _key = self._project(Path(temporary))
            visual_receipt = {"run_id": self.RUN, "handoff_kind": "visual-decisions",
                      "handoff_id": "visual-selection-001", "issuer": "owner"}
            canonical_name = f".brandkit/owner-receipts/{self.RUN}/visual-selection-001.json"
            plain = {"run_id": self.RUN, "handoff_kind": "discovery", "handoff_id": "discovery-001"}
            for receipt, wrong in ((visual_receipt, f".brandkit/owner-receipts/{self.RUN}.json"),
                                   (visual_receipt, f".brandkit/owner-receipts/{self.RUN}/visual-selection-001.JSON"),
                                   (visual_receipt, f".brandkit/owner-receipts/{self.RUN}/other.json"),
                                   (visual_receipt, f".brandkit/owner-receipts/{self.RUN}/./visual-selection-001.json"),
                                   (visual_receipt, f"owner-receipts/{self.RUN}/visual-selection-001.json"),
                                   (visual_receipt, f".brandkit/work/{self.RUN}/visual-decisions/handoff.json"),
                                   (plain, canonical_name)):
                with self.subTest(path=wrong, kind=receipt["handoff_kind"]):
                    with self.assertRaisesRegex(discovery.DiscoveryError,
                                                "owner receipt output path is not canonical"):
                        discovery.write_owner_receipt(project, wrong, receipt)
                    self.assertFalse((project / wrong).exists(), "a refused receipt must leave nothing behind")
            discovery.write_owner_receipt(project, canonical_name, visual_receipt)
            self.assertEqual(discovery.canonical(visual_receipt), (project / canonical_name).read_bytes())
            discovery.write_owner_receipt(project, f".brandkit/owner-receipts/{self.RUN}.json", plain)
            self.assertEqual(discovery.canonical(plain),
                             (project / f".brandkit/owner-receipts/{self.RUN}.json").read_bytes())

            # ``secure_read``: the final component is opened O_NOFOLLOW, so a
            # symlink there is refused however harmless its target looks.
            work = project / ".brandkit/work" / self.RUN
            (work / "real.md").write_bytes(b"real")
            self.assertEqual(b"real", discovery.secure_read(project, f".brandkit/work/{self.RUN}/real.md")[0])
            outside = Path(temporary) / "outside.md"; outside.write_bytes(b"outside")
            links = {"alias.md": work / "real.md", "escape.md": outside,
                     "dangling.md": work / "absent.md", "self.md": work / "self.md"}
            for name, target in links.items():
                (work / name).symlink_to(target)
                with self.subTest(link=name):
                    with self.assertRaisesRegex(discovery.DiscoveryError, "cannot safely read"):
                        discovery.secure_read(project, f".brandkit/work/{self.RUN}/{name}")
            # A symlinked *directory* component is refused by the same loop.
            (project / ".brandkit/aliased").symlink_to(work)
            with self.assertRaisesRegex(discovery.DiscoveryError, "cannot safely read"):
                discovery.secure_read(project, ".brandkit/aliased/real.md")
            # And the real file beside all of them still reads, so the rule
            # refuses links rather than everything.
            self.assertEqual(b"real", discovery.secure_read(project, f".brandkit/work/{self.RUN}/real.md")[0])

    def test_a_hardlink_is_not_an_external_file(self) -> None:
        """The third spelling of "inside the project", and the adapter was blind.

        Case folding and symlinks are both settled by identity; a hard link is
        neither.  Every component of the external name really is outside the
        project, ``realpath`` has nothing to resolve, and the bytes are a file
        inside it.  The core refuses it one layer down (``st_nlink != 1`` in
        ``_external_0600``), so the descriptor was fail-closed either way - but
        a defence that reads as two layers was one again.

        The rule is measured on *both* functions that answer "inside or
        outside".  ``_external_owner_key`` copied the identity disjunct in r1i
        and left the link count behind, and there the omission is not one layer
        of two: the discovery owner key never reaches the core, so accepting a
        hard link means the progress ledger and every owner receipt can be
        signed with a key the project itself can read.  Both spellings are
        checked - the second name inside the project, and a second name that is
        merely somewhere else - because the promise is exclusivity, not
        location alone.
        """
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            project = base / "project"; (project / ".brandkit").mkdir(parents=True)
            inside = project / ".brandkit/inside.key"; inside.write_bytes(b"k" * 32); inside.chmod(0o600)
            linked = base / "outside.key"; os.link(inside, linked)
            self.assertEqual(os.stat(inside).st_ino, os.stat(linked).st_ino)
            # The two tests that decide today both answer "outside", wrongly.
            self.assertNotIn(project, Path(os.path.abspath(linked)).resolve(strict=True).parents)
            self.assertFalse(discovery._identity_contains(project, linked))
            with self.assertRaisesRegex(discovery.DiscoveryError, "must be an exclusive external file"):
                discovery._outside_project(project, linked, "Affinity keys and replay ledger")
            # A genuinely external file still passes, and so does an external
            # directory, whose link count is greater than one by construction.
            genuine = base / "genuine.key"; genuine.write_bytes(b"k" * 32); genuine.chmod(0o600)
            self.assertEqual(genuine, discovery._outside_project(project, genuine, "external input"))
            sidecar = base / "sidecar"; (sidecar / "sub").mkdir(parents=True)
            self.assertGreater(os.stat(sidecar).st_nlink, 1)
            self.assertEqual(sidecar, discovery._outside_project(project, sidecar, "visual source sidecar"))

            # Case 1 - the hard link points at bytes inside the project.  This
            # is the whole attack: the writer inside the project makes the link
            # itself, the owner then calls exactly as before.  Register point 32.
            self.assertEqual(2, os.stat(linked).st_nlink)
            with self.assertRaisesRegex(discovery.DiscoveryError, "must be an exclusive external file"):
                discovery._external_owner_key(project, linked)
            # Through the public entry points, not only through the helper.
            for call in (lambda: discovery.initialize_progress(project, "hardlink-probe", linked),
                         lambda: discovery.read_progress(project, "hardlink-probe", linked),
                         lambda: discovery.write_progress(project, "hardlink-probe", [], [],
                                                          list(discovery.BRAND_AXES), "1" * 64, linked)):
                with self.assertRaisesRegex(discovery.DiscoveryError, "must be an exclusive external file"):
                    call()

            # Case 2 - both names lie outside the project.  The promise is an
            # *exclusive* external file, so a shared inode is refused here too,
            # exactly as ``_outside_project`` refuses it; otherwise "outside"
            # would mean "not textually below", which is the answer this class
            # keeps coming back for.
            elsewhere = base / "elsewhere.key"; os.link(genuine, elsewhere)
            self.assertEqual(2, os.stat(elsewhere).st_nlink)
            for name in (genuine, elsewhere):
                with self.subTest(owner_key=str(name)):
                    with self.assertRaisesRegex(discovery.DiscoveryError, "must be an exclusive external file"):
                        discovery._external_owner_key(project, name)
                    with self.assertRaisesRegex(discovery.DiscoveryError, "must be an exclusive external file"):
                        discovery._outside_project(project, name, "external input")
            # Removing the second name makes the very same file acceptable
            # again - the rule refuses sharing, not this key.
            elsewhere.unlink()
            self.assertEqual(1, os.stat(genuine).st_nlink)
            self.assertEqual(b"k" * 32, discovery._external_owner_key(project, genuine))
            self.assertEqual(genuine, discovery._outside_project(project, genuine, "external input"))
            # A directory handed to the owner-key gate is not refused by the
            # link count (directories carry more than one link by construction);
            # it is refused for being no readable owner key at all.
            with self.assertRaises(discovery.DiscoveryError) as refusal:
                discovery._external_owner_key(project, sidecar)
            self.assertNotIn("exclusive external file", str(refusal.exception))

    # ------------------------------------------------- the adapter's own gates

    def test_the_core_result_gate_recomputes_what_it_is_handed(self) -> None:
        """The closed shape of the core result, measured on the gate itself.

        This is the one rule whose negative cases cannot be produced by a real
        core - a correct core never returns a wrong shape.  Rather than put a
        fake core in front of the production path, the gate is a pure function
        and is called directly, so the seam keeps its no-substitute rule.
        """
        value = {"domain": "color", "roles": {"primary": "#112233"}}
        digest = hashlib.sha256(discovery.canonical(value)).hexdigest()
        candidate = {"id": "color-choice", "kind": "color", "value": value, "value_sha256": digest}
        good = {"candidate_set_sha256": "a" * 64, "eligible_candidates": [candidate], "blockers": []}
        self.assertEqual(good, discovery._closed_core_result(good))
        self.assertEqual(good, discovery._closed_core_result(json.loads(json.dumps(good))))

        closed = "invalid closed result"; broken = "invalid candidate"
        cases = [
            (closed, {**good, "extra": 1}),
            (closed, {name: good[name] for name in ("candidate_set_sha256", "blockers")}),
            (closed, {**good, "candidate_set_sha256": "z" * 64}),
            (closed, {**good, "blockers": {}}),
            (closed, {**good, "eligible_candidates": {}}),
            (closed, "not a dict"),
            # A value hash that is merely copied is a number nothing checks: it
            # is what the handoff, the receipt and brand-raw.md all carry.
            (broken, {**good, "eligible_candidates": [{**candidate, "value_sha256": "b" * 64}]}),
            (broken, {**good, "eligible_candidates": [{**candidate, "value": {"domain": "color", "roles": {}}}]}),
            (broken, {**good, "eligible_candidates": [{**candidate, "extra": 1}]}),
            (broken, {**good, "eligible_candidates": [{name: candidate[name] for name in ("id", "kind", "value")}]}),
            # The domain vocabulary is the five decision domains, not any string.
            (broken, {**good, "eligible_candidates": [{**candidate, "kind": "spacing"}]}),
        ]
        for message, result in cases:
            with self.subTest(refusal=message, result=str(result)[:60]):
                with self.assertRaisesRegex(discovery.DiscoveryError, message):
                    discovery._closed_core_result(result)

    def test_the_handoff_source_records_come_from_the_release_bound_contract(self) -> None:
        """``selected_dependency`` and ``candidates[].name`` - the core's model, not an invented one."""
        contract = json.loads((ROOT / "visual-decision-sources.v1.json").read_text(encoding="utf-8"))
        selected, records = discovery._visual_source_records(contract)
        self.assertEqual("impeccable", selected)
        self.assertEqual({"impeccable"}, set(records))
        self.assertEqual("selected", records["impeccable"]["decision"])
        # Every rejected candidate stays out of the citable records.
        self.assertTrue(any(item["decision"] == "rejected" for item in contract["candidates"]))

        cases = {
            "no usable selected dependency": [
                {**contract, "selected_dependency": ""},
                {**contract, "selected_dependency": "Impeccable"},
                {**contract, "selected_dependency": None},
            ],
            "collides with the reserved owner source": [
                {**contract, "selected_dependency": "owner-visual-selection",
                 "candidates": [{"name": "owner-visual-selection", "decision": "selected"}]},
            ],
            "carries no candidate list": [{**contract, "candidates": {}}],
            "does not select exactly its named dependency": [
                {**contract, "candidates": [item for item in contract["candidates"] if item["decision"] != "selected"]},
                {**contract, "candidates": contract["candidates"] + [{"name": "second", "decision": "selected"}]},
                {**contract, "candidates": [{**item, "name": "other"} if item["decision"] == "selected" else item
                                            for item in contract["candidates"]]},
            ],
        }
        for message, documents in cases.items():
            for index, document in enumerate(documents):
                with self.subTest(refusal=message, case=index):
                    with self.assertRaisesRegex(discovery.DiscoveryError, message):
                        discovery._visual_source_records(document)

        # The binding is pinned to the release artifact by name and by digest.
        binding = {"path": "visual-decision-sources.v1.json",
                   "sha256": hashlib.sha256((ROOT / "visual-decision-sources.v1.json").read_bytes()).hexdigest()}
        document, raw = discovery._release_bound_source_contract(binding)
        self.assertEqual(contract, document)
        self.assertEqual((ROOT / "visual-decision-sources.v1.json").read_bytes(), raw)
        with self.assertRaisesRegex(discovery.DiscoveryError, "differs from the release-bound artifact"):
            discovery._release_bound_source_contract({**binding, "path": "impeccable-adapter.v1.json"})
        with self.assertRaisesRegex(discovery.DiscoveryError, "differs from its binding"):
            discovery._release_bound_source_contract({**binding, "sha256": "c" * 64})

    def test_the_affinity_receipt_descriptor_is_closed_in_both_directions(self) -> None:
        """Closed means closed: a binding may carry neither less nor more."""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve() / "project"
            workspace = project / self.WORKSPACE; (workspace / "receipts").mkdir(parents=True)
            (workspace / "receipts/logo-a.json").write_bytes(b'{"receipt":"logo-a"}\n')
            external = Path(temporary) / "secrets"; external.mkdir()
            key_path = external / "owner.key"; key_path.write_bytes(b"k" * 32); key_path.chmod(0o600)
            ledger = external / "replay-ledger.json"; ledger.write_bytes(b"{}\n"); ledger.chmod(0o600)
            raw = (workspace / "receipts/logo-a.json").read_bytes()
            binding = {"path": "receipts/logo-a.json", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            auth = {"owner_key_path": str(key_path), "replay_ledger_path": str(ledger), "receipts": {"logo-a": binding}}
            retyped = discovery._closed_affinity_auth(project, self.RUN, auth)
            self.assertEqual({"logo-a": binding}, retyped["receipts"])
            self.assertEqual((key_path, ledger), (retyped["owner_key_path"], retyped["replay_ledger_path"]))
            for extra in ({**binding, "role": "receipt"}, {**binding, "mtime": 1}):
                with self.subTest(binding=sorted(extra)):
                    with self.assertRaisesRegex(discovery.DiscoveryError, "receipt binding is not closed"):
                        discovery._closed_affinity_auth(project, self.RUN, {**auth, "receipts": {"logo-a": extra}})
            with self.assertRaisesRegex(discovery.DiscoveryError, "receipt binding is not closed"):
                discovery._closed_affinity_auth(project, self.RUN, {**auth, "receipts": {"logo-a": {"path": "receipts/logo-a.json", "sha256": binding["sha256"]}}})
            # Both size and digest are checked; the core checks the digest alone.
            with self.assertRaisesRegex(discovery.DiscoveryError, "differs from its descriptor"):
                discovery._closed_affinity_auth(project, self.RUN, {**auth, "receipts": {"logo-a": {**binding, "size": binding["size"] + 1}}})

    def test_the_visual_handoff_binds_its_inventory_and_confines_every_member(self) -> None:
        """A handoff may not name a file outside its workspace or drift from its own hashes."""
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            five_domain_bundle(project, workspace, now, run_id=self.RUN)
            self._owner_inputs(project)
            review_sheet = self._sheet(project, now)
            prepared = discovery.prepare_visual_confirmation(
                project, self.RUN, key, self._state(project, review_sheet), now=now)
            handoff = prepared["visual_handoff"]["path"]
            original = json.loads((project / handoff).read_text(encoding="utf-8"))
            self.assertIsNotNone(discovery.handoff_snapshot(project, handoff, key))

            def rewrite(document: dict) -> None:
                (project / handoff).write_bytes(discovery.canonical(document))

            review_item = next(item for item in original["files"] if item["role"] == "candidate-review")
            drifted = json.loads(json.dumps(original)); drifted["candidate_bundle_sha256"] = review_item["sha256"]
            rewrite(drifted)
            with self.assertRaisesRegex(discovery.DiscoveryError, "top-level hashes differ from inventory"):
                discovery.handoff_snapshot(project, handoff, key)

            escaped = json.loads(json.dumps(original))
            escaped["files"] = [self._binding(project, ".brandkit/work/ci-pilot/brand-raw.md") | {"role": item["role"]}
                                if item["role"] == "candidate-review" else item for item in escaped["files"]]
            rewrite(escaped)
            with self.assertRaisesRegex(discovery.DiscoveryError, "outside its canonical workspace"):
                discovery.handoff_snapshot(project, handoff, key)

            unbound = json.loads(json.dumps(original))
            unbound["decisions"] = [{**item, "source_ids": ["owner-visual-selection", "not-a-bound-source"]}
                                    for item in unbound["decisions"]]
            rewrite(unbound)
            with self.assertRaisesRegex(discovery.DiscoveryError, "lacks a bound source"):
                discovery.handoff_snapshot(project, handoff, key)

            rewrite(original)
            self.assertIsNotNone(discovery.handoff_snapshot(project, handoff, key))

    def test_the_build_gate_and_the_disclosure_contract_role_stay_closed(self) -> None:
        """Two rules r1c repaired and left unmeasured."""
        fixtures = load(FIXTURES)
        cases = {item["request_id"]: item for item in fixtures["cases"]}
        positive = cases["build-positive"]
        for unusable in ("missing", "stale", "unknown"):
            with self.subTest(visual_decisions=unusable):
                state = {**positive["state"], "visual_decisions": unusable}
                request = {**positive["request"], "request_id": f"build-{unusable}", "state": state} if "request" in positive else {
                    "schema_version": "brandkit-skill-request.v1", "request_id": f"build-{unusable}",
                    "skill": "brandkit-build", "input_artifacts": fixtures["input_artifacts"], "state": state}
                result = execute_request(request)
                self.assertEqual("blocked", result["decision"])
                self.assertIn("visual_decisions_not_consumable", result["blockers"])
        self.assertEqual("package-draft-only", execute_request({
            "schema_version": "brandkit-skill-request.v1", "request_id": "build-required-open",
            "skill": "brandkit-build", "input_artifacts": fixtures["input_artifacts"],
            "state": {**positive["state"], "visual_decisions": "required-open"}})["decision"])

        contract = load(ROOT / "brandkit-skill-contract.v1.json")
        self.assertEqual([], validate_progressive_disclosure(ROOT, contract))
        entry = next(name for name, item in contract["progressive_disclosure"]["catalog"].items()
                     if item["path"] == "visual-decision-sources.v1.json")
        for widened in ("impeccable-adapter.v1.json", "brandkit-skill-contract.v1.json", "skill-contract-release.v1.json"):
            with self.subTest(path=widened):
                mutated = json.loads(json.dumps(contract))
                mutated["progressive_disclosure"]["catalog"][entry] = {
                    **mutated["progressive_disclosure"]["catalog"][entry], "role": "contract", "path": widened,
                    "sha256": hashlib.sha256((ROOT / widened).read_bytes()).hexdigest()}
                errors = validate_progressive_disclosure(ROOT, mutated)
                self.assertTrue(any("role/path combination is forbidden" in item for item in errors), errors)

    # -------------------------------------------- what the removed mocks measured

    def test_a_correction_invalidates_the_earlier_receipt_in_both_of_its_forms(self) -> None:
        """Nothing becomes normative without a second confirmation over the current bytes.

        Both correction forms are measured: a new selection id, and the *same*
        selection id whose review bytes have changed.  The second is the one a
        receipt-path check alone would miss.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            five_domain_bundle(project, workspace, now, run_id=self.RUN)
            self._owner_inputs(project)
            review_sheet = self._sheet(project, now)
            review = review_sheet
            first = discovery.prepare_visual_confirmation(project, self.RUN, key, self._state(project, review), now=now)
            handoff = first["visual_handoff"]["path"]
            rendered = discovery.render_review(project, handoff, key)
            self.assertIn("color-choice", rendered["markdown"])
            self.assertNotIn('"schema_version"', rendered["markdown"])
            receipt = discovery.make_owner_receipt(project, handoff, rendered["path"], key, "owner@example",
                                                   "approval-visual-001", "Diese Entscheidungen bestätige ich.", 600, now=now)
            receipt_path = ".brandkit/owner-receipts/ci-pilot/visual-selection-001.json"
            discovery.write_owner_receipt(project, receipt_path, receipt)
            self.assertIsNotNone(discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=now + 1))

            # Form one: a corrected selection under a new id.
            (project / self.WORKSPACE / "owner-correction.txt").write_text("Ich korrigiere die Auswahl.\n", encoding="utf-8")
            corrected = self._state(project, review, selection_id="selection-002", statement="owner-correction.txt")
            corrected["owner_selection"]["choices"] = [
                {"domain": item["domain"], "outcome": "not-applicable"} if item["domain"] == "lockup" else item
                for item in corrected["owner_selection"]["choices"]]
            second = discovery.prepare_visual_confirmation(project, self.RUN, key, corrected, now=now)
            self.assertNotEqual(first["visual_handoff"]["sha256"], second["visual_handoff"]["sha256"])
            with self.assertRaises(discovery.DiscoveryError):
                discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=now + 2)

            # Form two: the same selection id, different bytes underneath it.
            (project / self.WORKSPACE / "owner-selection.txt").write_text("Ich waehle dieselben Kandidaten, anders formuliert.\n", encoding="utf-8")
            again = discovery.prepare_visual_confirmation(project, self.RUN, key, self._state(project, review), now=now)
            self.assertEqual("selection-001", again["selection_id"])
            self.assertNotEqual(first["visual_handoff"]["sha256"], again["visual_handoff"]["sha256"])
            with self.assertRaises(discovery.DiscoveryError):
                discovery.verify_owner_receipt(project, handoff, receipt_path, key, now=now + 3)
            # And consume before confirmation never worked in the first place.
            fresh = ".brandkit/owner-receipts/ci-pilot/visual-selection-002.json"
            with self.assertRaises(discovery.DiscoveryError):
                discovery.verify_owner_receipt(project, second["visual_handoff"]["path"], fresh, key, now=now + 4)

    def test_no_refused_trust_descriptor_ever_reaches_the_core(self) -> None:
        """Eleven descriptor refusal classes, with the external ledger as the witness.

        The earlier version of this test proved "the core was not called" by
        counting calls on a stand-in.  The witness here is stronger and needs no
        stand-in: the core spends the bundle's single-use mutation receipts into
        the external replay ledger the moment it authenticates, so a ledger that
        is byte-identical afterwards is proof that the core never ran.
        """
        with tempfile.TemporaryDirectory() as temporary:
            project, key = self._project(Path(temporary))
            secrets = Path(temporary) / "secrets"; secrets.mkdir()
            workspace = visual_decisions.visual_workspace(project, self.RUN, create=True)
            now = int(time.time())
            fixture = five_domain_affinity_bundle(project, workspace, secrets, now, run_id=self.RUN)
            self._owner_inputs(project)
            review_sheet = self._sheet(project, now, affinity_auth=fixture["auth"])
            state = self._state(project, review_sheet)
            auth = fixture["auth"]; binding = dict(auth["receipts"]["mark-a"])
            genesis = fixture["ledger_path"].read_bytes()
            inside = project / ".brandkit/inside.key"; inside.write_bytes(b"k" * 32); inside.chmod(0o600)

            broken = {
                "descriptor is not closed": [
                    {**auth, "extra": 1},
                    {name: auth[name] for name in ("owner_key_path", "receipts")},
                    {**auth, "receipts": []},
                ],
                "not an absolute external path": [
                    {**auth, "owner_key_path": "relative/owner.key"},
                    {**auth, "replay_ledger_path": b"/tmp/x"},
                ],
                "must remain outside the project": [
                    {**auth, "owner_key_path": str(inside)},
                    {**auth, "replay_ledger_path": str(project)},
                ],
                "must be different files": [{**auth, "replay_ledger_path": auth["owner_key_path"]}],
                "receipt candidate id is invalid": [{**auth, "receipts": {"Mark-A": binding}}],
                "receipt binding is not closed": [
                    {**auth, "receipts": {"mark-a": {"path": binding["path"], "sha256": binding["sha256"]}}},
                    {**auth, "receipts": {"mark-a": {**binding, "role": "receipt"}}},
                ],
                "confined workspace-relative path": [
                    {**auth, "receipts": {"mark-a": {**binding, "path": "../" + binding["path"]}}},
                    {**auth, "receipts": {"mark-a": {**binding, "path": "/etc/passwd"}}},
                ],
                "differs from its descriptor": [
                    {**auth, "receipts": {"mark-a": {**binding, "sha256": "f" * 64}}},
                    {**auth, "receipts": {"mark-a": {**binding, "size": binding["size"] + 1}}},
                ],
            }
            for message, cases in broken.items():
                for index, case in enumerate(cases):
                    with self.subTest(refusal=message, case=index):
                        with self.assertRaisesRegex(discovery.DiscoveryError, message):
                            discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now, affinity_auth=case)
                        self.assertEqual(genesis, fixture["ledger_path"].read_bytes())

            # Neither trust input may travel inside the request document, which
            # lives in the project the run is protecting itself against.
            for field in ("affinity_auth", "sidecar_root"):
                with self.subTest(state_field=field):
                    with self.assertRaisesRegex(discovery.DiscoveryError, "visual confirmation request is not closed"):
                        discovery.prepare_visual_confirmation(project, self.RUN, key, {**state, field: auth}, now=now)
                    self.assertEqual(genesis, fixture["ledger_path"].read_bytes())

            # The adapter is bound to the accepted core signature, by name.
            self.assertEqual(["project_root", "run_id", "now", "sidecar_root", "affinity_auth"],
                             list(inspect.signature(visual_decisions.validate_bundle).parameters))
            self.assertEqual(["project_root", "run_id", "owner_key", "state", "now", "sidecar_root", "affinity_auth"],
                             list(inspect.signature(discovery.prepare_visual_confirmation).parameters))
            # Nothing was consumed by any of the above; the good descriptor still works.
            self.assertEqual(genesis, fixture["ledger_path"].read_bytes())
            self.assertFalse(discovery.prepare_visual_confirmation(project, self.RUN, key, state, now=now,
                                                                   affinity_auth=auth)["normative"])
            self.assertNotEqual(genesis, fixture["ledger_path"].read_bytes())

    def test_no_test_substitutes_the_visual_core(self) -> None:
        """The rule that would have caught the r1c blocker, kept enforceable.

        r1c's suite was green because every test that drove the operation
        replaced the core.  A rule that lives only in a review is a rule that
        comes back; this one is checked - and it is checked on the *syntax
        tree*, because the substring form it had before caught exactly one of
        the eight ways to write the same substitution (r1d's critic measured
        that: one CAUGHT, seven PASSES).  A tripwire that a different quote
        character walks past is not the guard its docstring claims.

        The detector is measured on both directions here: every one of those
        eight forms is flagged, and the four substitutions this suite makes on
        purpose - all of them far from the seam - are not.
        """
        # Direction one: the detector catches every spelling of the same act.
        module, symbol = "discovery", "_validated_visual_bundle"
        bypasses = {
            "the form the old tripwire was written for": f'patch.object({module}, "{symbol}")',
            "single quotes": f"patch.object({module}, '{symbol}')",
            "no space after the comma": f'patch.object({module},"{symbol}")',
            "a dotted target string": f'patch("scripts.brandkit_discovery.{symbol}")',
            "plain attribute assignment": f"{module}.{symbol} = lambda *a, **k: None",
            "setattr": f'setattr({module}, "{symbol}", None)',
            "another module alias": f'patch.object(other_alias, "{symbol}")',
            "a concatenated symbol name": f'p = patch.object({module}, "_valid" + "ated_visual_bundle"); p.start()',
            "a decorator": f'@patch.object({module}, "{symbol}")\ndef t(self, stub): pass',
            "monkeypatch": f'monkeypatch.setattr({module}, "{symbol}", None)',
        }
        for label, source in bypasses.items():
            with self.subTest(form=label):
                self.assertTrue(core_substitutions(source), f"the guard misses: {label}")
        for label, source in {
            "reading the signature": "inspect.signature(visual_decisions.validate_bundle).parameters",
            "calling the real thing": "visual_decisions.validate_bundle(project, run, now)",
            "naming it in a message": 'self.assertIn("validate_bundle", text)',
            "patching something else": 'patch.object(discovery, "_workspace_paths")',
        }.items():
            with self.subTest(benign=label):
                self.assertEqual([], core_substitutions(source))

        # Direction two: neither shipped test module does any of it.
        for name in ("tests/test_brandkit_skills.py", "tests/test_brandkit_visual_decisions.py"):
            with self.subTest(module=name):
                self.assertEqual([], core_substitutions((ROOT / name).read_text(encoding="utf-8")),
                                 f"{name} substitutes the visual core")

if __name__ == "__main__":
    unittest.main()
