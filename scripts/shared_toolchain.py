#!/usr/bin/env python3
"""Closed, offline orchestration for the BrandKit shared toolchain locks."""
from __future__ import annotations

import argparse
import array
import atexit
import base64
import concurrent.futures
import csv
import hashlib
import hmac
import io
import json
import os
import pathlib
import signal
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "toolchain/toolchain.lock.json"
HASH = set("0123456789abcdef")


class ToolchainError(RuntimeError):
    pass


def sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: pathlib.Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ToolchainError(f"required regular file is absent or linked: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ToolchainError(f"invalid JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ToolchainError(f"JSON root is not an object: {path}")
    return value


def closed(value: dict, required: set[str], label: str) -> None:
    if set(value) != required:
        raise ToolchainError(f"{label} fields differ: {sorted(set(value) ^ required)}")


def digest(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value) - HASH:
        raise ToolchainError(f"{label} is not a lowercase SHA-256")
    return value


def regular_under_root(relative: str, label: str) -> pathlib.Path:
    if not isinstance(relative, str) or pathlib.PurePosixPath(relative).is_absolute() or ".." in pathlib.PurePosixPath(relative).parts:
        raise ToolchainError(f"unsafe {label} path")
    path = ROOT / relative
    path.resolve(strict=True).relative_to(ROOT)
    if path.is_symlink() or not path.is_file():
        raise ToolchainError(f"{label} is absent or linked")
    return path


def validate_lock_structure(path: pathlib.Path = LOCK_PATH) -> tuple[dict, dict[str, dict]]:
    lock = load(path)
    closed(lock, {"schema_version", "release", "execution", "environment", "components", "artifacts", "runtime_archives", "bindings", "transforms", "normative", "commands"}, "shared lock")
    if lock["schema_version"] != "brandkit-toolchain-lock.v1":
        raise ToolchainError("shared lock schema identity differs")
    closed(lock["release"], {"id", "source_revision"}, "release")
    if lock["release"]["source_revision"] != "d651794":
        raise ToolchainError("release source revision differs")
    closed(lock["execution"], {"host_platform", "render_platform", "container_image", "container_digest", "base_image", "base_image_digest", "worker_container_image"}, "execution")
    image = lock["execution"]["container_image"]
    container_digest = lock["execution"]["container_digest"]
    if not isinstance(image, str) or "@sha256:" not in image or image.rsplit("@", 1)[1] != container_digest:
        raise ToolchainError("container reference is not digest-bound")
    if not isinstance(container_digest, str) or not container_digest.startswith("sha256:"):
        raise ToolchainError("container digest differs")
    digest(container_digest[7:], "container digest")
    if lock["execution"]["render_platform"] != "linux-amd64":
        raise ToolchainError("render platform differs")
    base_image, base_digest = lock["execution"]["base_image"], lock["execution"]["base_image_digest"]
    if not isinstance(base_image, str) or "@sha256:" not in base_image or base_image.rsplit("@", 1)[1] != base_digest:
        raise ToolchainError("base image is not digest-bound")
    digest(base_digest.removeprefix("sha256:"), "base image digest")
    closed(lock["environment"], {"LANG", "LC_ALL", "TZ", "SOURCE_DATE_EPOCH", "network"}, "environment")
    if lock["environment"] != {"LANG": "C", "LC_ALL": "C", "TZ": "UTC", "SOURCE_DATE_EPOCH": "0", "network": "disabled"}:
        raise ToolchainError("deterministic environment differs")
    expected_components = {"humanizer", "pdf_renderer", "mermaid", "mermaid_linux_worker", "graphviz"}
    if set(lock["components"]) != expected_components:
        raise ToolchainError("component lock set is not closed")
    components: dict[str, dict] = {}
    for name, record in lock["components"].items():
        closed(record, {"path", "sha256", "schema_version"}, f"component {name}")
        component_path = regular_under_root(record["path"], f"component {name}")
        if sha(component_path) != digest(record["sha256"], f"component {name}"):
            raise ToolchainError(f"component lock hash differs: {name}")
        component = load(component_path)
        if component.get("schema_version") != record["schema_version"]:
            raise ToolchainError(f"component schema differs: {name}")
        components[name] = component
    closed(lock["bindings"], {"python_version", "python_executable", "python_sha256", "node_version", "chrome_version", "chrome_sha256", "font_path", "font_postscript_name", "font_sha256"}, "bindings")
    bindings = lock["bindings"]
    if components["humanizer"]["python"]["version"] != bindings["python_version"] or components["pdf_renderer"]["platform"]["python"] != bindings["python_version"]:
        raise ToolchainError("Python cross-lock binding differs")
    python_executable = pathlib.Path(bindings["python_executable"])
    if not python_executable.is_absolute() or not python_executable.is_file() or sha(python_executable) != digest(bindings["python_sha256"], "Python executable"):
        raise ToolchainError("Python executable binding differs")
    if components["mermaid"]["node"]["version"] != bindings["node_version"] or components["graphviz"]["node"]["version"] != bindings["node_version"]:
        raise ToolchainError("Node cross-lock binding differs")
    if components["mermaid"]["browser"]["version"] != bindings["chrome_version"] or components["pdf_renderer"]["platform"]["chrome"]["version"] != bindings["chrome_version"]:
        raise ToolchainError("Chrome version cross-lock binding differs")
    if components["mermaid"]["browser"]["executable_sha256"] != bindings["chrome_sha256"] or components["pdf_renderer"]["platform"]["chrome"]["sha256"] != bindings["chrome_sha256"]:
        raise ToolchainError("Chrome hash cross-lock binding differs")
    font = regular_under_root(bindings["font_path"], "font")
    if sha(font) != bindings["font_sha256"] or components["graphviz"]["font"]["archive_sha256"] != bindings["font_sha256"]:
        raise ToolchainError("font cross-lock binding differs")
    if components["mermaid_linux_worker"]["base_image"]["reference"] != lock["execution"]["worker_container_image"]:
        raise ToolchainError("container cross-lock binding differs")
    closed(lock["transforms"], {"humanizer_doctor_runtime_adapter", "network_deny_policy"}, "transforms")
    adapter = lock["transforms"]["humanizer_doctor_runtime_adapter"]
    closed(adapter, {"path", "sha256", "child_timeout_seconds", "doctor_argument"}, "Humanizer doctor runtime adapter")
    if adapter["child_timeout_seconds"] != 600 or adapter["doctor_argument"] != "--require-full":
        raise ToolchainError("Humanizer doctor runtime adapter contract differs")
    adapter_path = regular_under_root(adapter["path"], "Humanizer doctor runtime adapter")
    if sha(adapter_path) != digest(adapter["sha256"], "Humanizer doctor runtime adapter"):
        raise ToolchainError("Humanizer doctor runtime adapter hash differs")
    policy = lock["transforms"]["network_deny_policy"]
    closed(policy, {"path", "sha256", "loopback"}, "network deny policy")
    if policy["loopback"] != "allowed":
        raise ToolchainError("network deny policy loopback contract differs")
    if sha(regular_under_root(policy["path"], "network deny policy")) != digest(policy["sha256"], "network deny policy"):
        raise ToolchainError("network deny policy hash differs")
    normative = lock["normative"]
    closed(normative, {"versions", "package_inventory", "version_inventory", "build", "installed_inventory", "fixtures", "gates"}, "normative toolchain")
    versions = normative["versions"]
    closed(versions, {"quarto", "node", "chrome", "mermaid", "graphviz", "lualatex", "poppler", "qpdf", "tesseract", "tesseract_languages", "tessdata_deu_sha256"}, "normative versions")
    if versions["tesseract_languages"] != ["deu"]:
        raise ToolchainError("normative OCR language set differs")
    digest(versions["tessdata_deu_sha256"], "German tessdata")
    for name in ("package_inventory", "version_inventory"):
        record = normative[name]
        closed(record, {"path", "sha256", "line_count"}, name)
        member = regular_under_root(record["path"], name)
        if sha(member) != digest(record["sha256"], name) or len(member.read_text(encoding="utf-8").splitlines()) != record["line_count"]:
            raise ToolchainError(f"{name} binding differs")
    build = normative["build"]
    closed(build, {"dockerfile", "dockerfile_sha256"}, "normative build")
    if sha(regular_under_root(build["dockerfile"], "normative Dockerfile")) != digest(build["dockerfile_sha256"], "normative Dockerfile"):
        raise ToolchainError("normative Dockerfile binding differs")
    if set(normative["installed_inventory"]) != {"humanizer", "mermaid", "graphviz", "pdf-home"}:
        raise ToolchainError("installed inventory set differs")
    for name, value in normative["installed_inventory"].items():
        digest(value, f"installed inventory {name}")
    if set(normative["gates"]) != {"M4", "M6", "M9", "U4"} or not all(value is True for value in normative["gates"].values()):
        raise ToolchainError("normative gate set differs")
    if not isinstance(normative["fixtures"], list) or not normative["fixtures"]:
        raise ToolchainError("normative fixture set differs")
    for index, record in enumerate(normative["fixtures"]):
        closed(record, {"path", "sha256"}, f"fixture {index}")
        if sha(regular_under_root(record["path"], f"fixture {index}")) != digest(record["sha256"], f"fixture {index}"):
            raise ToolchainError(f"fixture binding differs: {index}")
    if set(lock["commands"]) != {"implementation", "install", "verify", "version_log", "rollback", "restore", "clean_rooms", "runtime_builder"}:
        raise ToolchainError("canonical command set differs")
    for name, record in lock["commands"].items():
        closed(record, {"path", "sha256"}, f"command {name}")
        command = regular_under_root(record["path"], f"command {name}")
        if sha(command) != digest(record["sha256"], f"command {name}"):
            raise ToolchainError(f"command hash differs: {name}")
    # External stores are schema-bound here but deliberately not opened. Their
    # bytes are consumed exactly once by the run preflight below.
    expected_artifacts = {"humanizer_source", "humanizer_wheelhouse", "pdf_wheelhouse", "pdf_issuer_key", "mermaid_bundle", "graphviz_bundle", "normative_render_oci", "node_linux", "chrome_linux", "qemu_runtime", "runtime_humanizer", "runtime_pdf_home", "runtime_mermaid", "runtime_graphviz"}
    expected_kinds = {"humanizer_source":"git-archive-tar", "humanizer_wheelhouse":"content-addressed-directory",
        "pdf_wheelhouse":"content-addressed-directory", "pdf_issuer_key":"sha256-bound-secret-file",
        "mermaid_bundle":"content-addressed-directory", "graphviz_bundle":"content-addressed-directory",
        "normative_render_oci":"docker-save-archive", "node_linux":"official-archive",
        "chrome_linux":"official-archive", "qemu_runtime":"content-addressed-directory",
        "runtime_humanizer":"canonical-runtime-archive", "runtime_pdf_home":"canonical-runtime-archive",
        "runtime_mermaid":"canonical-runtime-archive", "runtime_graphviz":"canonical-runtime-archive"}
    if set(lock["artifacts"]) != expected_artifacts:
        raise ToolchainError("artifact set is not closed")
    for name, record in lock["artifacts"].items():
        required = {"path", "kind", "sha256"} | ({"manifest", "manifest_sha256"} if name in {"humanizer_wheelhouse", "pdf_wheelhouse", "mermaid_bundle", "graphviz_bundle", "qemu_runtime"} else set())
        closed(record, required, f"artifact {name}")
        if record["kind"] != expected_kinds[name]:
            raise ToolchainError(f"artifact kind differs: {name}")
        artifact = pathlib.Path(record["path"])
        if not artifact.is_absolute():
            raise ToolchainError(f"artifact path is relative: {name}")
        digest(record["sha256"], f"artifact {name}")
        if "manifest" in record:
            manifest = pathlib.Path(record["manifest"])
            if not manifest.is_absolute() or manifest.parent != artifact:
                raise ToolchainError(f"artifact manifest path binding differs: {name}")
            digest(record["manifest_sha256"], f"{name} manifest")
    runtime_fields = {"schema_version", "component", "relocation_token", "format", "source_digest", "package_digest",
                      "transform_digest", "archive_sha256", "archive_size", "manifest_sha256",
                      "entries", "files", "bytes"}
    if set(lock["runtime_archives"]) != {"humanizer", "pdf-home", "mermaid", "graphviz"}:
        raise ToolchainError("runtime archive set is not closed")
    for component, record in lock["runtime_archives"].items():
        closed(record, runtime_fields, f"runtime archive {component}")
        if record["schema_version"] != "brandkit-runtime-archive.v1" or record["component"] != component:
            raise ToolchainError(f"runtime archive identity differs: {component}")
        expected_token = f"/__BRANDKIT_RUNTIME_ROOT__/{component}" if component in {"humanizer", "pdf-home"} else None
        if record["relocation_token"] != expected_token:
            raise ToolchainError(f"runtime archive relocation token differs: {component}")
        if record["format"] not in {"ustar", "pax"}:
            raise ToolchainError(f"runtime archive format differs: {component}")
        artifact = lock["artifacts"]["runtime_" + component.replace("-", "_")]
        if record["archive_sha256"] != artifact["sha256"]:
            raise ToolchainError(f"runtime archive top-lock binding differs: {component}")
        digest(record["manifest_sha256"], f"runtime archive manifest {component}")
        for field in ("archive_size", "entries", "files", "bytes"):
            if type(record[field]) is not int or record[field] < 0:
                raise ToolchainError(f"runtime archive counter differs: {component}/{field}")
    # Lazy import avoids a module cycle; this is schema/cross-lock validation
    # only and deliberately does not open external runtime bytes.
    import runtime_archives
    for component, record in lock["runtime_archives"].items():
        expected = runtime_archives.bindings(lock, component)
        if any(record[field] != expected[field] for field in expected):
            raise ToolchainError(f"runtime archive source/package/transform binding differs: {component}")
    return lock, components


def inventory(root: pathlib.Path) -> list[dict]:
    rows = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        st = path.lstat()
        row = {"path": rel, "type": "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file"}
        if path.is_symlink():
            row["target"] = os.readlink(path)
        else:
            row["mode"] = format(stat.S_IMODE(st.st_mode), "04o")
            if path.is_file():
                row.update(size=st.st_size, sha256=sha(path))
        rows.append(row)
    return rows


SCAN_COUNTS: dict[tuple[str, str], int] = {}
SUPERVISOR_ENV = "BRANDKIT_TOOLCHAIN_SUPERVISOR_ID"
SUPERVISOR_FD_ENV = "BRANDKIT_TOOLCHAIN_SUPERVISOR_FD"
SUPERVISOR_ROOM_ENV = "BRANDKIT_TOOLCHAIN_SUPERVISOR_ROOM"
SUPERVISOR_PHASE_ENV = "BRANDKIT_TOOLCHAIN_SUPERVISOR_PHASE"
SUPERVISOR_OPERATION_ENV = "BRANDKIT_TOOLCHAIN_SUPERVISOR_OPERATION"


def supervisor_request(action: str, **payload: object) -> dict:
    """Use the inherited live supervisor capability; an fd number alone is not authority."""
    raw_fd = os.environ.get(SUPERVISOR_FD_ENV)
    if raw_fd is None:
        raise ToolchainError("live scan supervisor capability is absent")
    try:
        descriptor = int(raw_fd)
        duplicate = os.dup(descriptor)
        channel = socket.socket(fileno=duplicate)
    except (ValueError, OSError) as exc:
        raise ToolchainError("live scan supervisor capability differs") from exc
    try:
        request = {"schema_version":"brandkit-scan-supervisor-request.v1", "action":action, **payload}
        channel.sendall(json.dumps(request, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        response = b""
        while not response.endswith(b"\n"):
            part = channel.recv(65536)
            if not part: raise ToolchainError("scan supervisor closed the live capability")
            response += part
            if len(response) > 1024 * 1024: raise ToolchainError("scan supervisor response is oversized")
        value = json.loads(response)
    except (OSError, json.JSONDecodeError) as exc:
        raise ToolchainError("scan supervisor protocol failed") from exc
    finally:
        channel.close()
    if not isinstance(value, dict) or set(value) != {"ok", "result"} or value["ok"] is not True or not isinstance(value["result"], dict):
        raise ToolchainError(f"scan supervisor rejected request: {value}")
    return value["result"]


def supervisor_fd_request(action: str, *, send_fd: int | None = None,
                          receive_fd: bool = False, **payload: object) -> tuple[dict, int | None]:
    """Exchange a descriptor over the inherited Unix capability channel."""
    raw_fd = os.environ.get(SUPERVISOR_FD_ENV)
    if raw_fd is None: raise ToolchainError("live scan supervisor capability is absent")
    try: channel = socket.socket(fileno=os.dup(int(raw_fd)))
    except (ValueError, OSError) as exc: raise ToolchainError("live scan supervisor capability differs") from exc
    received: int | None = None
    try:
        request = {"schema_version":"brandkit-scan-supervisor-request.v1", "action":action, **payload}
        raw = json.dumps(request, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        ancillary = [] if send_fd is None else [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                                  array.array("i", [send_fd]).tobytes())]
        channel.sendmsg([raw], ancillary)
        response, ancdata, _, _ = channel.recvmsg(1024 * 1024, socket.CMSG_SPACE(array.array("i").itemsize))
        if not response.endswith(b"\n"): raise ToolchainError("scan supervisor descriptor response differs")
        for level, kind, data in ancdata:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                values = array.array("i"); values.frombytes(data[:values.itemsize])
                if received is not None or not values: raise ToolchainError("scan supervisor returned extra descriptors")
                received = values[0]
        value = json.loads(response)
    except (OSError, json.JSONDecodeError) as exc:
        if received is not None: os.close(received)
        raise ToolchainError("scan supervisor descriptor protocol failed") from exc
    finally: channel.close()
    if (not isinstance(value, dict) or set(value) != {"ok", "result"} or value["ok"] is not True
            or not isinstance(value["result"], dict) or receive_fd != (received is not None)):
        if received is not None: os.close(received)
        raise ToolchainError(f"scan supervisor rejected descriptor request: {value}")
    return value["result"], received


def _installed_tree_ustar(entries: list[dict], payloads: dict[str, bytes]) -> tuple[bytes, list[dict]]:
    """Canonical USTAR made exclusively from the scanner's already-read payload cache."""
    output = io.BytesIO(); manifest = []
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for entry in entries:
            name = entry["path"] + ("/" if entry["type"] == "directory" else "")
            info = tarfile.TarInfo(name); info.uid = info.gid = 0; info.uname = info.gname = "root"; info.mtime = 0
            info.mode = int(entry.get("mode", "0777"), 8)
            if entry["type"] == "file":
                data = payloads[entry["path"]]; info.type = tarfile.REGTYPE; info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
                manifest.append({"path":entry["path"], "type":"file", "mode":entry["mode"],
                                 "size":len(data), "sha256":hashlib.sha256(data).hexdigest()})
            elif entry["type"] == "directory":
                info.type = tarfile.DIRTYPE; info.size = 0; archive.addfile(info)
                manifest.append({"path":entry["path"], "type":"directory", "mode":entry["mode"]})
            else:
                info.type = tarfile.SYMTYPE; info.size = 0; info.linkname = entry["target"]; archive.addfile(info)
                manifest.append({"path":entry["path"], "type":"symlink", "target":entry["target"]})
    return output.getvalue(), manifest


def _seal_scan_snapshot(root: pathlib.Path, authorization: dict, entries: list[dict],
                        payloads: dict[str, bytes]) -> dict:
    raw, manifest = _installed_tree_ustar(entries, payloads)
    parent = pathlib.Path(os.environ.get("TMPDIR", str(root.parent)))
    parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="installed-tree-", suffix=".ustar", dir=parent)
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        readonly = os.open(name, os.O_RDONLY)
        os.unlink(name)
        try:
            proof, returned = supervisor_fd_request("seal-snapshot", send_fd=readonly,
                root=str(root), ordinal=authorization["ordinal"],
                authorization_digest=authorization["authorization_digest"],
                completion_digest=authorization["completion_digest"],
                archive_sha256=hashlib.sha256(raw).hexdigest(), archive_size=len(raw),
                member_manifest_digest=hashlib.sha256(json.dumps(manifest, sort_keys=True,
                    separators=(",", ":")).encode()).hexdigest(), member_count=len(manifest),
                member_files=sum(item["type"] == "file" for item in manifest),
                member_bytes=sum(item.get("size", 0) for item in manifest))
            if returned is not None: os.close(returned); raise ToolchainError("snapshot seal returned a descriptor")
            return proof
        finally: os.close(readonly)
    finally:
        try: os.unlink(name)
        except FileNotFoundError: pass


def _authorize_scan(root: pathlib.Path, logical_root: pathlib.Path) -> dict:
    if os.environ.get(SUPERVISOR_FD_ENV):
        context = {"room":os.environ.get(SUPERVISOR_ROOM_ENV),
                   "phase":os.environ.get(SUPERVISOR_PHASE_ENV),
                   "operation":os.environ.get(SUPERVISOR_OPERATION_ENV)}
        if not all(isinstance(value, str) and value for value in context.values()):
            raise ToolchainError("scan supervisor phase context is absent")
        result = supervisor_request("authorize-scan", root=str(logical_root), physical_root=str(root), **context)
        closed(result, {"kind", "supervisor_id", "ordinal", "authorization_digest", "completion_digest",
                        "room", "phase", "operation"}, "scan authorization")
        if result["kind"] != "live" or type(result["ordinal"]) is not int or not 1 <= result["ordinal"] <= 3:
            raise ToolchainError("scan supervisor authorization differs")
        digest(result["authorization_digest"], "scan supervisor event")
        return result
    supervisor = f"standalone:{os.getpid()}"
    key = (supervisor, str(logical_root))
    SCAN_COUNTS[key] = SCAN_COUNTS.get(key, 0) + 1
    if SCAN_COUNTS[key] > 3: raise ToolchainError("install tree exceeded three full scans")
    return {"kind":"standalone", "supervisor_id":supervisor,
            "ordinal":SCAN_COUNTS[key], "authorization_digest":"0" * 64,
            "completion_digest":"0" * 64, "room":"standalone",
            "phase":"standalone", "operation":"standalone"}


def _scan_proof_digest(value: dict) -> str:
    fields = {name:value[name] for name in ("component_merkles", "component_content_sha256",
        "closure_digest", "record_digest", "symlink_digest", "identity_digest")}
    fields.update(root=value["logical_root"] if "logical_root" in value else value["root"],
                  tree_merkle=value["merkle"] if "merkle" in value else value["tree_merkle"],
                  counters=value["counters"] if "counters" in value else value["scan_counters"])
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _portable_rows(root: pathlib.Path, entries: list[dict], payloads: dict[str, bytes]) -> list[dict]:
    root_text = str(root); root_bytes = root_text.encode()

    def normalized_data(relative: str) -> bytes:
        data = payloads[relative].replace(root_bytes, b"${INSTALL_ROOT}")
        if relative.startswith(".agents/activation-state/") and relative.endswith(".json"):
            def canonical(value):
                if isinstance(value, str): return value.replace(root_text, "${INSTALL_ROOT}")
                if isinstance(value, list): return [canonical(item) for item in value]
                if isinstance(value, dict):
                    return {key.replace(root_text, "${INSTALL_ROOT}"):
                            (f"${{{key.upper()}_VALIDATED}}" if key in {"sha256", "hmac_sha256", "manifest_sha256"}
                             else canonical(item)) for key, item in value.items()}
                return value
            try: value = json.loads(payloads[relative].decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ToolchainError(f"invalid installed activation state: {relative}") from exc
            return (json.dumps(canonical(value), sort_keys=True, separators=(",", ":")) + "\n").encode()
        if not (relative.endswith("/RECORD") and ".dist-info/RECORD" in relative):
            return data
        record = root / relative
        site_packages = record.parent.parent
        rows = []
        for row in csv.reader(io.StringIO(payloads[relative].decode("utf-8"))):
            target = (site_packages / row[0]).resolve(strict=False)
            normalized_name = row[0].replace(root_text, "${INSTALL_ROOT}")
            try: target_relative = target.relative_to(root).as_posix()
            except ValueError:
                rows.append([normalized_name, row[1], row[2]]); continue
            if target == record:
                rows.append([normalized_name, "", ""]); continue
            target_data = payloads[target_relative].replace(root_bytes, b"${INSTALL_ROOT}")
            encoded = base64.urlsafe_b64encode(hashlib.sha256(target_data).digest()).rstrip(b"=").decode()
            rows.append([normalized_name, f"sha256={encoded}", str(len(target_data))])
        output = io.StringIO(newline=""); csv.writer(output, lineterminator="\n").writerows(rows)
        return output.getvalue().encode()

    result = []
    for entry in entries:
        relative = entry["path"]
        if entry["type"] == "symlink":
            result.append({"path":relative, "type":"symlink",
                           "target":entry["target"].replace(root_text, "${INSTALL_ROOT}")})
        elif entry["type"] == "directory":
            result.append({"path":relative, "type":"directory", "mode":entry["mode"]})
        else:
            data = normalized_data(relative)
            result.append({"path":relative, "type":"file", "mode":entry["mode"],
                           "size":len(data), "sha256":hashlib.sha256(data).hexdigest()})
    return result


def _record_closure(root: pathlib.Path, entries: list[dict], payloads: dict[str, bytes]) -> list[dict]:
    types = {entry["path"]:entry["type"] for entry in entries}
    results = []
    for record_relative in sorted(name for name in payloads if name.endswith("/RECORD") and ".dist-info/RECORD" in name):
        record = root / record_relative; site_packages = record.parent.parent
        try: rows = list(csv.reader(io.StringIO(payloads[record_relative].decode("utf-8"))))
        except UnicodeError as exc: raise ToolchainError(f"installed RECORD is not UTF-8: {record}") from exc
        seen: set[str] = set(); self_seen = False
        for row in rows:
            if len(row) != 3 or not row[0] or "\\" in row[0]:
                raise ToolchainError(f"invalid installed RECORD row: {record}")
            pure = pathlib.PurePosixPath(row[0])
            if pure.is_absolute() or any(part in {"", "."} for part in pure.parts):
                raise ToolchainError(f"unsafe installed RECORD path: {record}: {row[0]}")
            target = (site_packages / pathlib.Path(*pure.parts)).resolve(strict=False)
            try: target_relative = target.relative_to(root).as_posix()
            except ValueError as exc:
                raise ToolchainError(f"installed RECORD escapes root: {record}: {row[0]}") from exc
            if target_relative in seen:
                raise ToolchainError(f"installed RECORD target is duplicated: {record}: {row[0]}")
            seen.add(target_relative)
            if target_relative == record_relative:
                if row[1:] != ["", ""]: raise ToolchainError(f"installed RECORD self row is not empty: {record}")
                self_seen = True; continue
            if types.get(target_relative) != "file":
                raise ToolchainError(f"installed RECORD target is absent or linked: {record}: {row[0]}")
            try: expected_size = int(row[2])
            except ValueError as exc: raise ToolchainError(f"installed RECORD size is invalid: {record}: {row[0]}") from exc
            encoded = base64.urlsafe_b64encode(hashlib.sha256(payloads[target_relative]).digest()).rstrip(b"=").decode()
            if row[1] != f"sha256={encoded}" or expected_size != len(payloads[target_relative]) or str(expected_size) != row[2]:
                raise ToolchainError(f"installed RECORD target hash/size differs: {record}: {row[0]}")
        if not self_seen: raise ToolchainError(f"installed RECORD self row is absent: {record}")
        results.append({"path":record_relative, "rows":len(rows),
                        "digest":hashlib.sha256(json.dumps(sorted(seen), separators=(",", ":")).encode()).hexdigest()})
    return results


def scan_tree(root: pathlib.Path, *, stale_roots: tuple[pathlib.Path, ...] = (),
              component_names: tuple[str, ...] | None = None,
              scan_counter: dict[str, int] | None = None,
              logical_root: pathlib.Path | None = None,
              seal_snapshot: bool = False,
              sealed_snapshot: dict | None = None) -> dict:
    """Read every regular file exactly once and derive the complete tree proof."""
    if not root.is_absolute() or root.is_symlink() or not root.is_dir() or root.resolve(strict=True) != root:
        raise ToolchainError("scan root is not canonical, real, and absolute")
    logical_root = root if logical_root is None else logical_root
    if (not logical_root.is_absolute() or str(logical_root) == "/"
            or pathlib.PurePosixPath(str(logical_root)).as_posix() != str(logical_root)):
        raise ToolchainError("logical scan root is not canonical and absolute")
    authorization = _authorize_scan(root, logical_root)
    if scan_counter is not None:
        scan_counter[str(root)] = scan_counter.get(str(root), 0) + 1
        if scan_counter[str(root)] > 3: raise ToolchainError("instrumented install tree exceeded three full scans")
    entries = []; payloads: dict[str, bytes] = {}; identities = []; files = total = reads = 0
    stale = []; old_values = [(str(value), str(value).encode()) for value in stale_roots]
    paths = sorted(root.rglob("*"), key=lambda item:item.relative_to(root).as_posix())
    for path in paths:
        relative = path.relative_to(root).as_posix(); info = path.lstat()
        identity = {"path":relative, "device":info.st_dev, "inode":info.st_ino, "mode":stat.S_IMODE(info.st_mode),
                    "size":info.st_size, "mtime_ns":info.st_mtime_ns, "ctime_ns":info.st_ctime_ns,
                    "links":info.st_nlink}
        if path.is_symlink():
            target = os.readlink(path); entry = {"path":relative, "type":"symlink", "target":target}
            identity["target"] = target
            try: path.resolve(strict=False).relative_to(root)
            except ValueError as exc:
                target_path = pathlib.Path(target)
                try: target_relative = target_path.relative_to(logical_root)
                except ValueError: raise ToolchainError(f"installed tree symlink escapes root: {path}") from exc
                mapped = root / target_relative
                if not mapped.exists() and not mapped.is_symlink():
                    raise ToolchainError(f"installed tree logical symlink target is absent: {path}")
            for text, _ in old_values:
                if text in target: stale.append({"path":relative, "kind":"symlink", "root":text})
        elif path.is_dir():
            entry = {"path":relative, "type":"directory", "mode":format(stat.S_IMODE(info.st_mode), "04o")}
        elif path.is_file():
            if info.st_nlink != 1: raise ToolchainError(f"installed tree contains hardlink: {path}")
            data = path.read_bytes(); payloads[relative] = data; files += 1; total += len(data); reads += 1
            entry = {"path":relative, "type":"file", "mode":format(stat.S_IMODE(info.st_mode), "04o"),
                     "size":len(data), "sha256":hashlib.sha256(data).hexdigest()}
            for text, value in old_values:
                if value in data:
                    try: data.decode("utf-8"); kind = "text"
                    except UnicodeDecodeError: kind = "binary"
                    stale.append({"path":relative, "kind":kind, "root":text})
        else: raise ToolchainError(f"installed tree contains special file: {path}")
        entries.append(entry); identities.append(identity)
    records = _record_closure(root, entries, payloads)
    portable = _portable_rows(logical_root, entries, payloads)
    component_merkles = {}; component_content_sha256 = {}

    def content_projection(project_entries: list[dict], project_payloads: dict[str, bytes]) -> str:
        value = hashlib.sha256(); skip = {".git", ".venv", "__pycache__", ".pytest_cache"}
        for item in project_entries:
            relative = pathlib.PurePosixPath(item["path"])
            if any(part in skip for part in relative.parts) or relative.suffix == ".pyc": continue
            encoded = relative.as_posix().encode()
            if item["type"] == "symlink": value.update(b"L\0" + encoded + b"\0" + item["target"].encode() + b"\0")
            elif item["type"] == "file": value.update(b"F\0" + encoded + b"\0" + hashlib.sha256(project_payloads[item["path"]]).digest())
        return value.hexdigest()

    if component_names is not None:
        for name in component_names:
            prefix = name + "/"; component_root = logical_root / name
            physical_component_root = root / name
            subset_entries = [{**entry, "path":entry["path"][len(prefix):]}
                              for entry in entries if entry["path"].startswith(prefix)]
            subset_payloads = {relative[len(prefix):]:data for relative,data in payloads.items()
                               if relative.startswith(prefix)}
            if (not subset_entries or physical_component_root.is_symlink()
                    or not physical_component_root.is_dir()):
                raise ToolchainError(f"installed component is absent or linked: {name}")
            rows = _portable_rows(component_root, subset_entries, subset_payloads)
            component_merkles[name] = hashlib.sha256(json.dumps(rows, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()
            component_content_sha256[name] = content_projection(subset_entries, subset_payloads)
    merkle = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    identity_digest = hashlib.sha256(json.dumps(identities, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = {"schema_version":"brandkit-tree-scan.v1", "root":str(root), "merkle":merkle,
              "portable_merkle":hashlib.sha256(json.dumps(portable, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
              "content_sha256":content_projection(entries, payloads), "portable_inventory":portable,
              "component_merkles":component_merkles,
              "component_content_sha256":component_content_sha256,
              "closure_digest":hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
              "record_digest":hashlib.sha256(json.dumps(records, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
              "symlink_digest":hashlib.sha256(json.dumps([entry for entry in entries if entry["type"] == "symlink"],
                  sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
              "identity_digest":identity_digest, "stale_roots":stale,
              "logical_root":str(logical_root), "scan_authorization":authorization,
              "counters":{"entries":len(entries), "files":files, "bytes":total, "reads":reads,
                          "records":len(records), "scan_ordinal":authorization["ordinal"]}}
    if authorization["kind"] == "live":
        completed = supervisor_request("complete-scan", root=str(logical_root), ordinal=authorization["ordinal"],
            authorization_digest=authorization["authorization_digest"], proof_digest=_scan_proof_digest(result),
            room=authorization["room"], phase=authorization["phase"], operation=authorization["operation"])
        if set(completed) != {"completion_digest"}: raise ToolchainError("scan completion differs")
        authorization["completion_digest"] = digest(completed["completion_digest"], "scan completion")
        if seal_snapshot:
            if sealed_snapshot is not None: raise ToolchainError("scan snapshot source differs")
            sealed_snapshot = _seal_scan_snapshot(logical_root, authorization, entries, payloads)
    if seal_snapshot and authorization["kind"] != "live":
        raise ToolchainError("installed snapshot sealing requires a live supervisor")
    result["sealed_snapshot"] = sealed_snapshot
    result["scan_identity"] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


def _live_identity_digest(root: pathlib.Path) -> str:
    if root.is_symlink() or not root.is_dir() or root.resolve(strict=True) != root:
        raise ToolchainError("live scan root differs")
    rows = []
    for path in sorted(root.rglob("*"), key=lambda item:item.relative_to(root).as_posix()):
        info = path.lstat(); relative = path.relative_to(root).as_posix()
        row = {"path":relative, "device":info.st_dev, "inode":info.st_ino,
               "mode":stat.S_IMODE(info.st_mode), "size":info.st_size,
               "mtime_ns":info.st_mtime_ns, "ctime_ns":info.st_ctime_ns, "links":info.st_nlink}
        if path.is_symlink(): row["target"] = os.readlink(path)
        rows.append(row)
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


TREE_RECEIPT_FIELDS = {"schema_version", "operation", "root", "root_device", "root_inode",
                       "lock_sha256", "installation_state_sha256", "component_merkles",
                       "component_content_sha256",
                       "tree_merkle", "closure_digest", "record_digest", "symlink_digest",
                       "identity_digest", "scan_identity", "scan_counters", "supervisor_id",
                       "scan_authorization", "sealed_snapshot",
                       "receipt_digest"}


def _receipt_scan_identity(value: dict) -> str:
    fields = {name:value[name] for name in ("root", "component_merkles", "component_content_sha256",
        "tree_merkle", "closure_digest", "record_digest", "symlink_digest", "identity_digest",
        "scan_counters", "scan_authorization")}
    fields["sealed_snapshot"] = value["sealed_snapshot"]
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def make_tree_receipt(scan: dict, lock: dict, operation: str) -> dict:
    if operation not in {"install", "verify", "version-log", "rollback", "restore"}:
        raise ToolchainError("tree receipt operation differs")
    root = pathlib.Path(scan["root"]); logical_root = pathlib.Path(scan["logical_root"])
    if root != logical_root:
        raise ToolchainError("only a published logical root can issue a tree receipt")
    state = root / "installation.json"; info = root.stat()
    if _live_identity_digest(root) != scan["identity_digest"]:
        raise ToolchainError("tree changed before receipt")
    if scan["stale_roots"]: raise ToolchainError("installed tree contains stale root bindings")
    if scan["component_merkles"] != lock["normative"]["installed_inventory"]:
        raise ToolchainError("installed component Merkle values differ")
    counters = scan["counters"]
    if counters["reads"] != counters["files"]:
        raise ToolchainError("tree scanner read counters differ")
    payload = {"schema_version":"brandkit-toolchain-tree-receipt.v1", "operation":operation,
               "root":str(root), "root_device":info.st_dev, "root_inode":info.st_ino,
               "lock_sha256":sha(LOCK_PATH), "installation_state_sha256":sha(state),
               "component_merkles":scan["component_merkles"],
               "component_content_sha256":scan["component_content_sha256"], "tree_merkle":scan["merkle"],
               "closure_digest":scan["closure_digest"], "record_digest":scan["record_digest"],
               "symlink_digest":scan["symlink_digest"], "identity_digest":scan["identity_digest"],
               "scan_identity":"", "scan_counters":counters,
               "supervisor_id":scan["scan_authorization"]["supervisor_id"],
               "scan_authorization":scan["scan_authorization"]}
    payload["sealed_snapshot"] = scan.get("sealed_snapshot")
    payload["scan_identity"] = _receipt_scan_identity(payload)
    payload["receipt_digest"] = hashlib.sha256(json.dumps(payload, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return payload


def validate_tree_receipt(receipt: dict, root: pathlib.Path, lock: dict, *, operation: str | None = None,
                          live_scan: dict | None = None, require_same_supervisor: bool = True,
                          receipt_root: pathlib.Path | None = None,
                          check_live_identity: bool = True,
                          require_live_capability: bool = False,
                          check_root_binding: bool = True,
                          expected_room: str | None = None,
                          expected_phase: str | None = None) -> dict:
    closed(receipt, TREE_RECEIPT_FIELDS, "tree receipt")
    counters = receipt["scan_counters"]
    closed(counters, {"entries", "files", "bytes", "reads", "records", "scan_ordinal"}, "tree receipt counters")
    unsigned = dict(receipt); claimed = unsigned.pop("receipt_digest")
    expected_digest = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if check_root_binding:
        root = root.resolve(strict=True); info = root.stat()
        bound_root = (receipt_root or root).resolve(strict=False)
    else:
        info = None; bound_root = receipt_root or root
    authorization = receipt["scan_authorization"]
    closed(authorization, {"kind", "supervisor_id", "ordinal", "authorization_digest", "completion_digest",
                           "room", "phase", "operation"}, "tree receipt authorization")
    receipt_root_text = receipt.get("root")
    root_parts = pathlib.PurePosixPath(receipt_root_text).parts if isinstance(receipt_root_text, str) else ()
    canonical_receipt_root = (isinstance(receipt_root_text, str) and receipt_root_text.startswith("/")
        and not receipt_root_text.startswith("//") and not receipt_root_text.endswith("/")
        and "\\" not in receipt_root_text and all(part not in {"", ".", ".."} for part in root_parts))
    if (receipt["schema_version"] != "brandkit-toolchain-tree-receipt.v1"
            or not canonical_receipt_root
            or digest(claimed, "tree receipt") != expected_digest
            or receipt["operation"] not in {"install", "verify", "version-log", "rollback", "restore"}
            or (operation is not None and receipt["operation"] != operation)
            or receipt["root"] != str(bound_root)
            or (check_root_binding and (receipt["root_device"] != info.st_dev or receipt["root_inode"] != info.st_ino))
            or receipt["lock_sha256"] != sha(LOCK_PATH)
            or (check_root_binding and receipt["installation_state_sha256"] != sha(root/"installation.json"))
            or set(receipt["component_merkles"]) != {"humanizer", "mermaid", "graphviz", "pdf-home"}
            or receipt["component_merkles"] != lock["normative"]["installed_inventory"]
            or set(receipt["component_content_sha256"]) != set(receipt["component_merkles"])
            or any(type(counters[name]) is not int or counters[name] < 0 for name in counters)
            or counters["reads"] != counters["files"] or not 1 <= counters["scan_ordinal"] <= 3
            or authorization["supervisor_id"] != receipt["supervisor_id"]
            or authorization["ordinal"] != counters["scan_ordinal"]
            or authorization["kind"] not in {"live", "standalone"}
            or (expected_room is not None and authorization["room"] != expected_room)
            or (expected_phase is not None and authorization["phase"] != expected_phase)
            or ((expected_room is not None or expected_phase is not None)
                and operation is not None and authorization["operation"] != operation)
            or receipt["scan_identity"] != _receipt_scan_identity(receipt)):
        raise ToolchainError("tree receipt binding differs")
    for name in ("tree_merkle", "closure_digest", "record_digest", "symlink_digest",
                 "identity_digest", "scan_identity"):
        digest(receipt[name], f"tree receipt {name}")
    for name, value in receipt["component_content_sha256"].items():
        digest(value, f"tree receipt component content {name}")
    snapshot = receipt["sealed_snapshot"]
    if snapshot is not None:
        closed(snapshot, {"schema_version", "supervisor_id", "root", "ordinal", "fd_device", "fd_inode",
                          "archive_sha256", "archive_size", "member_count", "member_files", "member_bytes",
                          "member_manifest_digest",
                          "seal_digest"}, "sealed installed snapshot")
        if (snapshot["schema_version"] != "brandkit-installed-tree-snapshot.v1"
                or snapshot["supervisor_id"] != receipt["supervisor_id"]
                or snapshot["root"] != receipt["root"] or type(snapshot["ordinal"]) is not int
                or not 1 <= snapshot["ordinal"] <= authorization["ordinal"]):
            raise ToolchainError("sealed installed snapshot binding differs")
        for name in ("archive_sha256", "member_manifest_digest", "seal_digest"):
            digest(snapshot[name], f"sealed snapshot {name}")
    digest(authorization["authorization_digest"], "tree receipt supervisor authorization")
    digest(authorization["completion_digest"], "tree receipt supervisor completion")
    if require_live_capability:
        if authorization["kind"] != "live":
            raise ToolchainError("tree receipt lacks live supervisor authority")
        confirmed = supervisor_request("validate-receipt", root=str(bound_root), ordinal=authorization["ordinal"],
            authorization_digest=authorization["authorization_digest"],
            completion_digest=authorization["completion_digest"], proof_digest=_scan_proof_digest(receipt),
            room=authorization["room"], phase=authorization["phase"], operation=authorization["operation"])
        if confirmed != {"valid":True, "supervisor_id":authorization["supervisor_id"]}:
            raise ToolchainError("tree receipt supervisor authority differs")
    elif require_same_supervisor and os.environ.get(SUPERVISOR_FD_ENV):
        if authorization["kind"] != "live": raise ToolchainError("tree receipt supervisor differs")
    if live_scan is None:
        if check_live_identity and _live_identity_digest(root) != receipt["identity_digest"]:
            raise ToolchainError("tree changed after receipt")
    else:
        expected = make_tree_receipt(live_scan, lock, receipt["operation"])
        for name in TREE_RECEIPT_FIELDS - {"receipt_digest"}:
            if receipt[name] != expected[name]: raise ToolchainError("tree receipt does not match live scan")
    return receipt


def tree_receipt_path(root: pathlib.Path) -> pathlib.Path:
    return root.parent / f".{root.name}.tree-receipt.json"


def write_tree_receipt(root: pathlib.Path, receipt: dict) -> pathlib.Path:
    path = tree_receipt_path(root)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.lstat().st_nlink != 1:
            raise ToolchainError("tree receipt target differs")
        path.chmod(0o600)
    _atomic_json(path, receipt); path.chmod(0o400)
    return path


def load_tree_receipt(root: pathlib.Path, lock: dict, *, operation: str | None = None,
                      require_live_capability: bool = False) -> dict:
    path = tree_receipt_path(root)
    if path.is_symlink() or not path.is_file() or path.lstat().st_nlink != 1:
        raise ToolchainError("tree receipt is absent, linked, or hardlinked")
    return validate_tree_receipt(load(path), root, lock, operation=operation,
                                 require_live_capability=require_live_capability)


def reissue_tree_receipt(receipt: dict, operation: str) -> dict:
    if operation not in {"install", "verify", "version-log", "rollback", "restore"}:
        raise ToolchainError("tree receipt operation differs")
    updated = dict(receipt); updated["operation"] = operation; updated.pop("receipt_digest")
    authorization = dict(updated["scan_authorization"])
    if authorization["kind"] == "live":
        room = os.environ.get(SUPERVISOR_ROOM_ENV); phase = os.environ.get(SUPERVISOR_PHASE_ENV)
        expected_operation = os.environ.get(SUPERVISOR_OPERATION_ENV)
        if not room or not phase or expected_operation != operation:
            raise ToolchainError("tree receipt reissue phase context differs")
        rebound = supervisor_request("rebind-receipt", root=updated["root"], ordinal=authorization["ordinal"],
            authorization_digest=authorization["authorization_digest"],
            prior_completion_digest=authorization["completion_digest"], proof_digest=_scan_proof_digest(updated),
            room=room, phase=phase, operation=operation)
        if set(rebound) != {"completion_digest"}: raise ToolchainError("tree receipt rebind differs")
        authorization.update(room=room, phase=phase, operation=operation,
                             completion_digest=digest(rebound["completion_digest"], "receipt rebind completion"))
        updated["scan_authorization"] = authorization
        updated["supervisor_id"] = authorization["supervisor_id"]
        updated["scan_identity"] = _receipt_scan_identity(updated)
    updated["receipt_digest"] = hashlib.sha256(json.dumps(updated, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return updated


def inventory_records(root: pathlib.Path) -> list[dict]:
    """Compatibility view backed by the sole installed-tree scanner."""
    return scan_tree(root.resolve(strict=True))["portable_inventory"]


def inventory_digest(root: pathlib.Path) -> str:
    return scan_tree(root.resolve(strict=True))["portable_merkle"]


def verify_closed_artifact_directory(root: pathlib.Path, manifest_path: pathlib.Path) -> None:
    manifest = load(manifest_path)
    records = manifest.get("files")
    if not isinstance(records, list):
        raise ToolchainError(f"artifact manifest files differ: {manifest_path}")
    expected: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict):
            raise ToolchainError("artifact manifest record differs")
        name = record.get("path", record.get("name", record.get("filename")))
        if not isinstance(name, str) or pathlib.PurePosixPath(name).is_absolute() or ".." in pathlib.PurePosixPath(name).parts:
            raise ToolchainError("artifact manifest path is unsafe")
        if set(record) - {"path", "name", "filename", "sha256", "size", "formula", "version", "url"} or "sha256" not in record:
            raise ToolchainError(f"artifact manifest record fields differ: {name}")
        expected[name] = record
    actual = {}
    for path in sorted(root.rglob("*")):
        if path == manifest_path:
            continue
        if path.is_symlink():
            raise ToolchainError(f"artifact contains symlink: {path}")
        if path.is_file():
            actual[path.relative_to(root).as_posix()] = path
        elif not path.is_dir():
            raise ToolchainError(f"artifact contains special file: {path}")
    if set(actual) != set(expected):
        raise ToolchainError(f"artifact closed inventory differs: {root}")
    for name, path in actual.items():
        record = expected[name]
        if sha(path) != digest(record["sha256"], f"artifact member {name}"):
            raise ToolchainError(f"artifact member hash differs: {name}")
        if "size" in record and path.stat().st_size != record["size"]:
            raise ToolchainError(f"artifact member size differs: {name}")


CONSUMED_RUN_ARTIFACTS = (
    "runtime_humanizer", "runtime_pdf_home", "runtime_mermaid",
    "runtime_graphviz", "pdf_issuer_key",
)
RUN_INPUT_ENV = "BRANDKIT_TOOLCHAIN_RUN_INPUTS"
RUN_ID_ENV = "BRANDKIT_TOOLCHAIN_RUN_ID"
RUN_FDS_ENV = "BRANDKIT_TOOLCHAIN_RUN_INPUT_FDS"
HELD_RUN_INPUT_FDS: dict[str, dict[str, int]] = {}


def close_run_input_fds(run_id: str | None = None) -> None:
    identifiers = [run_id] if run_id is not None else list(HELD_RUN_INPUT_FDS)
    for identifier in identifiers:
        for descriptor in HELD_RUN_INPUT_FDS.pop(identifier, {}).values():
            try: os.close(descriptor)
            except OSError: pass


atexit.register(close_run_input_fds)


def _atomic_json(path: pathlib.Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or (path.exists() and
            (path.is_symlink() or not path.is_file() or path.lstat().st_nlink != 1)):
        raise ToolchainError(f"run-input JSON target differs: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":")); handle.write("\n")
            handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise


def _source_identity(root: pathlib.Path) -> list[dict]:
    if root.is_symlink() or not root.exists():
        raise ToolchainError(f"external artifact is absent or linked: {root}")
    paths = [root] if root.is_file() else [root, *sorted(root.rglob("*"), key=lambda p:p.relative_to(root).as_posix())]
    records = []
    for path in paths:
        info = path.lstat()
        if path.is_symlink():
            raise ToolchainError(f"external artifact contains symlink: {path}")
        if not (path.is_file() or path.is_dir()):
            raise ToolchainError(f"external artifact contains special file: {path}")
        if path.is_file() and info.st_nlink != 1:
            raise ToolchainError(f"external artifact contains hardlink: {path}")
        relative = "." if path == root else path.relative_to(root).as_posix()
        records.append({"path":relative, "type":"file" if path.is_file() else "directory",
                        "device":info.st_dev, "inode":info.st_ino, "size":info.st_size,
                        "mode":stat.S_IMODE(info.st_mode), "mtime_ns":info.st_mtime_ns,
                        "ctime_ns":info.st_ctime_ns, "links":info.st_nlink})
    return records


def _snapshot_inventory(root: pathlib.Path) -> tuple[list[dict], int, int]:
    if root.is_symlink() or not root.exists():
        raise ToolchainError(f"run snapshot is absent or linked: {root}")
    paths = [root] if root.is_file() else [root, *sorted(root.rglob("*"), key=lambda p:p.relative_to(root).as_posix())]
    records: list[dict] = []; files = total = 0
    for path in paths:
        info = path.lstat(); relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ToolchainError(f"run snapshot contains linked or special entry: {path}")
        if path.is_file():
            if info.st_nlink != 1:
                raise ToolchainError(f"run snapshot contains hardlink: {path}")
            data = path.read_bytes(); files += 1; total += len(data)
            records.append({"path":relative, "type":"file", "mode":stat.S_IMODE(info.st_mode),
                            "size":len(data), "sha256":hashlib.sha256(data).hexdigest()})
        else:
            records.append({"path":relative, "type":"directory", "mode":stat.S_IMODE(info.st_mode)})
    return records, files, total


def _canonical_new_run_root(path: pathlib.Path) -> pathlib.Path:
    raw = str(path)
    if (not path.is_absolute() or raw.startswith("//") or raw.endswith("/") or "\\" in raw
            or "\x00" in raw or any(part in {"", ".", ".."} for part in raw[1:].split("/"))):
        raise ToolchainError("run-input root is not lexically canonical and absolute")
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink() or parent.resolve(strict=True) != parent:
        raise ToolchainError("run-input parent is absent, linked, or aliased")
    probe = pathlib.Path(path.anchor)
    for part in path.parts[1:-1]:
        probe /= part
        if probe.is_symlink(): raise ToolchainError("run-input root traverses a symlink")
    if path.exists() or path.is_symlink(): raise ToolchainError("run-input root must be fresh")
    return path


def _canonical_existing_run_root(path: pathlib.Path) -> pathlib.Path:
    raw = str(path)
    if (not path.is_absolute() or raw.startswith("//") or raw.endswith("/") or "\\" in raw
            or "\x00" in raw or any(part in {"", ".", ".."} for part in raw[1:].split("/"))
            or path.is_symlink() or not path.is_dir() or path.resolve(strict=True) != path):
        raise ToolchainError("run-input root is not canonical, real, and absolute")
    probe = pathlib.Path(path.anchor)
    for part in path.parts[1:]:
        probe /= part
        if probe.is_symlink(): raise ToolchainError("run-input root traverses a symlink")
    return path


def _canonical_ustar(files: dict[str, bytes], directories: set[str]) -> bytes:
    """Derive the one accepted USTAR byte representation from payload bytes."""
    if "." in files:
        if set(files) != {"."} or directories:
            raise ToolchainError("canonical single-file archive shape differs")
        relatives = ["."]
    else:
        if "." not in directories or "." in files:
            raise ToolchainError("canonical directory archive root differs")
        relatives = [".", *sorted((set(files) | directories) - {"."})]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as handle:
        for relative in relatives:
            name = "artifact" if relative == "." else f"artifact/{relative}"
            info = tarfile.TarInfo(name)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mtime = 0
            if relative in directories:
                info.mode = 0o500
                info.type = tarfile.DIRTYPE
                handle.addfile(info)
            else:
                value = files[relative]
                info.mode = 0o400
                info.size = len(value)
                handle.addfile(info, io.BytesIO(value))
    return output.getvalue()


def _write_snapshot_archive(snapshot: pathlib.Path, archive: pathlib.Path) -> dict:
    inventory, files, total = _snapshot_inventory(snapshot)
    payloads: dict[str, bytes] = {}
    directories: set[str] = set()
    for entry in inventory:
        relative = entry["path"]
        expected_mode = 0o500 if entry["type"] == "directory" else 0o400
        if entry["mode"] != expected_mode:
            raise ToolchainError("run snapshot mode is not canonical")
        if entry["type"] == "directory":
            directories.add(relative)
        else:
            payloads[relative] = (snapshot if relative == "." else snapshot/relative).read_bytes()
    raw = _canonical_ustar(payloads, directories)
    archive.write_bytes(raw)
    raw_sha = hashlib.sha256(raw).hexdigest(); size = len(raw)
    return {"format":"ustar", "archive_sha256":raw_sha, "archive_size":size,
            "content_digest":hashlib.sha256(json.dumps(inventory, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest(), "files":files, "bytes":total}


def _run_fd_map(run_id: str) -> dict[str, int]:
    if run_id in HELD_RUN_INPUT_FDS:
        return dict(HELD_RUN_INPUT_FDS[run_id])
    try: value = json.loads(os.environ.get(RUN_FDS_ENV, "{}"))
    except json.JSONDecodeError as exc: raise ToolchainError("run input FD map is invalid") from exc
    if set(value) != set(CONSUMED_RUN_ARTIFACTS) or not all(type(fd) is int and fd >= 0 for fd in value.values()):
        raise ToolchainError("run input FD map differs")
    for descriptor in value.values():
        try: os.fstat(descriptor)
        except OSError as exc: raise ToolchainError("run input FD is unavailable") from exc
    return value


def _read_archive_fd(descriptor: int) -> bytes:
    duplicate = os.dup(descriptor)
    try:
        os.lseek(duplicate, 0, os.SEEK_SET)
        blocks = []
        while True:
            block = os.read(duplicate, 1024 * 1024)
            if not block: break
            blocks.append(block)
        return b"".join(blocks)
    finally:
        os.close(duplicate)


def _locked_archive_payload(name: str, descriptor: int, record: dict) -> tuple[bytes, dict[str, bytes], set[str]]:
    raw = _read_archive_fd(descriptor)
    files: dict[str, bytes] = {}; directories: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            members = archive.getmembers()
            for member in members:
                original = member.name
                pure = pathlib.PurePosixPath(member.name)
                if ("\\" in original or "//" in original or "/./" in original
                        or original.endswith("/.") or pure.is_absolute() or ".." in pure.parts
                        or not pure.parts or pure.parts[0] != "artifact"
                        or not (member.isfile() or member.isdir())):
                    raise ToolchainError(f"run input archive member is unsafe: {name}")
                relative = pathlib.PurePosixPath(*pure.parts[1:]).as_posix() if len(pure.parts) > 1 else "."
                if relative in files or relative in directories:
                    raise ToolchainError(f"run input archive member is duplicated: {name}")
                if member.isdir(): directories.add(relative)
                else:
                    stream = archive.extractfile(member)
                    if stream is None: raise ToolchainError(f"run input archive member is unreadable: {name}")
                    files[relative] = stream.read()
    except tarfile.TarError as exc:
        raise ToolchainError(f"run input archive is invalid: {name}") from exc
    if pathlib.Path(record["path"]).suffix and "manifest" not in record:
        if set(files) != {"."} or directories:
            raise ToolchainError(f"single-file run input archive differs: {name}")
        if hashlib.sha256(files["."]).hexdigest() != record["sha256"]:
            raise ToolchainError(f"single-file run input differs from top lock: {name}")
    elif "manifest" not in record:
        raise ToolchainError(f"run input artifact type differs: {name}")
    else:
        manifest_name = pathlib.Path(record["manifest"]).name
        if manifest_name not in files or hashlib.sha256(files[manifest_name]).hexdigest() != record["manifest_sha256"]:
            raise ToolchainError(f"run input manifest differs from top lock: {name}")
        expected = _manifest_members(files[manifest_name], name)
        if set(files) != set(expected) | {manifest_name}:
            raise ToolchainError(f"run input member names differ from pinned manifest: {name}")
        expected_dirs = {parent.as_posix() for member in expected for parent in pathlib.PurePosixPath(member).parents
                         if parent.as_posix() != "."} | {"."}
        if directories != expected_dirs:
            raise ToolchainError(f"run input directories differ from pinned manifest: {name}")
        for relative, pin in expected.items():
            value = files[relative]
            if hashlib.sha256(value).hexdigest() != pin["sha256"] or ("size" in pin and len(value) != pin["size"]):
                raise ToolchainError(f"run input member differs from pinned manifest: {name}/{relative}")
    canonical = _canonical_ustar(files, directories)
    if (len(raw) != len(canonical)
            or hashlib.sha256(raw).digest() != hashlib.sha256(canonical).digest()
            or raw != canonical):
        raise ToolchainError(f"run input archive is not canonical USTAR: {name}")
    return raw, files, directories


def _manifest_members(raw: bytes, label: str) -> dict[str, dict]:
    try: value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc: raise ToolchainError(f"invalid artifact manifest: {label}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("files"), list):
        raise ToolchainError(f"artifact manifest fields differ: {label}")
    expected: dict[str, dict] = {}
    for record in value["files"]:
        if not isinstance(record, dict): raise ToolchainError(f"artifact manifest record differs: {label}")
        name = record.get("path", record.get("name", record.get("filename")))
        pure = pathlib.PurePosixPath(name) if isinstance(name, str) else None
        if (pure is None or pure.is_absolute() or ".." in pure.parts or not pure.parts
                or name != pure.as_posix() or name in expected
                or set(record) - {"path", "name", "filename", "sha256", "size", "formula", "version", "url"}
                or "sha256" not in record):
            raise ToolchainError(f"artifact manifest record is unsafe: {label}")
        digest(record["sha256"], f"artifact member {label}/{name}")
        if "size" in record and (type(record["size"]) is not int or record["size"] < 0):
            raise ToolchainError(f"artifact manifest size differs: {label}/{name}")
        expected[name] = record
    return expected


def _sequential_artifact_snapshot(name: str, record: dict, snapshot_parent: pathlib.Path | None,
                                  fault_hook=None) -> dict:
    source = pathlib.Path(record["path"])
    before = _source_identity(source)
    if fault_hook: fault_hook("after_precheck", name, source)
    consumed = snapshot_parent is not None
    target = snapshot_parent / name / source.name if consumed else None
    if target is not None:
        target.parent.mkdir(parents=True, mode=0o700)
    files = total = 0

    def transfer(path: pathlib.Path, destination: pathlib.Path | None, expected_sha: str,
                 expected_size: int | None = None, cached: bytes | None = None) -> None:
        nonlocal files, total
        if path.is_symlink() or not path.is_file() or path.lstat().st_nlink != 1:
            raise ToolchainError(f"external artifact member changed before read: {path}")
        value = hashlib.sha256(); size = 0
        output = destination.open("xb") if destination is not None else None
        try:
            if cached is not None:
                value.update(cached); size = len(cached)
                if output is not None: output.write(cached)
            else:
                with path.open("rb") as handle:
                    while True:
                        block = handle.read(1024 * 1024)
                        if not block: break
                        value.update(block); size += len(block)
                        if output is not None: output.write(block)
            if output is not None: output.flush(); os.fsync(output.fileno())
        finally:
            if output is not None: output.close()
        if value.hexdigest() != expected_sha or (expected_size is not None and size != expected_size):
            raise ToolchainError(f"external artifact bytes differ: {name}/{path.name}")
        files += 1; total += size

    if source.is_file():
        transfer(source, target, digest(record["sha256"], f"artifact {name}"))
    else:
        if source.name != digest(record["sha256"], f"artifact {name}") or "manifest" not in record:
            raise ToolchainError(f"content-addressed artifact identity differs: {name}")
        manifest_path = pathlib.Path(record["manifest"])
        if manifest_path.parent != source or manifest_path.is_symlink() or not manifest_path.is_file():
            raise ToolchainError(f"artifact manifest path differs: {name}")
        manifest_raw = manifest_path.read_bytes()
        if hashlib.sha256(manifest_raw).hexdigest() != record["manifest_sha256"]:
            raise ToolchainError(f"artifact manifest hash differs: {name}")
        expected = _manifest_members(manifest_raw, name)
        actual_files = {path.relative_to(source).as_posix():path for path in source.rglob("*") if path.is_file()}
        if set(actual_files) != set(expected) | {manifest_path.name}:
            raise ToolchainError(f"artifact closed file inventory differs: {name}")
        expected_dirs = {parent.as_posix() for member in expected for parent in pathlib.PurePosixPath(member).parents
                         if parent.as_posix() != "."}
        actual_dirs = {path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_dir()}
        if actual_dirs != expected_dirs:
            raise ToolchainError(f"artifact closed directory inventory differs: {name}")
        if target is not None:
            target.mkdir(mode=0o700)
            for directory in sorted(actual_dirs): (target / directory).mkdir(parents=True, exist_ok=True)
        transfer(manifest_path, target / manifest_path.name if target else None,
                 record["manifest_sha256"], cached=manifest_raw)
        for relative, member in sorted(actual_files.items()):
            if relative == manifest_path.name: continue
            pin = expected[relative]
            transfer(member, target / relative if target else None, pin["sha256"], pin.get("size"))
    if fault_hook: fault_hook("after_copy", name, source)
    after = _source_identity(source)
    if before != after:
        raise ToolchainError(f"external artifact changed across snapshot: {name}")
    if fault_hook: fault_hook("after_postcheck", name, source)
    snapshot_record = None
    if target is not None:
        for path in sorted(target.rglob("*"), reverse=True):
            path.chmod(0o500 if path.is_dir() else 0o400)
        target.chmod(0o500 if target.is_dir() else 0o400); target.parent.chmod(0o500)
        inventory, snapshot_files, snapshot_bytes = _snapshot_inventory(target)
        if snapshot_files != files or snapshot_bytes != total:
            raise ToolchainError(f"snapshot counters differ: {name}")
        snapshot_record = {"path":str(target.resolve()), "inventory":inventory,
                           "tree_digest":hashlib.sha256(json.dumps(inventory, sort_keys=True,
                           separators=(",", ":")).encode()).hexdigest(), "files":files, "bytes":total}
    return {"source_path":str(source), "source_identity_before":hashlib.sha256(json.dumps(before,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "source_identity_after":hashlib.sha256(json.dumps(after, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest(), "files":files, "bytes":total,
            "snapshot":snapshot_record}


def preflight_run_inputs(lock: dict, run_root: pathlib.Path, run_id: str, *, fault_hook=None,
                         full_read_counter: dict[str, int] | None = None) -> dict:
    run_root = _canonical_new_run_root(run_root)
    staging = pathlib.Path(tempfile.mkdtemp(prefix=f".{run_root.name}.next-", dir=run_root.parent))
    snapshots = staging / "input-snapshots"; snapshots.mkdir(mode=0o700)
    archives = staging / "archives"; archives.mkdir(mode=0o700)
    artifacts = {}; counts = {}; published = False
    try:
        for name, record in sorted(lock["artifacts"].items()):
            if full_read_counter is not None:
                full_read_counter[name] = full_read_counter.get(name, 0) + 1
                if full_read_counter[name] != 1:
                    raise ToolchainError(f"external artifact received more than one full-read pass: {name}")
            artifacts[name] = _sequential_artifact_snapshot(
                name, record, snapshots if name in CONSUMED_RUN_ARTIFACTS else None, fault_hook)
            counts[name] = 1
        for name in CONSUMED_RUN_ARTIFACTS:
            old = artifacts[name]["snapshot"]
            archive_record = _write_snapshot_archive(pathlib.Path(old["path"]), archives/f"{name}.tar")
            if archive_record["files"] != old["files"] or archive_record["bytes"] != old["bytes"]:
                raise ToolchainError(f"run input archive counters differ: {name}")
            artifacts[name]["snapshot"] = archive_record
        for path in sorted(snapshots.rglob("*")):
            if not path.is_symlink(): path.chmod(0o700 if path.is_dir() else 0o600)
        snapshots.chmod(0o700); shutil.rmtree(snapshots)
        manifest = {"schema_version":"brandkit-run-input-snapshot-manifest.v1", "run_id":run_id,
                    "lock_sha256":sha(LOCK_PATH), "artifacts":{
                        name:artifacts[name]["snapshot"] for name in CONSUMED_RUN_ARTIFACTS}}
        manifest_path = staging / "snapshot-manifest.json"; _atomic_json(manifest_path, manifest)
        if fault_hook: fault_hook("after_manifest", "", staging)
        receipt = {"schema_version":"brandkit-run-inputs.v1", "run_id":run_id,
                   "run_root":str(run_root), "lock_sha256":sha(LOCK_PATH),
                   "consumed_artifacts":list(CONSUMED_RUN_ARTIFACTS), "artifacts":artifacts,
                   "full_read_counts":counts, "snapshot_manifest_path":str(run_root/"snapshot-manifest.json"),
                   "snapshot_manifest_sha256":sha(manifest_path)}
        receipt["receipt_digest"] = hashlib.sha256(json.dumps(receipt, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()
        receipt_path = staging / "run-inputs.json"; _atomic_json(receipt_path, receipt)
        if fault_hook: fault_hook("after_receipt", "", staging)
        manifest_path.chmod(0o400); receipt_path.chmod(0o400)
        for archive in archives.iterdir(): archive.chmod(0o400)
        os.replace(staging, run_root); published = True
        descriptor_map = {}
        try:
            for name in CONSUMED_RUN_ARTIFACTS:
                descriptor_map[name] = os.open(run_root/"archives"/f"{name}.tar", os.O_RDONLY)
        except BaseException:
            for descriptor in descriptor_map.values(): os.close(descriptor)
            raise
        close_run_input_fds(run_id); HELD_RUN_INPUT_FDS[run_id] = descriptor_map
        for name in CONSUMED_RUN_ARTIFACTS: (run_root/"archives"/f"{name}.tar").unlink()
        (run_root/"archives").rmdir()
        return receipt
    except BaseException:
        failed = run_root if published else staging
        close_run_input_fds(run_id)
        if failed.exists():
            for path in sorted(failed.rglob("*")):
                if not path.is_symlink(): path.chmod(0o700 if path.is_dir() else 0o600)
            failed.chmod(0o700); shutil.rmtree(failed)
        raise


def load_run_inputs(lock: dict, path: pathlib.Path, *, expected_run_id: str | None = None) -> tuple[dict, dict]:
    if path.is_symlink() or not path.is_file() or path.lstat().st_nlink != 1:
        raise ToolchainError("run inputs receipt is absent, linked, or hardlinked")
    receipt = load(path)
    fields = {"schema_version", "run_id", "run_root", "lock_sha256", "consumed_artifacts", "artifacts",
              "full_read_counts", "snapshot_manifest_path", "snapshot_manifest_sha256", "receipt_digest"}
    closed(receipt, fields, "run inputs")
    unsigned = dict(receipt); claimed = unsigned.pop("receipt_digest")
    if (receipt["schema_version"] != "brandkit-run-inputs.v1" or not isinstance(receipt["run_id"], str)
            or not receipt["run_id"] or "/" in receipt["run_id"] or digest(claimed, "run inputs receipt") !=
            hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            or digest(receipt["lock_sha256"], "run inputs lock") != sha(LOCK_PATH)
            or digest(receipt["snapshot_manifest_sha256"], "run input snapshot manifest") != receipt["snapshot_manifest_sha256"]
            or receipt["consumed_artifacts"] != list(CONSUMED_RUN_ARTIFACTS)
            or set(receipt["artifacts"]) != set(lock["artifacts"])
            or receipt["full_read_counts"] != {name:1 for name in lock["artifacts"]}
            or (expected_run_id is not None and receipt["run_id"] != expected_run_id)):
        raise ToolchainError("run inputs receipt binding differs")
    run_root = _canonical_existing_run_root(pathlib.Path(receipt["run_root"]))
    if path != run_root / "run-inputs.json" or path.resolve(strict=True) != path:
        raise ToolchainError("run inputs receipt path differs")
    manifest_path = pathlib.Path(receipt["snapshot_manifest_path"])
    if (manifest_path != run_root / "snapshot-manifest.json" or manifest_path.is_symlink()
            or not manifest_path.is_file() or manifest_path.lstat().st_nlink != 1
            or sha(manifest_path) != receipt["snapshot_manifest_sha256"]):
        raise ToolchainError("run input snapshot manifest binding differs")
    manifest = load(manifest_path)
    closed(manifest, {"schema_version", "run_id", "lock_sha256", "artifacts"}, "run input snapshot manifest")
    if (manifest["schema_version"] != "brandkit-run-input-snapshot-manifest.v1"
            or manifest["run_id"] != receipt["run_id"] or manifest["lock_sha256"] != receipt["lock_sha256"]
            or set(manifest["artifacts"]) != set(CONSUMED_RUN_ARTIFACTS)):
        raise ToolchainError("run input snapshot manifest differs")
    rebound = json.loads(json.dumps(lock))
    descriptors = _run_fd_map(receipt["run_id"])
    artifact_fields = {"source_path", "source_identity_before", "source_identity_after",
                       "files", "bytes", "snapshot"}
    for name in CONSUMED_RUN_ARTIFACTS:
        artifact = receipt["artifacts"].get(name)
        if (not isinstance(artifact, dict) or set(artifact) != artifact_fields
                or artifact["source_path"] != lock["artifacts"][name]["path"]
                or digest(artifact["source_identity_before"], f"source identity {name}") != artifact["source_identity_after"]
                or type(artifact["files"]) is not int or artifact["files"] < 0
                or type(artifact["bytes"]) is not int or artifact["bytes"] < 0):
            raise ToolchainError(f"run input artifact receipt differs: {name}")
        snapshot = manifest["artifacts"][name]
        snapshot_fields = {"format", "archive_sha256", "archive_size", "content_digest", "files", "bytes"}
        if (not isinstance(snapshot, dict) or set(snapshot) != snapshot_fields
                or snapshot != artifact["snapshot"] or snapshot["format"] != "ustar"
                or digest(snapshot["archive_sha256"], f"snapshot archive {name}") != snapshot["archive_sha256"]
                or digest(snapshot["content_digest"], f"snapshot content {name}") != snapshot["content_digest"]
                or type(snapshot["archive_size"]) is not int or snapshot["archive_size"] <= 0
                or type(snapshot["files"]) is not int or snapshot["files"] < 0
                or type(snapshot["bytes"]) is not int or snapshot["bytes"] < 0
                or snapshot["files"] != artifact["files"] or snapshot["bytes"] != artifact["bytes"]):
            raise ToolchainError(f"run input snapshot receipt differs: {name}")
        raw, files_by_name, _ = _locked_archive_payload(name, descriptors[name], lock["artifacts"][name])
        if (hashlib.sha256(raw).hexdigest() != snapshot["archive_sha256"] or len(raw) != snapshot["archive_size"]
                or len(files_by_name) != snapshot["files"]
                or sum(len(value) for value in files_by_name.values()) != snapshot["bytes"]):
            raise ToolchainError(f"run input snapshot changed: {name}")
    for name in set(lock["artifacts"]) - set(CONSUMED_RUN_ARTIFACTS):
        artifact = receipt["artifacts"].get(name)
        if (not isinstance(artifact, dict) or set(artifact) != artifact_fields or artifact["snapshot"] is not None
                or artifact["source_path"] != lock["artifacts"][name]["path"]
                or digest(artifact["source_identity_before"], f"source identity {name}") != artifact["source_identity_after"]
                or type(artifact["files"]) is not int or artifact["files"] < 0
                or type(artifact["bytes"]) is not int or artifact["bytes"] < 0):
            raise ToolchainError(f"run input unconsumed artifact receipt differs: {name}")
    return rebound, receipt


def validate_lock(path: pathlib.Path = LOCK_PATH) -> tuple[dict, dict[str, dict]]:
    lock, components = validate_lock_structure(path)
    receipt_path = os.environ.get(RUN_INPUT_ENV)
    if receipt_path:
        if path.resolve() != LOCK_PATH.resolve():
            raise ToolchainError("run inputs require the canonical shared lock")
        lock, _ = load_run_inputs(lock, pathlib.Path(receipt_path), expected_run_id=os.environ.get(RUN_ID_ENV))
        return lock, components
    import runtime_archives
    for name, record in lock["artifacts"].items():
        if name.startswith("runtime_"):
            component = name.removeprefix("runtime_").replace("_", "-")
            runtime_archives.inspect_archive(pathlib.Path(record["path"]), lock["runtime_archives"][component])
        else:
            verify_artifact_record(record, name)
    return lock, components


def verify_artifact_record(record: dict, label: str) -> None:
    path = pathlib.Path(record["path"])
    if path.is_symlink() or not path.exists():
        raise ToolchainError(f"artifact disappeared or became linked: {label}")
    if path.is_file():
        if sha(path) != record["sha256"]:
            raise ToolchainError(f"artifact changed: {label}")
        return
    manifest = pathlib.Path(record["manifest"])
    if manifest.parent != path or manifest.is_symlink() or sha(manifest) != record["manifest_sha256"]:
        raise ToolchainError(f"artifact manifest changed: {label}")
    verify_closed_artifact_directory(path, manifest)


def immutable_snapshot(record: dict, destination: pathlib.Path, label: str) -> pathlib.Path:
    """Copy verified bytes once, recheck the source, then consume only the copy."""
    source = pathlib.Path(record["path"])
    verify_artifact_record(record, label)
    holder = destination / label
    holder.mkdir(mode=0o700)
    target = holder / source.name
    if source.is_file():
        shutil.copyfile(source, target)
        if sha(target) != record["sha256"]:
            raise ToolchainError(f"artifact snapshot hash differs: {label}")
        target.chmod(0o400)
    else:
        shutil.copytree(source, target, symlinks=True)
        copied_manifest = target / pathlib.Path(record["manifest"]).name
        verify_closed_artifact_directory(target, copied_manifest)
        for path in sorted(target.rglob("*"), reverse=True):
            path.chmod(0o500 if path.is_dir() else 0o400)
        target.chmod(0o500)
    holder.chmod(0o500)
    verify_artifact_record(record, label)
    return target


def remove_snapshot(root: pathlib.Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        if not path.is_symlink():
            path.chmod(0o700 if path.is_dir() else 0o600)
    root.chmod(0o700)
    shutil.rmtree(root)


def safe_extract(archive: pathlib.Path, destination: pathlib.Path) -> None:
    with tarfile.open(archive) as handle:
        for member in handle.getmembers():
            pure = pathlib.PurePosixPath(member.name)
            if pure.is_absolute() or ".." in pure.parts or member.isdev() or member.issym() or member.islnk():
                raise ToolchainError(f"unsafe Humanizer archive member: {member.name}")
        handle.extractall(destination, filter="data")


def content_hash(root: pathlib.Path) -> str:
    value = hashlib.sha256()
    skip = {".git", ".venv", "__pycache__", ".pytest_cache"}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        rel = path.relative_to(root)
        if any(part in skip for part in rel.parts) or path.suffix == ".pyc":
            continue
        encoded = rel.as_posix().encode()
        if path.is_symlink(): value.update(b"L\0" + encoded + b"\0" + os.readlink(path).encode() + b"\0")
        elif path.is_file(): value.update(b"F\0" + encoded + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return value.hexdigest()


def verify_manifested_directory(root: pathlib.Path, record: dict, schema_version: str) -> None:
    manifest = load(pathlib.Path(record["manifest"]))
    closed(manifest, {"schema_version", "files"}, "artifact manifest")
    if manifest["schema_version"] != schema_version:
        raise ToolchainError("artifact manifest schema differs")
    rows = [{"name": path.name, "size": path.stat().st_size, "sha256": sha(path)} for path in sorted(root.iterdir()) if path.is_file() and path.name != "files.sha256.json"]
    if rows != manifest["files"] or set(path.name for path in root.iterdir()) != {row["name"] for row in rows} | {"files.sha256.json"}:
        raise ToolchainError("artifact manifest inventory differs")


def sanitize_installed_records(root: pathlib.Path) -> None:
    """Remove metadata references to files outside the relocatable component."""
    resolved_root = root.resolve()
    pycache_text = os.environ.get("PYTHONPYCACHEPREFIX")
    if not pycache_text:
        raise ToolchainError("PYTHONPYCACHEPREFIX is required for RECORD sanitation")
    pycache_root = pathlib.Path(pycache_text).resolve()
    site_packages = root / ".venv/lib/python3.12/site-packages"
    if site_packages.is_symlink() or not site_packages.is_dir():
        raise ToolchainError("Humanizer site-packages root is absent or linked")
    record_paths = sorted(site_packages.rglob("*.dist-info/RECORD"))

    def sanitize_record(record_path: pathlib.Path) -> tuple[pathlib.Path, str]:
        rows = []
        record_base = record_path.parent.parent
        for row in csv.reader(io.StringIO(record_path.read_text(encoding="utf-8"))):
            if len(row) != 3:
                raise ToolchainError(f"invalid installed RECORD row: {record_path}")
            referenced = (record_base / row[0]).resolve(strict=False)
            if not referenced.is_relative_to(resolved_root):
                if row[1:] != ["", ""] or referenced.suffix != ".pyc" or not referenced.is_relative_to(pycache_root):
                    raise ToolchainError(f"installed RECORD has an unapproved external reference: {record_path}: {row[0]}")
                continue
            if referenced == record_path:
                if row[1:] != ["", ""]:
                    raise ToolchainError(f"installed RECORD self row is not empty: {record_path}")
                rows.append([row[0], "", ""])
                continue
            vendored_wheel_launcher = (
                record_path.relative_to(site_packages).as_posix()
                == "setuptools/_vendor/wheel-0.46.3.dist-info/RECORD"
                and row == ["bin/wheel", "sha256=OLjFs9G7MQ-f_mhuW2aLGpngsyphW_v5FIoyN88Hfk0", "339"]
                and not referenced.exists())
            if vendored_wheel_launcher:
                continue
            if referenced.is_symlink() or not referenced.is_file():
                raise ToolchainError(f"installed RECORD target is absent or linked: {record_path}: {row[0]}")
            data = referenced.read_bytes()
            encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            expected = [f"sha256={encoded}", str(len(data))]
            transformed_model_provenance = row[0].endswith("de_core_news_sm-3.8.0.dist-info/direct_url.json")
            if row[1:] != expected and not transformed_model_provenance:
                raise ToolchainError(f"installed RECORD target hash/size differs: {record_path}: {row[0]}")
            row = [row[0], *expected]
            rows.append(row)
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        return record_path, output.getvalue()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="record-sanitize") as pool:
        sanitized = list(pool.map(sanitize_record, record_paths))
    # No RECORD is changed until the entire input set has passed validation.
    for record_path, value in sanitized:
        record_path.write_text(value, encoding="utf-8")


def remove_internal_venv_bytecode(root: pathlib.Path) -> None:
    """Remove only complete, RECORD-owned bytecode from the relocatable venv."""
    root = root.resolve(strict=True)
    site_packages = root / ".venv/lib/python3.12/site-packages"
    record_paths = sorted(site_packages.rglob("*.dist-info/RECORD"))
    updates = {}; recorded = set()
    for record in record_paths:
        rows = []
        for row in csv.reader(io.StringIO(record.read_text(encoding="utf-8"))):
            if len(row) != 3:
                raise ToolchainError(f"invalid installed RECORD row: {record}")
            target = (site_packages / row[0]).resolve(strict=False)
            is_bytecode = target.suffix == ".pyc" and "__pycache__" in target.parts
            if not is_bytecode:
                rows.append(row); continue
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ToolchainError(f"installed bytecode RECORD escapes root: {record}") from exc
            if target.is_symlink() or not target.is_file():
                raise ToolchainError(f"installed bytecode RECORD target differs: {target}")
            recorded.add(target)
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        updates[record] = output.getvalue()
    actual = {path.resolve() for path in site_packages.rglob("*.pyc")}
    if actual != recorded:
        raise ToolchainError("installed bytecode set differs from direct RECORD closure")
    for target in sorted(recorded):
        target.unlink()
    for directory in sorted((path for path in site_packages.rglob("__pycache__") if path.is_dir() and not path.is_symlink()), key=lambda item: len(item.parts), reverse=True):
        directory.rmdir()
    for record, value in updates.items():
        record.write_text(value, encoding="utf-8")


def relocate_humanizer_install(root: pathlib.Path, previous_root: pathlib.Path,
                               final_root: pathlib.Path | None = None) -> None:
    root = root.resolve(strict=True)
    final_root = root if final_root is None else final_root
    old_text = str(previous_root); new_text = str(final_root)
    if not previous_root.is_absolute() or old_text == new_text:
        raise ToolchainError("Humanizer relocation roots differ")
    old_bytes = old_text.encode(); new_bytes = new_text.encode()
    relocated_payloads: dict[pathlib.Path, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            target = os.readlink(path)
            if old_text in target:
                path.unlink(); path.symlink_to(target.replace(old_text, new_text))
            continue
        if not path.is_file():
            continue
        data = path.read_bytes()
        if old_bytes in data:
            try: data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ToolchainError(f"Humanizer binary embeds previous relocation root: {path}") from exc
            mode = stat.S_IMODE(path.stat().st_mode); data = data.replace(old_bytes, new_bytes)
            path.chmod(mode | stat.S_IWUSR); path.write_bytes(data); path.chmod(mode)
        relocated_payloads[path.resolve()] = data
    site_packages = root / ".venv/lib/python3.12/site-packages"
    for record in sorted(site_packages.glob("*.dist-info/RECORD")):
        rows = []
        for row in csv.reader(io.StringIO(record.read_text(encoding="utf-8"))):
            if len(row) != 3:
                raise ToolchainError(f"invalid relocated Humanizer RECORD row: {record}")
            target = (site_packages / row[0]).resolve(strict=False)
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ToolchainError(f"relocated Humanizer RECORD escapes root: {record}") from exc
            if target == record.resolve():
                row = [row[0], "", ""]
            else:
                if target.is_symlink() or not target.is_file():
                    raise ToolchainError(f"relocated Humanizer RECORD target differs: {target}")
                try: data = relocated_payloads[target]
                except KeyError as exc: raise ToolchainError(f"relocated Humanizer target was not scanned once: {target}") from exc
                encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                row = [row[0], f"sha256={encoded}", str(len(data))]
            rows.append(row)
        output = io.StringIO(newline=""); csv.writer(output, lineterminator="\n").writerows(rows)
        mode = stat.S_IMODE(record.stat().st_mode)
        record_bytes = output.getvalue().encode()
        record.chmod(mode | stat.S_IWUSR); record.write_bytes(record_bytes); record.chmod(mode)
        relocated_payloads[record.resolve()] = record_bytes
    # The one mandatory post-move scan_tree() call proves text, binary and
    # symlink stale-root absence; do not perform another broad search here.


def build_humanizer(destination: pathlib.Path, lock: dict, source: pathlib.Path, wheelhouse: pathlib.Path) -> None:
    wheelhouse_record = lock["artifacts"]["humanizer_wheelhouse"]
    copied_record = {**wheelhouse_record, "path":str(wheelhouse), "manifest":str(wheelhouse / pathlib.Path(wheelhouse_record["manifest"]).name)}
    verify_manifested_directory(wheelhouse, copied_record, "humanizer-wheelhouse.v1")
    safe_extract(source, destination)
    wrapper = destination / "skills/humanizer-de/SKILL.md"
    bundle = wrapper.with_name("SKILL.bundle.md")
    if not wrapper.is_file() or bundle.exists(): raise ToolchainError("Humanizer source transform layout differs")
    wrapper.rename(bundle)
    doctor = destination / "scripts/doctor.py"; text = doctor.read_text(encoding="utf-8")
    needle = 'root / "skills" / "humanizer-de" / "SKILL.md"'
    if text.count(needle) != 2: raise ToolchainError("Humanizer doctor transform differs")
    doctor.write_text(text.replace(needle, 'root / "skills" / "humanizer-de" / "SKILL.bundle.md"'), encoding="utf-8")
    human_lock = load(ROOT / "toolchain/humanizer-de/toolchain.lock.json")
    if content_hash(destination) != human_lock["skill"]["content_sha256"]:
        raise ToolchainError("Humanizer transformed content hash differs")
    run([lock["bindings"]["python_executable"], "-m", "venv", "--copies", str(destination / ".venv")], phase="humanizer-venv")
    python = destination / ".venv/bin/python"
    wheels = sorted(wheelhouse.glob("*.whl"))
    if len(wheels) != human_lock["python"]["runtime_distribution_count"]:
        raise ToolchainError("Humanizer wheel count differs")
    requirement_text = (ROOT / "toolchain/humanizer-de/requirements-precise.lock.txt").read_text(encoding="utf-8")
    source_line = "de_core_news_sm @ https://github.com/explosion/spacy-models/releases/download/de_core_news_sm-3.8.0/de_core_news_sm-3.8.0-py3-none-any.whl"
    if requirement_text.count(source_line) != 1:
        raise ToolchainError("Humanizer model requirement source differs")
    offline_requirements = destination / ".venv/offline-requirements.lock.txt"
    offline_requirements.write_text(requirement_text.replace(source_line, "de_core_news_sm==3.8.0"), encoding="utf-8")
    run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-compile", "--no-index", "--only-binary=:all:", "--require-hashes", "--find-links", str(wheelhouse), "-r", str(offline_requirements)], phase="humanizer-wheel-install", timeout=1200)
    model_info = destination / ".venv/lib/python3.12/site-packages/de_core_news_sm-3.8.0.dist-info"
    if not model_info.is_dir() or model_info.is_symlink():
        raise ToolchainError("Humanizer model provenance target is absent")
    model_direct = model_info / "direct_url.json"
    model_hash = "fec69fec52b1780f2d269d5af7582a5e28028738bd3190532459aeb473bfa3e7"
    model_url = "https://github.com/explosion/spacy-models/releases/download/de_core_news_sm-3.8.0/de_core_news_sm-3.8.0-py3-none-any.whl"
    model_direct.write_text(json.dumps({"archive_info":{"hash":f"sha256={model_hash}","hashes":{"sha256":model_hash}},"url":model_url}, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    sanitize_installed_records(destination)
    remove_internal_venv_bytecode(destination)
    release_marker = {"schema_version":"humanizer-brandkit-install.v1", "version":human_lock["skill"]["version"],
                      "source_revision":human_lock["skill"]["source_revision"], "content_sha256":human_lock["skill"]["content_sha256"],
                      "doctor_adapter_sha256":lock["transforms"]["humanizer_doctor_runtime_adapter"]["sha256"]}
    (destination / ".venv/brandkit-release.json").write_text(json.dumps(release_marker, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    verify_manifested_directory(wheelhouse, copied_record, "humanizer-wheelhouse.v1")


def verify_humanizer(root: pathlib.Path, lock: dict, *, scanned_content_sha256: str | None = None) -> None:
    human_lock = load(ROOT / "toolchain/humanizer-de/toolchain.lock.json")
    actual_content = scanned_content_sha256 if scanned_content_sha256 is not None else content_hash(root)
    if actual_content != human_lock["skill"]["content_sha256"]:
        raise ToolchainError("Humanizer installed content differs")
    python = root / ".venv/bin/python"
    freeze = subprocess.check_output(readonly_command(root, [python, "-m", "pip", "freeze"]), text=True)
    packages = subprocess.check_output(readonly_command(root, [python, "-m", "pip", "list", "--format=freeze", "--disable-pip-version-check"]), text=True)
    package_lines = sorted(packages.splitlines(), key=str.casefold)
    if hashlib.sha256(freeze.encode()).hexdigest() != human_lock["python"]["freeze_sha256"]:
        raise ToolchainError("Humanizer freeze hash differs")
    if len(package_lines) != human_lock["python"]["installed_distribution_count"] or hashlib.sha256(("\n".join(package_lines) + "\n").encode()).hexdigest() != human_lock["python"]["installed_distributions_sha256"]:
        raise ToolchainError("Humanizer distribution inventory differs")
    for script in (root / ".venv/bin").iterdir():
        if script.is_file() and not script.is_symlink():
            first = script.open("rb").readline(4096)
            if first.startswith(b"#!") and str(root).encode() not in first and first.rstrip() not in {b"#!/bin/sh", b"#!/usr/bin/env bash"}:
                raise ToolchainError(f"Humanizer script has foreign shebang: {script.name}")
    run(readonly_command(root, [root / ".venv/bin/spacy", "--help"]), phase="humanizer-spacy", timeout=600)
    env = {**os.environ, "PATH": "/opt/homebrew/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}
    # The upstream doctor's 20 s child timeout is shorter than a measured cold
    # dyld load on removable APFS media. Keep the audited doctor bytes intact and
    # adapt only its subprocess deadline in this locked runtime invocation.
    adapter = ROOT / lock["transforms"]["humanizer_doctor_runtime_adapter"]["path"]
    run(readonly_command(root, [python, adapter, root / "scripts/doctor.py"]), env, phase="humanizer-doctor-full", timeout=1200)
    run(readonly_command(root, [python, root / "scripts/humanizer_audit.py", "--file", root / "tests/corpus/case_01_input.md", "--mode", "sachlich", "--precise", "--no-profile"]), env, phase="humanizer-precise", timeout=600)
    smoke_commands = [
        ["register_lint.py", "--text", "Die Idee war neu. Sie überzeugte sofort.", "--mode", "sachlich", "--precise", "--fail-on", "never"],
        ["rhythm_lint.py", "--text", "Kurz. Der zweite Satz erklärt den Zusammenhang genauer. Dann folgt ein knapper Abschluss.", "--scope", "user_text", "--mode", "sachlich", "--fail-on", "never"],
        ["evidence_lint.py", "--before", "Im Jahr 2025 sank der Wert um 12 Prozent.", "--after", "2025 ging der Wert um 12 Prozent zurück.", "--precise", "--fail-on", "blocker"],
        ["spell_lint.py", "--before", "Das Verfahren ist nachvollziehbar.", "--after", "Das Verfahren bleibt nachvollziehbar.", "--fail-on", "any"],
        ["syntax_lint.py", "--text", "Die Redaktion prüft den Text, bevor sie ihn veröffentlicht."],
    ]
    for smoke in smoke_commands:
        run(readonly_command(root, [python, root / "scripts" / smoke[0], *smoke[1:]]), env, phase=f"humanizer-smoke-{smoke[0]}", timeout=600)


def state_payload(root: pathlib.Path, lock: dict) -> dict:
    components = dict(lock["normative"]["installed_inventory"])
    return {"schema_version": "brandkit-toolchain-installation.v1", "release": lock["release"]["id"], "lock_sha256": sha(LOCK_PATH), "components": components}


def write_state(root: pathlib.Path, payload: dict) -> None:
    (root / "installation.json").write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def verify_pdf_install(pdf_home: pathlib.Path) -> None:
    pdf_version = load(ROOT / "toolchain/pdf-renderer.production.v1.lock.json")["engine"]["version"]
    pdf_verifier = pdf_home / f".agents/skill-store/brand-voice-pdf/{pdf_version}/scripts/verify-installation"
    pdf_key = pdf_home / ".agents/keys/brand-voice-pdf.key"
    subprocess.run(
        readonly_command(pdf_home, [pdf_verifier, pdf_home, pdf_key, pdf_version]),
        check=True,
        env={**os.environ, "BRANDKIT_HOME": str(pdf_home), "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.DEVNULL,
        timeout=1200,
    )


def relocate_pdf_install(pdf_home: pathlib.Path, previous_home: pathlib.Path,
                         final_home: pathlib.Path | None = None) -> None:
    """Re-seal the authenticated PDF home for its final logical path before publish."""
    pdf_home = pdf_home.resolve(strict=True)
    old_text = str(previous_home)
    final_home = pdf_home if final_home is None else final_home
    new_text = str(final_home)
    if not previous_home.is_absolute() or old_text == new_text:
        raise ToolchainError("PDF relocation roots differ")
    state = pdf_home / ".agents/activation-state"
    key_path = pdf_home / ".agents/keys/brand-voice-pdf.key"
    if key_path.is_symlink() or not key_path.is_file() or len(key_path.read_bytes()) < 32:
        raise ToolchainError("PDF relocation key differs")
    key = key_path.read_bytes()
    version = load(ROOT / "toolchain/pdf-renderer.production.v1.lock.json")["engine"]["version"]
    manifest_path = state / f"brand-voice-pdf-{version}.manifest.json"
    activation_path = state / "brand-voice-pdf.json"

    def signed_payload(path: pathlib.Path) -> dict:
        envelope = load(path)
        closed(envelope, {"payload", "hmac_sha256"}, "PDF signed relocation state")
        payload = envelope["payload"]
        if not isinstance(payload, dict):
            raise ToolchainError("PDF signed relocation payload differs")
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        if not hmac.compare_digest(envelope["hmac_sha256"], hmac.new(key, raw, hashlib.sha256).hexdigest()):
            raise ToolchainError("PDF signed relocation state signature differs")
        return payload

    manifest = signed_payload(manifest_path)
    activation = signed_payload(activation_path)
    closed(manifest, {"schema_version", "version", "predecessor_links", "release", "runtime", "link_topology"}, "PDF release manifest")
    closed(activation, {"schema_version", "version", "manifest_sha256", "predecessor_links", "links"}, "PDF activation")
    if manifest["version"] != version or activation["version"] != version:
        raise ToolchainError("PDF relocation version differs")

    def replace_tree(value):
        if isinstance(value, str):
            return value.replace(old_text, new_text)
        if isinstance(value, list):
            return [replace_tree(item) for item in value]
        if isinstance(value, dict):
            return {key.replace(old_text, new_text): replace_tree(item) for key, item in value.items()}
        return value

    old_bytes = old_text.encode()
    new_bytes = new_text.encode()
    relocated_payloads: dict[pathlib.Path, bytes] = {}
    for path in sorted(pdf_home.rglob("*")):
        if path.is_relative_to(state):
            continue
        if path.is_symlink():
            target = os.readlink(path)
            if old_text in target:
                path.unlink(); path.symlink_to(target.replace(old_text, new_text))
            continue
        if not path.is_file():
            continue
        data = path.read_bytes()
        if old_bytes in data:
            try: data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ToolchainError(f"PDF binary embeds previous relocation root: {path}") from exc
            mode = stat.S_IMODE(path.stat().st_mode); data = data.replace(old_bytes, new_bytes)
            path.chmod(mode | stat.S_IWUSR); path.write_bytes(data); path.chmod(mode)
        relocated_payloads[path.resolve()] = data

    for record in sorted(pdf_home.rglob("site-packages/*.dist-info/RECORD")):
        rows = []
        site_packages = record.parent.parent
        for row in csv.reader(io.StringIO(record.read_text(encoding="utf-8"))):
            if len(row) != 3:
                raise ToolchainError(f"invalid PDF installed RECORD row: {record}")
            target = (site_packages / row[0]).resolve(strict=False)
            try:
                target.relative_to(pdf_home)
            except ValueError as exc:
                raise ToolchainError(f"PDF installed RECORD escapes root: {record}") from exc
            if target == record.resolve():
                row = [row[0], "", ""]
            else:
                if target.is_symlink() or not target.is_file():
                    raise ToolchainError(f"PDF installed RECORD target differs: {target}")
                try: data = relocated_payloads[target]
                except KeyError as exc: raise ToolchainError(f"relocated PDF target was not scanned once: {target}") from exc
                encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                row = [row[0], f"sha256={encoded}", str(len(data))]
            rows.append(row)
        output = io.StringIO(newline="")
        csv.writer(output, lineterminator="\n").writerows(rows)
        mode = stat.S_IMODE(record.stat().st_mode)
        record_bytes = output.getvalue().encode()
        record.chmod(mode | stat.S_IWUSR); record.write_bytes(record_bytes); record.chmod(mode)
        relocated_payloads[record.resolve()] = record_bytes

    def raw_inventory(root: pathlib.Path) -> list[dict]:
        if root.is_symlink() or not root.is_dir():
            raise ToolchainError(f"PDF relocation inventory root differs: {root}")
        result = []
        for path in sorted(root.rglob("*")):
            item = {"path": path.relative_to(root).as_posix(), "mode": format(stat.S_IMODE(path.lstat().st_mode), "04o")}
            if path.is_symlink():
                item.update(type="symlink", target=os.readlink(path))
            elif path.is_dir():
                item["type"] = "directory"
            elif path.is_file():
                try: data = relocated_payloads[path.resolve()]
                except KeyError as exc: raise ToolchainError(f"PDF inventory target was not scanned once: {path}") from exc
                item.update(type="file", sha256=hashlib.sha256(data).hexdigest())
            else:
                raise ToolchainError(f"PDF relocation inventory contains special file: {path}")
            result.append(item)
        return result

    release = pdf_home / f".agents/skill-store/brand-voice-pdf/{version}"
    runtime = pdf_home / f".agents/runtime-store/brandkit-pdf-renderer/{version}"
    manifest = replace_tree(manifest)
    manifest["release"] = raw_inventory(release)
    manifest["runtime"] = raw_inventory(runtime)
    manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    manifest_envelope = {"payload": manifest, "hmac_sha256": hmac.new(key, manifest_raw, hashlib.sha256).hexdigest()}
    manifest_path.write_text(json.dumps(manifest_envelope, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    activation = replace_tree(activation)
    activation["manifest_sha256"] = sha(manifest_path)
    activation_raw = json.dumps(activation, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    activation_envelope = {"payload": activation, "hmac_sha256": hmac.new(key, activation_raw, hashlib.sha256).hexdigest()}
    activation_path.write_text(json.dumps(activation_envelope, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    # The one mandatory post-move scan_tree() call proves text, binary and
    # symlink stale-root absence; do not perform another broad search here.


def verify_install(root: pathlib.Path, lock: dict, *, full_humanizer: bool = True,
                   operation: str = "verify", stale_roots: tuple[pathlib.Path, ...] = (),
                   return_receipt: bool = False, reuse_receipt: bool = False,
                   sealed_snapshot: dict | None = None) -> dict | tuple[dict, dict, dict | None]:
    state = load(root / "installation.json")
    closed(state, {"schema_version", "release", "lock_sha256", "components"}, "installation state")
    if state["schema_version"] != "brandkit-toolchain-installation.v1" or state["release"] != lock["release"]["id"] or state["lock_sha256"] != sha(LOCK_PATH):
        raise ToolchainError("installation state binding differs")
    if set(state["components"]) != {"humanizer", "mermaid", "graphviz", "pdf-home"}:
        raise ToolchainError("installed component set differs")
    if set(path.name for path in root.iterdir()) != {"humanizer", "mermaid", "graphviz", "pdf-home", "installation.json"}:
        raise ToolchainError("installation root has missing or additional artifacts")
    if state["components"] != lock["normative"]["installed_inventory"]:
        raise ToolchainError("installation expected inventory binding differs")
    scan = None
    if reuse_receipt:
        if not os.environ.get(SUPERVISOR_FD_ENV):
            raise ToolchainError("receipt reuse requires a live supervisor capability")
        try:
            receipt = load_tree_receipt(root, lock, require_live_capability=True)
        except ToolchainError:
            verify_pdf_install(root / "pdf-home")
            names = tuple(sorted(lock["normative"]["installed_inventory"]))
            scan = scan_tree(root, stale_roots=stale_roots, component_names=names,
                             seal_snapshot=(operation == "install"), sealed_snapshot=sealed_snapshot)
            receipt = make_tree_receipt(scan, lock, operation)
            write_tree_receipt(root, receipt)
    else:
        verify_pdf_install(root / "pdf-home")
        names = tuple(sorted(lock["normative"]["installed_inventory"]))
        scan = scan_tree(root, stale_roots=stale_roots, component_names=names,
                         seal_snapshot=(operation == "install"), sealed_snapshot=sealed_snapshot)
        receipt = make_tree_receipt(scan, lock, operation)
        write_tree_receipt(root, receipt)
    actual_components = receipt["component_merkles"]
    mismatches = {
        name: {"expected": lock["normative"]["installed_inventory"][name], "actual": actual}
        for name, actual in actual_components.items()
        if actual != lock["normative"]["installed_inventory"][name]
    }
    if mismatches:
        raise ToolchainError(f"installed inventories differ: {json.dumps(mismatches, sort_keys=True, separators=(',', ':'))}")
    if full_humanizer:
        verify_humanizer(root / "humanizer", lock,
                         scanned_content_sha256=receipt["component_content_sha256"]["humanizer"])
    validate_tree_receipt(receipt, root, lock, live_scan=scan)
    if receipt["operation"] != operation:
        receipt = reissue_tree_receipt(receipt, operation)
        write_tree_receipt(root, receipt)
    return (state, receipt, scan) if return_receipt else state


def run(command: list[str], env: dict | None = None, *, phase: str = "command", timeout: int = 600) -> None:
    print(f"shared-toolchain phase={phase} status=start timeout={timeout}s", flush=True)
    process = subprocess.Popen(command, env=env, start_new_session=True)
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        raise ToolchainError(f"phase timed out after {timeout}s: {phase}") from exc
    if returncode:
        raise subprocess.CalledProcessError(returncode, command)
    print(f"shared-toolchain phase={phase} status=pass", flush=True)


def readonly_command(root: pathlib.Path, command: list[object]) -> list[str]:
    root = root.resolve(strict=True)
    escaped = str(root).replace("\\", "\\\\").replace('"', '\\"')
    profile = f'(version 1)(allow default)(deny file-write* (subpath "{escaped}"))'
    sandbox = pathlib.Path("/usr/bin/sandbox-exec")
    if not sandbox.is_file(): raise ToolchainError("filesystem write-deny sandbox is unavailable")
    return [str(sandbox), "-p", profile, *(str(item) for item in command)]


def install(args: argparse.Namespace) -> None:
    lock, _ = validate_lock()
    root = pathlib.Path(args.root)
    if root.is_symlink() or (root.exists() and (not root.is_dir() or any(root.iterdir()))):
        raise ToolchainError("--root must be an empty, non-linked directory")
    root.parent.mkdir(parents=True, exist_ok=True)
    stage = pathlib.Path(tempfile.mkdtemp(prefix=f".{root.name}.next-", dir=root.parent))
    snapshots = (None if os.environ.get(RUN_INPUT_ENV) else
                 pathlib.Path(tempfile.mkdtemp(prefix=f".{root.name}.artifacts-", dir=root.parent)))
    moved = False
    if root.exists():
        root.rmdir()
    try:
        consumer_inputs = stage / ".run-inputs" if snapshots is None else None
        materialized = install_artifact_inputs(lock, snapshots, consumer_inputs)
        import runtime_archives
        runtime_archives.materialize("humanizer", materialized["runtime_humanizer"],
            lock["runtime_archives"]["humanizer"], stage/"humanizer", root/"humanizer")
        runtime_archives.materialize("mermaid", materialized["runtime_mermaid"],
            lock["runtime_archives"]["mermaid"], stage/"mermaid", root/"mermaid")
        runtime_archives.materialize("graphviz", materialized["runtime_graphviz"],
            lock["runtime_archives"]["graphviz"], stage/"graphviz", root/"graphviz")
        runtime_archives.materialize("pdf-home", materialized["runtime_pdf_home"],
            lock["runtime_archives"]["pdf-home"], stage/"pdf-home", root/"pdf-home",
            key=materialized["pdf_issuer_key"])
        if consumer_inputs is not None:
            remove_snapshot(consumer_inputs)
        write_state(stage, state_payload(stage, lock))
        if os.environ.get("BRANDKIT_TOOLCHAIN_FAIL_AFTER_VERIFY") == "1":
            raise ToolchainError("injected failure after verification")
        previous_humanizer = pathlib.Path("/__BRANDKIT_RUNTIME_ROOT__/humanizer")
        previous_pdf_home = pathlib.Path("/__BRANDKIT_RUNTIME_ROOT__/pdf-home")
        # Produce every byte for the final logical paths while the unpublished
        # stage is still disposable.  The publish operation below is the only
        # post-proof mutation of the tree and performs no content rewrite.
        names = tuple(sorted(lock["normative"]["installed_inventory"]))
        prepublish = scan_tree(stage, stale_roots=(previous_humanizer, previous_pdf_home),
                               component_names=names, logical_root=root)
        if prepublish["component_merkles"] != lock["normative"]["installed_inventory"]:
            raise ToolchainError("prepublish logical-final component Merkle values differ")
        os.replace(stage, root)
        moved = True
        verify_install(root, lock, operation="install",
                       stale_roots=(previous_humanizer, previous_pdf_home))
    except BaseException:
        failed = root if moved else stage
        try:
            remove_snapshot(failed)
        except OSError:
            pass
        raise
    finally:
        if snapshots is not None:
            remove_snapshot(snapshots)
    print(root)


def install_artifact_inputs(lock: dict, snapshots: pathlib.Path | None,
                            consumer_root: pathlib.Path | None = None) -> dict[str, pathlib.Path]:
    """Return the already verified run inputs, or the legacy per-install copies."""
    if snapshots is None:
        if not os.environ.get(RUN_INPUT_ENV) or consumer_root is None or consumer_root.exists() or consumer_root.is_symlink():
            raise ToolchainError("run-local artifact inputs require a run-input receipt")
        consumer_root.mkdir(mode=0o700)
        descriptors = _run_fd_map(os.environ.get(RUN_ID_ENV, "")); output = {}
        for name in CONSUMED_RUN_ARTIFACTS:
            record = lock["artifacts"][name]
            _, files, directories = _locked_archive_payload(name, descriptors[name], record)
            source_name = pathlib.Path(record["path"]).name
            target = consumer_root / name / source_name
            if "manifest" not in record:
                target.parent.mkdir(parents=True); target.write_bytes(files["."]); target.chmod(0o400)
            else:
                target.mkdir(parents=True)
                for directory in sorted(directories - {"."}): (target/directory).mkdir(parents=True, exist_ok=True)
                for relative, value in files.items():
                    member = target/relative; member.parent.mkdir(parents=True, exist_ok=True); member.write_bytes(value); member.chmod(0o400)
                for directory in sorted((p for p in target.rglob("*") if p.is_dir()), reverse=True): directory.chmod(0o500)
                target.chmod(0o500)
            output[name] = target
        return output
    return {name:immutable_snapshot(lock["artifacts"][name], snapshots, name)
            for name in CONSUMED_RUN_ARTIFACTS}


def probe_network_deny() -> dict:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    client = socket.create_connection(server.getsockname(), timeout=3)
    accepted, _ = server.accept()
    client.close(); accepted.close(); server.close()
    try:
        socket.getaddrinfo("example.com", 443)
    except socket.gaierror as exc:
        dns = exc.__class__.__name__
    else:
        raise ToolchainError("external DNS unexpectedly succeeded")
    try:
        external = socket.create_connection(("1.1.1.1", 443), timeout=3)
    except OSError as exc:
        tcp = exc.__class__.__name__
    else:
        external.close()
        raise ToolchainError("external TCP unexpectedly succeeded")
    return {"loopback":"pass", "dns":"denied", "dns_error":dns, "tcp":"denied", "tcp_error":tcp}


def validate_container_envelope(envelope: dict, lock: dict) -> dict:
    closed(envelope, {"schema_version", "image_id", "network_mode", "container_log_sha256", "container_log"}, "container runtime envelope")
    if envelope["schema_version"] != "brandkit-container-runtime-envelope.v1" or envelope["image_id"] != lock["execution"]["container_digest"] or envelope["network_mode"] != "none":
        raise ToolchainError("actual container identity/network differs")
    payload = envelope["container_log"]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    if hashlib.sha256(raw.encode()).hexdigest() != envelope["container_log_sha256"]:
        raise ToolchainError("container version log envelope hash differs")
    closed(payload, {"schema_version", "environment", "platform", "quarto", "node", "chrome", "mermaid", "graphviz", "lualatex", "poppler", "qpdf", "tesseract", "tessdata_deu_sha256", "font_sha256", "tesseract_languages"}, "container version log")
    expected = lock["normative"]["versions"]
    checks = {
        "quarto":expected["quarto"], "node":expected["node"], "chrome":expected["chrome"], "mermaid":expected["mermaid"],
        "graphviz":expected["graphviz"], "lualatex":expected["lualatex"], "poppler":expected["poppler"],
        "qpdf":expected["qpdf"], "tesseract":expected["tesseract"], "tessdata_deu_sha256":expected["tessdata_deu_sha256"],
        "font_sha256":lock["bindings"]["font_sha256"], "tesseract_languages":expected["tesseract_languages"],
    }
    for name, expected_value in checks.items():
        if payload[name] != expected_value:
            raise ToolchainError(f"actual container value differs: {name}")
    if payload["environment"] != {key:lock["environment"][key] for key in ("LANG", "LC_ALL", "TZ", "SOURCE_DATE_EPOCH")} or payload["platform"] != {"system":"Linux", "machine":"x86_64"}:
        raise ToolchainError("actual container environment/platform differs")
    return envelope


def validate_container_log(path: pathlib.Path, lock: dict) -> dict:
    return validate_container_envelope(load(path), lock)


def version_log(args: argparse.Namespace) -> None:
    lock, components = validate_lock()
    root = pathlib.Path(args.root).resolve(strict=True)
    can_reuse = bool(os.environ.get(SUPERVISOR_FD_ENV)) and tree_receipt_path(root).is_file()
    state, tree_receipt, _ = verify_install(root, lock, full_humanizer=False,
        operation="version-log", return_receipt=True, reuse_receipt=can_reuse)
    human_py = root / "humanizer/.venv/bin/python"
    pdf_py = root / "pdf-home/.agents/runtime-store/brandkit-pdf-renderer/1.2.28/.venv/bin/python"
    human_release = load(root / "humanizer/.venv/brandkit-release.json")
    pdf_activation = load(root / "pdf-home/.agents/activation-state/brand-voice-pdf.json")["payload"]
    mermaid_root = root / "mermaid"
    mermaid_node = mermaid_root / "node/node-v22.18.0-darwin-arm64/bin/node"
    mermaid_npm = mermaid_root / "node/node-v22.18.0-darwin-arm64/bin/npm"
    mermaid_chrome = mermaid_root / "browser/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
    mermaid = {
        "schema_version":"mermaid-version-log.v1",
        "bundle_tree_sha256":lock["artifacts"]["mermaid_bundle"]["sha256"],
        "lock_sha256":lock["components"]["mermaid"]["sha256"],
        "node_executable_sha256":sha(mermaid_node),
        "node_version":subprocess.check_output(readonly_command(root, [mermaid_node, "--version"]), text=True).strip(),
        "npm_version":subprocess.check_output(readonly_command(root, [mermaid_npm, "--version"]), text=True,
            env={**os.environ, "PATH":f"{mermaid_node.parent}:/usr/bin:/bin"}).strip(),
        "mermaid_cli_version":subprocess.check_output(readonly_command(root, [mermaid_root/"node_modules/.bin/mmdc", "--version"]), text=True,
            env={**os.environ, "PATH":f"{mermaid_node.parent}:/usr/bin:/bin"}).strip(),
        "browser_executable_sha256":sha(mermaid_chrome),
        "browser_version":subprocess.check_output(readonly_command(root, [mermaid_chrome, "--version"]), text=True).strip(),
    }
    graphviz_package = load(root / "graphviz/viz/package.json")
    graphviz_backend = sha(root / "graphviz/viz/lib/backend.js")
    container = validate_container_log(pathlib.Path(args.container_log).resolve(strict=True), lock)
    environment = {key:os.environ.get(key) for key in ("LANG", "LC_ALL", "TZ", "SOURCE_DATE_EPOCH")}
    if environment != {key:lock["environment"][key] for key in environment}:
        raise ToolchainError("actual host environment differs")
    actual = {
        "schema_version": "brandkit-toolchain-version-log.v1",
        "release": lock["release"]["id"],
        "lock_sha256": sha(LOCK_PATH),
        "container": container,
        "environment": environment,
        "network": probe_network_deny(),
        "humanizer": {"version": human_release["version"],
                      "content_sha256":tree_receipt["component_content_sha256"]["humanizer"],
                      "python": subprocess.check_output(readonly_command(root, [human_py, "-c", "import platform;print(platform.python_version())"]), text=True).strip()},
        "pdf_renderer": {"version": pdf_activation["version"], "python": subprocess.check_output(readonly_command(root, [pdf_py, "-c", "import platform;print(platform.python_version())"]), text=True).strip()},
        "mermaid": mermaid,
        "graphviz": {"package_version":graphviz_package["version"], "backend_sha256":graphviz_backend, "node": subprocess.check_output(readonly_command(root, [root / "graphviz/node/bin/node", "--version"]), text=True).strip().removeprefix("v")},
        "font": {"path": lock["bindings"]["font_path"], "postscript_name": lock["bindings"]["font_postscript_name"], "sha256": sha(regular_under_root(lock["bindings"]["font_path"], "font"))},
        "installation_root": str(root),
        "installation_state_sha256": sha(root / "installation.json"),
        "tree_receipt_digest": tree_receipt["receipt_digest"],
    }
    if actual["humanizer"]["python"] != lock["bindings"]["python_version"] or actual["pdf_renderer"]["python"] != lock["bindings"]["python_version"]:
        raise ToolchainError("actual Python version differs")
    if human_release["version"] != components["humanizer"]["skill"]["version"] or actual["humanizer"]["content_sha256"] != components["humanizer"]["skill"]["content_sha256"]:
        raise ToolchainError("actual Humanizer release differs")
    if actual["pdf_renderer"]["version"] != components["pdf_renderer"]["engine"]["version"]:
        raise ToolchainError("actual PDF renderer version differs")
    if actual["mermaid"]["node_version"].removeprefix("v") != lock["bindings"]["node_version"] or actual["graphviz"]["node"] != lock["bindings"]["node_version"]:
        raise ToolchainError("actual Node version differs")
    if actual["mermaid"]["browser_executable_sha256"] != lock["bindings"]["chrome_sha256"] or actual["mermaid"]["mermaid_cli_version"] != components["mermaid"]["package_lock"]["root_version"]:
        raise ToolchainError("actual Chrome hash differs")
    if actual["graphviz"]["package_version"] != components["graphviz"]["package"]["version"] or actual["graphviz"]["backend_sha256"] != components["graphviz"]["graphviz"]["wasm_backend_sha256"]:
        raise ToolchainError("actual Graphviz installation differs")
    validate_tree_receipt(tree_receipt, root, lock)
    tree_receipt = reissue_tree_receipt(tree_receipt, "version-log")
    write_tree_receipt(root, tree_receipt)
    actual["tree_receipt_digest"] = tree_receipt["receipt_digest"]
    print(json.dumps(actual, sort_keys=True, separators=(",", ":")))


def validate_version_log(path: pathlib.Path, root: pathlib.Path, lock: dict, components: dict) -> dict:
    if path.is_symlink() or not path.is_file() or path.name != "version-log.json":
        raise ToolchainError("rollback verification log must be a regular canonical receipt")
    path = path.resolve(strict=True)
    if path.parent != root.parent:
        raise ToolchainError("rollback verification log is not adjacent to its installation root")
    payload = load(path)
    closed(payload, {"schema_version", "release", "lock_sha256", "container", "environment", "network",
                     "humanizer", "pdf_renderer", "mermaid", "graphviz", "font", "installation_root",
                     "installation_state_sha256", "tree_receipt_digest"}, "rollback verification log")
    if (payload["schema_version"] != "brandkit-toolchain-version-log.v1"
            or payload["release"] != lock["release"]["id"]
            or payload["lock_sha256"] != sha(LOCK_PATH)
            or payload["installation_root"] != str(root)
            or payload["installation_state_sha256"] != sha(root / "installation.json")
            or digest(payload["tree_receipt_digest"], "version-log tree receipt") != payload["tree_receipt_digest"]):
        raise ToolchainError("rollback verification log binding differs")
    if payload["environment"] != {key:lock["environment"][key] for key in ("LANG", "LC_ALL", "TZ", "SOURCE_DATE_EPOCH")}:
        raise ToolchainError("rollback verification environment differs")
    network = payload["network"]
    closed(network, {"loopback", "dns", "dns_error", "tcp", "tcp_error"}, "rollback network receipt")
    if network != {"loopback":"pass", "dns":"denied", "dns_error":"gaierror",
                   "tcp":"denied", "tcp_error":"PermissionError"}:
        raise ToolchainError("rollback network receipt differs")
    validate_container_envelope(payload["container"], lock)
    expected_host = {
        "humanizer": {"version":components["humanizer"]["skill"]["version"],
                      "content_sha256":components["humanizer"]["skill"]["content_sha256"],
                      "python":lock["bindings"]["python_version"]},
        "pdf_renderer": {"version":components["pdf_renderer"]["engine"]["version"],
                         "python":lock["bindings"]["python_version"]},
        "graphviz": {"package_version":components["graphviz"]["package"]["version"],
                     "backend_sha256":components["graphviz"]["graphviz"]["wasm_backend_sha256"],
                     "node":lock["bindings"]["node_version"]},
        "font": {"path":lock["bindings"]["font_path"], "postscript_name":lock["bindings"]["font_postscript_name"],
                 "sha256":lock["bindings"]["font_sha256"]},
    }
    for name, expected in expected_host.items():
        if payload[name] != expected:
            raise ToolchainError(f"rollback actual host value differs: {name}")
    mermaid = payload["mermaid"]
    closed(mermaid, {"schema_version", "lock_sha256", "bundle_tree_sha256", "node_version", "node_executable_sha256",
                     "npm_version", "mermaid_cli_version", "browser_version", "browser_executable_sha256"}, "rollback Mermaid receipt")
    if (mermaid["schema_version"] != "mermaid-version-log.v1"
            or mermaid["lock_sha256"] != lock["components"]["mermaid"]["sha256"]
            or mermaid["bundle_tree_sha256"] != lock["artifacts"]["mermaid_bundle"]["sha256"]
            or mermaid["node_version"].removeprefix("v") != lock["bindings"]["node_version"]
            or mermaid["node_executable_sha256"] != components["mermaid"]["node"]["executable_sha256"]
            or mermaid["npm_version"] != components["mermaid"]["node"]["npm_version"]
            or mermaid["mermaid_cli_version"] != components["mermaid"]["package_lock"]["root_version"]
            or mermaid["browser_version"].strip() != f"Google Chrome for Testing {components['mermaid']['browser']['version']}"
            or mermaid["browser_executable_sha256"] != lock["bindings"]["chrome_sha256"]):
        raise ToolchainError("rollback actual Mermaid value differs")
    return payload


def rollback(args: argparse.Namespace) -> None:
    lock, components = validate_lock()
    root = pathlib.Path(args.root).resolve(strict=True)
    proof = getattr(args, "verification_log", None)
    if not proof:
        raise ToolchainError("rollback requires the adjacent verified version log")
    version = validate_version_log(pathlib.Path(proof), root, lock, components)
    receipt = load_tree_receipt(root, lock, operation="version-log",
                                require_live_capability=bool(os.environ.get(SUPERVISOR_FD_ENV)))
    if version["tree_receipt_digest"] != receipt["receipt_digest"]:
        raise ToolchainError("rollback version-log/tree receipt boundary differs")
    receipt = reissue_tree_receipt(receipt, "rollback"); write_tree_receipt(root, receipt)
    quarantine = root.with_name(root.name + ".rolled-back-" + sha(root / "installation.json")[:16])
    if quarantine.exists() or quarantine.is_symlink():
        raise ToolchainError("rollback quarantine already exists")
    os.replace(root, quarantine)
    root.mkdir(mode=0o700)
    quarantine_info = quarantine.stat()
    marker = {"schema_version": "brandkit-toolchain-rollback.v2", "rolled_back_release": lock["release"]["id"],
              "quarantine": str(quarantine), "quarantine_device":quarantine_info.st_dev,
              "quarantine_inode":quarantine_info.st_ino, "lock_sha256": sha(LOCK_PATH),
              "tree_receipt_digest":receipt["receipt_digest"], "tree_merkle":receipt["tree_merkle"],
              "identity_digest":receipt["identity_digest"]}
    (root / "rollback.json").write_text(json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print(quarantine)


def restore(args: argparse.Namespace) -> None:
    lock, _ = validate_lock()
    root = pathlib.Path(args.root).resolve(strict=True)
    if root.is_symlink() or set(path.name for path in root.iterdir()) != {"rollback.json"}:
        raise ToolchainError("restore root is not a closed rollback marker")
    marker = load(root / "rollback.json")
    closed(marker, {"schema_version", "rolled_back_release", "quarantine", "quarantine_device",
                    "quarantine_inode", "lock_sha256", "tree_receipt_digest", "tree_merkle",
                    "identity_digest"}, "rollback marker")
    if marker["schema_version"] != "brandkit-toolchain-rollback.v2" or marker["rolled_back_release"] != lock["release"]["id"] or marker["lock_sha256"] != sha(LOCK_PATH):
        raise ToolchainError("rollback marker binding differs")
    quarantine = pathlib.Path(marker["quarantine"]).resolve(strict=True)
    quarantine_info = quarantine.stat()
    if (quarantine.parent != root.parent or quarantine.is_symlink() or not quarantine.is_dir()
            or marker["quarantine_device"] != quarantine_info.st_dev
            or marker["quarantine_inode"] != quarantine_info.st_ino):
        raise ToolchainError("rollback quarantine target differs")
    receipt_path = tree_receipt_path(root)
    receipt = load(receipt_path)
    validate_tree_receipt(receipt, quarantine, lock, operation="rollback", receipt_root=root,
                          require_live_capability=bool(os.environ.get(SUPERVISOR_FD_ENV)))
    if (marker["tree_receipt_digest"] != receipt["receipt_digest"]
            or marker["tree_merkle"] != receipt["tree_merkle"]
            or marker["identity_digest"] != receipt["identity_digest"]):
        raise ToolchainError("rollback marker/tree receipt differs")
    marker_shell = root.with_name(f".{root.name}.restore-marker-{uuid.uuid4().hex}")
    os.replace(root, marker_shell)
    try:
        os.replace(quarantine, root)
    except BaseException:
        os.replace(marker_shell, root)
        raise
    remove_snapshot(marker_shell)
    verify_install(root, lock, full_humanizer=False, operation="restore")
    print(root)


def restore_sealed(args: argparse.Namespace) -> None:
    """Supervisor-only restore from the pathless snapshot sealed by final scan #2."""
    lock, _ = validate_lock(); root = pathlib.Path(args.root).resolve(strict=True)
    if root.is_symlink() or set(path.name for path in root.iterdir()) != {"rollback.json"}:
        raise ToolchainError("sealed restore root is not a closed rollback marker")
    marker = load(root / "rollback.json")
    closed(marker, {"schema_version", "rolled_back_release", "quarantine", "quarantine_device",
                    "quarantine_inode", "lock_sha256", "tree_receipt_digest", "tree_merkle",
                    "identity_digest"}, "rollback marker")
    quarantine = pathlib.Path(marker["quarantine"]).resolve(strict=True); qinfo = quarantine.stat()
    if (marker["schema_version"] != "brandkit-toolchain-rollback.v2"
            or marker["rolled_back_release"] != lock["release"]["id"]
            or marker["lock_sha256"] != sha(LOCK_PATH) or quarantine.parent != root.parent
            or quarantine.is_symlink() or not quarantine.is_dir()
            or (qinfo.st_dev, qinfo.st_ino) != (marker["quarantine_device"], marker["quarantine_inode"])):
        raise ToolchainError("sealed restore rollback binding differs")
    receipt_path = tree_receipt_path(root); prior_receipt = receipt_path.read_bytes()
    receipt = load(receipt_path); snapshot = receipt.get("sealed_snapshot")
    validate_tree_receipt(receipt, quarantine, lock, operation="rollback", receipt_root=root,
                          check_live_identity=False, check_root_binding=False)
    if (marker["tree_receipt_digest"] != receipt["receipt_digest"]
            or marker["tree_merkle"] != receipt["tree_merkle"]
            or marker["identity_digest"] != receipt["identity_digest"]):
        raise ToolchainError("sealed restore marker/tree receipt differs")
    if not isinstance(snapshot, dict): raise ToolchainError("sealed restore proof is absent")
    proof, descriptor = supervisor_fd_request("get-snapshot", receive_fd=True,
                                               root=str(root), seal_digest=snapshot["seal_digest"])
    assert descriptor is not None
    try: raw = _read_archive_fd(descriptor)
    finally: os.close(descriptor)
    if proof != snapshot or len(raw) != proof["archive_size"] or hashlib.sha256(raw).hexdigest() != proof["archive_sha256"]:
        raise ToolchainError("sealed restore descriptor proof differs")
    entries = []; payloads: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for member in archive.getmembers():
                name = member.name[:-1] if member.isdir() and member.name.endswith("/") else member.name
                if member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None: raise ToolchainError("sealed restore member is unreadable")
                    data = stream.read(); payloads[name] = data
                    entries.append({"path":name,"type":"file","mode":format(member.mode,"04o"),
                                    "size":len(data),"sha256":hashlib.sha256(data).hexdigest()})
                elif member.isdir(): entries.append({"path":name,"type":"directory","mode":format(member.mode,"04o")})
                elif member.issym(): entries.append({"path":name,"type":"symlink","target":member.linkname})
                else: raise ToolchainError("sealed restore special member differs")
    except tarfile.TarError as exc: raise ToolchainError("sealed restore archive differs") from exc
    canonical, manifest = _installed_tree_ustar(entries, payloads)
    if (canonical != raw or len(manifest) != proof["member_count"]
            or sum(item["type"] == "file" for item in manifest) != proof["member_files"]
            or sum(item.get("size", 0) for item in manifest) != proof["member_bytes"]
            or hashlib.sha256(json.dumps(manifest, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest() != proof["member_manifest_digest"]):
        raise ToolchainError("sealed restore archive is not canonical")
    stage = pathlib.Path(tempfile.mkdtemp(prefix=f".{root.name}.sealed-", dir=root.parent))
    marker_shell = root.with_name(f".{root.name}.restore-marker-{uuid.uuid4().hex}")
    moved = False
    try:
        for entry in entries:
            target = stage / entry["path"]
            if entry["type"] == "directory": target.mkdir(parents=True, exist_ok=True); target.chmod(int(entry["mode"], 8))
            elif entry["type"] == "file":
                target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(payloads[entry["path"]]); target.chmod(int(entry["mode"], 8))
            else:
                target.parent.mkdir(parents=True, exist_ok=True); target.symlink_to(entry["target"])
        os.replace(root, marker_shell); os.replace(stage, root); moved = True
        verify_install(root, lock, full_humanizer=False, operation="restore", sealed_snapshot=proof)
        remove_snapshot(marker_shell)
    except BaseException:
        if moved:
            remove_snapshot(root); os.replace(marker_shell, root)
            if receipt_path.exists(): receipt_path.chmod(0o600)
            receipt_path.write_bytes(prior_receipt); receipt_path.chmod(0o400)
        else: remove_snapshot(stage)
        raise
    print(root)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify-lock")
    for name in ("install", "verify", "version-log", "rollback", "restore", "restore-sealed"):
        item = sub.add_parser(name)
        item.add_argument("--root", required=True)
        if name == "version-log":
            item.add_argument("--container-log", required=True)
        if name == "rollback":
            item.add_argument("--verification-log", required=True)
    args = parser.parse_args()
    if args.command == "verify-lock":
        validate_lock()
        print(json.dumps({"schema_version": "brandkit-toolchain-lock-verification.v1", "status": "pass", "lock_sha256": sha(LOCK_PATH)}, sort_keys=True, separators=(",", ":")))
    elif args.command == "install":
        install(args)
    elif args.command == "verify":
        lock, _ = validate_lock()
        verify_root = pathlib.Path(args.root).resolve(strict=True)
        verify_install(verify_root, lock, operation="verify",
                       reuse_receipt=bool(os.environ.get(SUPERVISOR_FD_ENV)) and tree_receipt_path(verify_root).is_file())
        print(json.dumps({"schema_version": "brandkit-toolchain-install-verification.v1", "status": "pass", "lock_sha256": sha(LOCK_PATH)}, sort_keys=True, separators=(",", ":")))
    elif args.command == "version-log":
        version_log(args)
    elif args.command == "restore":
        restore(args)
    elif args.command == "restore-sealed":
        restore_sealed(args)
    else:
        rollback(args)


if __name__ == "__main__":
    try:
        main()
    except (ToolchainError, OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"shared toolchain: {exc}")
