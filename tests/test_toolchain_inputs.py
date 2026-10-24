from __future__ import annotations

import hashlib
import io
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import tarfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))  # validate_lock_structure importiert runtime_archives
SPEC = importlib.util.spec_from_file_location("h2_shared_toolchain", ROOT / "scripts/shared_toolchain.py")
toolchain = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(toolchain)


class RunInputPreflightTests(unittest.TestCase):
    def fixture(self, root: pathlib.Path) -> tuple[dict, pathlib.Path, pathlib.Path]:
        external = root / "external"; external.mkdir()
        manifest_names = {"humanizer_wheelhouse", "pdf_wheelhouse", "mermaid_bundle",
                          "graphviz_bundle", "qemu_runtime"}
        names = {"humanizer_source", "humanizer_wheelhouse", "pdf_wheelhouse", "pdf_issuer_key",
                 "mermaid_bundle", "graphviz_bundle", "normative_render_oci", "node_linux",
                 "chrome_linux", "qemu_runtime"}
        records = {}
        for name in sorted(names):
            if name in manifest_names:
                cas_id = hashlib.sha256((name + "-cas").encode()).hexdigest()
                artifact = external / cas_id; artifact.mkdir()
                member = artifact / "member.bin"; member.write_bytes((name + "-bytes").encode())
                manifest = artifact / "files.sha256.json"
                payload = {"schema_version":"synthetic-artifact.v1", "files":[{
                    "name":"member.bin", "size":member.stat().st_size,
                    "sha256":hashlib.sha256(member.read_bytes()).hexdigest()}]}
                manifest.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
                records[name] = {"path":str(artifact), "kind":"directory", "sha256":cas_id,
                                 "manifest":str(manifest),
                                 "manifest_sha256":hashlib.sha256(manifest.read_bytes()).hexdigest()}
            else:
                artifact = external / f"{name}.bin"; artifact.write_bytes((name + "-bytes").encode())
                records[name] = {"path":str(artifact), "kind":"file",
                                 "sha256":hashlib.sha256(artifact.read_bytes()).hexdigest()}
        lock = {"artifacts":records}
        lock_path = root / "toolchain.lock.json"
        lock_path.write_text(json.dumps(lock, sort_keys=True), encoding="utf-8")
        return lock, lock_path, external

    def preflight(self, root: pathlib.Path, run_id: str = "run-a", **kwargs):
        lock, lock_path, external = self.fixture(root)
        run_root = root / run_id
        with mock.patch.object(toolchain, "LOCK_PATH", lock_path):
            receipt = toolchain.preflight_run_inputs(lock, run_root, run_id, **kwargs)
        return lock, lock_path, external, run_root, receipt

    def archive_variant(self, descriptor: int, *, rename=None, metadata=False,
                        tar_format=tarfile.USTAR_FORMAT) -> bytes:
        source = toolchain._read_archive_fd(descriptor)
        members = []
        with tarfile.open(fileobj=io.BytesIO(source), mode="r:") as archive:
            for member in archive.getmembers():
                stream = archive.extractfile(member) if member.isfile() else None
                members.append((member, stream.read() if stream is not None else None))
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w", format=tar_format) as archive:
            for index, (member, value) in enumerate(members):
                if rename is not None:
                    member.name = rename(member.name)
                if metadata and index == 0:
                    member.mode = 0o777; member.uid = 123; member.gid = 456
                    member.uname = "attacker"; member.gname = "attackers"; member.mtime = 99
                archive.addfile(member, io.BytesIO(value) if value is not None else None)
        return output.getvalue()

    def coordinate_archive_receipt(self, run_root: pathlib.Path, name: str, raw: bytes) -> None:
        manifest_path = run_root / "snapshot-manifest.json"
        receipt_path = run_root / "run-inputs.json"
        manifest = json.loads(manifest_path.read_text())
        receipt = json.loads(receipt_path.read_text())
        snapshot = receipt["artifacts"][name]["snapshot"]
        snapshot["archive_sha256"] = hashlib.sha256(raw).hexdigest()
        snapshot["archive_size"] = len(raw)
        manifest["artifacts"][name] = snapshot
        manifest_path.chmod(0o600)
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        receipt["snapshot_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        unsigned = dict(receipt); unsigned.pop("receipt_digest")
        receipt["receipt_digest"] = hashlib.sha256(json.dumps(
            unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        receipt_path.chmod(0o600)
        receipt_path.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")

    def test_single_full_read_snapshot_and_ab_identity_without_external_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); reads = {}
            lock, lock_path, external, run_root, receipt = self.preflight(
                root, full_read_counter=reads)
            self.assertEqual({name:1 for name in lock["artifacts"]}, reads)
            self.assertEqual(reads, receipt["full_read_counts"])
            self.assertEqual(set(toolchain.CONSUMED_RUN_ARTIFACTS),
                             set(json.loads((run_root/"snapshot-manifest.json").read_text())["artifacts"]))
            original_open = pathlib.Path.open
            def guarded(path: pathlib.Path, *args, **kwargs):
                if path.resolve(strict=False).is_relative_to(external.resolve()):
                    raise AssertionError(f"external source reopened after preflight: {path}")
                return original_open(path, *args, **kwargs)
            with mock.patch.object(toolchain, "LOCK_PATH", lock_path), mock.patch.object(pathlib.Path, "open", guarded):
                rebound_a, loaded_a = toolchain.load_run_inputs(lock, run_root/"run-inputs.json", expected_run_id="run-a")
                rebound_b, loaded_b = toolchain.load_run_inputs(lock, run_root/"run-inputs.json", expected_run_id="run-a")
                with mock.patch.dict(os.environ, {toolchain.RUN_INPUT_ENV:str(run_root/"run-inputs.json"),
                                                   toolchain.RUN_ID_ENV:"run-a"}):
                    inputs_a = toolchain.install_artifact_inputs(rebound_a, None, root/"consumer-A")
                    inputs_b = toolchain.install_artifact_inputs(rebound_b, None, root/"consumer-B")
            self.assertEqual(loaded_a["receipt_digest"], loaded_b["receipt_digest"])
            self.assertNotEqual(inputs_a, inputs_b)
            self.assertEqual({name:toolchain._snapshot_inventory(path)[0] for name,path in inputs_a.items()},
                             {name:toolchain._snapshot_inventory(path)[0] for name,path in inputs_b.items()})
            self.assertEqual(len(inputs_a), 6)
            self.assertTrue(all(path.stat().st_mode & 0o222 == 0 for path in inputs_a.values()))

    def test_source_exchange_extra_symlink_hardlink_and_toctou_fail(self) -> None:
        attacks = ("exchange", "extra", "symlink", "hardlink", "source-manifest")
        for attack in attacks:
            with self.subTest(attack=attack), tempfile.TemporaryDirectory() as raw:
                root = pathlib.Path(raw); lock, lock_path, _, = self.fixture(root)
                if attack == "exchange":
                    pathlib.Path(lock["artifacts"]["humanizer_source"]["path"]).write_bytes(b"exchange")
                elif attack == "extra":
                    pathlib.Path(lock["artifacts"]["mermaid_bundle"]["path"]).joinpath("extra").write_text("x")
                elif attack == "symlink":
                    source = pathlib.Path(lock["artifacts"]["humanizer_source"]["path"])
                    target = source.with_name("target"); target.write_bytes(source.read_bytes()); source.unlink(); source.symlink_to(target)
                else:
                    if attack == "hardlink":
                        source = pathlib.Path(lock["artifacts"]["humanizer_source"]["path"])
                        os.link(source, source.with_name("alias"))
                    else:
                        manifest = pathlib.Path(lock["artifacts"]["graphviz_bundle"]["manifest"])
                        manifest.write_bytes(manifest.read_bytes() + b" ")
                with mock.patch.object(toolchain, "LOCK_PATH", lock_path), self.assertRaises(toolchain.ToolchainError):
                    toolchain.preflight_run_inputs(lock, root/"run", "attack")

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); lock, lock_path, _ = self.fixture(root)
            def race(stage: str, name: str, source: pathlib.Path) -> None:
                if stage == "after_copy" and name == "humanizer_source":
                    source.chmod(0o600 if source.stat().st_mode & 0o777 != 0o600 else 0o400)
            with mock.patch.object(toolchain, "LOCK_PATH", lock_path), self.assertRaises(toolchain.ToolchainError):
                toolchain.preflight_run_inputs(lock, root/"run", "race", fault_hook=race)

    def test_manifest_tamper_snapshot_tamper_and_receipt_swap_replay_fail(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); lock, lock_path, _, run_root, receipt = self.preflight(root)
            manifest = run_root / "snapshot-manifest.json"; manifest.chmod(0o600)
            manifest.write_bytes(manifest.read_bytes() + b" ")
            with mock.patch.object(toolchain, "LOCK_PATH", lock_path), self.assertRaises(toolchain.ToolchainError):
                toolchain.load_run_inputs(lock, run_root/"run-inputs.json", expected_run_id="run-a")

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); lock, lock_path, _, run_root, receipt = self.preflight(root)
            with mock.patch.object(toolchain, "LOCK_PATH", lock_path):
                with self.assertRaises(toolchain.ToolchainError):
                    toolchain.load_run_inputs(lock, run_root/"run-inputs.json", expected_run_id="other-run")
                swapped = root / "swapped"; swapped.mkdir(); (swapped/"run-inputs.json").write_bytes(
                    (run_root/"run-inputs.json").read_bytes())
                with self.assertRaises(toolchain.ToolchainError):
                    toolchain.load_run_inputs(lock, swapped/"run-inputs.json", expected_run_id="run-a")
                (run_root/"run-inputs.json").chmod(0o600)
                nested = json.loads((run_root/"run-inputs.json").read_text())
                nested["artifacts"]["humanizer_source"]["unknown"] = True
                unsigned = dict(nested); unsigned.pop("receipt_digest")
                nested["receipt_digest"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest()
                (run_root/"run-inputs.json").write_text(json.dumps(nested, sort_keys=True))
                with self.assertRaises(toolchain.ToolchainError):
                    toolchain.load_run_inputs(lock, run_root/"run-inputs.json", expected_run_id="run-a")

    def test_coordinated_rehash_post_load_mutation_aliases_and_transaction_failure_fail(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); lock, lock_path, _, run_root, receipt = self.preflight(root)
            with mock.patch.object(toolchain, "LOCK_PATH", lock_path):
                toolchain.load_run_inputs(lock, run_root/"run-inputs.json", expected_run_id="run-a")
                descriptor = toolchain.HELD_RUN_INPUT_FDS["run-a"]["humanizer_source"]
                with self.assertRaises(OSError): os.write(descriptor, b"attack")
                malicious_path = root/"malicious.tar"
                payload = b"X" * len(pathlib.Path(lock["artifacts"]["humanizer_source"]["path"]).read_bytes())
                with tarfile.open(malicious_path, "w", format=tarfile.USTAR_FORMAT) as archive:
                    info = tarfile.TarInfo("artifact"); info.size = len(payload); archive.addfile(info, io.BytesIO(payload))
                malicious_fd = os.open(malicious_path, os.O_RDONLY); malicious_path.unlink()
                toolchain.HELD_RUN_INPUT_FDS["run-a"]["humanizer_source"] = malicious_fd
                with mock.patch.dict(os.environ, {toolchain.RUN_INPUT_ENV:str(run_root/"run-inputs.json"),
                                                   toolchain.RUN_ID_ENV:"run-a"}), \
                     self.assertRaises(toolchain.ToolchainError):
                    toolchain.install_artifact_inputs(lock, None, root/"post-load-consumer")
                attacked = json.loads((run_root/"run-inputs.json").read_text())
                malicious_raw = toolchain._read_archive_fd(malicious_fd)
                attacked["artifacts"]["humanizer_source"]["snapshot"]["archive_sha256"] = hashlib.sha256(malicious_raw).hexdigest()
                attacked["artifacts"]["humanizer_source"]["snapshot"]["archive_size"] = len(malicious_raw)
                manifest = json.loads((run_root/"snapshot-manifest.json").read_text())
                manifest["artifacts"]["humanizer_source"] = attacked["artifacts"]["humanizer_source"]["snapshot"]
                (run_root/"snapshot-manifest.json").chmod(0o600)
                (run_root/"snapshot-manifest.json").write_text(json.dumps(manifest, sort_keys=True))
                attacked["snapshot_manifest_sha256"] = hashlib.sha256(
                    (run_root/"snapshot-manifest.json").read_bytes()).hexdigest()
                unsigned = dict(attacked); unsigned.pop("receipt_digest")
                attacked["receipt_digest"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest()
                (run_root/"run-inputs.json").chmod(0o600)
                (run_root/"run-inputs.json").write_text(json.dumps(attacked, sort_keys=True))
                with self.assertRaises(toolchain.ToolchainError):
                    toolchain.load_run_inputs(lock, run_root/"run-inputs.json", expected_run_id="run-a")
                os.close(descriptor)

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); lock, lock_path, _ = self.fixture(root)
            actual = root/"actual"; actual.mkdir(); alias = root/"alias"; alias.symlink_to(actual, target_is_directory=True)
            for candidate in (alias/"run", root/"actual/../actual/run"):
                with self.subTest(candidate=candidate), mock.patch.object(toolchain, "LOCK_PATH", lock_path), \
                     self.assertRaises(toolchain.ToolchainError):
                    toolchain.preflight_run_inputs(lock, candidate, "alias")

        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); lock, lock_path, _ = self.fixture(root); target = root/"failed"
            def fail(stage: str, name: str, path: pathlib.Path) -> None:
                if stage == "after_manifest": raise toolchain.ToolchainError("injected publication failure")
            with mock.patch.object(toolchain, "LOCK_PATH", lock_path), self.assertRaises(toolchain.ToolchainError):
                toolchain.preflight_run_inputs(lock, target, "failed", fault_hook=fail)
            self.assertFalse(target.exists())
            self.assertEqual([], list(root.glob(".failed.next-*")))

    def test_noncanonical_ustar_representations_fail_loader_and_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = pathlib.Path(raw); lock, lock_path, _, run_root, _ = self.preflight(root)
            original_receipt = (run_root/"run-inputs.json").read_bytes()
            original_manifest = (run_root/"snapshot-manifest.json").read_bytes()
            original_fds = dict(toolchain.HELD_RUN_INPUT_FDS["run-a"])
            attacks = {
                "artifact-dot": ("humanizer_source", dict(rename=lambda name:
                    "artifact/." if name == "artifact" else name)),
                "double-separator": ("humanizer_wheelhouse", dict(rename=lambda name:
                    "artifact//member.bin" if name == "artifact/member.bin" else name)),
                "embedded-dot": ("humanizer_wheelhouse", dict(rename=lambda name:
                    "artifact/./member.bin" if name == "artifact/member.bin" else name)),
                "backslash": ("humanizer_wheelhouse", dict(rename=lambda name:
                    "artifact\\member.bin" if name == "artifact/member.bin" else name)),
                "metadata": ("humanizer_source", dict(metadata=True)),
                "alternate-tar": ("humanizer_source", dict(tar_format=tarfile.GNU_FORMAT)),
            }
            for attack, (name, options) in attacks.items():
                with self.subTest(attack=attack):
                    (run_root/"run-inputs.json").chmod(0o600)
                    (run_root/"snapshot-manifest.json").chmod(0o600)
                    (run_root/"run-inputs.json").write_bytes(original_receipt)
                    (run_root/"snapshot-manifest.json").write_bytes(original_manifest)
                    variant = self.archive_variant(original_fds[name], **options)
                    self.assertNotEqual(toolchain._read_archive_fd(original_fds[name]), variant)
                    candidate = root/f"{attack}.tar"; candidate.write_bytes(variant)
                    descriptor = os.open(candidate, os.O_RDONLY); candidate.unlink()
                    toolchain.HELD_RUN_INPUT_FDS["run-a"] = dict(original_fds)
                    toolchain.HELD_RUN_INPUT_FDS["run-a"][name] = descriptor
                    self.coordinate_archive_receipt(run_root, name, variant)
                    with mock.patch.object(toolchain, "LOCK_PATH", lock_path), \
                         self.assertRaises(toolchain.ToolchainError):
                        toolchain.load_run_inputs(lock, run_root/"run-inputs.json", expected_run_id="run-a")
                    with mock.patch.dict(os.environ, {toolchain.RUN_INPUT_ENV:str(run_root/"run-inputs.json"),
                                                       toolchain.RUN_ID_ENV:"run-a"}), \
                         self.assertRaises(toolchain.ToolchainError):
                        toolchain.install_artifact_inputs(lock, None, root/f"consumer-{attack}")
                    os.close(descriptor)
            toolchain.HELD_RUN_INPUT_FDS["run-a"] = original_fds

    def test_structure_validation_does_not_open_external_artifact_paths(self) -> None:
        real_lock = json.loads((ROOT/"toolchain/toolchain.lock.json").read_text())
        external = [toolchain.artifact_path(record["path"], name).resolve(strict=False) for name, record in real_lock["artifacts"].items()]
        original_open = pathlib.Path.open
        def guarded(path: pathlib.Path, *args, **kwargs):
            resolved = path.resolve(strict=False)
            if any(resolved == source or resolved.is_relative_to(source) for source in external):
                raise AssertionError(f"structure validation opened external artifact: {path}")
            return original_open(path, *args, **kwargs)
        with mock.patch.object(pathlib.Path, "open", guarded):
            lock, components = toolchain.validate_lock_structure()
        self.assertEqual(set(lock["artifacts"]), set(real_lock["artifacts"]))
        self.assertEqual(5, len(components))


class ArtifactRootTests(unittest.TestCase):
    """Lock-Pfade relativ zu ${BRANDKIT_SIDECAR} oder ${HOME}: kein Ausbruch, keine Link-Umgehung."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name).resolve()
        self.sidecar = base / "sidecar"; self.home = base / "home"; self.outside = base / "outside"
        for path in (self.sidecar / "external-artifacts", self.home / ".agents/keys", self.outside):
            path.mkdir(parents=True)
        (self.sidecar / "external-artifacts/a.tar").write_bytes(b"a")
        (self.outside / "secret").write_bytes(b"s")
        env = mock.patch.dict(os.environ, {"BRANDKIT_SIDECAR": str(self.sidecar), "HOME": str(self.home)})
        env.start(); self.addCleanup(env.stop); self.addCleanup(self.tmp.cleanup)

    def test_tokens_resolve_against_their_root(self) -> None:
        self.assertEqual(self.sidecar / "external-artifacts/a.tar", toolchain.artifact_path("${BRANDKIT_SIDECAR}/external-artifacts/a.tar"))
        self.assertEqual(self.home / ".agents/keys/k.key", toolchain.artifact_path("${HOME}/.agents/keys/k.key"))

    def test_absolute_paths_stay_for_runtime_copies(self) -> None:
        self.assertEqual(self.outside / "secret", toolchain.artifact_path(str(self.outside / "secret")))

    def test_escape_and_malformed_paths_fail(self) -> None:
        for value in ("${BRANDKIT_SIDECAR}/../outside/secret", "${BRANDKIT_SIDECAR}/external-artifacts/../../outside/secret",
                      "${BRANDKIT_SIDECAR}/./a", "${BRANDKIT_SIDECAR}//a", "${BRANDKIT_SIDECAR}/", "${BRANDKIT_SIDECAR}/a\\b",
                      "${UNKNOWN}/a", "external-artifacts/a.tar", "~/a", ""):
            with self.subTest(value=value), self.assertRaises(toolchain.ToolchainError):
                toolchain.artifact_path(value)

    def test_symlink_out_of_the_root_fails(self) -> None:
        (self.sidecar / "external-artifacts/link").symlink_to(self.outside)
        (self.sidecar / "external-artifacts/file-link").symlink_to(self.outside / "secret")
        for value in ("${BRANDKIT_SIDECAR}/external-artifacts/link/secret", "${BRANDKIT_SIDECAR}/external-artifacts/file-link"):
            with self.subTest(value=value), self.assertRaisesRegex(toolchain.ToolchainError, "link"):
                toolchain.artifact_path(value)

    def test_symlink_inside_the_root_is_allowed(self) -> None:
        (self.sidecar / "inner").symlink_to(self.sidecar / "external-artifacts")
        self.assertEqual(self.sidecar / "inner/a.tar", toolchain.artifact_path("${BRANDKIT_SIDECAR}/inner/a.tar"))

    def test_real_lock_has_no_personal_absolute_artifact_paths(self) -> None:
        lock = json.loads((ROOT / "toolchain/toolchain.lock.json").read_text())
        for name, record in lock["artifacts"].items():
            for field in ("path", "manifest"):
                if field in record:
                    with self.subTest(artifact=name, field=field):
                        self.assertRegex(record[field], r"^\$\{(BRANDKIT_SIDECAR|HOME)\}/")


if __name__ == "__main__":
    unittest.main()
