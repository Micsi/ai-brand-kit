#!/usr/bin/env python3
"""Run the buildless mmdc evidence suite against an image-empty, networkless daemon."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER_SPEC = importlib.util.spec_from_file_location(
    "mermaid_linux_worker", ROOT / "toolchain/mermaid-svg/linux_worker.py")
worker = importlib.util.module_from_spec(WORKER_SPEC)
assert WORKER_SPEC.loader is not None
WORKER_SPEC.loader.exec_module(worker)


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def run(command: list[str], *, timeout: float, check: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, env=env)
    if check and result.returncode:
        raise worker.WorkerFailure((result.stderr or result.stdout).strip())
    return result


def guest(arguments: list[str], *, timeout: float = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["colima", "ssh", "--", *arguments], timeout=timeout, check=check)


def private_docker(host: str, arguments: list[str], *, timeout: float = 120,
                   check: bool = True) -> subprocess.CompletedProcess[str]:
    return guest(["docker", "--host", host, *arguments], timeout=timeout, check=check)


def wait_ready(host: str) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = private_docker(host, ["info", "--format", "{{json .}}"], timeout=15, check=False)
        if result.returncode == 0:
            return
        time.sleep(1)
    raise worker.WorkerFailure("isolated Docker daemon did not become ready")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: mermaid_buildless_empty_daemon.py EVIDENCE_DIR")
    evidence = Path(sys.argv[1]).resolve()
    if evidence.exists() or evidence.is_symlink():
        raise SystemExit("evidence directory must be fresh")
    evidence.mkdir(parents=True)
    run_id = uuid.uuid4().hex[:10]
    label = f"brandkit-buildless-{run_id}"
    host = f"unix:///run/{label}/docker.sock"
    guest_run = f"/run/{label}"
    data_root = Path("/Volumes/Daten/Projekte/BrandKit/.runtime") / f"empty-daemon-{run_id}"
    data_root.mkdir()
    daemon_log = data_root / "dockerd.log"
    containerd_log = data_root / "containerd.log"
    storage_image = data_root / "private-storage.ext4"
    storage_root = f"{guest_run}/storage"
    supervisor = " ".join([
        "set -eu;",
        f"containerd --config=/etc/containerd/config.toml --root={storage_root}/containerd-root",
        f"--state={guest_run}/containerd-state",
        f"--address={guest_run}/containerd.sock",
        f"> {shlex.quote(str(containerd_log))} 2>&1 &",
        "containerd_pid=$!;",
        f"echo $containerd_pid > {guest_run}/containerd.pid;",
        "cleanup() { kill $containerd_pid 2>/dev/null || true; wait $containerd_pid 2>/dev/null || true; };",
        "trap cleanup EXIT INT TERM;",
        f"for attempt in $(seq 1 120); do test -S {guest_run}/containerd.sock && break; kill -0 $containerd_pid 2>/dev/null || exit 71; sleep 1; done;",
        f"test -S {guest_run}/containerd.sock;",
        "/usr/bin/dockerd",
        f"--host={host}",
        f"--data-root={storage_root}/docker-data",
        f"--exec-root={guest_run}/exec",
        f"--pidfile={guest_run}/dockerd.pid",
        f"--containerd={guest_run}/containerd.sock",
        "--bridge=none --iptables=false --ip-forward=false --ip-masq=false --userland-proxy=false",
    ])
    launch = " ".join([
        "set -eu;",
        f"test ! -e {shlex.quote(guest_run)};",
        f"truncate -s 8G {shlex.quote(str(storage_image))};",
        f"mkfs.ext4 -q -F {shlex.quote(str(storage_image))};",
        f"mkdir -p {shlex.quote(guest_run)} {shlex.quote(storage_root)};",
        f"mount -t ext4 -o loop,nosuid,nodev {shlex.quote(str(storage_image))} {shlex.quote(storage_root)};",
        "nohup unshare --net -- sh -c",
        shlex.quote(f"echo $$ > {guest_run}/ns.pid; {supervisor}"),
        f"> {shlex.quote(str(daemon_log))} 2>&1 &",
        f"echo $! > {guest_run}/launcher.pid",
    ])
    cleanup = {"containers": "not-checked", "daemon": "not-stopped",
               "runtime_dir": "not-removed"}
    primary_error = ""
    summary: dict[str, object] = {
        "schema_version": "mermaid-buildless-empty-daemon.v1",
        "run_id": run_id,
        "docker_host": host,
        "standalone_containerd": True,
        "external_storage_backing": str(storage_image),
        "launch_command_sha256": hashlib.sha256(launch.encode()).hexdigest(),
    }
    try:
        guest(["sudo", "sh", "-c", launch], timeout=30)
        wait_ready(host)
        info = private_docker(host, ["info"], timeout=120).stdout
        images_before = private_docker(host, ["image", "ls", "--all", "-q"], timeout=120).stdout.split()
        containers_before = private_docker(host, ["ps", "-aq"], timeout=120).stdout.split()
        pid = guest(["sudo", "cat", f"{guest_run}/ns.pid"], timeout=30).stdout.strip()
        global_ip_raw = guest(["sudo", "nsenter", "--target", pid, "--net",
                               "ip", "-j", "address", "show", "scope", "global"], timeout=30).stdout
        global_interfaces = json.loads(global_ip_raw or "[]")
        global_ip = [address for interface in global_interfaces
                     for address in interface.get("addr_info", []) if address.get("scope") == "global"]
        summary.update({"images_before": images_before, "containers_before": containers_before,
                        "global_ip_interfaces": global_interfaces, "global_ip_addresses": global_ip})
        if images_before or containers_before or global_ip:
            raise worker.WorkerFailure("isolated daemon did not start empty and networkless")
        env = {**os.environ, "BRANDKIT_DOCKER_HOST": host}
        integration_dir = evidence / "integration"
        integration = run([sys.executable, str(ROOT / "tests/mermaid_linux_worker_integration.py"),
                           str(integration_dir)], timeout=1800, check=False, env=env)
        (evidence / "integration.stdout").write_text(integration.stdout, encoding="utf-8")
        (evidence / "integration.stderr").write_text(integration.stderr, encoding="utf-8")
        if integration.returncode:
            raise worker.WorkerFailure(f"isolated integration failed: {integration.stderr.strip()}")
        images_after = private_docker(host, ["image", "ls", "--all", "--no-trunc", "--format", "{{.ID}}"], timeout=120).stdout.split()
        containers_after = private_docker(host, ["ps", "-aq"], timeout=120).stdout.split()
        summary.update({"images_after": images_after, "containers_after": containers_after})
        lock = worker.load_lock()
        allowed_images = {lock["base_image"]["digest"], lock["base_image"]["loaded_image_id"],
                          lock["node_image"]["digest"], lock["node_image"]["loaded_image_id"]}
        if containers_after or len(images_after) != 2 or not set(images_after).issubset(allowed_images):
            raise worker.WorkerFailure("isolated daemon loaded unexpected images or retained containers")
        summary.update({"engine_info_sha256": hashlib.sha256(info.encode()).hexdigest(),
                        "images_before": images_before, "containers_before": containers_before,
                        "global_ip_addresses": global_ip, "images_after": images_after,
                        "containers_after": containers_after,
                        "integration_stdout_sha256": hashlib.sha256(integration.stdout.encode()).hexdigest()})
    except BaseException as exc:
        primary_error = f"{type(exc).__name__}: {exc}"
    finally:
        listed = private_docker(host, ["ps", "-aq"], timeout=30, check=False)
        if listed.returncode == 0:
            ids = listed.stdout.split()
            if ids:
                private_docker(host, ["rm", "--force", *ids], timeout=120, check=False)
            cleanup["containers"] = "removed"
            image_ids = private_docker(host, ["image", "ls", "--all", "-q"], timeout=30, check=False).stdout.split()
            if image_ids:
                private_docker(host, ["image", "rm", "--force", *sorted(set(image_ids))], timeout=600, check=False)
        stop = f"if [ -f {guest_run}/dockerd.pid ]; then kill $(cat {guest_run}/dockerd.pid) 2>/dev/null || true; fi; " \
               f"if [ -f {guest_run}/containerd.pid ]; then kill $(cat {guest_run}/containerd.pid) 2>/dev/null || true; fi; " \
               f"if [ -f {guest_run}/launcher.pid ]; then kill $(cat {guest_run}/launcher.pid) 2>/dev/null || true; fi"
        guest(["sudo", "sh", "-c", stop], timeout=30, check=False)
        time.sleep(2)
        cleanup["daemon"] = "stopped"
        unmount_command = f"for attempt in $(seq 1 30); do " \
                          f"mountpoint -q {shlex.quote(storage_root)} || exit 0; " \
                          f"umount {shlex.quote(storage_root)} && exit 0; sleep 1; done; exit 70"
        unmount = guest(["sudo", "sh", "-c", unmount_command], timeout=600, check=False)
        if unmount.returncode == 0:
            runtime_cleanup = "import shutil; shutil.rmtree(" + repr(guest_run) + ", ignore_errors=True)"
            guest(["sudo", "python3", "-c", runtime_cleanup], timeout=600, check=False)
        else:
            cleanup["runtime_dir"] = "cleanup-failed"
        guest(["sudo", "chown", "-R", "501:1000", str(data_root)], timeout=600, check=False)
        guest(["sudo", "chmod", "-R", "u+rwX", str(data_root)], timeout=600, check=False)
        if data_root.exists():
            worker.runtime.remove_partial_tree(data_root)
        if cleanup["runtime_dir"] != "cleanup-failed":
            cleanup["runtime_dir"] = "removed" if not data_root.exists() else "cleanup-failed"
    summary["cleanup"] = cleanup
    summary["primary_error"] = primary_error or None
    (evidence / "isolated-daemon.json").write_bytes(canonical(summary))
    if primary_error or any(value == "cleanup-failed" for value in cleanup.values()):
        print(primary_error or "isolated daemon cleanup failed", file=sys.stderr)
        return 70
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
