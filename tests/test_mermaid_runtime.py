import base64
import hashlib
import importlib.util
import inspect
import io
import json
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mermaid_runtime", ROOT / "toolchain/mermaid-svg/runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runtime)


class ProcessGroupInspectionTests(unittest.TestCase):
    def test_esrch_is_empty_without_ps(self) -> None:
        with patch.object(runtime.os, "killpg", side_effect=ProcessLookupError), patch.object(runtime.subprocess, "run") as run:
            self.assertEqual(runtime.PROCESS_GROUP_EMPTY, runtime.process_group_state(41))
            run.assert_not_called()

    def test_eperm_is_exists_when_bounded_ps_confirms_membership(self) -> None:
        snapshot = subprocess.CompletedProcess([], 0, " 100  41\n 101 42\n", "")
        with patch.object(runtime.os, "killpg", side_effect=PermissionError), patch.object(runtime.subprocess, "run", return_value=snapshot) as run:
            self.assertEqual(runtime.PROCESS_GROUP_EXISTS, runtime.process_group_state(41))
            self.assertEqual(runtime.PS_SNAPSHOT_TIMEOUT, run.call_args.kwargs["timeout"])

    def test_eperm_without_visible_member_is_unknown_not_empty(self) -> None:
        snapshot = subprocess.CompletedProcess([], 0, " 101 42\n", "")
        with patch.object(runtime.os, "killpg", side_effect=PermissionError), patch.object(runtime.subprocess, "run", return_value=snapshot):
            self.assertEqual(runtime.PROCESS_GROUP_UNKNOWN, runtime.process_group_state(41))

    def test_ps_failure_is_unknown(self) -> None:
        snapshot = subprocess.CompletedProcess([], 1, "", "")
        with patch.object(runtime.os, "killpg", return_value=None), patch.object(runtime.subprocess, "run", return_value=snapshot):
            self.assertEqual(runtime.PROCESS_GROUP_UNKNOWN, runtime.process_group_state(41))

    def test_successful_probe_without_visible_member_is_unknown_not_empty(self) -> None:
        snapshot = subprocess.CompletedProcess([], 0, " 101 42\n", "")
        with patch.object(runtime.os, "killpg", return_value=None), patch.object(runtime.subprocess, "run", return_value=snapshot):
            self.assertEqual(runtime.PROCESS_GROUP_UNKNOWN, runtime.process_group_state(41))

    def test_term_kill_and_leader_reap_are_deterministic(self) -> None:
        proc = Mock()
        states = [runtime.PROCESS_GROUP_EXISTS, runtime.PROCESS_GROUP_EXISTS, runtime.PROCESS_GROUP_EXISTS,
                  runtime.PROCESS_GROUP_EMPTY, runtime.PROCESS_GROUP_EMPTY]
        with patch.object(runtime, "process_group_state", side_effect=states), patch.object(runtime.os, "killpg") as killpg:
            self.assertEqual((True, True), runtime.terminate_group(41, proc, grace=0))
        self.assertEqual([call(41, runtime.signal.SIGTERM), call(41, runtime.signal.SIGKILL)], killpg.call_args_list)
        proc.wait.assert_called_once_with(timeout=0)

    def test_term_eperm_is_a_cleanup_failure_not_absence(self) -> None:
        proc = Mock()
        with patch.object(runtime, "process_group_state", return_value=runtime.PROCESS_GROUP_EXISTS), \
             patch.object(runtime.os, "killpg", side_effect=PermissionError):
            with self.assertRaisesRegex(runtime.RuntimeFailure, "permission denied sending SIGTERM"):
                runtime.terminate_group(41, proc, grace=0)
        proc.wait.assert_not_called()

    def test_already_ended_leader_is_reaped_without_signals(self) -> None:
        proc = Mock()
        with patch.object(runtime, "process_group_state", return_value=runtime.PROCESS_GROUP_EMPTY), \
             patch.object(runtime.os, "killpg") as killpg:
            self.assertEqual((False, False), runtime.terminate_group(41, proc, grace=0))
        killpg.assert_not_called()
        proc.wait.assert_called_once_with(timeout=0)


class ProcessPhasePrimaryErrorTests(unittest.TestCase):
    def _phase(self, wait_error: BaseException) -> tuple[BaseException, dict]:
        proc = Mock(pid=123, returncode=None)
        proc.wait.side_effect = wait_error
        cleanup = runtime.RuntimeFailure("cleanup evidence failed")
        with tempfile.TemporaryDirectory(dir="/Volumes/Daten/Projekte/BrandKit/.runtime/tmp") as temporary:
            log_dir = Path(temporary)
            with patch.object(runtime.subprocess, "Popen", return_value=proc), patch.object(runtime.os, "getpgid", return_value=123), \
                 patch.object(runtime, "terminate_group", side_effect=cleanup):
                with self.assertRaises(BaseException) as raised:
                    runtime.process_phase("unit", "mmdc", ["fake"], log_dir, timeout=0.01)
            events = [json.loads(line) for line in (log_dir / "unit.jsonl").read_text().splitlines()]
        return raised.exception, events[-1]

    def test_timeout_remains_primary_when_cleanup_fails(self) -> None:
        error, end = self._phase(subprocess.TimeoutExpired(["fake"], 0.01))
        self.assertIsInstance(error, runtime.RuntimeFailure)
        self.assertEqual("Mermaid phase timed out: mmdc", str(error))
        self.assertTrue(any("secondary process-group cleanup error" in note for note in error.__notes__))
        self.assertTrue(end["timeout"])
        self.assertEqual("process-group-cleanup-failed", end["cleanup"])
        self.assertIn("cleanup evidence failed", end["cleanup_error"])

    def test_arbitrary_wait_exception_remains_primary_when_cleanup_fails(self) -> None:
        primary = ValueError("primary wait failure")
        error, end = self._phase(primary)
        self.assertIs(error, primary)
        self.assertEqual("process-group-cleanup-failed", end["cleanup"])
        self.assertTrue(any("secondary process-group cleanup error" in note for note in error.__notes__))

    def test_timeout_remains_primary_when_end_event_write_fails(self) -> None:
        proc = Mock(pid=123, returncode=None)
        proc.wait.side_effect = subprocess.TimeoutExpired(["fake"], 0.01)
        writes = 0

        def fail_second_write(_path: Path, _event: dict) -> None:
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("end event unavailable")

        with tempfile.TemporaryDirectory(dir="/Volumes/Daten/Projekte/BrandKit/.runtime/tmp") as temporary:
            with patch.object(runtime.subprocess, "Popen", return_value=proc), \
                 patch.object(runtime.os, "getpgid", return_value=123), \
                 patch.object(runtime, "terminate_group", return_value=(True, True)), \
                 patch.object(runtime, "log_event", side_effect=fail_second_write):
                with self.assertRaises(runtime.RuntimeFailure) as raised:
                    runtime.process_phase("unit", "mmdc", ["fake"], Path(temporary), timeout=0.01)
        self.assertEqual("Mermaid phase timed out: mmdc", str(raised.exception))
        self.assertTrue(any("secondary end-event log error" in note for note in raised.exception.__notes__))

    def test_real_short_timeout_reaps_process_group(self) -> None:
        command = ["/bin/sleep", "30"]
        started = time.monotonic()
        with tempfile.TemporaryDirectory(dir="/Volumes/Daten/Projekte/BrandKit/.runtime/tmp") as temporary:
            log_dir = Path(temporary)
            with self.assertRaisesRegex(runtime.RuntimeFailure, "Mermaid phase timed out: mmdc"):
                runtime.process_phase("real-timeout", "mmdc", command, log_dir, timeout=0.05)
            events = [json.loads(line) for line in (log_dir / "real-timeout.jsonl").read_text().splitlines()]
        self.assertLess(time.monotonic() - started, 6.0)
        self.assertEqual("end", events[-1]["event"])
        self.assertTrue(events[-1]["timeout"])
        self.assertTrue(events[-1]["term_sent"])
        self.assertFalse(events[-1]["kill_sent"])
        self.assertEqual("process-group-reaped", events[-1]["cleanup"])

    def test_nonzero_exit_preserves_code_and_bounded_stderr_cause(self) -> None:
        command = [str(runtime.PYTHON), "-c", "import sys; print('chrome-first-cause', file=sys.stderr); raise SystemExit(42)"]
        with tempfile.TemporaryDirectory(dir="/Volumes/Daten/Projekte/BrandKit/.runtime/tmp") as temporary:
            log_dir = Path(temporary)
            with self.assertRaises(runtime.RuntimeFailure) as raised:
                runtime.process_phase("real-failure", "chrome", command, log_dir, timeout=5)
            events = [json.loads(line) for line in (log_dir / "real-failure.jsonl").read_text().splitlines()]
            self.assertEqual("chrome-first-cause\n", (log_dir / "real-failure-chrome.stderr").read_text())
        self.assertIn("chrome (exit 42)", str(raised.exception))
        self.assertIn("chrome-first-cause", str(raised.exception))
        self.assertEqual(42, events[-1]["exit_code"])
        self.assertEqual("process-group-reaped", events[-1]["cleanup"])


class LegacyBrowserIsolationTests(unittest.TestCase):
    def test_profiles_are_explicit_unique_and_sandbox_disable_flags_are_rejected(self) -> None:
        base = {"executablePath": "./browser/chrome", "headless": True, "pipe": True,
                "args": ["--disable-background-networking"]}
        chrome = Path("/runtime/browser/chrome")
        preflight = runtime._legacy_config(base, chrome, Path("/evidence/preflight-profile"))
        render = runtime._legacy_config(base, chrome, Path("/evidence/render-profile"))
        self.assertEqual(str(chrome), preflight["executablePath"])
        self.assertIn("--user-data-dir=/evidence/preflight-profile", preflight["args"])
        self.assertIn("--user-data-dir=/evidence/render-profile", render["args"])
        self.assertNotEqual(preflight["args"], render["args"])
        self.assertEqual(base["args"], ["--disable-background-networking"])
        for forbidden in ("--no-sandbox", "--disable-setuid-sandbox"):
            with self.subTest(forbidden=forbidden), self.assertRaisesRegex(runtime.RuntimeFailure, "forbidden"):
                runtime._legacy_config({**base, "args": [forbidden]}, chrome, Path("/evidence/profile"))

    def test_shared_profile_in_base_config_is_rejected(self) -> None:
        base = {"executablePath": "./browser/chrome", "headless": True, "pipe": True,
                "args": ["--user-data-dir=/shared"]}
        with self.assertRaisesRegex(runtime.RuntimeFailure, "must not share"):
            runtime._legacy_config(base, Path("/runtime/browser/chrome"), Path("/evidence/profile"))

    def test_preflight_records_real_chrome_exit_without_retry(self) -> None:
        source = inspect.getsource(runtime.browser_process_preflight)
        self.assertIn("--remote-debugging-port=0", source)
        self.assertIn("CHROME_READY_MARKER", source)
        self.assertIn("shell_exit_code", source)
        self.assertIn("terminate_group", source)
        self.assertNotIn("retry", source.lower())


class OfflineMaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(dir="/Volumes/Daten/Projekte/BrandKit/.runtime/tmp"))
        self.addCleanup(runtime.remove_partial_tree, self.base)

    def _fixture(self, *, member_name: str = "package/cli.js", package_name: str = "example-cli",
                 duplicate_bytes: bytes | None = None, suffix: str = "") -> tuple[Path, Path, dict]:
        root = self.base / f"root{suffix}"; root.mkdir()
        bundle = self.base / f"bundle{suffix}"; bundle.mkdir()
        package_json = json.dumps({"name": package_name, "version": "1.2.3", "bin": {"example": "cli.js"}}, sort_keys=True).encode()
        archive_bytes = io.BytesIO()
        with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
            root_member = tarfile.TarInfo("package"); root_member.type = tarfile.DIRTYPE; root_member.mode = 0o755
            archive.addfile(root_member)
            identity = tarfile.TarInfo("package/package.json"); identity.size = len(package_json); identity.mode = 0o644
            archive.addfile(identity, io.BytesIO(package_json))
            executable = b"#!/usr/bin/env node\n"
            script = tarfile.TarInfo(member_name); script.size = len(executable); script.mode = 0o644
            archive.addfile(script, io.BytesIO(executable))
            if duplicate_bytes is not None:
                duplicate = tarfile.TarInfo("package/./cli.js"); duplicate.size = len(duplicate_bytes); duplicate.mode = 0o644
                archive.addfile(duplicate, io.BytesIO(duplicate_bytes))
        payload = archive_bytes.getvalue()
        integrity_bytes = hashlib.sha512(payload).digest()
        integrity = "sha512-" + base64.b64encode(integrity_bytes).decode("ascii")
        encoded = integrity_bytes.hex()
        cache = bundle / "npm-cache/_cacache/content-v2/sha512" / encoded[:2] / encoded[2:4]
        cache.mkdir(parents=True)
        (cache / encoded[4:]).write_bytes(payload)
        package_lock = {"packages": {
            "": {"name": "fixture", "version": "1.0.0"},
            "node_modules/example-cli": {"version": "1.2.3", "integrity": integrity},
        }}
        lock_path = root / "package-lock.json"; lock_path.write_text(json.dumps(package_lock))
        lock = {"artifact_bundle": {"path": str(bundle)},
                "package_lock": {"path": "package-lock.json", "sha256": runtime.digest(lock_path), "package_count": 2}}
        return root, bundle, lock

    def _materialize(self, root: Path, bundle: Path, lock: dict, suffix: str) -> tuple[Path, bytes, dict]:
        stage = self.base / f"stage-{suffix}"; stage.mkdir()
        inventory = self.base / f"inventory-{suffix}.json"
        with patch.object(runtime, "ROOT", root), patch.object(runtime, "load_lock", return_value=lock), \
             patch.object(runtime, "ensure_python"):
            evidence = runtime.materialize_npm_packages(stage, bundle, inventory)
        return stage, inventory.read_bytes(), evidence

    def test_materializer_is_deterministic_and_creates_safe_bin_link(self) -> None:
        root, bundle, lock = self._fixture()
        first, first_inventory, first_evidence = self._materialize(root, bundle, lock, "a")
        second, second_inventory, second_evidence = self._materialize(root, bundle, lock, "b")
        self.assertEqual(first_inventory, second_inventory)
        self.assertEqual(first_evidence, second_evidence)
        link = first / "node_modules/.bin/example"
        self.assertTrue(link.is_symlink())
        self.assertEqual("../example-cli/cli.js", link.readlink().as_posix())
        self.assertEqual(0o555, (first / "node_modules/example-cli/cli.js").stat().st_mode & 0o777)

    def test_materializer_records_explicit_linux_target(self) -> None:
        root, bundle, lock = self._fixture()
        stage = self.base / "stage-linux"; stage.mkdir()
        inventory = self.base / "inventory-linux.json"
        with patch.object(runtime, "ROOT", root), patch.object(runtime, "load_lock", return_value=lock), \
             patch.object(runtime, "ensure_python"):
            evidence = runtime.materialize_npm_packages(
                stage, bundle, inventory, target_os="linux", target_cpu="arm64")
        self.assertEqual("linux-arm64", evidence["platform"])

    def test_materializer_rejects_unknown_target(self) -> None:
        root, bundle, lock = self._fixture()
        stage = self.base / "stage-unknown"; stage.mkdir()
        with self.assertRaisesRegex(runtime.RuntimeFailure, "unsupported npm materializer target"):
            runtime.materialize_npm_packages(
                stage, bundle, self.base / "inventory-unknown.json", target_os="plan9", target_cpu="arm64")

    def test_archive_traversal_is_rejected(self) -> None:
        root, bundle, lock = self._fixture(member_name="package/../escape")
        stage = self.base / "stage-traversal"; stage.mkdir()
        with patch.object(runtime, "ROOT", root), patch.object(runtime, "load_lock", return_value=lock), \
             patch.object(runtime, "ensure_python"):
            with self.assertRaisesRegex(runtime.RuntimeFailure, "unsafe npm archive member"):
                runtime.materialize_npm_packages(stage, bundle, self.base / "traversal.json")
        self.assertFalse((self.base / "escape").exists())

    def test_package_identity_mismatch_is_rejected(self) -> None:
        root, bundle, lock = self._fixture(package_name="different-name")
        stage = self.base / "stage-identity"; stage.mkdir()
        with patch.object(runtime, "ROOT", root), patch.object(runtime, "load_lock", return_value=lock), \
             patch.object(runtime, "ensure_python"):
            with self.assertRaisesRegex(runtime.RuntimeFailure, "identity differs"):
                runtime.materialize_npm_packages(stage, bundle, self.base / "identity.json")

    def test_only_byte_identical_duplicate_archive_members_are_accepted(self) -> None:
        root, bundle, lock = self._fixture(duplicate_bytes=b"#!/usr/bin/env node\n")
        self._materialize(root, bundle, lock, "identical-duplicate")
        root, bundle, lock = self._fixture(duplicate_bytes=b"different\n", suffix="-conflict")
        stage = self.base / "stage-conflicting-duplicate"; stage.mkdir()
        with patch.object(runtime, "ROOT", root), patch.object(runtime, "load_lock", return_value=lock), \
             patch.object(runtime, "ensure_python"):
            with self.assertRaisesRegex(runtime.RuntimeFailure, "conflicting duplicate"):
                runtime.materialize_npm_packages(stage, bundle, self.base / "conflicting-duplicate.json")

    def test_platform_selection_and_link_normalization_are_fail_closed(self) -> None:
        self.assertTrue(runtime._platform_matches(["darwin"], "darwin"))
        self.assertFalse(runtime._platform_matches(["!darwin"], "darwin"))
        self.assertEqual(Path("node_modules/example/cli.js").as_posix(),
                         runtime._normalize_relative_link(Path("node_modules/.bin/example"), "../example/cli.js").as_posix())
        with self.assertRaisesRegex(runtime.RuntimeFailure, "escapes image"):
            runtime._normalize_relative_link(Path("node_modules/.bin/example"), "../../../outside")

    def test_runtime_inventory_accepts_internal_parent_link_and_rejects_escape(self) -> None:
        safe = self.base / "safe"; (safe / "node/bin").mkdir(parents=True); (safe / "node/lib").mkdir()
        (safe / "node/lib/corepack.js").write_text("safe")
        (safe / "node/bin/corepack").symlink_to("../lib/corepack.js")
        records = runtime.inventory(safe)
        self.assertIn("node/bin/corepack", {record["path"] for record in records})
        unsafe = self.base / "unsafe"; unsafe.mkdir(); (unsafe / "escape").symlink_to("../../outside")
        with self.assertRaisesRegex(runtime.RuntimeFailure, "escapes image"):
            runtime.inventory(unsafe)

    def test_runtime_inventory_uses_canonical_path_string_order(self) -> None:
        root = self.base / "ordering"; (root / "uv").mkdir(parents=True)
        (root / "uv.h").write_text("header")
        (root / "uv/aix.h").write_text("aix")
        self.assertEqual(["uv", "uv.h", "uv/aix.h"], [record["path"] for record in runtime.inventory(root)])


if __name__ == "__main__":
    unittest.main()
