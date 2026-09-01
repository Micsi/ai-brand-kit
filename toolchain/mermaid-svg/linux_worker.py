#!/usr/bin/env python3
"""Build and run the digest-locked, networkless Linux Mermaid worker."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = ROOT / "toolchain/mermaid-linux-worker.v2.lock.json"
MAC_LOCK_PATH = ROOT / "toolchain/mermaid-svg.v1.lock.json"
TOOL_DIR = ROOT / "toolchain/mermaid-svg"
RUNTIME_SPEC = importlib.util.spec_from_file_location("mermaid_runtime", TOOL_DIR / "runtime.py")
runtime = importlib.util.module_from_spec(RUNTIME_SPEC)
assert RUNTIME_SPEC.loader is not None
RUNTIME_SPEC.loader.exec_module(runtime)


class WorkerFailure(RuntimeError):
    pass


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


RUNTIME_MOUNT_PARENT = Path("/Volumes/Daten/AgentStores/joh/runtime-store/mermaid-mmdc-mounts")


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def load_lock(lock_path: Path = LOCK_PATH, *, verify_mount: bool = True) -> dict:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("schema_version") != "mermaid-linux-worker.v2" or lock.get("platform") != "linux-arm64":
        raise WorkerFailure("Linux worker lock identity differs")
    if lock.get("execution_model") != "buildless-image-and-bind-mounts":
        raise WorkerFailure("Linux worker execution model differs")
    policy = lock.get("policy", {})
    expected = {"network": "none", "read_only_root": True, "user": "1000:1000",
                "cap_drop": ["ALL"], "no_new_privileges": True, "cpus": "1.0",
                "memory": "768m", "pids_limit": 128, "shm_size": "256m",
                "max_input_bytes": 65536}
    if any(policy.get(key) != value for key, value in expected.items()):
        raise WorkerFailure("Linux worker isolation policy differs")
    timeout = policy.get("timeout_seconds")
    if not isinstance(timeout, int) or not 1 <= timeout <= 900:
        raise WorkerFailure("Linux worker timeout is not finite")
    for key in ("base_image", "node_image"):
        verify_archive(lock.get(key, {}), key)
    for item in lock.get("source_files", []):
        path = ROOT / item["path"]
        if path.is_symlink() or not path.is_file() or digest(path) != item["sha256"]:
            raise WorkerFailure(f"Linux worker source differs: {item['path']}")
    package_lock = TOOL_DIR / "package-lock.json"
    if digest(package_lock) != lock.get("package_lock_sha256"):
        raise WorkerFailure("Linux worker package lock differs")
    mac_lock = json.loads(MAC_LOCK_PATH.read_text(encoding="utf-8"))
    if mac_lock.get("artifact_bundle", {}).get("tree_sha256") != lock.get("artifact_bundle_tree_sha256"):
        raise WorkerFailure("Linux worker artifact bundle pin differs")
    profile = json.loads((ROOT / policy["seccomp_profile"]["path"]).read_text(encoding="utf-8"))
    if profile.get("defaultAction") != "SCMP_ACT_ERRNO":
        raise WorkerFailure("Linux worker seccomp must default-deny")
    forbidden = {"clone3", "setns", "mount"}
    allowed = {name for rule in profile.get("syscalls", []) if rule.get("action") == "SCMP_ACT_ALLOW"
               for name in rule.get("names", [])}
    if forbidden & allowed:
        raise WorkerFailure("Linux worker seccomp grants a forbidden namespace syscall")
    unshare_rules = [rule for rule in profile.get("syscalls", []) if "unshare" in rule.get("names", [])]
    expected_unshare = [{"index": 0, "value": 4294967295, "valueTwo": 268435456,
                         "op": "SCMP_CMP_MASKED_EQ"}]
    if len(unshare_rules) != 1 or unshare_rules[0].get("args") != expected_unshare:
        raise WorkerFailure("Linux worker seccomp unshare grant is not CLONE_NEWUSER-only")
    clone_rules = [rule for rule in profile.get("syscalls", [])
                   if rule.get("names") == ["clone"] and rule.get("args")]
    clone_masks = {rule["args"][0].get("valueTwo") for rule in clone_rules}
    if clone_masks != {0, 268435456, 536870912, 1879048192} or any(
            rule["args"][0].get("value") != 2114060416 for rule in clone_rules):
        raise WorkerFailure("Linux worker seccomp clone namespace masks differ")
    if verify_mount:
        verify_runtime_mount(lock)
    return lock


def docker(arguments: list[str], *, timeout: float, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["colima", "ssh", "--"]
    command.append("docker")
    docker_host = os.environ.get("BRANDKIT_DOCKER_HOST")
    if docker_host:
        local_socket = (docker_host.startswith("unix:///Volumes/Daten/Projekte/BrandKit/.runtime/")
                        or re.fullmatch(r"unix:///run/brandkit-buildless-[a-z0-9-]+/docker\.sock",
                                        docker_host) is not None)
        loopback = re.fullmatch(r"tcp://127\.0\.0\.1:[2-9][0-9]{4}", docker_host) is not None
        if (not (local_socket or loopback) or any(character.isspace() for character in docker_host)):
            raise WorkerFailure("isolated Docker host must be runtime-local or loopback-only")
        command.extend(["--host", docker_host])
    command.extend(arguments)
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise WorkerFailure(f"Docker phase timed out after {timeout:g}s") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise WorkerFailure(f"Docker phase failed: {detail}")
    return result


def resolve_local_image(spec: dict, archive: Path) -> dict:
    allowed = {spec["digest"], spec["loaded_image_id"]}
    for load in (False, True):
        for reference in (spec["digest"], spec["loaded_image_id"]):
            result = docker(["image", "inspect", reference, "--format", "{{.Id}}"],
                            timeout=120, check=False)
            if result.returncode == 0:
                image_id = result.stdout.strip()
                if image_id not in allowed:
                    raise WorkerFailure(f"local image identity differs: {reference}")
                return {"reference": reference, "image_id": image_id}
        if not load:
            docker(["load", "--input", str(archive)], timeout=1800)
    raise WorkerFailure("pinned local image could not be resolved")


def verify_archive(spec: dict, label: str) -> Path:
    required = {"archive_path", "archive_sha256", "archive_size", "digest", "loaded_image_id"}
    if not required.issubset(spec):
        raise WorkerFailure(f"{label} archive pin is incomplete")
    archive = Path(spec["archive_path"])
    if (not str(archive).startswith("/Volumes/Daten/AgentStores/") or archive.is_symlink()
            or not archive.is_file() or archive.stat().st_size != spec["archive_size"]
            or digest(archive) != spec["archive_sha256"]):
        raise WorkerFailure(f"{label} archive differs from lock")
    if archive.stat().st_mode & 0o222:
        raise WorkerFailure(f"{label} archive must be read-only")
    return archive


def ensure_images(lock: dict) -> list[dict]:
    loaded = []
    for key in ("base_image", "node_image"):
        spec = lock[key]
        archive = verify_archive(spec, key)
        resolved = resolve_local_image(spec, archive)
        loaded.append({"key": key, "archive_path": str(archive),
                       "archive_sha256": spec["archive_sha256"],
                       "source_digest": spec["digest"], **resolved})
    return loaded


def _mount_spec(root: Path) -> dict:
    entries = runtime.inventory(root)
    inventory_sha256 = hashlib.sha256(canonical(entries)).hexdigest()
    return {"path": str(RUNTIME_MOUNT_PARENT / inventory_sha256),
            "inventory_sha256": inventory_sha256, "entry_count": len(entries),
            "size_bytes": sum(item.get("size", 0) for item in entries), "entries": entries}


def verify_runtime_mount(lock: dict) -> Path:
    spec = lock.get("runtime_mount", {})
    required = {"path", "inventory_sha256", "entry_count", "size_bytes"}
    if not required.issubset(spec):
        raise WorkerFailure("mmdc runtime mount pin is incomplete")
    root = Path(spec["path"])
    if (root.parent != RUNTIME_MOUNT_PARENT or root.name != spec["inventory_sha256"]
            or root.is_symlink() or not root.is_dir() or root.resolve(strict=True) != root):
        raise WorkerFailure("mmdc runtime mount path is outside the immutable CAS")
    entries = runtime.inventory(root)
    actual_hash = hashlib.sha256(canonical(entries)).hexdigest()
    if (actual_hash != spec["inventory_sha256"] or len(entries) != spec["entry_count"]
            or sum(item.get("size", 0) for item in entries) != spec["size_bytes"]):
        raise WorkerFailure("mmdc runtime mount inventory differs")
    for item in entries:
        if item["type"] == "directory" and item["mode"] != 0o555:
            raise WorkerFailure(f"writable or non-canonical runtime directory: {item['path']}")
        if item["type"] == "file" and item["mode"] not in (0o444, 0o555):
            raise WorkerFailure(f"writable or non-canonical runtime file: {item['path']}")
    required_paths = {"linux-worker-entrypoint", "package.json", "package-lock.json",
                      "MERMAID_CONFIG.json", "LINUX_PUPPETEER_CONFIG.json",
                      "node_modules/.bin/mmdc"}
    present = {item["path"] for item in entries}
    if not required_paths.issubset(present):
        raise WorkerFailure("mmdc runtime mount is incomplete")
    entrypoint = root / "linux-worker-entrypoint"
    if stat.S_IMODE(entrypoint.stat().st_mode) != 0o555:
        raise WorkerFailure("mmdc runtime entrypoint is not immutable and executable")
    return root


def prepare_runtime_mount(destination: Path, inventory_path: Path) -> dict:
    lock = load_lock(verify_mount=False)
    if destination.exists() or destination.is_symlink():
        raise WorkerFailure("mmdc runtime mount destination must be fresh")
    runtime_root = Path("/Volumes/Daten/Projekte/BrandKit/.runtime").resolve(strict=True)
    if runtime_root not in destination.parent.resolve(strict=True).parents and destination.parent.resolve(strict=True) != runtime_root:
        raise WorkerFailure("mmdc runtime mount staging must remain under project .runtime")
    destination.mkdir(parents=True)
    node_modules_stage = destination / "payload"
    node_modules_stage.mkdir()
    mac_lock = json.loads(MAC_LOCK_PATH.read_text(encoding="utf-8"))
    bundle = Path(mac_lock["artifact_bundle"]["path"])
    evidence = runtime.materialize_npm_packages(
        node_modules_stage, bundle, inventory_path, target_os="linux", target_cpu="arm64")
    if evidence.get("platform") != "linux-arm64":
        raise WorkerFailure("Linux npm materializer selected the wrong platform")
    for name in ("package.json", "package-lock.json", "MERMAID_CONFIG.json",
                 "LINUX_PUPPETEER_CONFIG.json", "linux-worker-entrypoint"):
        shutil.copy2(TOOL_DIR / name, destination / name)
    materialized_root = node_modules_stage / "node_modules"
    # APFS rejects renaming a read-only directory even when both parents are
    # writable. Open only the root for the metadata move, then reseal it.
    materialized_root.chmod(0o755)
    materialized_root.rename(destination / "node_modules")
    (destination / "node_modules").chmod(0o555)
    node_modules_stage.rmdir()
    for current, directories, files in os.walk(destination):
        for name in directories + files:
            os.utime(Path(current) / name, (0, 0), follow_symlinks=False)
    os.utime(destination, (0, 0), follow_symlinks=False)
    runtime.normalize_tree(destination)
    spec = _mount_spec(destination)
    inventory_path.write_bytes(canonical({"schema_version": "mermaid-mmdc-runtime-mount.v1",
                                          **spec, "npm_materialization": evidence}))
    return spec


def publish_runtime_mount(workspace: Path, inventory_path: Path) -> dict:
    spec = prepare_runtime_mount(workspace, inventory_path)
    lock = load_lock(verify_mount=False)
    expected = lock.get("runtime_mount", {}).get("inventory_sha256")
    if expected not in (None, "PENDING") and expected != spec["inventory_sha256"]:
        raise WorkerFailure("materialized mmdc runtime mount differs from lock")
    RUNTIME_MOUNT_PARENT.mkdir(parents=True, exist_ok=True)
    if RUNTIME_MOUNT_PARENT.is_symlink() or RUNTIME_MOUNT_PARENT.resolve(strict=True) != RUNTIME_MOUNT_PARENT:
        raise WorkerFailure("mmdc runtime CAS parent must be physical")
    destination = Path(spec["path"])
    if destination.exists():
        runtime.remove_partial_tree(workspace)
    else:
        workspace.chmod(0o755)
        workspace.rename(destination)
        destination.chmod(0o555)
    verify_runtime_mount({"runtime_mount": spec})
    return {key: value for key, value in spec.items() if key != "entries"}


def ensure_runtime(lock: dict) -> dict:
    version = docker(["version", "--format", "{{.Server.Version}}"], timeout=120).stdout.strip()
    try:
        version_tuple = tuple(int(part) for part in version.split(".")[:3])
        minimum = tuple(int(part) for part in lock["engine_requirements"]["minimum_server_version"].split("."))
    except ValueError as exc:
        raise WorkerFailure("Docker server version is not numeric") from exc
    if version_tuple < minimum or lock["engine_requirements"].get("image_mount") is not True:
        raise WorkerFailure("Docker server lacks the locked read-only image-mount contract")
    loaded = ensure_images(lock)
    mount = verify_runtime_mount(lock)
    by_key = {item["key"]: item for item in loaded}
    return {"base_image_reference": by_key["base_image"]["reference"],
            "base_image_id": by_key["base_image"]["image_id"],
            "node_image_reference": by_key["node_image"]["reference"],
            "node_image_id": by_key["node_image"]["image_id"],
            "runtime_mount": str(mount), "runtime_mount_inventory_sha256": lock["runtime_mount"]["inventory_sha256"],
            "loaded_inputs": loaded}


def container_arguments(lock: dict, state: dict, name: str, input_dir: Path, output_dir: Path,
                        output_mode: str = "svg") -> list[str]:
    policy = lock["policy"]
    seccomp = ROOT / policy["seccomp_profile"]["path"]
    if seccomp.is_symlink() or not seccomp.is_file() or digest(seccomp) != policy["seccomp_profile"]["sha256"]:
        raise WorkerFailure("Chromium user-namespace seccomp profile differs")
    return ["run", "--rm", "--name", name, "--label", "brandkit.task=linux-worker",
            "--pull=never", "--network", policy["network"], "--read-only", "--user", policy["user"],
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--security-opt", f"seccomp={seccomp}", "--cpus", policy["cpus"],
            "--memory", policy["memory"], "--memory-swap", policy["memory"],
            "--pids-limit", str(policy["pids_limit"]), "--shm-size", policy["shm_size"],
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=256m,mode=1777",
            "--env", "PATH=/node/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "--env", "HOME=/tmp/brandkit-home", "--env", "LANG=C", "--env", "LC_ALL=C",
            "--env", "TZ=UTC", "--env", "SOURCE_DATE_EPOCH=0", "--env", "PUPPETEER_SKIP_DOWNLOAD=true",
            "--env", f"BRANDKIT_OUTPUT_MODE={output_mode}",
            "--mount", f"type=image,src={state['node_image_reference']},dst=/node",
            "--mount", f"type=bind,src={state['runtime_mount']},dst=/runtime,readonly",
            "--mount", f"type=bind,src={TOOL_DIR / 'linux-worker-entrypoint'},dst=/brandkit-entrypoint,readonly",
            "--mount", f"type=bind,src={input_dir},dst=/input,readonly",
            "--mount", f"type=bind,src={output_dir},dst=/output",
            "--entrypoint", "/brandkit-entrypoint", state["base_image_reference"]]


def cleanup_container(name: str) -> None:
    inspected = docker(["inspect", name, "--format", "{{index .Config.Labels \"brandkit.task\"}}"], timeout=30, check=False)
    if inspected.returncode == 0 and inspected.stdout.strip() == "linux-worker":
        docker(["rm", "--force", name], timeout=120, check=False)


def render(source: Path, output: Path, config:Path|None=None) -> dict:
    lock = load_lock()
    state = ensure_runtime(lock)
    if source.is_symlink() or not source.is_file() or output.exists() or output.is_symlink():
        raise WorkerFailure("Linux worker input/output boundary failed")
    if source.stat().st_size > lock["policy"]["max_input_bytes"]:
        raise WorkerFailure("Linux worker input exceeds locked payload limit")
    input_dir = source.parent.resolve(strict=True)
    output_dir = output.parent.resolve(strict=True)
    if not str(input_dir).startswith("/Volumes/Daten/") or not str(output_dir).startswith("/Volumes/Daten/"):
        raise WorkerFailure("Linux worker I/O must remain under /Volumes/Daten")
    if source.name != "input.mmd" or output.name != "output.svg":
        raise WorkerFailure("Linux worker uses fixed input/output names")
    if config is not None and (config.name!="brand-config.json" or config.parent!=source.parent or config.is_symlink() or not config.is_file()):
        raise WorkerFailure("Linux worker brand config boundary failed")
    name = f"brandkit-mermaid-{uuid.uuid4().hex[:16]}"
    arguments = container_arguments(lock, state, name, input_dir, output_dir)
    started = time.monotonic()
    try:
        docker(arguments, timeout=float(lock["policy"]["timeout_seconds"]))
    except WorkerFailure:
        cleanup_container(name)
        raise
    if output.is_symlink() or not output.is_file() or not output.read_bytes().lstrip().startswith(b"<svg"):
        raise WorkerFailure("Linux worker did not produce a regular SVG")
    return {"duration_seconds": round(time.monotonic() - started, 6), "svg_sha256": digest(output),
            "config_sha256":digest(config if config is not None else TOOL_DIR/"MERMAID_CONFIG.json"),
            "base_image_id": state["base_image_id"], "node_image_id": state["node_image_id"],
            "runtime_mount_inventory_sha256": state["runtime_mount_inventory_sha256"],
            "network": "none", "sandbox_disable_flags": []}


def render_pair(source: Path, output_dir: Path, config:Path|None=None) -> dict:
    lock = load_lock()
    state = ensure_runtime(lock)
    svg = output_dir / "output.svg"; png = output_dir / "output.png"
    if (source.is_symlink() or not source.is_file() or output_dir.is_symlink()
            or not output_dir.is_dir() or svg.exists() or svg.is_symlink()
            or png.exists() or png.is_symlink()):
        raise WorkerFailure("Linux worker pair input/output boundary failed")
    if source.stat().st_size > lock["policy"]["max_input_bytes"]:
        raise WorkerFailure("Linux worker input exceeds locked payload limit")
    input_dir = source.parent.resolve(strict=True); resolved_output = output_dir.resolve(strict=True)
    if not str(input_dir).startswith("/Volumes/Daten/") or not str(resolved_output).startswith("/Volumes/Daten/"):
        raise WorkerFailure("Linux worker I/O must remain under /Volumes/Daten")
    if source.name != "input.mmd":
        raise WorkerFailure("Linux worker uses a fixed input name")
    if config is not None and (config.name!="brand-config.json" or config.parent!=source.parent or config.is_symlink() or not config.is_file()):
        raise WorkerFailure("Linux worker brand config boundary failed")
    name = f"brandkit-mermaid-{uuid.uuid4().hex[:12]}"
    svg_name = f"{name}-svg"; png_name = f"{name}-png"
    svg_arguments = container_arguments(lock, state, svg_name, input_dir, resolved_output, "svg")
    png_arguments = container_arguments(lock, state, png_name, input_dir, resolved_output, "png")
    started = time.monotonic()
    try:
        docker(svg_arguments, timeout=float(lock["policy"]["timeout_seconds"]))
        docker(png_arguments, timeout=float(lock["policy"]["timeout_seconds"]))
    except WorkerFailure:
        cleanup_container(svg_name); cleanup_container(png_name)
        raise
    if svg.is_symlink() or not svg.is_file() or not svg.read_bytes().lstrip().startswith(b"<svg"):
        raise WorkerFailure("Linux worker did not produce a regular SVG")
    if png.is_symlink() or not png.is_file() or not png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
        raise WorkerFailure("Linux worker did not produce a regular PNG")
    return {"duration_seconds": round(time.monotonic() - started, 6),
            "svg_sha256": digest(svg), "png_sha256": digest(png),
            "config_sha256":digest(config if config is not None else TOOL_DIR/"MERMAID_CONFIG.json"),
            "base_image_id": state["base_image_id"], "node_image_id": state["node_image_id"],
            "runtime_mount_inventory_sha256": state["runtime_mount_inventory_sha256"],
            "network": "none", "sandbox_disable_flags": []}


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("materialize-mount"); prepare.add_argument("workspace", type=Path); prepare.add_argument("inventory", type=Path)
    renderer = commands.add_parser("render"); renderer.add_argument("source", type=Path); renderer.add_argument("output", type=Path);renderer.add_argument("--config",type=Path)
    pair = commands.add_parser("render-pair"); pair.add_argument("source", type=Path); pair.add_argument("output_dir", type=Path);pair.add_argument("--config",type=Path)
    commands.add_parser("verify")
    args = parser.parse_args()
    try:
        if args.command == "materialize-mount": result = publish_runtime_mount(args.workspace, args.inventory)
        elif args.command == "render": result = render(args.source, args.output,args.config)
        elif args.command == "render-pair": result = render_pair(args.source,args.output_dir,args.config)
        else:
            lock = load_lock(); state = ensure_runtime(lock); result = {**state, "verified": True}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (WorkerFailure, runtime.RuntimeFailure, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"mermaid-linux-worker: {exc}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
