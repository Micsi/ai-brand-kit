#!/usr/bin/env python3
"""CLI and filesystem regression tests for the architecture contract."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "agent-skill-architecture.v1.json"
VALIDATOR = ROOT / "scripts" / "validate_agent_skill_architecture.py"


class AgentSkillArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def run_cli(self, *args: str, expect_ok: bool) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(VALIDATOR), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if expect_ok and result.returncode != 0:
            self.fail(f"CLI unexpectedly failed:\n{result.stderr}")
        if not expect_ok and result.returncode == 0:
            self.fail(f"CLI unexpectedly passed:\n{result.stdout}")
        return result

    def mutated_cli(self, mutation, *, expect_ok: bool = False) -> subprocess.CompletedProcess[str]:
        candidate = copy.deepcopy(self.document)
        mutation(candidate)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(json.dumps(candidate), encoding="utf-8")
            return self.run_cli(str(path), expect_ok=expect_ok)

    @staticmethod
    def write_skill(path: Path, name: str, *, valid_frontmatter: bool = True) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if valid_frontmatter:
            content = f"---\nname: {name}\ndescription: Fixture\n---\n\n# {name}\n"
        else:
            content = "# Missing frontmatter name\n"
        (path / "SKILL.md").write_text(content, encoding="utf-8")

    def make_standard_fixture(self, base: Path, project_name: str = "project") -> tuple[Path, Path, Path]:
        project = base / project_name
        global_root = base / "home" / ".agents" / "skills"
        claude_global = base / "home" / ".claude" / "skills"
        project.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        self.write_skill(global_root / "global-skill", "global-skill")
        self.write_skill(project / ".agents" / "skills" / "project-skill", "project-skill")
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "skills").symlink_to("../.agents/skills")
        claude_global.mkdir(parents=True, exist_ok=True)
        claude_bridge = claude_global / "global-skill"
        if not claude_bridge.exists() and not claude_bridge.is_symlink():
            claude_bridge.symlink_to("../../.agents/skills/global-skill")
        return project, global_root, claude_global

    def make_llmwiki_fixture(self, base: Path) -> tuple[Path, Path]:
        project = base / "wiki-project"
        global_root = base / "home" / ".agents" / "skills"
        project.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        self.write_skill(global_root / "global-skill", "global-skill")
        self.write_skill(project / ".claude" / "skills" / "wiki-skill", "wiki-skill")
        (project / ".agents").mkdir(parents=True)
        (project / ".codex").mkdir(parents=True)
        (project / ".agents" / "skills").symlink_to("../.claude/skills")
        (project / ".codex" / "skills").symlink_to("../.claude/skills")
        return project, global_root

    def preflight_args(
        self, project: Path, global_root: Path, profile: str = "standard", claude_global: Path | None = None
    ) -> list[str]:
        args = [
            "--preflight-root", str(project), "--profile", profile,
            "--global-root", str(global_root),
            "--hermes-external-dir", str(global_root),
            "--hermes-trusted-project-dir", str(project),
        ]
        if claude_global is not None:
            args.extend(["--claude-global-root", str(claude_global)])
        return args

    @staticmethod
    def tree_hash(path: Path) -> str:
        digest = hashlib.sha256()
        for item in sorted(path.rglob("*"), key=lambda value: value.relative_to(path).as_posix()):
            relative = item.relative_to(path).as_posix().encode()
            if item.is_file():
                digest.update(b"F\0" + relative + b"\0" + item.read_bytes() + b"\0")
            elif item.is_dir():
                digest.update(b"D\0" + relative + b"\0")
        return digest.hexdigest()

    def test_spacing_has_one_identity_across_all_provider_discovery_roots(self) -> None:
        spacing = [item for item in self.document["skill_portfolio"] if item["name"] == "brandkit-spacing"]
        self.assertEqual(1, len(spacing))
        self.assertEqual("skills/brandkit-spacing", spacing[0]["source"])
        self.assertEqual("global", spacing[0]["scope"])
        self.assertEqual("model-invoked", spacing[0]["activation"])
        providers = self.document["providers"]
        self.assertEqual("~/.agents/skills", providers["codex"]["global_discovery"])
        self.assertEqual("~/.agents/skills/<skill-name>", providers["claude"]["global_bridge_target"])
        self.assertEqual("~/.agents/skills", providers["hermes"]["global_bridge_target"])
        self.assertEqual("fail-closed", self.document["collision_policy"]["mode"])

    def materialize_project_state(self, base: Path, project_name: str) -> tuple[Path, Path, dict, dict]:
        project, _, _ = self.make_standard_fixture(base, project_name)
        release_root = base / "immutable-releases"
        engine = release_root / "brandkit-engine" / "1.0.0"
        brand = release_root / "brands" / "same-brand" / "3.2.1"
        engine.mkdir(parents=True, exist_ok=True)
        brand.mkdir(parents=True, exist_ok=True)
        (engine / "engine.txt").write_text("engine-v1\n", encoding="utf-8")
        (brand / "brand.json").write_text('{"id":"same-brand"}\n', encoding="utf-8")
        activation = {
            "brands": [{"id": "same-brand", "version": "3.2.1"}],
            "engine_version": "1.0.0",
            "run_id": "run-1",
        }
        lock = {"pins": [
            {"name": "brandkit-engine", "version": "1.0.0", "source_locator": f"snapshot:engine.txt@sha256:{self.tree_hash(engine)}",
             "content_sha256": self.tree_hash(engine), "path": str(engine)},
            {"name": "brand:same-brand", "version": "3.2.1", "source_locator": f"snapshot:brand.json@sha256:{self.tree_hash(brand)}",
             "content_sha256": self.tree_hash(brand), "path": str(brand)},
        ]}
        state = project / ".brandkit"
        state.mkdir(exist_ok=True)
        (state / "project.json").write_text(json.dumps(activation, sort_keys=True), encoding="utf-8")
        (state / "skill-lock.json").write_text(json.dumps(lock, sort_keys=True), encoding="utf-8")
        root_hash = hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:16]
        for path in (
            state / "work" / "same-brand" / "run-1",
            state / "cache" / root_hash / "same-brand" / "3.2.1" / "1.0.0",
            project / "output" / "same-brand" / "run-1",
        ):
            path.mkdir(parents=True)
            (path / f"{project_name}.marker").write_text(project_name, encoding="utf-8")
        return project, release_root, activation, lock

    def project_state_args(self, project: Path, release_root: Path) -> list[str]:
        return ["--validate-project-state", str(project), "--release-root", str(release_root)]

    def test_committed_contract_and_draft_2020_12_schema_are_valid(self) -> None:
        self.run_cli(expect_ok=True)

    def test_all_normative_blocks_are_required_and_closed(self) -> None:
        blocks = [
            "canonical_roots", "providers", "llmwiki_profile", "collision_policy",
            "trust_policy", "content_zones", "isolation", "versioning",
        ]
        for block in blocks:
            with self.subTest(block=block):
                result = self.mutated_cli(lambda doc, key=block: doc.__setitem__(key, {}))
                self.assertIn("schema", result.stderr)

        result = self.mutated_cli(lambda doc: doc["trust_policy"].__setitem__("unknown", True))
        self.assertIn("Additional properties", result.stderr)

    def test_required_contract_fields_reject_review_mutations(self) -> None:
        mutations = {
            "trust command": lambda d: d["trust_policy"].pop("hermes_project_trust_command"),
            "rollback": lambda d: d["versioning"].pop("rollback_strategy"),
            "activation": lambda d: d["isolation"].pop("activation_file"),
            "work root": lambda d: d["isolation"].pop("work_root_template"),
            "output root": lambda d: d["isolation"].pop("output_root_template"),
            "LLMWiki applies": lambda d: d["llmwiki_profile"].pop("applies_when"),
            "LLMWiki content": lambda d: d["llmwiki_profile"].pop("content_rule"),
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                self.assertIn("required", self.mutated_cli(mutation).stderr)

    def test_content_zone_mutations_fail_closed(self) -> None:
        mutations = [
            lambda d: d["content_zones"]["brand_package"].__setitem__("allowed", []),
            lambda d: d["content_zones"]["brand_package"]["allowed"].append("renderer fork"),
            lambda d: d["content_zones"]["global_engine"]["forbidden"].remove("brand assets"),
            lambda d: d["content_zones"]["commercial_project"]["forbidden"].remove("cross-project output paths"),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.mutated_cli(mutation)

    def test_isolation_pin_and_rollback_mutations_fail_closed(self) -> None:
        mutations = [
            lambda d: d["isolation"]["cache_key_fields"].remove("project-root-realpath-hash"),
            lambda d: d["isolation"].__setitem__("exactly_one_brand_per_run", False),
            lambda d: d["isolation"].__setitem__("work_root_template", d["isolation"]["output_root_template"]),
            lambda d: d["versioning"]["required_pin_fields"].remove("content_sha256"),
            lambda d: d["versioning"].pop("source_locator_format"),
            lambda d: d["versioning"].__setitem__("floating_versions_allowed", True),
            lambda d: d["versioning"].__setitem__("retain_previous_release", False),
            lambda d: d["versioning"].pop("rollback_strategy"),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.mutated_cli(mutation)

    def test_standard_relative_bridge_resolves_inside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, global_root, claude_global = self.make_standard_fixture(Path(temporary))
            bridge = project / ".claude" / "skills"
            self.assertEqual("../.agents/skills", os.readlink(bridge))
            self.assertEqual((project / ".agents" / "skills").resolve(), bridge.resolve(strict=True))
            self.run_cli(*self.preflight_args(project, global_root, claude_global=claude_global), expect_ok=True)

    def test_llmwiki_relative_bridges_resolve_to_the_same_facade(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, global_root = self.make_llmwiki_fixture(Path(temporary))
            facade = (project / ".claude" / "skills").resolve()
            for bridge in (project / ".agents" / "skills", project / ".codex" / "skills"):
                self.assertEqual("../.claude/skills", os.readlink(bridge))
                self.assertEqual(facade, bridge.resolve(strict=True))
            self.run_cli(*self.preflight_args(project, global_root, "llmwiki"), expect_ok=True)

    def test_dead_cyclic_wrong_and_out_of_allowlist_links_are_rejected(self) -> None:
        cases = ("dead", "cyclic", "wrong", "outside")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                project, global_root, _ = self.make_standard_fixture(base)
                bridge = project / ".claude" / "skills"
                bridge.unlink()
                if case == "dead":
                    bridge.symlink_to("../missing/skills")
                elif case == "cyclic":
                    bridge.symlink_to("../.claude/skills")
                elif case == "wrong":
                    (project / "wrong").mkdir()
                    bridge.symlink_to("../wrong")
                else:
                    bridge.symlink_to("../.agents/skills")
                    outside = base / "outside" / "escaped"
                    self.write_skill(outside, "escaped")
                    (project / ".agents" / "skills" / "escaped").symlink_to(outside)
                self.run_cli(*self.preflight_args(project, global_root), expect_ok=False)

    def test_frontmatter_identity_and_distinct_realpath_collision_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, global_root, _ = self.make_standard_fixture(base)
            self.write_skill(project / ".agents" / "skills" / "bad", "bad", valid_frontmatter=False)
            result = self.run_cli(*self.preflight_args(project, global_root), expect_ok=False)
            self.assertIn("frontmatter.name", result.stderr)

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, global_root, _ = self.make_standard_fixture(base)
            self.write_skill(project / ".agents" / "skills" / "fork", "global-skill")
            result = self.run_cli(*self.preflight_args(project, global_root), expect_ok=False)
            self.assertIn("skill name collision", result.stderr)

    def test_frontmatter_is_complete_unique_scalar_and_valid_yaml(self) -> None:
        fixtures = {
            "missing-close": "---\nname: broken\ndescription: never closed\n",
            "duplicate-name": "---\nname: first\nname: second\n---\n",
            "invalid-yaml": "---\nname: [broken\ndescription: nope\n---\n",
            "non-scalar-name": "---\nname: [broken]\n---\n",
        }
        for label, content in fixtures.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                project, global_root, _ = self.make_standard_fixture(Path(temporary))
                broken = project / ".agents" / "skills" / "broken"
                broken.mkdir()
                (broken / "SKILL.md").write_text(content, encoding="utf-8")
                self.assertIn("frontmatter", self.run_cli(*self.preflight_args(project, global_root), expect_ok=False).stderr)

        with tempfile.TemporaryDirectory() as temporary:
            project, global_root, _ = self.make_standard_fixture(Path(temporary))
            skill = project / ".agents" / "skills" / "multiline"
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "---\nname: multiline\ndescription: >\n  valid multiline\n  description\ntags:\n  - fixture\n---\n# OK\n",
                encoding="utf-8",
            )
            self.run_cli(*self.preflight_args(project, global_root), expect_ok=True)

    def test_each_skill_package_symlink_type_is_allowlisted(self) -> None:
        for kind, relative in {
            "SKILL.md": Path("SKILL.md"),
            "script": Path("scripts/run.py"),
            "hook": Path("hooks/preflight.sh"),
            "reference": Path("references/policy.md"),
        }.items():
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                project, global_root, _ = self.make_standard_fixture(base)
                skill = project / ".agents" / "skills" / "project-skill"
                outside = base / "outside" / kind.replace(".", "-")
                outside.parent.mkdir(parents=True, exist_ok=True)
                outside.write_text("external\n", encoding="utf-8")
                target = skill / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target.unlink()
                target.symlink_to(outside)
                result = self.run_cli(*self.preflight_args(project, global_root), expect_ok=False)
                self.assertIn("outside allowlisted roots", result.stderr)

    def test_dead_and_cyclic_package_links_fail_but_release_aliases_pass(self) -> None:
        for kind in ("dead", "cyclic"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                project, global_root, _ = self.make_standard_fixture(Path(temporary))
                scripts = project / ".agents" / "skills" / "project-skill" / "scripts"
                scripts.mkdir()
                link = scripts / "alias"
                link.symlink_to("missing" if kind == "dead" else "alias")
                self.assertIn("dead or cyclic", self.run_cli(*self.preflight_args(project, global_root), expect_ok=False).stderr)

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, global_root, _ = self.make_standard_fixture(base)
            release_root = base / "releases"
            release = release_root / "project-skill" / "1.0.0"
            self.write_skill(release, "project-skill")
            for relative, content in (("scripts/run.py", "pass\n"), ("hooks/preflight.sh", "true\n"),
                                      ("references/policy.md", "allowed\n")):
                target = release / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            project_skill = project / ".agents" / "skills" / "project-skill"
            for child in list(project_skill.iterdir()):
                child.unlink()
            (project_skill / "SKILL.md").symlink_to(release / "SKILL.md")
            for directory in ("scripts", "hooks", "references"):
                (project_skill / directory).symlink_to(release / directory)
            (project_skill / "scripts-alias").symlink_to(release / "scripts")
            args = self.preflight_args(project, global_root) + ["--allow-root", str(release_root)]
            self.run_cli(*args, expect_ok=True)

    def test_package_directory_ancestor_cycles_are_rejected(self) -> None:
        for kind in ("child-to-parent", "multi-stage"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                project, global_root, _ = self.make_standard_fixture(Path(temporary))
                skill = project / ".agents" / "skills" / "project-skill"
                if kind == "child-to-parent":
                    child = skill / "scripts"
                    child.mkdir()
                    (child / "back-to-skill").symlink_to("..")
                else:
                    a = skill / "a"
                    b = skill / "b"
                    a.mkdir()
                    b.mkdir()
                    (a / "to-b").symlink_to("../b")
                    (b / "to-a").symlink_to("../a")
                result = self.run_cli(*self.preflight_args(project, global_root), expect_ok=False)
                self.assertIn("ancestor cycle", result.stderr)

    def test_same_name_same_realpath_alias_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, global_root, claude_global = self.make_standard_fixture(Path(temporary))
            self.run_cli(*self.preflight_args(project, global_root, claude_global=claude_global), expect_ok=True)

    def test_hermes_planned_and_actual_state_are_separate_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, global_root, _ = self.make_standard_fixture(base)
            missing_external = self.preflight_args(project, global_root)
            index = missing_external.index("--hermes-external-dir")
            del missing_external[index:index + 2]
            self.assertIn("external_dirs", self.run_cli(*missing_external, expect_ok=False).stderr)

            wrong_trust = self.preflight_args(project, global_root)
            index = wrong_trust.index("--hermes-trusted-project-dir")
            wrong_trust[index + 1] = str(base)
            self.assertIn("exact project Git root", self.run_cli(*wrong_trust, expect_ok=False).stderr)

            config = base / "hermes.yaml"
            config.write_text(
                "skills:\n  external_dirs:\n    - " + str(global_root) +
                "\n  trusted_project_dirs:\n    - " + str(project) + "\n",
                encoding="utf-8",
            )
            actual = ["--preflight-root", str(project), "--profile", "standard", "--global-root", str(global_root),
                      "--hermes-mode", "actual", "--hermes-state-file", str(config)]
            self.run_cli(*actual, expect_ok=True)
            self.run_cli(*(actual + ["--hermes-trusted-project-dir", str(project)]), expect_ok=False)
            config.write_text("skills:\n  external_dirs: []\n  trusted_project_dirs: []\n", encoding="utf-8")
            result = self.run_cli(*actual, expect_ok=False)
            self.assertIn("external_dirs", result.stderr)
            self.assertIn("exact project Git root", result.stderr)

    def test_two_real_git_projects_materialize_isolated_brand_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project_a, release_root, _, _ = self.materialize_project_state(base, "project-a")
            project_b, _, _, _ = self.materialize_project_state(base, "project-b")
            self.run_cli(*self.project_state_args(project_a, release_root), expect_ok=True)
            self.run_cli(*self.project_state_args(project_b, release_root), expect_ok=True)
            for zone in (".brandkit/work", ".brandkit/cache", "output"):
                files_a = {path.name for path in (project_a / zone).rglob("*.marker")}
                files_b = {path.name for path in (project_b / zone).rglob("*.marker")}
                self.assertEqual({"project-a.marker"}, files_a)
                self.assertEqual({"project-b.marker"}, files_b)
                self.assertTrue(files_a.isdisjoint(files_b))

    def test_project_pin_failures_are_rejected_by_cli(self) -> None:
        mutations = ("missing", "movable", "hash", "ambiguous")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                project, release_root, _, lock = self.materialize_project_state(Path(temporary), "project")
                if mutation == "missing":
                    lock["pins"][0].pop("source_locator")
                elif mutation == "movable":
                    lock["pins"][0]["version"] = "latest"
                elif mutation == "hash":
                    lock["pins"][0]["content_sha256"] = "0" * 64
                else:
                    lock["pins"].append(copy.deepcopy(lock["pins"][0]))
                (project / ".brandkit" / "skill-lock.json").write_text(json.dumps(lock), encoding="utf-8")
                self.run_cli(*self.project_state_args(project, release_root), expect_ok=False)

    def test_source_locator_requires_matching_content_addressed_snapshot(self) -> None:
        for source_path in ("engine.py", "nested/engine.py"):
            with self.subTest(source_path=source_path), tempfile.TemporaryDirectory() as temporary:
                project, release_root, _, lock = self.materialize_project_state(Path(temporary), "project")
                for pin in lock["pins"]:
                    pin["source_locator"] = f"snapshot:{source_path}@sha256:{pin['content_sha256']}"
                (project / ".brandkit" / "skill-lock.json").write_text(json.dumps(lock), encoding="utf-8")
                self.run_cli(*self.project_state_args(project, release_root), expect_ok=True)

        for locator in ("main", "latest", "snapshot:engine.py@sha256:" + "0" * 64, "snapshot:../engine.py@sha256:" + "0" * 64):
            with self.subTest(locator=locator), tempfile.TemporaryDirectory() as temporary:
                project, release_root, _, lock = self.materialize_project_state(Path(temporary), "project")
                lock["pins"][0]["source_locator"] = locator
                (project / ".brandkit" / "skill-lock.json").write_text(json.dumps(lock), encoding="utf-8")
                result = self.run_cli(*self.project_state_args(project, release_root), expect_ok=False)
                self.assertIn("content-addressed snapshot", result.stderr)

    def test_atomic_release_switch_and_failed_smoke_roll_back_byte_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project, release_root, _, lock_v1 = self.materialize_project_state(base, "project")
            v1 = release_root / "brandkit-engine" / "1.0.0"
            v2 = release_root / "brandkit-engine" / "2.0.0"
            v2.mkdir(parents=True)
            (v2 / "engine.txt").write_text("SMOKE_FAIL\n", encoding="utf-8")
            active = base / "active" / "brandkit-engine"
            active.parent.mkdir()
            active.symlink_to(v1)
            lock_path = project / ".brandkit" / "skill-lock.json"
            lock_v1_bytes = lock_path.read_bytes()
            v1_real = active.resolve(strict=True)

            lock_v2 = copy.deepcopy(lock_v1)
            v2_hash = self.tree_hash(v2)
            lock_v2["pins"][0].update(version="2.0.0", source_locator=f"snapshot:engine.txt@sha256:{v2_hash}",
                                       content_sha256=v2_hash, path=str(v2))
            lock_path.write_text(json.dumps(lock_v2, sort_keys=True), encoding="utf-8")
            replacement = active.with_name(".brandkit-engine.next")
            replacement.symlink_to(v2)
            os.replace(replacement, active)
            smoke = subprocess.run([sys.executable, "-c", "import sys; sys.exit('SMOKE_FAIL' in open(sys.argv[1]).read())",
                                    str(active / "engine.txt")], check=False)
            self.assertNotEqual(0, smoke.returncode)

            restore = active.with_name(".brandkit-engine.rollback")
            restore.symlink_to(v1)
            os.replace(restore, active)
            lock_path.write_bytes(lock_v1_bytes)
            self.assertEqual(lock_v1_bytes, lock_path.read_bytes())
            self.assertEqual(v1_real, active.resolve(strict=True))
            self.run_cli(*self.project_state_args(project, release_root), expect_ok=True)


if __name__ == "__main__":
    unittest.main()
