from __future__ import annotations

import importlib.util
import importlib.machinery
import hashlib
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("harness_phases", ROOT / "scripts/harness_phases.py")
phases = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
sys.modules[SPEC.name] = phases
SPEC.loader.exec_module(phases)
HARNESS_LOADER = importlib.machinery.SourceFileLoader(
    "h1_shared_clean_rooms", str(ROOT / "toolchain/verify-shared-clean-rooms")
)
HARNESS_SPEC = importlib.util.spec_from_loader(HARNESS_LOADER.name, HARNESS_LOADER)
harness = importlib.util.module_from_spec(HARNESS_SPEC)
assert HARNESS_SPEC.loader
sys.modules[HARNESS_LOADER.name] = harness
HARNESS_LOADER.exec_module(harness)


class HarnessPhaseContractTests(unittest.TestCase):
    @staticmethod
    def assert_pid_gone(test: unittest.TestCase, pid: int) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        test.fail(f"owned descendant survived: {pid}")

    def test_instrumented_subprocess_records_real_output_file_and_counters(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); output = root / "result.txt"
            dag = phases.PhaseDAG(root / "dag", [phases.PhaseSpec("command")], environment={"LANG":"C"})
            harness.ACTIVE_DAG = dag
            try:
                result = harness.phase("command", [sys.executable, "-c",
                    "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('bound\\n');print('actual')",
                    str(output)], timeout=10, capture=True, output_paths=[output])
            finally:
                harness.ACTIVE_DAG = None
            self.assertEqual(b"actual\n", result.stdout)
            events = [json.loads(line) for line in dag.events.path.read_text().splitlines()]
            self.assertEqual(["start", "end"], [event["event"] for event in events])
            self.assertEqual("pass", events[-1]["detail"])
            self.assertEqual(2, events[-1]["output_files"])
            self.assertEqual(len(b"actual\n") + len(b"bound\n"), events[-1]["output_bytes"])

    def miniature(self, state: pathlib.Path, source_text: str, counters: dict[str, int], *,
                  resume: bool = False, fresh_release: bool = False,
                  environment_tag: str = "stable") -> tuple[phases.PhaseDAG, dict[str, pathlib.Path]]:
        code = state / "mini-code.py"
        if not code.exists():
            code.write_text("MINI_CODE = 1\n", encoding="utf-8")
        specs = [
            phases.PhaseSpec("source", resumable=True),
            phases.PhaseSpec("A-install", ("source",)), phases.PhaseSpec("A-verify", ("A-install",)),
            phases.PhaseSpec("A-package", ("A-verify",), resumable=True),
            phases.PhaseSpec("B-install", ("source",)), phases.PhaseSpec("B-verify", ("B-install",)),
            phases.PhaseSpec("B-package", ("B-verify",), resumable=True),
            phases.PhaseSpec("compare", ("A-package", "B-package")),
        ]
        dag = phases.PhaseDAG(state / "dag", specs, resume=resume, fresh_release=fresh_release,
                              environment={"LANG":"C", "TZ":"UTC", "SOURCE_DATE_EPOCH":"0",
                                           "MINI_ENV":environment_tag})

        def build_source(destination: pathlib.Path) -> None:
            counters["source"] = counters.get("source", 0) + 1
            (destination / "payload.txt").write_text(source_text, encoding="utf-8")

        source = dag.run_immutable("source", build_source, inputs={"source_sha256":phases.canonical_digest(source_text)},
                                   code_paths=[code])
        outputs: dict[str, pathlib.Path] = {}
        for label in ("A", "B"):
            install = state / "runs" / dag.run_id / label / "install"
            def install_action(install: pathlib.Path = install) -> tuple[str, int, int]:
                counters[f"{label}-install"] = counters.get(f"{label}-install", 0) + 1
                install.mkdir(parents=True)
                shutil.copyfile(source / "payload.txt", install / "payload.txt")
                return phases.scan_tree(install)
            dag.run(f"{label}-install", install_action, inputs={"source_receipt":dag.completed["source"]}, code_paths=[code])

            def verify_action(install: pathlib.Path = install) -> tuple[str, int, int]:
                counters[f"{label}-verify"] = counters.get(f"{label}-verify", 0) + 1
                actual = phases.scan_tree(install)
                if (install / "payload.txt").read_text(encoding="utf-8") != source_text:
                    raise phases.PhaseError("mini installed root mutation")
                return actual
            dag.run(f"{label}-verify", verify_action, inputs={"root_role":label}, code_paths=[code])

            def package(destination: pathlib.Path, install: pathlib.Path = install, label: str = label) -> None:
                counters[f"{label}-package"] = counters.get(f"{label}-package", 0) + 1
                shutil.copyfile(install / "payload.txt", destination / "payload.txt")
                (destination / "package.json").write_text('{"schema_version":"mini-package.v1"}\n', encoding="utf-8")
            outputs[label] = dag.run_immutable(f"{label}-package", package,
                                               inputs={"verified_root":dag.completed[f"{label}-verify"]},
                                               code_paths=[code])

        def compare() -> tuple[str, int, int]:
            left = phases.scan_tree(outputs["A"]); right = phases.scan_tree(outputs["B"])
            if left != right:
                raise phases.PhaseError("mini A/B differs")
            return left
        dag.run("compare", compare, inputs={"A":dag.completed["A-package"], "B":dag.completed["B-package"]},
                code_paths=[code])
        dag.finalize()
        return dag, outputs

    def test_miniature_ab_resume_fresh_release_and_fault_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = pathlib.Path(raw); counters: dict[str, int] = {}
            first, outputs = self.miniature(state, "locked\n", counters)
            first_package_counts = (counters["A-package"], counters["B-package"])
            second, _ = self.miniature(state, "locked\n", counters, resume=True)
            self.assertEqual(first_package_counts, (counters["A-package"], counters["B-package"]))
            self.assertEqual(2, counters["A-verify"])
            self.assertEqual(2, counters["B-verify"], "mutable installed roots must be live-verified across runs")
            events = [json.loads(line) for line in second.events.path.read_text().splitlines()]
            resumed = {event["phase"] for event in events if event["detail"] == "resumed"}
            self.assertEqual({"source", "A-package", "B-package"}, resumed)
            required = {"schema_version", "run_id", "timestamp_ns", "event", "phase", "predecessors",
                        "input_digest", "code_fingerprint", "environment_digest", "output_digest",
                        "input_files", "input_bytes", "output_files", "output_bytes", "wall_seconds",
                        "receipt_digest", "detail"}
            self.assertTrue(all(set(event) == required for event in events))

            code = state / "mini-code.py"
            code.write_text("MINI_CODE = 2\n", encoding="utf-8")
            before_code = (counters["A-package"], counters["B-package"])
            self.miniature(state, "locked\n", counters, resume=True)
            self.assertEqual((before_code[0] + 1, before_code[1] + 1),
                             (counters["A-package"], counters["B-package"]))

            code.write_text("MINI_CODE = 1\n", encoding="utf-8")
            before_input = (counters["A-package"], counters["B-package"])
            self.miniature(state, "changed\n", counters, resume=True)
            self.assertEqual((before_input[0] + 1, before_input[1] + 1),
                             (counters["A-package"], counters["B-package"]))

            before_environment = (counters["A-package"], counters["B-package"])
            self.miniature(state, "changed\n", counters, resume=True, environment_tag="changed")
            self.assertEqual((before_environment[0] + 1, before_environment[1] + 1),
                             (counters["A-package"], counters["B-package"]))

            before_fresh = counters["source"]
            _, fresh_outputs = self.miniature(state, "changed\n", counters, fresh_release=True,
                                               environment_tag="changed")
            self.assertEqual(before_fresh + 1, counters["source"], "fresh release must ignore resume receipts")
            with self.assertRaises(phases.PhaseError):
                phases.PhaseDAG(state / "bad", [phases.PhaseSpec("x")], resume=True, fresh_release=True)

            attacked = fresh_outputs["A"]
            attacked.chmod(0o700); (attacked / "payload.txt").chmod(0o600)
            (attacked / "payload.txt").write_text("attack\n", encoding="utf-8")
            with self.assertRaises(phases.PhaseError):
                self.miniature(state, "changed\n", counters, resume=True, environment_tag="changed")

    def test_resume_receipt_traversal_symlink_and_output_symlink_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = pathlib.Path(raw); counters: dict[str, int] = {}
            dag, _ = self.miniature(state, "locked\n", counters)
            receipt = state / "dag/receipts/source.json"
            value = json.loads(receipt.read_text())
            value["object_path"] = str(state.parent)
            phases.atomic_json(receipt, value)
            before = counters["source"]
            self.miniature(state, "locked\n", counters, resume=True)
            self.assertEqual(before + 1, counters["source"], "unsafe receipt must invalidate and rebuild")
            events = []
            for path in sorted((state / "dag/events").glob("*.jsonl")):
                events.extend(json.loads(line) for line in path.read_text().splitlines())
            self.assertTrue(any(event["phase"] == "source" and event["event"] == "invalidate" for event in events))
            value = json.loads(receipt.read_text())
            value["output_digest"] = "../outside"
            value["object_path"] = str(state / "dag/objects/../outside")
            phases.atomic_json(receipt, value)
            before = counters["source"]
            self.miniature(state, "locked\n", counters, resume=True)
            self.assertEqual(before + 1, counters["source"], "digest traversal must invalidate and rebuild")

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); code = root / "code"; code.write_text("x")
            dag = phases.PhaseDAG(root / "dag", [phases.PhaseSpec("linked", resumable=True),
                                                  phases.PhaseSpec("traversal", resumable=True)])
            def linked(destination: pathlib.Path) -> None:
                (destination / "escape").symlink_to("../outside")
            with self.assertRaises(phases.PhaseError):
                dag.run_immutable("linked", linked, inputs={}, code_paths=[code])
            def traversal(destination: pathlib.Path) -> None:
                (destination.parent / "escaped.txt").write_text("attack", encoding="utf-8")
            with self.assertRaises(phases.PhaseError):
                dag.run_immutable("traversal", traversal, inputs={}, code_paths=[code])
            self.assertFalse((root / "dag/staging/escaped.txt").exists())

    def test_dag_rejects_cycles_unknown_predecessors_and_out_of_order_execution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); code = root / "code"; code.write_text("x")
            with self.assertRaises(phases.PhaseError):
                phases.PhaseDAG(root / "cycle", [phases.PhaseSpec("a", ("b",)), phases.PhaseSpec("b", ("a",))])
            with self.assertRaises(phases.PhaseError):
                phases.PhaseDAG(root / "unknown", [phases.PhaseSpec("a", ("missing",))])
            with self.assertRaises(phases.PhaseError):
                phases.PhaseDAG(root / "duplicate", [phases.PhaseSpec("a"), phases.PhaseSpec("b", ("a", "a"))])
            dag = phases.PhaseDAG(root / "order", [phases.PhaseSpec("a"), phases.PhaseSpec("b", ("a",))])
            with self.assertRaises(phases.PhaseError):
                dag.run("b", lambda: ("0" * 64, 0, 0), inputs={}, code_paths=[code])
            linked_root = root / "linked-root"; linked_root.symlink_to(root / "order", target_is_directory=True)
            with self.assertRaises(phases.PhaseError):
                phases.PhaseDAG(linked_root, [phases.PhaseSpec("a")])
            event_target = root / "event-target"; event_target.write_text("do-not-touch", encoding="utf-8")
            dag.events.path.symlink_to(event_target)
            with self.assertRaises(phases.PhaseError):
                dag.run("a", lambda: ("0" * 64, 0, 0), inputs={}, code_paths=[code])
            self.assertEqual("do-not-touch", event_target.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); code = root / "code"; code.write_text("x")
            for output in (("bad", 0, 0), ("0" * 64, -1, 0), ("0" * 64, 0, -1)):
                dag = phases.PhaseDAG(root / phases.canonical_digest(output), [phases.PhaseSpec("a")])
                with self.subTest(output=output), self.assertRaises(phases.PhaseError):
                    dag.run("a", lambda output=output: output, inputs={}, code_paths=[code])

    def test_hardlinks_fail_during_build_resume_receipt_and_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); code = root / "code"; code.write_text("x")
            external = root / "external"; external.write_text("locked")
            dag = phases.PhaseDAG(root / "build", [phases.PhaseSpec("artifact", resumable=True)])
            with self.assertRaises(phases.PhaseError):
                dag.run_immutable("artifact", lambda destination: os.link(external, destination / "member"),
                                  inputs={}, code_paths=[code])

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); code = root / "code"; code.write_text("x")
            specs = [phases.PhaseSpec("artifact", resumable=True)]
            for attack in ("object", "receipt", "event"):
                state = root / attack
                dag = phases.PhaseDAG(state, specs)
                obj = dag.run_immutable("artifact", lambda destination:(destination/"member").write_text("locked"),
                                        inputs={}, code_paths=[code]); dag.finalize()
                if attack == "object": target = obj / "member"
                elif attack == "receipt": target = state / "receipts/artifact.json"
                else: target = dag.events.path
                alias = root / f"{attack}-alias"; os.link(target, alias)
                if attack == "event":
                    with self.assertRaises(phases.PhaseError): phases.validate_event_log(target)
                with self.assertRaises(phases.PhaseError):
                    retry = phases.PhaseDAG(state, specs, resume=True)
                    retry.run_immutable("artifact", lambda destination:(destination/"member").write_text("locked"),
                                        inputs={}, code_paths=[code])
                alias.unlink()

    def test_process_group_is_empty_after_success_error_and_timeout(self) -> None:
        child = ("import pathlib,signal,sys,time;"
                 "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                 "pathlib.Path(sys.argv[1]).write_text(str(__import__('os').getpid()));time.sleep(60)")
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw)
            for label, tail, timeout in (("success", "raise SystemExit(0)", 5),
                                         ("error", "raise SystemExit(7)", 5),
                                         ("timeout", "time.sleep(60)", 0.5)):
                pidfile = root / f"{label}.pid"
                parent = ("import pathlib,subprocess,sys,time;"
                          f"p=pathlib.Path({str(pidfile)!r});"
                          f"subprocess.Popen([sys.executable,'-c',{child!r},str(p)]);"
                          "deadline=time.time()+3;"
                          "\nwhile not p.exists() and time.time()<deadline: time.sleep(.01)\n" + tail)
                with self.subTest(label=label), self.assertRaises(harness.HarnessError):
                    harness.phase(f"group-{label}", [sys.executable, "-c", parent], timeout=timeout)
                self.assertTrue(pidfile.is_file())
                self.assert_pid_gone(self, int(pidfile.read_text()))

    def test_product_dag_resumes_only_fixture_and_render_objects(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); counters: dict[str, int] = {}

            def run_once(*, resume: bool = False, fresh: bool = False) -> phases.PhaseDAG:
                dag = phases.PhaseDAG(root / "product", harness.lifecycle_specs(), resume=resume,
                                      fresh_release=fresh, environment={"PATH":"/locked"})
                harness.ACTIVE_DAG = dag
                code = ROOT / "scripts/harness_phases.py"
                def regular(name: str) -> None:
                    counters[name] = counters.get(name, 0) + 1
                    dag.run(name, lambda name=name:(phases.canonical_digest({"phase":name}), 1, 1),
                            inputs={"logical_phase":name}, code_paths=[code])
                try:
                    regular("run-preflight")
                    regular("oci-inspect")
                    def fixture(destination: pathlib.Path) -> None:
                        counters["fixture-bundle"] = counters.get("fixture-bundle", 0) + 1
                        (destination / "fixtures.tar").write_text("fixture")
                    harness.immutable_product_phase("fixture-bundle", fixture, inputs={"fixture":"locked"})
                    for label in ("A", "B"):
                        for suffix in ("host-network-deny", "install", "verify"):
                            regular(f"{label}-{suffix}")
                        def render(destination: pathlib.Path, label: str = label) -> None:
                            key = f"{label}-render-output"; counters[key] = counters.get(key, 0) + 1
                            (destination / "result").write_text("same")
                        harness.immutable_product_phase(f"{label}-render-output", render,
                                                        inputs={"verified":dag.completed[f"{label}-verify"]})
                        for suffix in ("version-log", "rollback", "restore", "restore-verify"):
                            regular(f"{label}-{suffix}")
                    regular("compare")
                    dag.finalize()
                    return dag
                finally:
                    harness.ACTIVE_DAG = None

            first = run_once()
            immutable_counts = {name:counters[name] for name in
                                ("fixture-bundle", "A-render-output", "B-render-output")}
            second = run_once(resume=True)
            self.assertEqual(immutable_counts, {name:counters[name] for name in immutable_counts})
            self.assertEqual(2, counters["A-install"]); self.assertEqual(2, counters["B-install"])
            self.assertEqual(2, counters["A-verify"]); self.assertEqual(2, counters["B-verify"])
            resumed = {json.loads(line)["phase"] for line in second.events.path.read_text().splitlines()
                       if json.loads(line)["detail"] == "resumed"}
            self.assertEqual(set(immutable_counts), resumed)
            specs = {spec.name:spec for spec in harness.lifecycle_specs()}
            self.assertEqual(("fixture-bundle",), specs["A-host-network-deny"].predecessors)
            self.assertEqual(("fixture-bundle",), specs["B-host-network-deny"].predecessors)
            self.assertFalse(specs["A-install"].resumable); self.assertFalse(specs["A-verify"].resumable)
            before = counters["fixture-bundle"]
            run_once(fresh=True)
            self.assertEqual(before + 1, counters["fixture-bundle"])

    def test_product_fixture_mode_change_invalidates_resume_and_preserves_executable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); fixtures = root / "fixtures"; fixtures.mkdir()
            runner = fixtures / "run-render-smoke"
            runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8"); runner.chmod(0o600)
            objects: list[pathlib.Path] = []
            with mock.patch.object(harness, "FIXTURES", fixtures):
                for index, mode in enumerate((0o600, 0o700)):
                    runner.chmod(mode)
                    dag = phases.PhaseDAG(root / "dag", [phases.PhaseSpec("oci-inspect"),
                        phases.PhaseSpec("fixture-bundle", ("oci-inspect",), resumable=True)],
                        resume=bool(index), environment={"PATH":"/locked"})
                    harness.ACTIVE_DAG = dag
                    try:
                        dag.run("oci-inspect", lambda:("0" * 64, 0, 0), inputs={},
                                code_paths=[ROOT / "scripts/harness_phases.py"])
                        objects.append(harness.materialize_fixture_bundle())
                        dag.finalize()
                    finally:
                        harness.ACTIVE_DAG = None
            self.assertNotEqual(objects[0], objects[1], "chmod must invalidate the productive resume binding")
            with tarfile.open(objects[0] / "fixtures.tar") as archive:
                self.assertEqual(0o600, archive.getmember("run-render-smoke").mode)
            with tarfile.open(objects[1] / "fixtures.tar") as archive:
                self.assertEqual(0o700, archive.getmember("run-render-smoke").mode)
                self.assertTrue(archive.getmember("run-render-smoke").mode & 0o111)

    def test_live_phases_bind_complete_roots_and_real_counters(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); install = root / "install"; quarantine = root / "quarantine"
            names = ["A-install", "A-verify", "A-rollback", "A-restore", "A-restore-verify"]
            specs = [phases.PhaseSpec(name, (names[index - 1],) if index else ())
                     for index, name in enumerate(names)]
            dag = phases.PhaseDAG(root / "dag", specs, environment={"PATH":os.environ.get("PATH", "")})
            harness.ACTIVE_DAG = dag
            try:
                harness.phase("A-install", [sys.executable, "-c",
                    "import pathlib,sys;r=pathlib.Path(sys.argv[1]);(r/'bin').mkdir(parents=True);"
                    "(r/'installation.json').write_text('{}');(r/'bin/tool').write_text('tool')", str(install)],
                    timeout=10, output_roots=[install])
                harness.phase("A-verify", [sys.executable, "-c",
                    "import pathlib,sys;(pathlib.Path(sys.argv[1])/'bin/tool').write_text('tool-verified')",
                    str(install)], timeout=10, output_roots=[install])
                install_info = install.stat()
                marker = json.dumps({"schema_version":"brandkit-toolchain-rollback.v2",
                    "rolled_back_release":"test", "quarantine":str(quarantine),
                    "quarantine_device":install_info.st_dev, "quarantine_inode":install_info.st_ino,
                    "lock_sha256":"0" * 64, "tree_receipt_digest":"1"*64,
                    "tree_merkle":"2"*64, "identity_digest":"3"*64},
                    sort_keys=True, separators=(",", ":")) + "\n"
                harness.phase("A-rollback", [sys.executable, "-c",
                    "import os,pathlib,sys;root=pathlib.Path(sys.argv[1]);q=pathlib.Path(sys.argv[2]);"
                    "os.replace(root,q);root.mkdir();(root/'rollback.json').write_text(sys.argv[3])",
                    str(install), str(quarantine), marker], timeout=10,
                    output_roots=lambda:harness.rollback_output_roots(install))
                harness.phase("A-restore", [sys.executable, "-c",
                    "import os,pathlib,sys;root=pathlib.Path(sys.argv[1]);q=pathlib.Path(sys.argv[2]);"
                    "(root/'rollback.json').unlink();root.rmdir();os.replace(q,root)",
                    str(install), str(quarantine)], timeout=10, output_roots=[install])
                harness.phase("A-restore-verify", [sys.executable, "-c", "pass"], timeout=10,
                              output_roots=[install])
                dag.finalize()
            finally:
                harness.ACTIVE_DAG = None
            ends = {event["phase"]:event for event in map(json.loads, dag.events.path.read_text().splitlines())
                    if event["event"] == "end"}
            self.assertEqual(2, ends["A-install"]["output_files"])
            self.assertEqual(2, ends["A-verify"]["output_files"])
            self.assertEqual(3, ends["A-rollback"]["output_files"])
            self.assertEqual(2, ends["A-restore"]["output_files"])
            self.assertEqual(2, ends["A-restore-verify"]["output_files"])
            self.assertGreater(ends["A-install"]["output_bytes"], len(b"{}"))
            self.assertNotEqual(ends["A-install"]["output_digest"], ends["A-verify"]["output_digest"],
                                "a non-marker live-root mutation must change the phase output digest")
            receipt = harness.live_tree_receipt(install)
            broken = dict(receipt); broken["unknown"] = True
            with self.assertRaises(harness.HarnessError):
                harness.validate_live_tree_receipt(broken)
            broken = dict(receipt); broken["regular_bytes"] += 1
            with self.assertRaises(harness.HarnessError):
                harness.validate_live_tree_receipt(broken)

            def coordinated(mutator) -> dict:
                attacked = json.loads(json.dumps(receipt))
                mutator(attacked["entries"])
                attacked["tree_digest"] = phases.canonical_digest(attacked["entries"])
                files = [entry for entry in attacked["entries"] if entry.get("type") == "file"]
                attacked["regular_files"] = len(files)
                attacked["regular_bytes"] = sum(entry.get("size", 0) for entry in files)
                return attacked

            attacks = [
                lambda entries: entries[1].update(unknown=True),
                lambda entries: entries.append(dict(entries[1])),
                lambda entries: entries[1].update(type="device"),
                lambda entries: entries[1].update(path="../escape"),
                lambda entries: entries[1].update(path="/absolute"),
                lambda entries: entries[1].update(mode=True),
                lambda entries: entries[-1].update(sha256="g" * 64),
                lambda entries: entries.append({"path":"bin/link", "type":"symlink", "target":""}),
                lambda entries: entries[-1].update(path="missing/child"),
            ]
            for attack in attacks:
                with self.subTest(attack=attacks.index(attack)), self.assertRaises(harness.HarnessError):
                    harness.validate_live_tree_receipt(coordinated(attack))
            for unsafe_root in ("/tmp/../tmp/tree", "/tmp/./tree", "/tmp//tree", "/tmp/tree/",
                                "tmp/tree", "//tmp/tree"):
                attacked = json.loads(json.dumps(receipt))
                attacked["root"] = unsafe_root
                attacked["tree_digest"] = phases.canonical_digest(attacked["entries"])
                files = [entry for entry in attacked["entries"] if entry["type"] == "file"]
                attacked["regular_files"] = len(files)
                attacked["regular_bytes"] = sum(entry["size"] for entry in files)
                with self.subTest(root=unsafe_root), self.assertRaises(harness.HarnessError):
                    harness.validate_live_tree_receipt(attacked)

    def test_transitive_code_and_effective_environment_faults_invalidate(self) -> None:
        required = {"scripts/shared_toolchain.py", "toolchain/install-from-lock", "toolchain/verify-installation",
                    "toolchain/version-log", "toolchain/rollback", "toolchain/restore",
                    "toolchain/mermaid-svg/install-offline", "toolchain/mermaid-svg/verify-bundle",
                    "toolchain/graphviz-svg/install-offline", "toolchain/graphviz-svg/verify-bundle",
                    "toolchain/pdf-renderer/install-from-lock", "toolchain/pdf-renderer/verify-installation",
                    "toolchain/pdf-renderer/render", "skills/brand-voice-pdf/scripts/run",
                    "toolchain/mermaid-svg/version-log", "toolchain/humanizer-doctor-runtime-adapter.py"}
        self.assertTrue(required <= {path.relative_to(ROOT).as_posix() for path in harness.PRODUCT_CODE_INPUTS})
        for key in ("PATH", "PYTHONPATH", "HOME", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT"):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key:f"/fault/{key}"}):
                self.assertEqual(f"/fault/{key}", harness.process_environment()[key])

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); code = root / "code"; code.write_text("x")
            for key in ("PATH", "PYTHONPATH", "HOME", "TMPDIR", "DOCKER_HOST", "DOCKER_CONTEXT"):
                state = root / key; count = {"build":0}; specs = [phases.PhaseSpec("artifact", resumable=True)]
                for index, value in enumerate(("before", "after")):
                    dag = phases.PhaseDAG(state, specs, resume=bool(index), environment={key:value})
                    def build(destination: pathlib.Path) -> None:
                        count["build"] += 1; (destination/"value").write_text("same")
                    dag.run_immutable("artifact", build, inputs={}, code_paths=[code]); dag.finalize()
                self.assertEqual(2, count["build"], f"{key} change must invalidate")

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); wrapper = root / "wrapper"; target = root / "target.py"
            wrapper.write_text("#!/bin/sh\n"); target.write_text("VALUE=1\n")
            original = harness.PRODUCT_CODE_INPUTS; counters = {"build":0}
            specs = [phases.PhaseSpec("fixture-bundle", resumable=True)]
            try:
                harness.PRODUCT_CODE_INPUTS = (wrapper, target)
                for index in range(2):
                    dag = phases.PhaseDAG(root / "dag", specs, resume=bool(index), environment={"PATH":"/locked"})
                    harness.ACTIVE_DAG = dag
                    if index: target.write_text("VALUE=2\n")
                    def build(destination: pathlib.Path) -> None:
                        counters["build"] += 1; (destination/"value").write_text("same")
                    harness.immutable_product_phase("fixture-bundle", build, inputs={})
                    dag.finalize()
                self.assertEqual(2, counters["build"], "transitive wrapper target change must invalidate")
            finally:
                harness.ACTIVE_DAG = None; harness.PRODUCT_CODE_INPUTS = original

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); adapter = ROOT / "toolchain/humanizer-doctor-runtime-adapter.py"
            adapter_copy = root / adapter.name; adapter_copy.write_bytes(adapter.read_bytes())
            original = harness.PRODUCT_CODE_INPUTS; count = {"build":0}
            try:
                harness.PRODUCT_CODE_INPUTS = tuple(
                    adapter_copy if path == adapter else path for path in original
                )
                for index in range(2):
                    dag = phases.PhaseDAG(root / "adapter-dag",
                        [phases.PhaseSpec("fixture-bundle", resumable=True)], resume=bool(index),
                        environment={"PATH":"/locked"})
                    harness.ACTIVE_DAG = dag
                    if index:
                        adapter_copy.write_bytes(adapter_copy.read_bytes() + b"\n# mutation fault\n")
                    def build(destination: pathlib.Path) -> None:
                        count["build"] += 1; (destination / "value").write_text("same")
                    harness.immutable_product_phase("fixture-bundle", build, inputs={})
                    dag.finalize()
                self.assertEqual(2, count["build"], "Humanizer adapter mutation must invalidate resume")
            finally:
                harness.ACTIVE_DAG = None; harness.PRODUCT_CODE_INPUTS = original

    def test_summary_is_bound_to_closed_live_event_log_and_material_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); code = root / "code"; code.write_text("x")
            dag = phases.PhaseDAG(root / "dag", [phases.PhaseSpec("complete")])
            dag.run("complete", lambda:("0"*64, 0, 0), inputs={}, code_paths=[code])
            closure = dag.finalize()
            renders = []
            for label in ("A", "B"):
                render = root / label; (render/"out/gallery").mkdir(parents=True)
                for name in ("mermaid.svg", "graphviz.svg", "rich.html", "rich-a.pdf",
                             "ocr.txt", "container-version-log.json"):
                    (render/"out"/name).write_text(name)
                (render/"out/gallery/page-1.png").write_text("gallery/page-1.png")
                renders.append(render)
            hashes = harness.output_hashes(renders[0])
            ledger = {"schema_version":"brandkit-scan-supervisor-ledger.v1", "run_id":dag.run_id,
                      "counts":{}, "events":[]}
            ledger["ledger_digest"] = phases.canonical_digest(ledger)
            summary = {"schema_version":"brandkit-shared-clean-rooms.v1", "status":"pass",
                       "image_id":"sha256:"+"0"*64, "network":"deny-all-with-loopback",
                       "fresh_release":False, "resumed":False, "run_id":dag.run_id,
                       "rooms":[str(root/"A-room"),str(root/"B-room")],
                       "render_objects":[str(path) for path in renders], "hashes":hashes,
                       "event_log_sha256":closure["event_log_sha256"],
                       "event_log_size":closure["event_log_size"], "event_count":closure["event_count"],
                       "run_closure_digest":closure["closure_digest"],
                       "run_inputs_path":str(root/"run-inputs.json"),
                       "run_inputs_receipt_digest":"1"*64,
                       "scan_ledger":ledger, "scan_ledger_digest":ledger["ledger_digest"],
                       "product_phase_proofs":{}}
            summary_path = root / "summary.json"; phases.atomic_json(summary_path, summary)
            with mock.patch.object(harness.shared_toolchain, "validate_lock_structure", return_value=({}, {})), \
                 mock.patch.object(harness.shared_toolchain, "load_run_inputs",
                                   return_value=({}, {"receipt_digest":"1"*64})):
                harness.validate_summary(summary_path, closure)
            dag.events.path.chmod(0o600); dag.events.path.write_bytes(dag.events.path.read_bytes()+b"{}\n")
            with mock.patch.object(harness.shared_toolchain, "validate_lock_structure", return_value=({}, {})), \
                 mock.patch.object(harness.shared_toolchain, "load_run_inputs",
                                   return_value=({}, {"receipt_digest":"1"*64})), \
                 self.assertRaises((phases.PhaseError, harness.HarnessError)):
                    harness.validate_summary(summary_path, closure)

    def test_live_supervisor_budget_survives_restart_receipt_reset_and_fake_fd(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw).resolve() / "tree"; root.mkdir(); (root/"payload").write_text("x")
            supervisor = harness.ScanSupervisor("mini-live-supervisor")
            try:
                env = {**os.environ, harness.shared_toolchain.SUPERVISOR_ENV:supervisor.run_id,
                       harness.shared_toolchain.SUPERVISOR_FD_ENV:str(supervisor.fd),
                       harness.shared_toolchain.SUPERVISOR_ROOM_ENV:"A",
                       harness.shared_toolchain.SUPERVISOR_PHASE_ENV:"A-install",
                       harness.shared_toolchain.SUPERVISOR_OPERATION_ENV:"install"}
                code = ("import json,pathlib,sys;sys.path.insert(0,sys.argv[1]);"
                        "import shared_toolchain as t;print(json.dumps(t.scan_tree(pathlib.Path(sys.argv[2]))))")
                ordinals = []
                for index in range(3):
                    fake = root.parent / f".{root.name}.tree-receipt.json"
                    fake.write_text(json.dumps({"scan_counters":{"scan_ordinal":0}, "receipt_digest":"0"*64}))
                    if index: fake.unlink()
                    result = subprocess.run([sys.executable, "-c", code, str(ROOT/"scripts"), str(root)],
                        env=env, pass_fds=(supervisor.fd,), capture_output=True, text=True, check=True)
                    ordinals.append(json.loads(result.stdout)["counters"]["scan_ordinal"])
                self.assertEqual([1, 2, 3], ordinals)
                fourth = subprocess.run([sys.executable, "-c", code, str(ROOT/"scripts"), str(root)],
                    env=env, pass_fds=(supervisor.fd,), capture_output=True, text=True)
                self.assertNotEqual(0, fourth.returncode)
                copied_env = {**env, harness.shared_toolchain.SUPERVISOR_ENV:"forged-copy"}
                other = root.parent/"other"; other.mkdir(); (other/"payload").write_text("x")
                no_capability = subprocess.run([sys.executable, "-c", code, str(ROOT/"scripts"),
                    str(other)], env=copied_env, capture_output=True, text=True)
                self.assertNotEqual(0, no_capability.returncode)
                ledger = supervisor.summary()
                self.assertEqual(3, ledger["counts"][str(root)])
                self.assertEqual(3, sum(event["event"] == "scan-complete" for event in ledger["events"]))
            finally:
                supervisor.close()

    def test_product_receipt_binds_live_scan_without_harness_payload_reread(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw).resolve(); root = parent/"install"; root.mkdir()
            for component in ("humanizer", "mermaid", "graphviz", "pdf-home"):
                target = root/component; target.mkdir(); (target/"payload").write_text(component)
            names = ("graphviz", "humanizer", "mermaid", "pdf-home")
            inventory = {name:harness.shared_toolchain.inventory_digest(root/name) for name in names}
            lock = {"release":{"id":"mini"}, "normative":{"installed_inventory":inventory}}
            state = {"schema_version":"brandkit-toolchain-installation.v1", "release":"mini",
                     "lock_sha256":harness.sha(harness.shared_toolchain.LOCK_PATH), "components":inventory}
            (root/"installation.json").write_text(json.dumps(state, sort_keys=True, separators=(",", ":"))+"\n")
            supervisor = harness.ScanSupervisor("receipt-live")
            try:
                env = {**os.environ, harness.shared_toolchain.SUPERVISOR_ENV:supervisor.run_id,
                       harness.shared_toolchain.SUPERVISOR_FD_ENV:str(supervisor.fd),
                       harness.shared_toolchain.SUPERVISOR_ROOM_ENV:"A",
                       harness.shared_toolchain.SUPERVISOR_PHASE_ENV:"A-verify",
                       harness.shared_toolchain.SUPERVISOR_OPERATION_ENV:"verify"}
                with mock.patch.dict(os.environ, env, clear=True):
                    scan = harness.shared_toolchain.scan_tree(root, component_names=names, seal_snapshot=True)
                    receipt = harness.shared_toolchain.make_tree_receipt(scan, lock, "verify")
                    receipt_path = harness.shared_toolchain.write_tree_receipt(root, receipt)
                    reads: dict[str, int] = {}; original = pathlib.Path.read_bytes
                    def counted(path: pathlib.Path):
                        if path.is_relative_to(root) and path.name != "installation.json":
                            reads[str(path)] = reads.get(str(path), 0) + 1
                        return original(path)
                    with mock.patch.object(harness.shared_toolchain, "validate_lock_structure", return_value=(lock, {})), \
                         mock.patch.object(pathlib.Path, "read_bytes", counted):
                        harness._validated_product_tree_receipt(receipt_path, root, root, "A", "A-verify", "verify")
                    self.assertEqual({}, reads, "harness must not reread product payloads")
                    (root/"graphviz/payload").write_text("mutated-between-phases")
                    env[harness.shared_toolchain.SUPERVISOR_PHASE_ENV] = "A-version-log"
                    env[harness.shared_toolchain.SUPERVISOR_OPERATION_ENV] = "version-log"
                    with mock.patch.dict(os.environ, env, clear=True), \
                         mock.patch.object(harness.shared_toolchain, "verify_pdf_install"), \
                         self.assertRaises(harness.shared_toolchain.ToolchainError):
                        harness.shared_toolchain.verify_install(root, lock, full_humanizer=False,
                            operation="version-log", reuse_receipt=True)
                    self.assertEqual(2, supervisor.summary()["counts"][str(root)],
                                     "mutation across a process boundary must force a new full scan")
                    (root/"graphviz/payload").write_text("graphviz")
                standalone = json.loads(json.dumps(receipt)); standalone["scan_authorization"]["kind"] = "standalone"
                standalone["scan_identity"] = harness.shared_toolchain._receipt_scan_identity(standalone)
                unsigned = dict(standalone); unsigned.pop("receipt_digest")
                standalone["receipt_digest"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest()
                receipt_path.chmod(0o600); receipt_path.write_text(json.dumps(standalone)); receipt_path.chmod(0o400)
                with mock.patch.dict(os.environ, env, clear=True), \
                     mock.patch.object(harness.shared_toolchain, "validate_lock_structure", return_value=(lock, {})), \
                     self.assertRaises(harness.shared_toolchain.ToolchainError):
                    harness._validated_product_tree_receipt(receipt_path, root, root, "A", "A-verify", "verify")
            finally:
                supervisor.close()

    def test_logical_final_prepublish_move_and_restore_use_exactly_three_scans(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw).resolve(); stage = parent/".install.next"; final = parent/"install"
            stage.mkdir(); names = ("graphviz", "humanizer", "mermaid", "pdf-home")
            for name in names:
                component = stage/name; component.mkdir(); (component/"payload").write_text(str(final/name))
            (stage/"installation.json").write_text("{}")
            supervisor = harness.ScanSupervisor("logical-final")
            env = {**os.environ, harness.shared_toolchain.SUPERVISOR_ENV:supervisor.run_id,
                   harness.shared_toolchain.SUPERVISOR_FD_ENV:str(supervisor.fd),
                   harness.shared_toolchain.SUPERVISOR_ROOM_ENV:"A",
                   harness.shared_toolchain.SUPERVISOR_PHASE_ENV:"A-install",
                   harness.shared_toolchain.SUPERVISOR_OPERATION_ENV:"install"}
            try:
                with mock.patch.dict(os.environ, env, clear=True):
                    prepublish = harness.shared_toolchain.scan_tree(stage, component_names=names,
                                                                    logical_root=final)
                    os.replace(stage, final)
                    published = harness.shared_toolchain.scan_tree(final, component_names=names)
                    self.assertEqual(prepublish["portable_merkle"], published["portable_merkle"])
                    self.assertEqual(prepublish["component_merkles"], published["component_merkles"])
                    quarantine = parent/"quarantine"; os.replace(final, quarantine); os.replace(quarantine, final)
                    restored = harness.shared_toolchain.scan_tree(final, component_names=names)
                    self.assertEqual(published["portable_merkle"], restored["portable_merkle"])
                ledger = supervisor.summary()
                self.assertEqual(3, ledger["counts"][str(final)])
                self.assertEqual([1, 2, 3], [event["ordinal"] for event in ledger["events"]
                                             if event["event"] == "scan"])
            finally:
                supervisor.close()

    def test_product_receipt_cross_room_root_alias_mutation_and_phase_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw).resolve(); roots = {}
            names = ("graphviz", "humanizer", "mermaid", "pdf-home")
            inventory = None
            for room in ("A", "B"):
                root = parent/room/"install"; root.mkdir(parents=True); roots[room] = root
                for name in names:
                    component = root/name; component.mkdir(); (component/"payload").write_text(name)
                current = {name:harness.shared_toolchain.inventory_digest(root/name) for name in names}
                inventory = current if inventory is None else inventory
                self.assertEqual(inventory, current)
            lock = {"release":{"id":"mini"}, "normative":{"installed_inventory":inventory}}
            for root in roots.values():
                state = {"schema_version":"brandkit-toolchain-installation.v1", "release":"mini",
                         "lock_sha256":harness.sha(harness.shared_toolchain.LOCK_PATH), "components":inventory}
                (root/"installation.json").write_text(json.dumps(state, sort_keys=True, separators=(",", ":"))+"\n")
            supervisor = harness.ScanSupervisor("cross-room")
            env = {**os.environ, harness.shared_toolchain.SUPERVISOR_ENV:supervisor.run_id,
                   harness.shared_toolchain.SUPERVISOR_FD_ENV:str(supervisor.fd),
                   harness.shared_toolchain.SUPERVISOR_ROOM_ENV:"A",
                   harness.shared_toolchain.SUPERVISOR_PHASE_ENV:"A-verify",
                   harness.shared_toolchain.SUPERVISOR_OPERATION_ENV:"verify"}
            try:
                with mock.patch.dict(os.environ, env, clear=True):
                    scan = harness.shared_toolchain.scan_tree(roots["A"], component_names=names, seal_snapshot=True)
                    receipt = harness.shared_toolchain.make_tree_receipt(scan, lock, "verify")
                    a_path = harness.shared_toolchain.write_tree_receipt(roots["A"], receipt)
                    with mock.patch.object(harness.shared_toolchain, "validate_lock_structure", return_value=(lock, {})):
                        harness._validated_product_tree_receipt(a_path, roots["A"], roots["A"], "A", "A-verify", "verify")
                        for room, phase_name, operation in (("B", "A-verify", "verify"),
                                                            ("A", "A-restore-verify", "verify"),
                                                            ("A", "A-verify", "install")):
                            with self.subTest(room=room, phase=phase_name, operation=operation), \
                                 self.assertRaises((harness.HarnessError, harness.shared_toolchain.ToolchainError)):
                                harness._validated_product_tree_receipt(a_path, roots["A"], roots["A"],
                                                             room, phase_name, operation)
                        alias = pathlib.Path(str(roots["A"].parent/".."/"A"/"install"))
                        with self.assertRaises(harness.HarnessError):
                            harness._validated_product_tree_receipt(a_path, alias, roots["A"], "A", "A-verify", "verify")
                        symlink = parent/"alias-install"; symlink.symlink_to(roots["A"])
                        with self.assertRaises(harness.HarnessError):
                            harness._validated_product_tree_receipt(a_path, symlink, roots["A"], "A", "A-verify", "verify")

                    b_path = harness.shared_toolchain.tree_receipt_path(roots["B"])
                    b_path.write_bytes(a_path.read_bytes()); b_path.chmod(0o400)
                    (roots["B"]/"graphviz/payload").write_text("MUTATED-B")
                    with mock.patch.object(harness.shared_toolchain, "validate_lock_structure", return_value=(lock, {})), \
                         self.assertRaises((harness.HarnessError, harness.shared_toolchain.ToolchainError)):
                        harness._validated_product_tree_receipt(b_path, roots["B"], roots["B"], "B", "B-verify", "verify")

                    coordinated = json.loads(a_path.read_text()); b_info = roots["B"].stat()
                    coordinated.update(root=str(roots["B"]), root_device=b_info.st_dev, root_inode=b_info.st_ino,
                        installation_state_sha256=harness.sha(roots["B"]/"installation.json"))
                    coordinated["scan_identity"] = harness.shared_toolchain._receipt_scan_identity(coordinated)
                    unsigned = dict(coordinated); unsigned.pop("receipt_digest")
                    coordinated["receipt_digest"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True,
                        separators=(",", ":")).encode()).hexdigest()
                    b_path.chmod(0o600); b_path.write_text(json.dumps(coordinated)); b_path.chmod(0o400)
                    with mock.patch.object(harness.shared_toolchain, "validate_lock_structure", return_value=(lock, {})), \
                         self.assertRaises(harness.shared_toolchain.ToolchainError):
                        harness._validated_product_tree_receipt(b_path, roots["B"], roots["B"], "B", "B-verify", "verify")

                    old_a = roots["A"].with_name("install-old"); os.replace(roots["A"], old_a)
                    shutil.copytree(old_a, roots["A"], symlinks=True)
                    with mock.patch.object(harness.shared_toolchain, "validate_lock_structure", return_value=(lock, {})):
                        proof_before = harness._validated_product_tree_receipt(
                            a_path, roots["A"], roots["A"], "A", "A-verify", "verify")
                    (roots["A"]/"graphviz/payload").write_text("MUTATED-A-AFTER-PROOF")
                    with mock.patch.object(harness.shared_toolchain, "validate_lock_structure", return_value=(lock, {})):
                        proof_after = harness._validated_product_tree_receipt(
                            a_path, roots["A"], roots["A"], "A", "A-verify", "verify")
                    self.assertEqual(proof_before["sealed_snapshot"], proof_after["sealed_snapshot"])
                self.assertEqual({str(roots["A"]):1}, supervisor.summary()["counts"])
            finally:
                supervisor.close()

    def test_product_phase_input_and_commit_bind_expected_root_context(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw).resolve(); root = parent/"A/install"; root.mkdir(parents=True)
            names = ("graphviz", "humanizer", "mermaid", "pdf-home")
            for name in names:
                component = root/name; component.mkdir(); (component/"payload").write_text(name)
            inventory = {name:harness.shared_toolchain.inventory_digest(root/name) for name in names}
            lock = {"release":{"id":"mini"}, "normative":{"installed_inventory":inventory}}
            state = {"schema_version":"brandkit-toolchain-installation.v1", "release":"mini",
                     "lock_sha256":harness.sha(harness.shared_toolchain.LOCK_PATH), "components":inventory}
            (root/"installation.json").write_text(json.dumps(state, sort_keys=True, separators=(",", ":"))+"\n")
            supervisor = harness.ScanSupervisor("phase-binding")
            phase_env = harness.process_environment({harness.shared_toolchain.SUPERVISOR_ENV:supervisor.run_id,
                harness.shared_toolchain.SUPERVISOR_FD_ENV:str(supervisor.fd),
                harness.shared_toolchain.SUPERVISOR_ROOM_ENV:"A",
                harness.shared_toolchain.SUPERVISOR_PHASE_ENV:"A-verify",
                harness.shared_toolchain.SUPERVISOR_OPERATION_ENV:"verify"})
            dag = phases.PhaseDAG(parent/"dag", [phases.PhaseSpec("A-verify")], environment=phase_env)
            original_dag = harness.ACTIVE_DAG; original_proofs = harness.PRODUCT_PHASE_PROOFS
            try:
                with mock.patch.dict(os.environ, phase_env, clear=True):
                    scan = harness.shared_toolchain.scan_tree(root, component_names=names, seal_snapshot=True)
                    receipt = harness.shared_toolchain.make_tree_receipt(scan, lock, "verify")
                    receipt_path = harness.shared_toolchain.write_tree_receipt(root, receipt)
                harness.ACTIVE_DAG = dag; harness.PRODUCT_PHASE_PROOFS = {}
                payload_reads: list[str] = []; original_read = pathlib.Path.read_bytes
                def counted_read(path: pathlib.Path):
                    if path.is_relative_to(root) and path.name != "installation.json":
                        payload_reads.append(str(path))
                    return original_read(path)
                with mock.patch.object(harness.shared_toolchain, "validate_lock_structure", return_value=(lock, {})), \
                     mock.patch.object(pathlib.Path, "read_bytes", counted_read):
                    harness.phase("A-verify", ["/usr/bin/true"], timeout=10, env=phase_env,
                        product_receipts=[(receipt_path, root, root, "A", "verify")])
                closure = dag.finalize(); proof = harness.PRODUCT_PHASE_PROOFS["A-verify"][0]
                self.assertEqual((str(root), "A", "A-verify", "verify"),
                                 (proof["expected_root"], proof["room"], proof["phase"], proof["operation"]))
                events = [json.loads(line) for line in dag.events.path.read_text().splitlines()]
                start = next(event for event in events if event["event"] == "start")
                self.assertEqual(64, len(start["input_digest"]))
                self.assertIn("A-verify", closure["phase_receipts"])
                self.assertEqual([], payload_reads, "atomic commit must not reread product payload bytes")
                rebound_env = {**phase_env,
                    harness.shared_toolchain.SUPERVISOR_PHASE_ENV:"A-version-log",
                    harness.shared_toolchain.SUPERVISOR_OPERATION_ENV:"version-log"}
                with mock.patch.dict(os.environ, rebound_env, clear=True):
                    rebound = harness.shared_toolchain.reissue_tree_receipt(receipt, "version-log")
                    harness.shared_toolchain.write_tree_receipt(root, rebound)
                with mock.patch.object(harness.shared_toolchain, "validate_lock_structure", return_value=(lock, {})):
                    proof = harness._validated_product_tree_receipt(receipt_path, root, root, "A",
                                                         "A-version-log", "version-log")
                self.assertEqual("A-version-log", proof["scan_authorization"]["phase"])
                self.assertTrue(any(event["event"] == "receipt-reuse"
                                    for event in supervisor.summary()["events"]))
                harness.validate_scan_ledger(supervisor.summary(), supervisor.run_id)
            finally:
                harness.ACTIVE_DAG = original_dag; harness.PRODUCT_PHASE_PROOFS = original_proofs
                supervisor.close()

    def test_sealed_snapshot_fd_attacks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw).resolve(); root = parent/"install"; root.mkdir()
            (root/"payload").write_text("sealed")
            supervisor = harness.ScanSupervisor("fd-attacks")
            env = {**os.environ, harness.shared_toolchain.SUPERVISOR_ENV:supervisor.run_id,
                   harness.shared_toolchain.SUPERVISOR_FD_ENV:str(supervisor.fd),
                   harness.shared_toolchain.SUPERVISOR_ROOM_ENV:"A",
                   harness.shared_toolchain.SUPERVISOR_PHASE_ENV:"A-install",
                   harness.shared_toolchain.SUPERVISOR_OPERATION_ENV:"install"}
            try:
                with mock.patch.dict(os.environ, env, clear=True):
                    scan = harness.shared_toolchain.scan_tree(root, seal_snapshot=True)
                proof = scan["sealed_snapshot"]; self.assertEqual(1, scan["counters"]["reads"])
                self.assertEqual(proof, supervisor._snapshot_proof(str(root)))
                held = supervisor._snapshots[str(root)]; original_fd = held["fd"]
                original_raw = supervisor._read_fd(original_fd)
                variants = {"swap":b"x" * len(original_raw), "truncated":original_raw[:-512],
                            "tar-tamper":original_raw[:100] + bytes([original_raw[100] ^ 1]) + original_raw[101:]}
                for label, value in variants.items():
                    attack = parent/f"{label}.tar"; attack.write_bytes(value)
                    replacement = os.open(attack, os.O_RDONLY); held["fd"] = replacement
                    with self.subTest(label=label), self.assertRaises(harness.HarnessError):
                        supervisor._snapshot_proof(str(root))
                    os.close(replacement); held["fd"] = original_fd
                backup = os.dup(original_fd); replacement = os.open(parent/"swap.tar", os.O_RDONLY)
                os.dup2(replacement, original_fd); os.close(replacement)
                with self.subTest(label="fd-reuse"), self.assertRaises(harness.HarnessError):
                    supervisor._snapshot_proof(str(root))
                os.dup2(backup, original_fd); os.close(backup)
                self.assertEqual(proof, supervisor._snapshot_proof(str(root)))
                os.close(original_fd)
                with self.subTest(label="closed"), self.assertRaises(harness.HarnessError):
                    supervisor._snapshot_proof(str(root))
            finally: supervisor.close()

    def test_six_late_live_mutations_cannot_change_proof_or_sealed_restore(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parent = pathlib.Path(raw).resolve(); stage = parent/".install.next"; root = parent/"install"
            stage.mkdir(); names = ("graphviz", "humanizer", "mermaid", "pdf-home")
            for name in names:
                component = stage/name; component.mkdir(); (component/"payload").write_text(name)
            inventory = {name:harness.shared_toolchain.inventory_digest(stage/name) for name in names}
            lock = {"release":{"id":"mini"}, "normative":{"installed_inventory":inventory}}
            state = {"schema_version":"brandkit-toolchain-installation.v1", "release":"mini",
                     "lock_sha256":harness.sha(harness.shared_toolchain.LOCK_PATH), "components":inventory}
            (stage/"installation.json").write_text(json.dumps(state, sort_keys=True, separators=(",", ":"))+"\n")
            supervisor = harness.ScanSupervisor("late-mutations")
            install_env = {**os.environ, harness.shared_toolchain.SUPERVISOR_ENV:supervisor.run_id,
                harness.shared_toolchain.SUPERVISOR_FD_ENV:str(supervisor.fd),
                harness.shared_toolchain.SUPERVISOR_ROOM_ENV:"A",
                harness.shared_toolchain.SUPERVISOR_PHASE_ENV:"A-install",
                harness.shared_toolchain.SUPERVISOR_OPERATION_ENV:"install"}
            try:
                with mock.patch.dict(os.environ, install_env, clear=True):
                    harness.shared_toolchain.scan_tree(stage, component_names=names, logical_root=root)
                    os.replace(stage, root)
                    scan = harness.shared_toolchain.scan_tree(root, component_names=names, seal_snapshot=True)
                    receipt = harness.shared_toolchain.make_tree_receipt(scan, lock, "install")
                    receipt_path = harness.shared_toolchain.write_tree_receipt(root, receipt)
                proof = scan["sealed_snapshot"]; expected = {name:(root/name/"payload").read_bytes() for name in names}
                attacks = [
                    lambda: (root/"graphviz/payload").write_text("changed"),
                    lambda: (root/"extra").write_text("extra"),
                    lambda: (root/"humanizer/payload").unlink(),
                    lambda: (root/"mermaid").chmod(0o700),
                    lambda: ((root/"graphviz/payload").unlink(),
                             (root/"graphviz/payload").symlink_to("../mermaid/payload")),
                    lambda: (root/"pdf-home/payload").rename(root/"pdf-home/renamed"),
                ]
                for attack in attacks:
                    attack(); self.assertEqual(proof, supervisor._snapshot_proof(str(root)))
                rollback_env = {**install_env, harness.shared_toolchain.SUPERVISOR_PHASE_ENV:"A-rollback",
                                harness.shared_toolchain.SUPERVISOR_OPERATION_ENV:"rollback"}
                with mock.patch.dict(os.environ, rollback_env, clear=True):
                    receipt = harness.shared_toolchain.reissue_tree_receipt(receipt, "rollback")
                    harness.shared_toolchain.write_tree_receipt(root, receipt)
                quarantine = root.with_name(root.name+".rolled-back-"+harness.sha(root/"installation.json")[:16])
                os.replace(root, quarantine); root.mkdir(mode=0o700); qinfo = quarantine.stat()
                marker = {"schema_version":"brandkit-toolchain-rollback.v2", "rolled_back_release":"mini",
                          "quarantine":str(quarantine), "quarantine_device":qinfo.st_dev,
                          "quarantine_inode":qinfo.st_ino, "lock_sha256":harness.sha(harness.shared_toolchain.LOCK_PATH),
                          "tree_receipt_digest":receipt["receipt_digest"], "tree_merkle":receipt["tree_merkle"],
                          "identity_digest":receipt["identity_digest"]}
                (root/"rollback.json").write_text(json.dumps(marker, sort_keys=True, separators=(",", ":"))+"\n")
                restore_env = {**install_env, harness.shared_toolchain.SUPERVISOR_PHASE_ENV:"A-restore",
                               harness.shared_toolchain.SUPERVISOR_OPERATION_ENV:"restore"}
                with mock.patch.dict(os.environ, restore_env, clear=True), \
                     mock.patch.object(harness.shared_toolchain, "validate_lock", return_value=(lock, {})), \
                     mock.patch.object(harness.shared_toolchain, "verify_pdf_install"):
                    harness.shared_toolchain.restore_sealed(type("Args", (), {"root":str(root)})())
                self.assertEqual(expected, {name:(root/name/"payload").read_bytes() for name in names})
                self.assertTrue(quarantine.is_dir(), "public rollback quarantine remains recoverable")
                self.assertEqual(3, supervisor.summary()["counts"][str(root)])
            finally: supervisor.close()

    def test_receipt_swap_replay_and_closed_event_log_attacks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            state = pathlib.Path(raw); counters: dict[str, int] = {}
            first, _ = self.miniature(state, "locked\n", counters)
            receipts = state / "dag/receipts"
            source_receipt = json.loads((receipts/"source.json").read_text()); source_receipt["unknown"] = True
            phases.atomic_json(receipts/"source.json", source_receipt)
            before_source = counters["source"]
            self.miniature(state, "locked\n", counters, resume=True)
            self.assertEqual(before_source + 1, counters["source"])
            a = json.loads((receipts/"A-package.json").read_text())
            b = json.loads((receipts/"B-package.json").read_text())
            phases.atomic_json(receipts/"A-package.json", b); phases.atomic_json(receipts/"B-package.json", a)
            before = (counters["A-package"], counters["B-package"])
            self.miniature(state, "locked\n", counters, resume=True)
            self.assertEqual((before[0]+1, before[1]+1), (counters["A-package"], counters["B-package"]))
            replay = json.loads((receipts/"A-package.json").read_text())
            self.miniature(state, "changed\n", counters, resume=True)
            phases.atomic_json(receipts/"A-package.json", replay)
            before = counters["A-package"]
            self.miniature(state, "changed\n", counters, resume=True)
            self.assertEqual(before + 1, counters["A-package"])

            log = first.events.path
            original = log.read_bytes()
            attacks = []
            event = json.loads(original.splitlines()[0]); event["unknown"] = True
            attacks.append((json.dumps(event)+"\n").encode() + b"\n".join(original.splitlines()[1:]) + b"\n")
            event = json.loads(original.splitlines()[0]); event["event"] = "unknown"
            attacks.append((json.dumps(event)+"\n").encode() + b"\n".join(original.splitlines()[1:]) + b"\n")
            attacks.append(original[:-1])
            for index, attack in enumerate(attacks):
                path = state / f"event-attack-{index}.jsonl"; path.write_bytes(attack)
                with self.subTest(index=index), self.assertRaises(phases.PhaseError):
                    phases.validate_event_log(path)

            log.chmod(0o600); log.write_bytes(original + original.splitlines(keepends=True)[0])
            with self.assertRaises(phases.PhaseError):
                phases.PhaseDAG(state / "dag", [phases.PhaseSpec("source", resumable=True)], resume=True)


if __name__ == "__main__":
    unittest.main()
