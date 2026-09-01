from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts import pdf_e2e_repro as repro

ROOT = Path(__file__).resolve().parents[1]
TEST_TREE = "b" * 40
TEST_CANONICAL_ROOT = "fixtures/pdf-renderer/goldens/repro-test"


class PdfE2eReproTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary = tempfile.TemporaryDirectory(
            dir=os.environ.get("BRANDKIT_SESSION_ROOT")
        )
        cls.base = Path(cls._temporary.name)
        cls.source = cls.base / "source"
        for relative in repro.SOURCE_PIN_PATHS:
            source = ROOT / relative
            destination = cls.source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        cls.key_path = cls.base / "issuer.key"
        cls.key_path.write_bytes(b"repro-project-identity-test-key-material-0001")
        cls.key_path.chmod(0o600)
        trust_path = cls.source / "renderer-trust.v1.json"
        trust = json.loads(trust_path.read_text(encoding="utf-8"))
        trust["issuer_key_sha256"] = repro.sha(cls.key_path)
        trust_path.write_text(
            json.dumps(trust, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        cls.baseline = cls.base / "baseline"
        shutil.copytree(
            ROOT / "fixtures/pdf-renderer/goldens/candidate-1.2.28",
            cls.baseline,
        )
        cls._resign_all_profiles(cls.baseline)

    @classmethod
    def tearDownClass(cls):
        cls._temporary.cleanup()

    @staticmethod
    def expected_identity(profile):
        return hashlib.sha256(
            f"brandkit-pdf-e2e-project.v1:{profile}".encode("utf-8")
        ).hexdigest()

    @classmethod
    def _sign_envelope(cls, value):
        signed = {
            name: value[name] for name in ("schema_version", "issuer", "payload")
        }
        value["hmac_sha256"] = hmac.new(
            cls.key_path.read_bytes(), repro.canonical(signed), hashlib.sha256
        ).hexdigest()

    @classmethod
    def _write_json(cls, path, value):
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def _resign_all_profiles(cls, root):
        original = repro.load(root / "inventory.json")
        run = original["payload"]["run"]
        records = repro.records_by_profile(run)
        for profile in repro.PROFILE_ORDER:
            audit_path = root / profile / "humanizer-audit.json"
            audit = repro.load(audit_path)
            cls._sign_envelope(audit)
            cls._write_json(audit_path, audit)
            result_path = root / profile / "render-result.json"
            result = repro.load(result_path)
            result["payload"]["binding"]["project_identity_sha256"] = cls.expected_identity(
                profile
            )
            result["payload"]["binding"]["humanizer_audit_sha256"] = repro.sha(
                audit_path
            )
            cls._sign_envelope(result)
            cls._write_json(result_path, result)
            records[profile]["render_result"]["sha256"] = repro.sha(result_path)
        generated = {
            "schema_version": "brandkit-pdf-e2e-goldens.v2",
            **run,
            "closed_file_inventory": repro.closed_inventory(root),
        }
        cls._write_json(root / "inventory.json", generated)
        repro.sign_root(
            root,
            cls.source,
            cls.key_path,
            TEST_TREE,
            TEST_CANONICAL_ROOT,
        )

    def roots(self):
        case = Path(
            tempfile.mkdtemp(prefix=f"{self._testMethodName}-", dir=self.base)
        )
        left = case / "left"
        right = case / "right"
        shutil.copytree(self.baseline, left)
        shutil.copytree(self.baseline, right)
        return left, right

    def verify(self, root):
        return repro.verify_root(
            root,
            self.source,
            self.key_path,
            TEST_TREE,
            TEST_CANONICAL_ROOT,
        )

    def compare(self, left, right):
        return repro.compare_roots(
            left,
            right,
            self.source,
            self.source,
            self.key_path,
            TEST_TREE,
            TEST_CANONICAL_ROOT,
        )

    def attack_identity(self, root, profile, identity, *, result=True, summary=True):
        inventory = repro.load(root / "inventory.json")
        profile_summary = next(
            item for item in inventory["payload"]["profiles"] if item["id"] == profile
        )
        if result:
            result_path = root / profile / "render-result.json"
            value = repro.load(result_path)
            value["payload"]["binding"]["project_identity_sha256"] = identity
            self._sign_envelope(value)
            self._write_json(result_path, value)
            records = repro.records_by_profile(inventory["payload"]["run"])
            records[profile]["render_result"]["sha256"] = repro.sha(result_path)
            profile_summary["render_result_sha256"] = repro.sha(result_path)
            profile_summary["render_result_hmac_sha256"] = value["hmac_sha256"]
        if summary and "project_identity_sha256" in profile_summary:
            profile_summary["project_identity_sha256"] = identity
        inventory["payload"]["closed_file_inventory"] = repro.closed_inventory(root)
        self._sign_envelope(inventory)
        self._write_json(root / "inventory.json", inventory)

    def attack_owner_evidence(self, root, profile, value):
        inventory = repro.load(root / "inventory.json")
        approval_path = root / profile / "owner-approval.fixture.json"
        self._write_json(approval_path, value)
        result_path = root / profile / "render-result.json"
        result = repro.load(result_path)
        result["payload"]["binding"]["owner_approval_sha256"] = repro.sha(approval_path)
        self._sign_envelope(result)
        self._write_json(result_path, result)
        records = repro.records_by_profile(inventory["payload"]["run"])
        records[profile]["render_result"]["sha256"] = repro.sha(result_path)
        profile_summary = next(
            item for item in inventory["payload"]["profiles"] if item["id"] == profile
        )
        profile_summary["owner_evidence_sha256"] = repro.sha(approval_path)
        profile_summary["render_result_sha256"] = repro.sha(result_path)
        profile_summary["render_result_hmac_sha256"] = result["hmac_sha256"]
        inventory["payload"]["closed_file_inventory"] = repro.closed_inventory(root)
        self._sign_envelope(inventory)
        self._write_json(root / "inventory.json", inventory)

    def attack_run(self, root, mutate):
        inventory = repro.load(root / "inventory.json")
        mutate(inventory["payload"]["run"])
        self._sign_envelope(inventory)
        self._write_json(root / "inventory.json", inventory)

    def test_fully_signed_baseline_has_eight_expected_project_identities(self):
        inventory = self.verify(self.baseline)
        observed = {
            item["id"]: item["project_identity_sha256"]
            for item in inventory["payload"]["profiles"]
        }
        self.assertEqual(
            {profile: self.expected_identity(profile) for profile in repro.PROFILE_ORDER},
            observed,
        )

    def test_symmetric_fully_resigned_foreign_identity_fails_verify_and_compare(self):
        left, right = self.roots()
        for root in (left, right):
            self.attack_identity(root, "report", "f" * 64)
            with self.assertRaisesRegex(repro.ReproError, "project identity"):
                self.verify(root)
        with self.assertRaisesRegex(repro.ReproError, "project identity"):
            self.compare(left, right)

    def test_one_sided_foreign_identity_fails_before_diff(self):
        left, right = self.roots()
        self.attack_identity(left, "report", "e" * 64)
        with self.assertRaisesRegex(repro.ReproError, "project identity"):
            self.verify(left)
        with self.assertRaisesRegex(repro.ReproError, "project identity"):
            self.compare(left, right)

    def test_profile_identity_swap_and_cross_profile_duplicate_fail(self):
        for label, identities in (
            (
                "swap",
                {
                    "report": self.expected_identity("offer"),
                    "offer": self.expected_identity("report"),
                },
            ),
            (
                "duplicate",
                {
                    "report": self.expected_identity("report"),
                    "offer": self.expected_identity("report"),
                },
            ),
        ):
            with self.subTest(label=label):
                left, _ = self.roots()
                for profile, identity in identities.items():
                    self.attack_identity(left, profile, identity)
                with self.assertRaisesRegex(repro.ReproError, "project identity"):
                    self.verify(left)

    def test_summary_only_and_result_only_identity_attacks_fail(self):
        for label, result, summary in (
            ("summary-only", False, True),
            ("result-only", True, False),
        ):
            with self.subTest(label=label):
                left, _ = self.roots()
                self.attack_identity(
                    left, "report", "d" * 64, result=result, summary=summary
                )
                with self.assertRaisesRegex(repro.ReproError, "project identity"):
                    self.verify(left)

    def test_wrong_identity_format_and_case_fail(self):
        for identity in ("A" * 64, "a" * 63, "a" * 65):
            with self.subTest(identity=identity):
                left, _ = self.roots()
                self.attack_identity(left, "report", identity)
                with self.assertRaisesRegex(repro.ReproError, "project identity"):
                    self.verify(left)

    def test_missing_duplicate_and_unknown_run_profiles_fail(self):
        attacks = {
            "missing": lambda run: run["documents"].pop(),
            "duplicate": lambda run: run["documents"].append(
                dict(run["documents"][0])
            ),
            "unknown": lambda run: run["documents"].append(
                {**run["documents"][0], "id": "unknown-report"}
            ),
        }
        for label, mutate in attacks.items():
            with self.subTest(label=label):
                left, _ = self.roots()
                self.attack_run(left, mutate)
                with self.assertRaisesRegex(repro.ReproError, "profile"):
                    self.verify(left)

    def test_productive_or_extended_owner_evidence_fails_when_fully_rebound(self):
        for value in (
            {"approved": True},
            {
                "evidence_origin": "deterministic-e2e-fixture-not-provider-evidence",
                "extra": True,
            },
        ):
            with self.subTest(value=value):
                left, _ = self.roots()
                self.attack_owner_evidence(left, "report", value)
                with self.assertRaisesRegex(repro.ReproError, "owner evidence"):
                    self.verify(left)

    def test_recursive_diff_uses_escaped_json_pointers(self):
        left = {"a/b": {"x~y": 1}}
        right = {"a/b": {"x~y": 2}}
        self.assertEqual(["/a~1b/x~0y"], repro.recursive_json_pointers(left, right))

    def test_worker_allowlist_rejects_foreign_pointer(self):
        with self.assertRaises(repro.ReproError):
            repro.require_allowed(
                ["/duration_seconds", "/network"], {"/duration_seconds"}, "worker evidence"
            )

    def test_result_allowlist_rejects_foreign_pointer(self):
        allowed = {"/hmac_sha256", "/payload/mermaid/0/linux_worker/duration_seconds"}
        with self.assertRaises(repro.ReproError):
            repro.require_allowed(
                ["/payload/output_pdf/sha256"], allowed, "Rich render result"
            )

    def test_stable_profile_hmac_is_not_allowlisted(self):
        inventory = self.inventory_fixture()
        allowlist = repro.inventory_allowlist(inventory)
        self.assertNotIn("/payload/profiles/0/render_result_hmac_sha256", allowlist)
        with self.assertRaises(repro.ReproError):
            repro.require_allowed(
                ["/payload/profiles/0/render_result_hmac_sha256"], allowlist, "root inventory"
            )

    def test_inventory_allowlist_names_only_rich_derived_fields(self):
        allowlist = repro.inventory_allowlist(self.inventory_fixture())
        self.assertEqual(
            {
                "/hmac_sha256",
                "/payload/profiles/4/render_result_sha256",
                "/payload/profiles/4/render_result_hmac_sha256",
                "/payload/run/documents/4/render_result/sha256",
                "/payload/closed_file_inventory/entries/0/sha256",
                "/payload/closed_file_inventory/entries/0/size_bytes",
                "/payload/closed_file_inventory/entries/1/sha256",
                "/payload/closed_file_inventory/entries/1/size_bytes",
            },
            allowlist,
        )

    def test_original_envelope_must_be_hmac_valid(self):
        key = b"k" * 32
        signed = {"schema_version": "render-result.v1", "issuer": "issuer", "payload": {"x": 1}}
        valid = {
            **signed,
            "hmac_sha256": hmac.new(key, repro.canonical(signed), hashlib.sha256).hexdigest(),
        }
        self.assertEqual(valid, repro.signed_envelope(valid, key, "render-result.v1"))
        valid["payload"]["x"] = 2
        with self.assertRaises(repro.ReproError):
            repro.signed_envelope(valid, key, "render-result.v1")

    def test_closed_inventory_matches_pathlib_component_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "report").mkdir()
            (root / "report" / "member.json").write_text("{}\n", encoding="utf-8")
            (root / "report-toggle-off").mkdir()
            (root / "report-toggle-off" / "member.json").write_text("{}\n", encoding="utf-8")
            paths = [item["path"] for item in repro.closed_inventory(root)["entries"]]
            self.assertEqual(
                ["report/member.json", "report-toggle-off/member.json"], paths
            )

    def test_result_normalization_recomputes_valid_hmac(self):
        key = b"k" * 32
        signed = {
            "schema_version": "render-result.v1",
            "issuer": "issuer",
            "payload": {"mermaid": [{"linux_worker": {"duration_seconds": 2.5}}]},
        }
        value = {
            **signed,
            "hmac_sha256": hmac.new(key, repro.canonical(signed), hashlib.sha256).hexdigest(),
        }
        normalized = json.loads(repro.normalize_member(repro.RICH_RESULT, repro.canonical(value), key))
        self.assertEqual(0.0, normalized["payload"]["mermaid"][0]["linux_worker"]["duration_seconds"])
        repro.signed_envelope(normalized, key, "render-result.v1")

    @staticmethod
    def inventory_fixture():
        profiles = [
            {"id": name, "render_result_sha256": "a", "render_result_hmac_sha256": "b"}
            for name in repro.PROFILE_ORDER
        ]
        documents = [
            {"id": name, "render_result": {"sha256": "a"}}
            for name in repro.PROFILE_ORDER[:6]
        ]
        return {
            "schema_version": "brandkit-pdf-e2e-goldens.v3",
            "issuer": "issuer",
            "hmac_sha256": "c",
            "payload": {
                "profiles": profiles,
                "run": {"documents": documents, "report_activation_double_render": []},
                "closed_file_inventory": {
                    "entries": [
                        {"path": repro.RICH_RESULT, "sha256": "d", "size_bytes": 1},
                        {"path": repro.WORKER, "sha256": "e", "size_bytes": 1},
                    ]
                },
            },
        }


if __name__ == "__main__":
    unittest.main()
