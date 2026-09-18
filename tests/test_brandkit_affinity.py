import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest
from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "brandkit-affinity"
TOOLCHAIN = ROOT / "toolchain" / "affinity-codex"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BrandKitAffinityTests(unittest.TestCase):
    def test_contract_routes_affinity_as_optional_fail_closed_adapter(self) -> None:
        contract = json.loads((ROOT / "brandkit-skill-contract.v1.json").read_text(encoding="utf-8"))
        schema = json.loads((ROOT / "schemas/brandkit-skill-contract.v1.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(contract)
        adapter = contract["optional_adapters"]["brandkit-affinity"]
        self.assertNotIn("brandkit-affinity", contract["sequence"])
        self.assertEqual("brandkit-affinity", contract["routing"]["affinity-vector-or-logo-work"])
        self.assertEqual(
            [".brandkit/work/<run-id>/visual-decisions/staging/<document-id>"],
            adapter["mutates"],
        )
        self.assertIn("missing_mutation_receipt", adapter["fails_on"])

    def test_observation_schema_rejects_foreign_origins(self) -> None:
        schema = json.loads((ROOT / "schemas/brandkit-affinity-observation.v1.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        sample = {
            "schema_version": "brandkit-affinity-observation.v1",
            "project_identity_sha256": "a" * 64,
            "run_id": "visual-1",
            "observed_at": 1,
            "expires_at": 2,
            "status": "available",
            "probe": {
                "mode": "read-only",
                "complete": True,
                "inventory_sha256": "b" * 64,
                "providers": [{
                    "server_id": "affinity-native",
                    "origin": "http://127.0.0.1:6767/sse",
                    "version": "1.0.0",
                    "tools": [{"name": "execute_script", "schema_sha256": "c" * 64}],
                }],
            },
            "matching_servers": ["affinity-native"],
            "lock": {"path": "toolchain/affinity.lock", "sha256": "d" * 64, "server_id": "affinity-native"},
            "mutation_capable": True,
            "blocker": None,
        }
        self.assertEqual([], list(validator.iter_errors(sample)))
        sample["probe"]["providers"][0]["origin"] = "http://127.0.0.1.evil.invalid/sse"
        self.assertTrue(list(validator.iter_errors(sample)))

    def test_skill_routes_to_closed_progressive_references(self) -> None:
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: brandkit-affinity", skill)
        self.assertIn("references/setup-and-probe.md", skill)
        self.assertIn("references/vector-workflow.md", skill)
        self.assertTrue((SKILL / "references" / "setup-and-probe.md").is_file())
        self.assertTrue((SKILL / "references" / "vector-workflow.md").is_file())
        self.assertNotIn("disable-model-invocation", skill)

    def test_agent_instructions_keep_branches_and_sources_single(self) -> None:
        skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        setup = (SKILL / "references" / "setup-and-probe.md").read_text(encoding="utf-8")
        vector = (SKILL / "references" / "vector-workflow.md").read_text(encoding="utf-8")
        self.assertIn("Lies nur den Playbook-Pfad", skill)
        self.assertGreaterEqual(setup.count("abgeschlossen, wenn"), 4)
        self.assertGreaterEqual(vector.count("abgeschlossen, wenn"), 6)
        self.assertNotIn("c5097f49fafc47a55855f45b3cb7ac273a34f992", setup)
        self.assertIn("toolchain/affinity-codex/UPSTREAM.json", setup)
        self.assertNotIn("Monitor-/Pfeil", vector)

    def test_vendored_bridge_is_exactly_pinned_and_licensed(self) -> None:
        metadata = json.loads((TOOLCHAIN / "UPSTREAM.json").read_text(encoding="utf-8"))
        self.assertEqual("c5097f49fafc47a55855f45b3cb7ac273a34f992", metadata["commit"])
        self.assertEqual(metadata["source_sha256"], sha256(TOOLCHAIN / "affinity-codex-bridge.mjs"))
        self.assertEqual(
            metadata["vendored_license_sha256"],
            sha256(TOOLCHAIN / "LICENSE.upstream"),
        )
        self.assertEqual(
            "faee45e3a6d72cb5a20cad2849d388b887b2123eb1f5a690d8a09142c05e2390",
            metadata["upstream_license_sha256"],
        )
        self.assertEqual("MIT", metadata["license"])

    def test_project_launcher_injects_affinity_without_global_write(self) -> None:
        launcher = TOOLCHAIN / "codex-project"
        text = launcher.read_text(encoding="utf-8")
        self.assertIn("mcp_servers.affinity.command", text)
        self.assertIn("mcp_servers.affinity.args", text)
        self.assertNotIn(".codex/config.toml", text)
        result = subprocess.run(
            [str(launcher), "mcp", "list"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("affinity", result.stdout)
        self.assertIn("affinity-codex-bridge.mjs", result.stdout)

    def test_project_launcher_rejects_non_loopback_server(self) -> None:
        environment = os.environ.copy()
        environment["AFFINITY_MCP_SSE_URL"] = "https://example.invalid/sse"
        result = subprocess.run(
            [str(TOOLCHAIN / "codex-project"), "--version"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("explicit loopback", result.stderr)


if __name__ == "__main__":
    unittest.main()
