from __future__ import annotations

import base64
import copy
import csv
import hashlib
import hmac
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("h3_shared_toolchain", ROOT/"scripts/shared_toolchain.py")
toolchain = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(toolchain)


class TreeScannerContractTests(unittest.TestCase):
    def fixture(self, parent: pathlib.Path, name: str = "install"):
        root = parent/name; root.mkdir()
        for component in ("humanizer", "mermaid", "graphviz", "pdf-home"):
            target = root/component; target.mkdir(); (target/"payload.txt").write_text(component+"\n")
        site = root/"humanizer/site-packages"; dist = site/"demo-1.0.dist-info"
        dist.mkdir(parents=True); package = site/"demo.py"; package.write_text("VALUE = 1\n")
        encoded = base64.urlsafe_b64encode(hashlib.sha256(package.read_bytes()).digest()).rstrip(b"=").decode()
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows([
            ["demo.py", f"sha256={encoded}", str(package.stat().st_size)],
            ["demo-1.0.dist-info/RECORD", "", ""],
        ])
        (dist/"RECORD").write_text(output.getvalue())
        (root/"mermaid/link").symlink_to("payload.txt")
        component_merkles = {component:toolchain.inventory_digest((root/component).resolve())
                             for component in ("humanizer", "mermaid", "graphviz", "pdf-home")}
        lock = {"release":{"id":"mini"}, "normative":{"installed_inventory":component_merkles}}
        lock_path = parent/f"{name}.lock.json"; lock_path.write_text(json.dumps(lock, sort_keys=True))
        state = {"schema_version":"brandkit-toolchain-installation.v1", "release":"mini",
                 "lock_sha256":hashlib.sha256(lock_path.read_bytes()).hexdigest(), "components":component_merkles}
        (root/"installation.json").write_text(json.dumps(state, sort_keys=True, separators=(",", ":"))+"\n")
        return root.resolve(), lock, lock_path

    def scan(self, root: pathlib.Path):
        return toolchain.scan_tree(root, component_names=("graphviz", "humanizer", "mermaid", "pdf-home"))

    def test_one_pass_closure_record_stale_modes_and_real_counters(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, lock, lock_path = self.fixture(pathlib.Path(raw))
            opened = {}; original = pathlib.Path.read_bytes
            def counted(path: pathlib.Path):
                if path.is_relative_to(root): opened[str(path)] = opened.get(str(path), 0) + 1
                return original(path)
            with mock.patch.object(pathlib.Path, "read_bytes", counted): scan = self.scan(root)
            self.assertEqual(scan["counters"]["files"], scan["counters"]["reads"])
            self.assertEqual(scan["counters"]["bytes"], sum(path.stat().st_size for path in root.rglob("*")
                if path.is_file() and not path.is_symlink()))
            self.assertTrue(opened and all(value == 1 for value in opened.values()))
            self.assertEqual(1, scan["counters"]["records"])
            self.assertEqual([], scan["stale_roots"])
            self.assertEqual(lock["normative"]["installed_inventory"], scan["component_merkles"])
            with mock.patch.object(toolchain, "LOCK_PATH", lock_path):
                receipt = toolchain.make_tree_receipt(scan, lock, "install")
                toolchain.validate_tree_receipt(receipt, root, lock, live_scan=scan)

    def test_record_internal_external_duplicate_hash_size_and_self_fail(self) -> None:
        attacks = ("internal", "external", "duplicate", "hash", "size", "self")
        for attack in attacks:
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as raw:
                root, _, _ = self.fixture(pathlib.Path(raw)); record = root/"humanizer/site-packages/demo-1.0.dist-info/RECORD"
                rows = list(csv.reader(io.StringIO(record.read_text())))
                if attack == "internal": rows[0][0] = "absent.py"
                elif attack == "external": rows[0][0] = "../../../../../../outside"
                elif attack == "duplicate": rows.insert(1, list(rows[0]))
                elif attack == "hash": rows[0][1] = "sha256=" + "A"*43
                elif attack == "size": rows[0][2] = str(int(rows[0][2])+1)
                else: rows[-1][1] = "sha256=" + "A"*43
                output = io.StringIO(newline=""); csv.writer(output, lineterminator="\n").writerows(rows)
                record.write_text(output.getvalue())
                with self.assertRaises(toolchain.ToolchainError): self.scan(root)

    def test_stale_binary_text_symlink_and_link_special_attacks_fail(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root, _, _ = self.fixture(pathlib.Path(raw)); old = pathlib.Path(raw)/"old-stage"
            (root/"graphviz/text").write_text(str(old))
            (root/"graphviz/binary").write_bytes(str(old).encode()+b"\xff")
            (root/"graphviz/old-stage").write_text("target")
            (root/"graphviz/stale-link").symlink_to("old-stage")
            scan = toolchain.scan_tree(root, stale_roots=(old, pathlib.Path("old-stage")))
            self.assertEqual({"text", "binary", "symlink"}, {row["kind"] for row in scan["stale_roots"]})
            with mock.patch.object(toolchain, "_live_identity_digest", return_value=scan["identity_digest"]), \
                 self.assertRaises(toolchain.ToolchainError):
                toolchain.make_tree_receipt(scan, {"normative":{"installed_inventory":scan["component_merkles"]}}, "install")

        for attack in ("symlink", "hardlink", "special"):
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as raw:
                root, _, _ = self.fixture(pathlib.Path(raw)); target = root/"graphviz/attack"
                if attack == "symlink": target.symlink_to("/etc/passwd")
                elif attack == "hardlink": os.link(root/"graphviz/payload.txt", target)
                else: os.mkfifo(target)
                with self.assertRaises(toolchain.ToolchainError): self.scan(root)

    def test_receipt_mutation_replay_swap_and_counter_spoof_fail(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw); root, lock, lock_path = self.fixture(parent)
            with mock.patch.object(toolchain, "LOCK_PATH", lock_path):
                scan = self.scan(root); receipt = toolchain.make_tree_receipt(scan, lock, "verify")
                (root/"graphviz/payload.txt").write_text("mutated\n")
                with self.assertRaises(toolchain.ToolchainError): toolchain.validate_tree_receipt(receipt, root, lock)
                (root/"graphviz/payload.txt").write_text("graphviz\n")
                with self.assertRaises(toolchain.ToolchainError): toolchain.validate_tree_receipt(receipt, root, lock)

            other, other_lock, other_lock_path = self.fixture(parent, "other")
            with mock.patch.object(toolchain, "LOCK_PATH", other_lock_path), self.assertRaises(toolchain.ToolchainError):
                toolchain.validate_tree_receipt(receipt, other, other_lock)

            with mock.patch.object(toolchain, "LOCK_PATH", lock_path):
                scan = self.scan(root); honest = toolchain.make_tree_receipt(scan, lock, "verify")
                spoof = copy.deepcopy(honest); spoof["scan_counters"]["reads"] -= 1
                unsigned = dict(spoof); unsigned.pop("receipt_digest")
                spoof["receipt_digest"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest()
                with self.assertRaises(toolchain.ToolchainError): toolchain.validate_tree_receipt(spoof, root, lock)

    def test_mutation_before_receipt_and_write_denied_child_fail(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw); root, lock, lock_path = self.fixture(parent); scan = self.scan(root)
            (root/"pdf-home/payload.txt").write_text("changed\n")
            with mock.patch.object(toolchain, "LOCK_PATH", lock_path), self.assertRaises(toolchain.ToolchainError):
                toolchain.make_tree_receipt(scan, lock, "verify")
            sentinel = root/"child-write"; started = parent/"child-started"
            result = subprocess.run(toolchain.readonly_command(root, [sys.executable, "-c",
                "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('started');pathlib.Path(sys.argv[2]).write_text('attack')",
                started, sentinel]), check=False)
            self.assertTrue(started.is_file()); self.assertNotEqual(0, result.returncode); self.assertFalse(sentinel.exists())

    def test_rollback_quarantine_swap_and_restore_failure_are_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw); root, lock, lock_path = self.fixture(parent); supervisor = "mini-supervisor"
            with mock.patch.dict(os.environ, {toolchain.SUPERVISOR_ENV:supervisor}), \
                 mock.patch.object(toolchain, "LOCK_PATH", lock_path):
                scan = self.scan(root); receipt = toolchain.make_tree_receipt(scan, lock, "version-log")
                toolchain.write_tree_receipt(root, receipt)
                proof = parent/"version-log.json"; proof.write_text("{}")
                args = type("Args", (), {"root":str(root), "verification_log":str(proof)})()
                with mock.patch.object(toolchain, "validate_lock", return_value=(lock, {})), \
                     mock.patch.object(toolchain, "validate_version_log", return_value={"tree_receipt_digest":receipt["receipt_digest"]}):
                    toolchain.rollback(args)
                marker = json.loads((root/"rollback.json").read_text()); quarantine = pathlib.Path(marker["quarantine"])
                swapped = parent/"swapped"; shutil.copytree(quarantine, swapped); os.replace(quarantine, parent/"honest"); os.replace(swapped, quarantine)
                with mock.patch.object(toolchain, "validate_lock", return_value=(lock, {})), self.assertRaises(toolchain.ToolchainError):
                    toolchain.restore(type("Args", (), {"root":str(root)})())
                os.replace(quarantine, swapped); os.replace(parent/"honest", quarantine)

                original_replace = os.replace; calls = 0
                def fail_second(source, destination):
                    nonlocal calls; calls += 1
                    if calls == 2: raise OSError("injected restore move failure")
                    return original_replace(source, destination)
                with mock.patch.object(toolchain, "validate_lock", return_value=(lock, {})), \
                     mock.patch.object(toolchain.os, "replace", side_effect=fail_second), \
                     self.assertRaises(OSError):
                    toolchain.restore(type("Args", (), {"root":str(root)})())
                self.assertTrue((root/"rollback.json").is_file()); self.assertTrue(quarantine.is_dir())

    def test_restore_mutation_and_scan_budget_fail_closed(self) -> None:
        def rolled_back(parent: pathlib.Path):
            root, lock, lock_path = self.fixture(parent)
            with mock.patch.object(toolchain, "LOCK_PATH", lock_path):
                scan = self.scan(root); receipt = toolchain.make_tree_receipt(scan, lock, "version-log")
                toolchain.write_tree_receipt(root, receipt)
                proof = parent/"version-log.json"; proof.write_text("{}")
                args = type("Args", (), {"root":str(root), "verification_log":str(proof)})()
                with mock.patch.object(toolchain, "validate_lock", return_value=(lock, {})), \
                     mock.patch.object(toolchain, "validate_version_log", return_value={"tree_receipt_digest":receipt["receipt_digest"]}):
                    toolchain.rollback(args)
            marker = json.loads((root/"rollback.json").read_text())
            return root, pathlib.Path(marker["quarantine"]), lock, lock_path

        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {toolchain.SUPERVISOR_ENV:"mutation"}):
            root, quarantine, lock, lock_path = rolled_back(pathlib.Path(raw))
            (quarantine/"graphviz/payload.txt").write_text("mutated\n")
            with mock.patch.object(toolchain, "validate_lock", return_value=(lock, {})), \
                 mock.patch.object(toolchain, "LOCK_PATH", lock_path), self.assertRaises(toolchain.ToolchainError):
                toolchain.restore(type("Args", (), {"root":str(root)})())

        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(os.environ, {toolchain.SUPERVISOR_ENV:"budget"}):
            root, quarantine, lock, lock_path = rolled_back(pathlib.Path(raw))
            toolchain.SCAN_COUNTS.clear()  # simulate the next lifecycle command process
            with mock.patch.object(toolchain, "validate_lock", return_value=(lock, {})), \
                 mock.patch.object(toolchain, "LOCK_PATH", lock_path), \
                 mock.patch.object(toolchain, "verify_pdf_install"):
                toolchain.restore(type("Args", (), {"root":str(root)})())
            key = (f"standalone:{os.getpid()}", str(root))
            self.assertEqual(1, toolchain.SCAN_COUNTS[key],
                             "a standalone restart has no authoritative prior budget")
            self.assertEqual("standalone", json.loads(toolchain.tree_receipt_path(root).read_text())
                             ["scan_authorization"]["kind"])
            toolchain.scan_tree(root)
            toolchain.scan_tree(root)
            with self.assertRaises(toolchain.ToolchainError): toolchain.scan_tree(root)

    def test_humanizer_and_pdf_are_final_root_bound_before_atomic_publish(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw).resolve(); stage = parent/"stage"; stage.mkdir()
            final = parent/"final"
            human = stage/"humanizer"; site = human/".venv/lib/python3.12/site-packages"
            dist = site/"demo-1.dist-info"; dist.mkdir(parents=True)
            package = site/"demo.py"; package.write_text(f"ROOT={human!s}\n")
            encoded = base64.urlsafe_b64encode(hashlib.sha256(package.read_bytes()).digest()).rstrip(b"=").decode()
            (dist/"RECORD").write_text(f"demo.py,sha256={encoded},{package.stat().st_size}\n"
                                      "demo-1.dist-info/RECORD,,\n")
            pdf = stage/"pdf-home"; state = pdf/".agents/activation-state"; state.mkdir(parents=True)
            key_path = pdf/".agents/keys/brand-voice-pdf.key"; key_path.parent.mkdir(parents=True)
            key = b"k" * 32; key_path.write_bytes(key)
            version = json.loads((ROOT/"toolchain/pdf-renderer.production.v1.lock.json").read_text())["engine"]["version"]
            release = pdf/f".agents/skill-store/brand-voice-pdf/{version}"
            runtime = pdf/f".agents/runtime-store/brandkit-pdf-renderer/{version}"
            release.mkdir(parents=True); runtime.mkdir(parents=True)
            (release/"payload").write_text(str(pdf)); (runtime/"payload").write_text(str(pdf))
            def envelope(payload: dict) -> dict:
                raw_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                return {"payload":payload, "hmac_sha256":hmac.new(key, raw_payload, hashlib.sha256).hexdigest()}
            manifest_path = state/f"brand-voice-pdf-{version}.manifest.json"
            manifest_path.write_text(json.dumps(envelope({"schema_version":"brandkit-release-manifest.v1",
                "version":version, "predecessor_links":[], "release":[], "runtime":[],
                "link_topology":{"root":str(pdf)}})))
            activation_path = state/"brand-voice-pdf.json"
            activation_path.write_text(json.dumps(envelope({"schema_version":"brandkit-activation.v1",
                "version":version, "manifest_sha256":"0"*64, "predecessor_links":[],
                "links":{"root":str(pdf)}})))

            toolchain.relocate_humanizer_install(human, human, final/"humanizer")
            toolchain.relocate_pdf_install(pdf, pdf, final/"pdf-home")
            old = str(stage).encode(); new = str(final).encode()
            for path in (package, release/"payload", runtime/"payload", manifest_path, activation_path):
                self.assertNotIn(old, path.read_bytes()); self.assertIn(new, path.read_bytes())
            original_write_bytes = pathlib.Path.write_bytes; original_write_text = pathlib.Path.write_text
            writes: list[pathlib.Path] = []
            def forbidden_bytes(path: pathlib.Path, data: bytes):
                writes.append(path); return original_write_bytes(path, data)
            def forbidden_text(path: pathlib.Path, data: str, *args, **kwargs):
                writes.append(path); return original_write_text(path, data, *args, **kwargs)
            with mock.patch.object(pathlib.Path, "write_bytes", forbidden_bytes), \
                 mock.patch.object(pathlib.Path, "write_text", forbidden_text):
                os.replace(stage, final)
            self.assertEqual([], writes, "atomic publish must perform zero post-move content writes")
            self.assertTrue((final/"humanizer/.venv/lib/python3.12/site-packages/demo.py").is_file())


if __name__ == "__main__":
    unittest.main()
