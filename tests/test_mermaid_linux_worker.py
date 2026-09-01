import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mermaid_linux_worker", ROOT / "toolchain/mermaid-svg/linux_worker.py")
worker = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(worker)


class LinuxWorkerPolicyTests(unittest.TestCase):
    def test_real_lock_and_sources_are_closed(self) -> None:
        lock = worker.load_lock()
        self.assertEqual("none", lock["policy"]["network"])
        self.assertTrue(lock["policy"]["read_only_root"])
        self.assertEqual(["ALL"], lock["policy"]["cap_drop"])
        self.assertIn("@sha256:", lock["base_image"]["reference"])
        self.assertIn("@sha256:", lock["node_image"]["reference"])
        self.assertEqual("buildless-image-and-bind-mounts", lock["execution_model"])
        self.assertTrue(Path(lock["runtime_mount"]["path"]).is_dir())
        for key in ("base_image", "node_image"):
            self.assertTrue(Path(lock[key]["archive_path"]).is_file())
            self.assertEqual(lock[key]["archive_sha256"], worker.digest(Path(lock[key]["archive_path"])))

    def test_container_arguments_are_hardened_and_have_no_sandbox_disable_flag(self) -> None:
        lock = worker.load_lock()
        state = {"base_image_reference": lock["base_image"]["digest"],
                 "base_image_id": lock["base_image"]["digest"],
                 "node_image_reference": lock["node_image"]["digest"],
                 "node_image_id": lock["node_image"]["digest"],
                 "runtime_mount": lock["runtime_mount"]["path"]}
        arguments = worker.container_arguments(
            lock, state, "brandkit-mermaid-unit",
            Path("/Volumes/Daten/input"), Path("/Volumes/Daten/output"))
        joined = " ".join(arguments)
        for required in ("--network none", "--read-only", "--user 1000:1000", "--cap-drop ALL",
                         "--security-opt no-new-privileges", "--cpus 1.0", "--memory 768m",
                         "--memory-swap 768m", "--pids-limit 128", "--pull=never"):
            self.assertIn(required, joined)
        self.assertIn(f"type=image,src={lock['node_image']['digest']},dst=/node", joined)
        self.assertIn(f"type=bind,src={lock['runtime_mount']['path']},dst=/runtime,readonly", joined)
        self.assertIn("dst=/brandkit-entrypoint,readonly", joined)
        self.assertIn("--entrypoint /brandkit-entrypoint", joined)
        self.assertNotIn("--no-sandbox", joined)
        self.assertNotIn("--disable-setuid-sandbox", joined)
        pair = " ".join(worker.container_arguments(
            lock, state, "brandkit-mermaid-pair-unit",
            Path("/Volumes/Daten/input"), Path("/Volumes/Daten/output"), "png"))
        self.assertIn("BRANDKIT_OUTPUT_MODE=png", pair)

    def test_worker_files_forbid_sandbox_disable_flags(self) -> None:
        config = json.loads((ROOT / "toolchain/mermaid-svg/LINUX_PUPPETEER_CONFIG.json").read_text())
        self.assertFalse(any(flag in {"--no-sandbox", "--disable-setuid-sandbox"} for flag in config["args"]))
        self.assertFalse((ROOT / "toolchain/mermaid-svg/linux-worker.Dockerfile").exists())
        self.assertFalse((ROOT / "toolchain/mermaid-svg/build-linux-worker").exists())
        self.assertNotIn("docker([\"build\"", (ROOT / "toolchain/mermaid-svg/linux_worker.py").read_text())

    def test_seccomp_is_default_deny_and_namespace_scoped(self) -> None:
        lock = worker.load_lock()
        profile = json.loads((ROOT / lock["policy"]["seccomp_profile"]["path"]).read_text())
        self.assertEqual("SCMP_ACT_ERRNO", profile["defaultAction"])
        allowed = {name for rule in profile["syscalls"] if rule["action"] == "SCMP_ACT_ALLOW"
                   for name in rule["names"]}
        self.assertTrue({"clone3", "setns", "mount"}.isdisjoint(allowed))
        unshare = [rule for rule in profile["syscalls"] if rule["names"] == ["unshare"]]
        self.assertEqual(268435456, unshare[0]["args"][0]["valueTwo"])
        clone = [rule for rule in profile["syscalls"] if rule["names"] == ["clone"]]
        self.assertTrue(all(rule["args"][0]["value"] == 2114060416 for rule in clone))
        self.assertTrue(all(rule["args"][0]["valueTwo"] & 128 == 0 for rule in clone))

    def test_cleanup_refuses_unlabelled_container(self) -> None:
        inspection = worker.subprocess.CompletedProcess([], 0, "someone-else\n", "")
        with patch.object(worker, "docker", return_value=inspection) as docker:
            worker.cleanup_container("brandkit-mermaid-unit")
        docker.assert_called_once()


if __name__ == "__main__":
    unittest.main()
