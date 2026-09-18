import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import validate_visual_decision_sources as validator  # noqa: E402


class VisualDecisionSourceContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sidecar = Path(self.temp.name)
        self.root = self.sidecar / "code" / "ai-brand-kit"
        for relative in (
            "visual-decision-sources.v1.json",
            "impeccable-adapter.v1.json",
            "schemas/visual-decision-sources.v1.schema.json",
            "schemas/reputation-evidence.v1.schema.json",
        ):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        captures = self.root / "trust" / "visual-decisions" / "reputation"
        captures.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ROOT / "trust" / "visual-decisions" / "reputation", captures)
        narrative = self.sidecar / "quality-loop" / "evidence" / "base-visual-decisions-source-review-r1.md"
        narrative.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT.parents[1] / "quality-loop" / "evidence" / narrative.name, narrative)

    def tearDown(self):
        self.temp.cleanup()

    def load_source(self):
        return json.loads((self.root / validator.SOURCE).read_text())

    def write_source(self, value):
        (self.root / validator.SOURCE).write_text(json.dumps(value, indent=2) + "\n")

    def rewrite_capture(self, source, index, mutate):
        signal = source["candidates"][0]["reputation_signals"][index]
        path = self.root / signal["path"]
        capture = json.loads(path.read_text())
        mutate(capture, signal)
        path.write_text(json.dumps(capture, indent=2) + "\n")
        signal["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()

    def errors(self):
        return validator.validate_visual_decision_sources(self.root, self.sidecar)

    def test_repository_contract_passes(self):
        self.assertEqual([], self.errors())

    def test_rejects_duplicate_selected_candidate(self):
        source = self.load_source()
        source["candidates"].append(copy.deepcopy(source["candidates"][0]))
        self.write_source(source)
        self.assertTrue(any("exactly one" in error or "unique" in error for error in self.errors()))

    def test_rejects_same_domain_identity_and_family(self):
        source = self.load_source()

        def mutate(capture, signal):
            changes = {
                "source_url": "https://github.com/pbakaus/impeccable",
                "registrable_domain": "github.com",
                "publisher_identity": "GitHub, Inc.",
                "publisher_family": "github",
            }
            capture.update(changes)
            signal.update(changes)

        self.rewrite_capture(source, 1, mutate)
        self.write_source(source)
        errors = "\n".join(self.errors())
        self.assertIn("duplicate registrable_domain", errors)
        self.assertIn("duplicate publisher_identity", errors)
        self.assertIn("duplicate publisher_family", errors)

    def test_rejects_duplicate_signal(self):
        source = self.load_source()
        source["candidates"][0]["reputation_signals"].append(
            copy.deepcopy(source["candidates"][0]["reputation_signals"][0])
        )
        self.write_source(source)
        self.assertNotEqual([], self.errors())

    def test_rejects_capture_hash_drift(self):
        source = self.load_source()
        source["candidates"][0]["reputation_signals"][0]["sha256"] = "0" * 64
        self.write_source(source)
        self.assertTrue(any("SHA-256 mismatch" in error for error in self.errors()))

    def test_rejects_capture_schema_extra_field(self):
        source = self.load_source()
        self.rewrite_capture(source, 0, lambda capture, _signal: capture.update({"unbound": True}))
        self.write_source(source)
        self.assertTrue(any("Additional properties" in error for error in self.errors()))

    def test_rejects_descriptor_capture_field_drift(self):
        cases = {
            "publisher_identity": "Different Publisher",
            "retrieved_at": "2026-09-02",
            "observed_claim": "A different sufficiently long reputation claim for the mutation test.",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                source = self.load_source()
                self.rewrite_capture(source, 1, lambda capture, _signal, f=field, v=value: capture.update({f: v}))
                self.write_source(source)
                self.assertTrue(any(f"{field} differs" in error for error in self.errors()))
                shutil.copy2(ROOT / validator.SOURCE, self.root / validator.SOURCE)
                shutil.copytree(
                    ROOT / "trust" / "visual-decisions" / "reputation",
                    self.root / "trust" / "visual-decisions" / "reputation",
                    dirs_exist_ok=True,
                )

    def test_rejects_url_outside_declared_domain(self):
        source = self.load_source()

        def mutate(capture, signal):
            capture["source_url"] = "https://example.invalid/review"
            signal["source_url"] = capture["source_url"]

        self.rewrite_capture(source, 2, mutate)
        self.write_source(source)
        self.assertTrue(any("reviewed host map" in error for error in self.errors()))

    def test_rejects_api_github_subdomain_independence_bypass(self):
        source = self.load_source()

        def mutate(capture, signal):
            changes = {
                "source_url": "https://api.github.com/repos/pbakaus/impeccable",
                "registrable_domain": "api.github.com",
                "publisher_identity": "GitHub API",
                "publisher_family": "github-api",
            }
            capture.update(changes)
            signal.update(changes)

        self.rewrite_capture(source, 1, mutate)
        self.write_source(source)
        self.assertTrue(any("reviewed URL base domain" in error for error in self.errors()))

    def test_rejects_duplicate_json_key_in_capture(self):
        source = self.load_source()
        signal = source["candidates"][0]["reputation_signals"][0]
        path = self.root / signal["path"]
        text = path.read_text()
        text = text.replace(
            '  "scope": "reputation-only-not-security-approval"',
            '  "scope": "reputation-only-not-security-approval",\n'
            '  "scope": "reputation-only-not-security-approval"',
        )
        path.write_text(text)
        signal["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.write_source(source)
        self.assertTrue(any("duplicate JSON object key" in error for error in self.errors()))

    def test_rejects_non_finite_json_number_in_capture(self):
        source = self.load_source()
        signal = source["candidates"][0]["reputation_signals"][0]
        path = self.root / signal["path"]
        text = path.read_text().replace(
            '  "sentiment": "positive",',
            '  "sentiment": "positive",\n  "non_finite_probe": NaN,',
        )
        path.write_text(text)
        signal["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.write_source(source)
        self.assertTrue(any("non-finite JSON number" in error for error in self.errors()))

    def test_rejects_capture_path_escape(self):
        source = self.load_source()
        source["candidates"][0]["reputation_signals"][0]["path"] = "../outside.json"
        self.write_source(source)
        self.assertNotEqual([], self.errors())

    def test_rejects_symlinked_capture_component(self):
        source = self.load_source()
        signal = source["candidates"][0]["reputation_signals"][0]
        original = self.root / signal["path"]
        outside = self.sidecar / "outside-capture.json"
        shutil.copy2(original, outside)
        original.unlink()
        original.symlink_to(outside)
        self.assertTrue(any("symlink" in error for error in self.errors()))

    def test_rejects_adapter_pin_drift(self):
        source = self.load_source()
        source["candidates"][0]["selected_pin"]["commit"] = "0" * 40
        self.write_source(source)
        self.assertNotEqual([], self.errors())


if __name__ == "__main__":
    unittest.main()
