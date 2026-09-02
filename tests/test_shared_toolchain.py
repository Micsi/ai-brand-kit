from __future__ import annotations

import copy
import io
import importlib.machinery
import importlib.util
import json
import pathlib
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("shared_toolchain", ROOT / "scripts/shared_toolchain.py")
toolchain = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(toolchain)
HARNESS_LOADER = importlib.machinery.SourceFileLoader(
    "shared_clean_rooms", str(ROOT / "toolchain/verify-shared-clean-rooms")
)
HARNESS_SPEC = importlib.util.spec_from_loader(HARNESS_LOADER.name, HARNESS_LOADER)
harness = importlib.util.module_from_spec(HARNESS_SPEC)
HARNESS_LOADER.exec_module(harness)


class SharedToolchainContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lock = json.loads((ROOT / "toolchain/toolchain.lock.json").read_text())

    def write_lock(self, value: dict, directory: pathlib.Path) -> pathlib.Path:
        path = directory / "lock.json"
        path.write_text(json.dumps(value))
        return path

    def test_real_lock_and_every_external_artifact_validate(self) -> None:
        lock, components = toolchain.validate_lock()
        self.assertEqual(set(components), {"humanizer", "pdf_renderer", "mermaid", "mermaid_linux_worker", "graphviz"})
        self.assertEqual(lock["environment"]["network"], "disabled")

    def test_docker_output_extractor_is_portable_and_fail_closed(self) -> None:
        def archive(member: tarfile.TarInfo, payload: bytes = b"") -> bytes:
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as handle:
                handle.addfile(member, io.BytesIO(payload) if member.isfile() else None)
            return stream.getvalue()

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            regular = tarfile.TarInfo("out/result.txt"); regular.size = 2
            harness.extract_output(archive(regular, b"ok"), root / "good")
            self.assertEqual((root / "good/out/result.txt").read_bytes(), b"ok")
            for index, member in enumerate((tarfile.TarInfo("../escape"), tarfile.TarInfo("out/link"))):
                if index:
                    member.type = tarfile.SYMTYPE; member.linkname = "/etc/passwd"
                with self.subTest(member=member.name), self.assertRaises(harness.HarnessError):
                    harness.extract_output(archive(member), root / f"bad-{index}")

    def test_timeouts_terminate_the_complete_owned_process_group(self) -> None:
        child_code = "import pathlib,sys,time;time.sleep(2);pathlib.Path(sys.argv[1]).write_text('escaped')"
        leader_code = "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);time.sleep(60)"
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            cases = (
                ("installer", toolchain.ToolchainError,
                 lambda command: toolchain.run(command, phase="timeout-attack", timeout=1)),
                ("harness", harness.HarnessError,
                 lambda command: harness.phase("timeout-attack", command, timeout=1)),
            )
            for label, error, invoke in cases:
                sentinel = root / label
                with self.subTest(label=label), self.assertRaises(error):
                    invoke([sys.executable, "-c", leader_code, child_code, str(sentinel)])
                time.sleep(2.5)
                self.assertFalse(sentinel.exists(), "timed-out descendant escaped its owned process group")

    def test_unknown_missing_and_moving_values_fail_closed(self) -> None:
        attacks = []
        extra = copy.deepcopy(self.lock); extra["surprise"] = True; attacks.append(extra)
        missing = copy.deepcopy(self.lock); del missing["components"]["graphviz"]; attacks.append(missing)
        moving = copy.deepcopy(self.lock); moving["execution"]["container_image"] = "zenika/alpine-chrome:latest"; attacks.append(moving)
        range_pin = copy.deepcopy(self.lock); range_pin["bindings"]["node_version"] = ">=22"; attacks.append(range_pin)
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            for index, attack in enumerate(attacks):
                with self.subTest(index=index), self.assertRaises(toolchain.ToolchainError):
                    toolchain.validate_lock(self.write_lock(attack, root))

    def test_cross_lock_version_font_chrome_and_container_attacks_fail(self) -> None:
        paths = [
            ("python_version", "3.12.11"),
            ("node_version", "22.17.0"),
            ("chrome_sha256", "0" * 64),
            ("font_sha256", "1" * 64),
        ]
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            for field, value in paths:
                attack = copy.deepcopy(self.lock); attack["bindings"][field] = value
                with self.subTest(field=field), self.assertRaises(toolchain.ToolchainError):
                    toolchain.validate_lock(self.write_lock(attack, root))
            attack = copy.deepcopy(self.lock); attack["execution"]["container_digest"] = "sha256:" + "0" * 64
            with self.assertRaises(toolchain.ToolchainError):
                toolchain.validate_lock(self.write_lock(attack, root))

    def test_installation_inventory_rejects_missing_wrong_and_extra_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            for name in ("humanizer", "mermaid", "graphviz", "pdf-home"):
                (root / name).mkdir()
                (root / name / "member").write_text(name)
            lock = copy.deepcopy(self.lock)
            lock["normative"]["installed_inventory"] = {name: toolchain.inventory_digest(root / name) for name in ("humanizer", "mermaid", "graphviz", "pdf-home")}
            toolchain.write_state(root, toolchain.state_payload(root, lock))
            with mock.patch.object(toolchain, "verify_humanizer"), mock.patch.object(toolchain, "verify_pdf_install"):
                toolchain.verify_install(root, lock)
                (root / "extra").write_text("attack")
                with self.assertRaises(toolchain.ToolchainError): toolchain.verify_install(root, lock)
                (root / "extra").unlink(); (root / "mermaid/member").write_text("wrong")
                with self.assertRaises(toolchain.ToolchainError): toolchain.verify_install(root, lock)
                (root / "mermaid/member").unlink()
                with self.assertRaises(toolchain.ToolchainError): toolchain.verify_install(root, lock)

    def test_pdf_signed_state_normalizes_only_verified_bindings_not_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw); roots = [parent / name for name in ("A", "B")]
            for root in roots:
                state = root / ".agents/activation-state"; state.mkdir(parents=True)
                payload = {
                    "payload": {"version": "1.2.28", "root": str(root), "sha256": ("a" if root.name == "A" else "b") * 64,
                                "manifest_sha256": ("c" if root.name == "A" else "d") * 64},
                    "hmac_sha256": ("e" if root.name == "A" else "f") * 64,
                }
                (state / "brand-voice-pdf.json").write_text(json.dumps(payload))
            self.assertEqual(toolchain.inventory_digest(roots[0]), toolchain.inventory_digest(roots[1]))
            attack = json.loads((roots[1] / ".agents/activation-state/brand-voice-pdf.json").read_text())
            attack["payload"]["version"] = "1.2.29"
            (roots[1] / ".agents/activation-state/brand-voice-pdf.json").write_text(json.dumps(attack))
            self.assertNotEqual(toolchain.inventory_digest(roots[0]), toolchain.inventory_digest(roots[1]))

    def test_expected_install_inventory_is_prelocked_not_derived_from_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            payload = toolchain.state_payload(root, self.lock)
        self.assertEqual(self.lock["normative"]["installed_inventory"], payload["components"])

    def test_closed_artifact_manifest_rejects_extra_wrong_and_symlink_members(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            member = root / "member.bin"; member.write_bytes(b"locked")
            manifest = root / "files.sha256.json"
            manifest.write_text(json.dumps({"schema_version":"test.v1", "files":[{"name":member.name, "size":6, "sha256":toolchain.sha(member)}]}))
            toolchain.verify_closed_artifact_directory(root, manifest)
            (root / "extra").write_text("attack")
            with self.assertRaises(toolchain.ToolchainError): toolchain.verify_closed_artifact_directory(root, manifest)
            (root / "extra").unlink(); member.write_bytes(b"wrong!")
            with self.assertRaises(toolchain.ToolchainError): toolchain.verify_closed_artifact_directory(root, manifest)
            member.unlink(); member.symlink_to(manifest.name)
            with self.assertRaises(toolchain.ToolchainError): toolchain.verify_closed_artifact_directory(root, manifest)

    def test_container_version_log_rejects_every_fake_actual_value(self) -> None:
        versions = self.lock["normative"]["versions"]
        payload = {
            "schema_version":"brandkit-normative-container-version-log.v1",
            "environment":{key:self.lock["environment"][key] for key in ("LANG","LC_ALL","TZ","SOURCE_DATE_EPOCH")},
            "platform":{"system":"Linux","machine":"x86_64"},
            "quarto":versions["quarto"], "node":versions["node"], "chrome":versions["chrome"], "mermaid":versions["mermaid"],
            "graphviz":versions["graphviz"], "lualatex":versions["lualatex"], "poppler":versions["poppler"], "qpdf":versions["qpdf"],
            "tesseract":versions["tesseract"], "tessdata_deu_sha256":versions["tessdata_deu_sha256"],
            "font_sha256":self.lock["bindings"]["font_sha256"], "tesseract_languages":["deu"],
        }
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            for field in ("quarto", "node", "chrome", "mermaid", "graphviz", "lualatex", "poppler", "qpdf", "tesseract", "tessdata_deu_sha256", "font_sha256", "tesseract_languages"):
                attack = copy.deepcopy(payload); attack[field] = [] if field == "tesseract_languages" else "fake"
                encoded = json.dumps(attack, sort_keys=True, separators=(",", ":")) + "\n"
                envelope = {"schema_version":"brandkit-container-runtime-envelope.v1", "image_id":self.lock["execution"]["container_digest"], "network_mode":"none", "container_log_sha256":__import__("hashlib").sha256(encoded.encode()).hexdigest(), "container_log":attack}
                path = root / f"{field}.json"; path.write_text(json.dumps(envelope))
                with self.subTest(field=field), self.assertRaises(toolchain.ToolchainError):
                    toolchain.validate_container_log(path, self.lock)

    def test_generated_source_node_modules_is_not_part_of_pdf_source_closure(self) -> None:
        self.assertFalse((ROOT / "toolchain/mermaid-svg/node_modules").exists())

    def test_record_sanitizer_is_atomic_and_rejects_internal_and_escape_attacks(self) -> None:
        def fixture(parent: pathlib.Path, attack: str) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
            root = parent / "humanizer"; site = root / ".venv/lib/python3.12/site-packages"
            dist = site / "demo-1.0.dist-info"; package = site / "demo.py"
            dist.mkdir(parents=True); package.write_text("VALUE = 1\n")
            digest = toolchain.base64.urlsafe_b64encode(toolchain.hashlib.sha256(package.read_bytes()).digest()).rstrip(b"=").decode()
            record = dist / "RECORD"
            if attack == "wrong-hash": rows = [["demo.py", "sha256=" + "A" * 43, str(package.stat().st_size)], ["demo-1.0.dist-info/RECORD", "", ""]]
            elif attack == "missing": rows = [["absent.py", "", ""], ["demo-1.0.dist-info/RECORD", "", ""]]
            elif attack == "foreign-escape": rows = [["../../../../../../foreign.txt", "", ""], ["demo-1.0.dist-info/RECORD", "", ""]]
            else: rows = [["demo.py", f"sha256={digest}", str(package.stat().st_size)], ["demo-1.0.dist-info/RECORD", "", ""]]
            output = toolchain.io.StringIO(newline=""); toolchain.csv.writer(output, lineterminator="\n").writerows(rows); record.write_text(output.getvalue())
            return root, record, package

        for attack in ("wrong-hash", "missing", "foreign-escape"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as raw:
                parent = pathlib.Path(raw); root, record, _ = fixture(parent, attack); before = record.read_bytes()
                pycache = parent / "pycache"; pycache.mkdir()
                with mock.patch.dict(toolchain.os.environ, {"PYTHONPYCACHEPREFIX": str(pycache)}), self.assertRaises(toolchain.ToolchainError):
                    toolchain.sanitize_installed_records(root)
                self.assertEqual(before, record.read_bytes())

    def test_bytecode_closure_and_relocator_reject_unowned_or_binary_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw) / "humanizer"
            dist = root / ".venv/lib/python3.12/site-packages/demo-1.0.dist-info"
            cache = dist.parent / "demo/__pycache__"; cache.mkdir(parents=True); dist.mkdir()
            record = dist / "RECORD"; record.write_text("demo-1.0.dist-info/RECORD,,\n")
            unowned = cache / "demo.cpython-312.pyc"; unowned.write_bytes(b"unowned")
            before = record.read_bytes()
            with self.assertRaises(toolchain.ToolchainError):
                toolchain.remove_internal_venv_bytecode(root)
            self.assertEqual(before, record.read_bytes()); self.assertTrue(unowned.is_file())

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw) / "new"; root.mkdir()
            previous = pathlib.Path(raw).resolve() / "old"
            (root / "binary").write_bytes(str(previous).encode() + b"\xff")
            with self.assertRaises(toolchain.ToolchainError):
                toolchain.relocate_humanizer_install(root, previous)

    def test_transaction_failure_removes_stage_and_leaves_no_root(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw); root = parent / "install"
            args = type("Args", (), {"root": str(root)})()
            with mock.patch.object(toolchain, "validate_lock", return_value=(self.lock, {})), \
                 mock.patch.object(toolchain, "immutable_snapshot", side_effect=lambda record, destination, label: pathlib.Path(record["path"])), \
                 mock.patch.object(toolchain, "safe_extract", side_effect=toolchain.ToolchainError("attack")), \
                 self.assertRaises(toolchain.ToolchainError):
                toolchain.install(args)
            self.assertFalse(root.exists())
            self.assertEqual([], list(parent.iterdir()))

    def test_verified_rollback_is_recoverable_quarantine_not_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw) / "install"; root.mkdir()
            (root / "installation.json").write_text("{}")
            proof = pathlib.Path(raw) / "version-log.json"; proof.write_text("{}")
            args = type("Args", (), {"root": str(root), "verification_log": str(proof)})()
            tree = {"receipt_digest":"1"*64, "tree_merkle":"2"*64, "identity_digest":"3"*64}
            with mock.patch.object(toolchain, "validate_lock", return_value=(self.lock, {})), \
                 mock.patch.object(toolchain, "validate_version_log", return_value={"tree_receipt_digest":"1"*64}), \
                 mock.patch.object(toolchain, "load_tree_receipt", return_value=tree), \
                 mock.patch.object(toolchain, "reissue_tree_receipt", return_value=tree), \
                 mock.patch.object(toolchain, "write_tree_receipt"):
                toolchain.rollback(args)
            marker = json.loads((root / "rollback.json").read_text())
            quarantine = pathlib.Path(marker["quarantine"])
            self.assertTrue(quarantine.is_dir())
            self.assertEqual("{}", (quarantine / "installation.json").read_text())

    def test_rollback_without_adjacent_verified_version_log_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw) / "install"; root.mkdir()
            args = type("Args", (), {"root": str(root), "verification_log": None})()
            with mock.patch.object(toolchain, "validate_lock", return_value=(self.lock, {})), \
                 mock.patch.object(toolchain, "verify_install") as verifier, \
                 self.assertRaises(toolchain.ToolchainError):
                toolchain.rollback(args)
            verifier.assert_not_called()

    def test_rollback_receipt_rejects_fake_actual_host_values(self) -> None:
        components = {name: json.loads((ROOT / record["path"]).read_text())
                      for name, record in self.lock["components"].items()}
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw); root = parent / "install"; root.mkdir()
            (root / "installation.json").write_text("sealed\n")
            mermaid_lock = components["mermaid"]
            payload = {
                "schema_version":"brandkit-toolchain-version-log.v1", "release":self.lock["release"]["id"],
                "lock_sha256":toolchain.sha(ROOT / "toolchain/toolchain.lock.json"), "container":{},
                "environment":{key:self.lock["environment"][key] for key in ("LANG", "LC_ALL", "TZ", "SOURCE_DATE_EPOCH")},
                "network":{"loopback":"pass", "dns":"denied", "dns_error":"gaierror", "tcp":"denied", "tcp_error":"PermissionError"},
                "humanizer":{"version":components["humanizer"]["skill"]["version"], "content_sha256":components["humanizer"]["skill"]["content_sha256"], "python":self.lock["bindings"]["python_version"]},
                "pdf_renderer":{"version":components["pdf_renderer"]["engine"]["version"], "python":self.lock["bindings"]["python_version"]},
                "mermaid":{"schema_version":"mermaid-version-log.v1", "lock_sha256":self.lock["components"]["mermaid"]["sha256"], "bundle_tree_sha256":self.lock["artifacts"]["mermaid_bundle"]["sha256"], "node_version":"v" + self.lock["bindings"]["node_version"], "node_executable_sha256":mermaid_lock["node"]["executable_sha256"], "npm_version":mermaid_lock["node"]["npm_version"], "mermaid_cli_version":mermaid_lock["package_lock"]["root_version"], "browser_version":"Google Chrome for Testing " + mermaid_lock["browser"]["version"], "browser_executable_sha256":self.lock["bindings"]["chrome_sha256"]},
                "graphviz":{"package_version":components["graphviz"]["package"]["version"], "backend_sha256":components["graphviz"]["graphviz"]["wasm_backend_sha256"], "node":self.lock["bindings"]["node_version"]},
                "font":{"path":self.lock["bindings"]["font_path"], "postscript_name":self.lock["bindings"]["font_postscript_name"], "sha256":self.lock["bindings"]["font_sha256"]},
                "installation_root":str(root), "installation_state_sha256":toolchain.sha(root / "installation.json"),
                "tree_receipt_digest":"1"*64,
            }
            proof = parent / "version-log.json"
            with mock.patch.object(toolchain, "validate_container_envelope"):
                proof.write_text(json.dumps(payload)); toolchain.validate_version_log(proof, root, self.lock, components)
                for field in ("release", "lock_sha256", "installation_root", "installation_state_sha256",
                              "tree_receipt_digest", "humanizer", "pdf_renderer", "mermaid", "graphviz", "font"):
                    attack = copy.deepcopy(payload); attack[field] = "fake"
                    proof.write_text(json.dumps(attack))
                    with self.subTest(field=field), self.assertRaises(toolchain.ToolchainError):
                        toolchain.validate_version_log(proof, root, self.lock, components)


if __name__ == "__main__":
    unittest.main()
