from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from jsonschema import ValidationError

import scripts.materialize_brandkit_instructions as instruction_materializer
from scripts.materialize_brandkit_instructions import materialize
from scripts.validate_brandkit_skills import COLLISION_FIXTURES, FIXTURES, IMPECCABLE_COMMANDS, ROOT, collision_dry_run, dry_run, execute_request, frontmatter, impeccable_load_trace, impeccable_source_routes, load, route_design_request, transitive_playbook_trace, validate, validate_impeccable_load_trace, validate_progressive_disclosure, validate_public_contracts, validate_real_impeccable_context_matrix, validate_release_manifest


class BrandKitSkillContractTests(unittest.TestCase):
    RELEASE_MANIFEST_SHA256 = "2040dfaedd87dd7f88765f76486ca3ed38e7f1a7cfa8f46d7ab656a44b34383f"
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
        self.assertEqual(7, len(disclosure["routes"]))
        self.assertEqual(7, len({route["skill"] for route in disclosure["routes"].values()}))
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
        self.assertEqual(9, len(cases))
        self.assertEqual(
            {
                "route-spacing-audit": "brandkit-spacing",
                "route-ui-redesign": "brandkit-impeccable -> impeccable",
                "route-brand-package": "brandkit-build",
                "route-pdf-publication": "brand-voice-pdf -> brandkit-publish",
                "route-design-md": "brandkit-impeccable",
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
        self.assertEqual(["brandkit-setup", "brandkit-interview", "brandkit-build", "brandkit-publish"], contract["sequence"])
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

    def _copy_repo(self, temporary: str) -> Path:
        root = Path(temporary).resolve() / "repo"
        root.mkdir()
        # This suite mutates only contract sources. Copying unrelated PDF goldens
        # made every negative case depend on tens of thousands of I/O operations.
        # Keep the cleanroom source set explicit and fail if a validator dependency
        # escapes it; this is also the frozen inventory used by every mutation case.
        source_directories = (
            "docs", "document-types", "fixtures/brandkit-skills", "schemas",
            "scripts", "skills", "toolchain",
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


if __name__ == "__main__":
    unittest.main()
