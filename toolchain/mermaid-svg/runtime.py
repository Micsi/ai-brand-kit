#!/opt/homebrew/bin/python3.12
"""Build, verify, and use the sealed offline Mermaid runtime image."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path, PurePosixPath

PYTHON = Path("/opt/homebrew/bin/python3.12")
ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = ROOT / "toolchain/mermaid-svg.v1.lock.json"
SCHEMA = "mermaid-runtime-image.v1"
DEAD_PROXY = "http://127.0.0.1:9"
PHASE_TIMEOUTS = {
    # The runtime store is on an externally attached APFS volume.  These are
    # finite I/O budgets, not liveness relaxations: process cleanup and error
    # preservation remain identical when a budget expires.
    "verify-bundle": 900.0,
    "extract-browser": 300.0,
    "extract-node": 900.0,
    "materialize-npm": 1800.0,
    "verify-runtime": 1800.0,
    "npm-ci": 600.0,
    "mmdc": 180.0,
    # A cold ad-hoc-signed Chrome-for-Testing framework on external APFS can
    # spend well over two minutes in Darwin code-signing/page validation before
    # Chromium creates its profile or DevTools endpoint.  Keep this bounded
    # below the real component budget without misclassifying that cold start as
    # a browser hang.
    "chrome": 300.0,
}
DIAGNOSTIC_EXCERPT_BYTES = 8192
CHROME_READY_MARKER = b"DevTools listening on ws://127.0.0.1:"
CHROME_STABILITY_SECONDS = 1.0
PROCESS_GROUP_EMPTY = "empty"
PROCESS_GROUP_EXISTS = "exists"
PROCESS_GROUP_UNKNOWN = "unknown"
PS_SNAPSHOT_TIMEOUT = 1.0


class RuntimeFailure(RuntimeError):
    pass


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class DigestReader:
    """Hash a cache object while tarfile consumes it sequentially."""

    def __init__(self, stream) -> None:
        self.stream = stream
        self.hasher = hashlib.sha512()
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.hasher.update(data)
        self.bytes_read += len(data)
        return data


def _safe_relative(parts: tuple[str, ...], label: str) -> PurePosixPath:
    if not parts or any(not part or part in {".", ".."} or "\\" in part for part in parts):
        raise RuntimeFailure(f"unsafe {label} path")
    path = PurePosixPath(*parts)
    if path.is_absolute():
        raise RuntimeFailure(f"absolute {label} path")
    return path


def _normalize_relative_link(link_path: PurePosixPath, target: str) -> PurePosixPath:
    target_path = PurePosixPath(target)
    if target_path.is_absolute() or not target or "\\" in target:
        raise RuntimeFailure(f"unsafe runtime symlink target: {link_path}")
    stack = list(link_path.parent.parts)
    for part in target_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not stack:
                raise RuntimeFailure(f"runtime symlink escapes image: {link_path}")
            stack.pop()
        else:
            stack.append(part)
    if not stack:
        raise RuntimeFailure(f"runtime symlink has empty target: {link_path}")
    return PurePosixPath(*stack)


class StreamingInventory:
    """Write a fresh tree and record final hashes without rereading payloads."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.records: dict[str, dict] = {}
        self.directories: set[str] = set()

    def ensure_directory(self, relative: PurePosixPath) -> Path:
        current = self.root
        accumulated: list[str] = []
        for part in relative.parts:
            accumulated.append(part)
            rel = "/".join(accumulated)
            current = current / part
            if current.is_symlink() or (current.exists() and not current.is_dir()):
                raise RuntimeFailure(f"materializer directory collision: {rel}")
            if not current.exists():
                current.mkdir(mode=0o755)
            self.directories.add(rel)
            self.records[rel] = {"path": rel, "type": "directory", "mode": 0o555}
        return current

    def write_file(self, relative: PurePosixPath, source, expected_size: int, executable: bool) -> bytes | None:
        destination = self.ensure_directory(relative.parent) / relative.name
        rel = relative.as_posix()
        if destination.exists() or destination.is_symlink() or rel in self.records:
            raise RuntimeFailure(f"materializer file collision: {rel}")
        hasher = hashlib.sha256()
        captured = bytearray() if relative.name == "package.json" else None
        written = 0
        with destination.open("xb") as output:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
                hasher.update(block)
                written += len(block)
                if captured is not None:
                    captured.extend(block)
            output.flush()
            os.fsync(output.fileno())
        if written != expected_size:
            raise RuntimeFailure(f"materializer size mismatch: {rel}")
        mode = 0o555 if executable else 0o444
        destination.chmod(mode)
        os.utime(destination, (0, 0), follow_symlinks=False)
        self.records[rel] = {"path": rel, "type": "file", "mode": mode,
                             "size": written, "sha256": hasher.hexdigest()}
        return None if captured is None else bytes(captured)

    def make_executable(self, relative: PurePosixPath) -> None:
        path = self.root / relative
        record = self.records.get(relative.as_posix())
        if record is None or record.get("type") != "file" or not path.is_file() or path.is_symlink():
            raise RuntimeFailure(f"package bin target is not a regular file: {relative}")
        path.chmod(0o555)
        record["mode"] = 0o555

    def verify_duplicate_file(self, relative: PurePosixPath, source, expected_size: int, executable: bool) -> None:
        record = self.records.get(relative.as_posix())
        hasher = hashlib.sha256(); size = 0
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(block); size += len(block)
        expected_mode = 0o555 if executable else 0o444
        if record is None or record.get("type") != "file" or size != expected_size or \
           record.get("size") != size or record.get("sha256") != hasher.hexdigest() or record.get("mode") != expected_mode:
            raise RuntimeFailure(f"conflicting duplicate npm archive member: {relative}")

    def symlink(self, relative: PurePosixPath, target: str) -> None:
        destination = self.ensure_directory(relative.parent) / relative.name
        rel = relative.as_posix()
        normalized = _normalize_relative_link(relative, target)
        if not normalized.parts or normalized.parts[0] != "node_modules":
            raise RuntimeFailure(f"package bin link escapes node_modules: {rel}")
        if destination.exists() or destination.is_symlink() or rel in self.records:
            raise RuntimeFailure(f"materializer symlink collision: {rel}")
        os.symlink(target, destination)
        self.records[rel] = {"path": rel, "type": "symlink",
                             "mode": stat.S_IMODE(destination.lstat().st_mode), "target": target}

    def finalize(self) -> list[dict]:
        for rel in sorted(self.directories, key=lambda value: (value.count("/"), value), reverse=True):
            path = self.root / rel
            path.chmod(0o555)
            os.utime(path, (0, 0), follow_symlinks=False)
        return [self.records[key] for key in sorted(self.records)]


def _integrity_bytes(value: object) -> bytes:
    if not isinstance(value, str) or not value.startswith("sha512-"):
        raise RuntimeFailure("npm package integrity must be sha512")
    try:
        result = base64.b64decode(value[7:], validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeFailure("npm package integrity is malformed") from exc
    if len(result) != hashlib.sha512().digest_size:
        raise RuntimeFailure("npm package integrity has the wrong length")
    return result


def _cache_object(cache_root: Path, integrity: object) -> tuple[Path, bytes]:
    expected = _integrity_bytes(integrity)
    encoded = expected.hex()
    path = cache_root / encoded[:2] / encoded[2:4] / encoded[4:]
    if path.is_symlink() or not path.is_file() or path.resolve(strict=True).parent != path.parent.resolve(strict=True):
        raise RuntimeFailure("npm cache object is absent or linked")
    return path, expected


def _platform_matches(values: object, actual: str) -> bool:
    if values is None:
        return True
    if not isinstance(values, list) or not values or any(not isinstance(value, str) for value in values):
        raise RuntimeFailure("npm platform selector is invalid")
    if f"!{actual}" in values:
        return False
    positives = [value for value in values if not value.startswith("!")]
    return not positives or actual in positives


def _package_name(package_path: str) -> str:
    parts = PurePosixPath(package_path).parts
    positions = [index for index, part in enumerate(parts) if part == "node_modules"]
    if not positions:
        raise RuntimeFailure("npm package path lacks node_modules")
    start = positions[-1] + 1
    if start >= len(parts):
        raise RuntimeFailure("npm package path is incomplete")
    if parts[start].startswith("@"):
        if start + 2 != len(parts):
            raise RuntimeFailure("scoped npm package path is invalid")
        return f"{parts[start]}/{parts[start + 1]}"
    if start + 1 != len(parts):
        raise RuntimeFailure("npm package path is invalid")
    return parts[start]


def _bin_directory(package_path: PurePosixPath) -> PurePosixPath:
    positions = [index for index, part in enumerate(package_path.parts) if part == "node_modules"]
    position = positions[-1]
    return PurePosixPath(*package_path.parts[:position + 1], ".bin")


def _metadata_inventory(root: Path) -> list[dict]:
    entries: list[dict] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        directories.sort(); files.sort()
        current_path = Path(current)
        for name in directories + files:
            path = current_path / name
            rel = path.relative_to(root).as_posix()
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                entries.append({"path": rel, "type": "symlink", "mode": mode, "target": os.readlink(path)})
                if name in directories:
                    directories.remove(name)
            elif path.is_dir():
                entries.append({"path": rel, "type": "directory", "mode": mode})
            elif path.is_file():
                entries.append({"path": rel, "type": "file", "mode": mode, "size": path.stat().st_size})
            else:
                raise RuntimeFailure(f"unsupported materialized entry: {rel}")
    return sorted(entries, key=lambda entry: entry["path"])


def _without_hashes(entries: list[dict]) -> list[dict]:
    return [{key: value for key, value in entry.items() if key != "sha256"} for entry in entries]


def materialize_npm_packages(stage: Path, bundle: Path, inventory_path: Path, *,
                             target_os: str = "darwin", target_cpu: str = "arm64") -> dict:
    if target_os not in {"darwin", "linux"} or target_cpu not in {"arm64", "x64"}:
        raise RuntimeFailure("unsupported npm materializer target")
    lock = load_lock(); ensure_python(lock)
    expected_bundle = Path(lock["artifact_bundle"]["path"])
    if bundle.is_symlink() or bundle.resolve(strict=True) != bundle or bundle != expected_bundle:
        raise RuntimeFailure("materializer bundle differs from lock")
    if stage.is_symlink() or not stage.is_dir() or stage.resolve(strict=True) != stage:
        raise RuntimeFailure("materializer stage is absent, linked, or non-physical")
    if inventory_path.exists() or inventory_path.is_symlink() or inventory_path.parent.resolve(strict=True) != inventory_path.parent:
        raise RuntimeFailure("materializer inventory destination must be fresh and physical")
    package_lock_path = ROOT / lock["package_lock"]["path"]
    if digest(package_lock_path) != lock["package_lock"]["sha256"]:
        raise RuntimeFailure("package lock hash differs")
    package_lock = json.loads(package_lock_path.read_text(encoding="utf-8"))
    packages = package_lock.get("packages")
    if not isinstance(packages, dict) or len(packages) != lock["package_lock"]["package_count"]:
        raise RuntimeFailure("package lock closure differs")
    cache_root = bundle / "npm-cache/_cacache/content-v2/sha512"
    if cache_root.is_symlink() or not cache_root.is_dir() or cache_root.resolve(strict=True) != cache_root:
        raise RuntimeFailure("npm cache root is absent or linked")
    output = StreamingInventory(stage)
    bins: list[tuple[PurePosixPath, str, PurePosixPath]] = []
    selected = 0; skipped = 0; archive_bytes = 0
    for package_path, record in sorted(packages.items()):
        if package_path == "":
            continue
        if not isinstance(record, dict) or not isinstance(record.get("version"), str):
            raise RuntimeFailure("npm package record is invalid")
        expected_name = _package_name(package_path)
        cache_path, expected_integrity = _cache_object(cache_root, record.get("integrity"))
        compatible = _platform_matches(record.get("os"), target_os) and _platform_matches(record.get("cpu"), target_cpu)
        if not compatible:
            if not record.get("optional"):
                raise RuntimeFailure(f"incompatible required npm package: {package_path}")
            hasher = hashlib.sha512()
            with cache_path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(block); archive_bytes += len(block)
            if hasher.digest() != expected_integrity:
                raise RuntimeFailure(f"npm cache integrity differs: {package_path}")
            skipped += 1
            continue
        selected += 1
        package_relative = _safe_relative(PurePosixPath(package_path).parts, "npm package")
        if (stage / package_relative).exists() or (stage / package_relative).is_symlink():
            raise RuntimeFailure(f"npm package destination is not fresh: {package_path}")
        package_json_bytes: bytes | None = None
        archive_root: str | None = None
        seen: set[str] = set()
        with cache_path.open("rb") as raw_stream:
            digest_stream = DigestReader(raw_stream)
            with tarfile.open(fileobj=digest_stream, mode="r|gz") as archive:
                for member in archive:
                    member_path = PurePosixPath(member.name)
                    if member_path.is_absolute() or not member_path.parts:
                        raise RuntimeFailure(f"unsafe npm archive member: {package_path}")
                    if archive_root is None:
                        archive_root = member_path.parts[0]
                    if member_path.parts[0] != archive_root:
                        raise RuntimeFailure(f"npm archive has multiple roots: {package_path}")
                    remainder = member_path.parts[1:]
                    if not remainder:
                        if not member.isdir():
                            raise RuntimeFailure(f"npm archive root is not a directory: {package_path}")
                        output.ensure_directory(package_relative)
                        continue
                    relative_member = _safe_relative(remainder, "npm archive member")
                    destination = package_relative / relative_member
                    key = destination.as_posix()
                    if key in seen:
                        if member.isdir():
                            output.ensure_directory(destination)
                            continue
                        if member.isfile():
                            source = archive.extractfile(member)
                            if source is None:
                                raise RuntimeFailure(f"duplicate npm archive member cannot be read: {key}")
                            output.verify_duplicate_file(destination, source, member.size, bool(member.mode & 0o111))
                            continue
                        raise RuntimeFailure(f"unsupported duplicate npm archive member: {key}")
                    seen.add(key)
                    if member.isdir():
                        output.ensure_directory(destination)
                    elif member.isfile():
                        source = archive.extractfile(member)
                        if source is None:
                            raise RuntimeFailure(f"npm archive member cannot be read: {key}")
                        captured = output.write_file(destination, source, member.size, bool(member.mode & 0o111))
                        if relative_member.as_posix() == "package.json":
                            package_json_bytes = captured
                    else:
                        raise RuntimeFailure(f"unsupported npm archive member: {key}")
            while digest_stream.read(1024 * 1024):
                pass
            archive_bytes += digest_stream.bytes_read
            if digest_stream.hasher.digest() != expected_integrity:
                raise RuntimeFailure(f"npm cache integrity differs: {package_path}")
        if package_json_bytes is None:
            raise RuntimeFailure(f"npm package identity is absent: {package_path}")
        try:
            package_json = json.loads(package_json_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeFailure(f"npm package identity is invalid: {package_path}") from exc
        if package_json.get("name") != expected_name or package_json.get("version") != record["version"]:
            raise RuntimeFailure(f"npm package identity differs: {package_path}")
        bin_value = package_json.get("bin")
        if isinstance(bin_value, str):
            bin_value = {expected_name.rsplit("/", 1)[-1]: bin_value}
        if bin_value is not None:
            if not isinstance(bin_value, dict) or any(not isinstance(name, str) or not isinstance(target, str) for name, target in bin_value.items()):
                raise RuntimeFailure(f"npm package bin map is invalid: {package_path}")
            for name, target in sorted(bin_value.items()):
                if "/" in name or "\\" in name or name in {"", ".", ".."}:
                    raise RuntimeFailure(f"npm package bin name is unsafe: {package_path}")
                target_relative = _safe_relative(PurePosixPath(target).parts, "npm bin target")
                output.make_executable(package_relative / target_relative)
                link = _bin_directory(package_relative) / name
                relative_target = os.path.relpath((stage / package_relative / target_relative), (stage / link.parent))
                bins.append((link, relative_target, package_relative / target_relative))
    for link, target, expected_target in sorted(bins):
        if _normalize_relative_link(link, target) != expected_target:
            raise RuntimeFailure(f"npm package bin link target differs: {link}")
        output.symlink(link, target)
    entries = output.finalize()
    node_modules = stage / "node_modules"
    if node_modules.is_symlink() or not node_modules.is_dir():
        raise RuntimeFailure("materialized node_modules root is absent or linked")
    actual_metadata = [{"path": "node_modules", "type": "directory",
                        "mode": stat.S_IMODE(node_modules.stat().st_mode)}]
    for entry in _metadata_inventory(node_modules):
        actual_metadata.append({**entry, "path": f"node_modules/{entry['path']}"})
    actual_metadata.sort(key=lambda entry: entry["path"])
    expected_metadata = _without_hashes(entries)
    if actual_metadata != expected_metadata:
        first_difference = next((index for index, pair in enumerate(zip(actual_metadata, expected_metadata))
                                 if pair[0] != pair[1]), min(len(actual_metadata), len(expected_metadata)))
        actual = actual_metadata[first_difference] if first_difference < len(actual_metadata) else None
        expected = expected_metadata[first_difference] if first_difference < len(expected_metadata) else None
        raise RuntimeFailure(f"materialized npm metadata closure differs at {first_difference}: actual={actual!r} expected={expected!r}")
    evidence = {"schema_version": "mermaid-npm-materialization.v1", "platform": f"{target_os}-{target_cpu}",
                "package_lock_sha256": lock["package_lock"]["sha256"], "selected_packages": selected,
                "skipped_incompatible_optional_packages": skipped, "verified_archive_bytes": archive_bytes,
                "entry_count": len(entries), "entries": entries}
    inventory_path.write_bytes(canonical(evidence))
    fsync_file(inventory_path); fsync_directory(inventory_path.parent)
    return evidence


def load_lock() -> dict:
    if LOCK_PATH.is_symlink() or not LOCK_PATH.is_file():
        raise RuntimeFailure("Mermaid lock is absent or linked")
    value = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    if value.get("schema_version") != "mermaid-svg-toolchain.v1":
        raise RuntimeFailure("Mermaid lock identity is invalid")
    return value


def ensure_python(lock: dict) -> None:
    pin = lock.get("python")
    if sys.version_info[:2] != (3, 12) or Path(sys.executable).resolve() != PYTHON.resolve():
        raise RuntimeFailure("Mermaid runtime requires the pinned /opt/homebrew/bin/python3.12")
    if not isinstance(pin, dict) or pin.get("executable") != str(PYTHON) or digest(PYTHON) != pin.get("sha256"):
        raise RuntimeFailure("pinned Python 3.12 identity differs")


def log_event(log_path: Path, event: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as stream:
        stream.write(canonical(event) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def diagnostic_excerpt(path: Path, limit: int = DIAGNOSTIC_EXCERPT_BYTES) -> str:
    """Return a bounded, single-line-safe tail without hiding Chrome's first cause."""
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - limit))
            payload = stream.read(limit)
    except OSError as exc:
        return f"<stderr unavailable: {type(exc).__name__}: {exc}>"
    text = payload.decode("utf-8", errors="replace").strip()
    if not text:
        return "<empty>"
    return "".join(character if character in "\n\r\t" or ord(character) >= 32 else "?" for character in text)


def process_group_state(pgid: int) -> str:
    """Inspect a Darwin process group without treating EPERM as absence.

    killpg(2) is the authoritative empty check (ESRCH).  A successful probe or
    EPERM proves that absence was *not* established.  The bounded membership
    snapshot may confirm a member, but a missing row remains unknown because a
    concurrent ps snapshot is not strong enough to prove group disappearance.
    """
    permission_denied = False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return PROCESS_GROUP_EMPTY
    except PermissionError:
        permission_denied = True
    try:
        snapshot = subprocess.run(
            ["/bin/ps", "-axo", "pid=,pgid="],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=PS_SNAPSHOT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return PROCESS_GROUP_UNKNOWN
    if snapshot.returncode != 0:
        return PROCESS_GROUP_UNKNOWN
    for line in snapshot.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit() and int(fields[1]) == pgid:
            return PROCESS_GROUP_EXISTS
    return PROCESS_GROUP_UNKNOWN


def terminate_group(
    pgid: int,
    proc: subprocess.Popen[bytes],
    grace: float = 2.0,
    kill_grace: float = 10.0,
) -> tuple[bool, bool]:
    term_sent = False
    kill_sent = False
    if process_group_state(pgid) != PROCESS_GROUP_EMPTY:
        try:
            os.killpg(pgid, signal.SIGTERM); term_sent = True
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise RuntimeFailure(f"permission denied sending SIGTERM to process group {pgid}") from exc
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        proc.poll()
        if process_group_state(pgid) == PROCESS_GROUP_EMPTY:
            break
        time.sleep(0.02)
    proc.poll()
    if process_group_state(pgid) != PROCESS_GROUP_EMPTY:
        try:
            os.killpg(pgid, signal.SIGKILL); kill_sent = True
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise RuntimeFailure(f"permission denied sending SIGKILL to process group {pgid}") from exc
    reap_grace = kill_grace if kill_sent else grace
    try:
        proc.wait(timeout=reap_grace)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeFailure("process-group leader survived SIGKILL") from exc
    # Chrome's process group can contain grandchildren whose exit is reaped by
    # launchd rather than by this supervisor.  On Darwin that reaping can lag
    # the leader after SIGKILL, especially when the runtime lives on external
    # storage.  Keep the TERM budget short, but give the already-killed group a
    # separate finite disappearance budget before declaring cleanup unproven.
    deadline = time.monotonic() + reap_grace
    while process_group_state(pgid) != PROCESS_GROUP_EMPTY and time.monotonic() < deadline:
        time.sleep(0.02)
    final_state = process_group_state(pgid)
    if final_state != PROCESS_GROUP_EMPTY:
        raise RuntimeFailure(f"process group cleanup not proven ({final_state}): {pgid}")
    return term_sent, kill_sent


def process_phase(run_id: str, phase: str, command: list[str], log_dir: Path, *, cwd: Path | None = None,
                  env: dict[str, str] | None = None, timeout: float | None = None) -> None:
    timeout = PHASE_TIMEOUTS[phase] if timeout is None else timeout
    stdout_path = log_dir / f"{run_id}-{phase}.stdout"
    stderr_path = log_dir / f"{run_id}-{phase}.stderr"
    events = log_dir / f"{run_id}.jsonl"
    started = time.monotonic()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
        pgid = os.getpgid(proc.pid)
        base = {"schema_version": "mermaid-phase-log.v1", "run_id": run_id, "phase": phase,
                "command_id": hashlib.sha256(canonical(command)).hexdigest(), "pid": proc.pid, "pgid": pgid,
                "timeout_seconds": timeout, "stdout": str(stdout_path), "stderr": str(stderr_path)}
        log_event(events, {**base, "event": "start", "monotonic_seconds": started})
        timed_out = False
        term_sent = False
        kill_sent = False
        unexpected_children = False
        primary_error: BaseException | None = None
        cleanup_error: BaseException | None = None
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            primary_error = RuntimeFailure(f"Mermaid phase timed out: {phase}")
            primary_error.__cause__ = exc
            try:
                term_sent, kill_sent = terminate_group(pgid, proc)
            except BaseException as exc_cleanup:
                cleanup_error = exc_cleanup
        except BaseException as exc:
            primary_error = exc
            try:
                term_sent, kill_sent = terminate_group(pgid, proc)
            except BaseException as exc_cleanup:
                cleanup_error = exc_cleanup
        else:
            if process_group_state(pgid) != PROCESS_GROUP_EMPTY:
                unexpected_children = True
                try:
                    term_sent, kill_sent = terminate_group(pgid, proc)
                except BaseException as exc_cleanup:
                    cleanup_error = exc_cleanup
        ended = time.monotonic()
        end_log_error: BaseException | None = None
        try:
            log_event(events, {**base, "event": "end", "monotonic_seconds": ended,
                               "duration_seconds": round(ended - started, 6), "exit_code": proc.returncode,
                               "timeout": timed_out, "term_sent": term_sent, "kill_sent": kill_sent,
                               "unexpected_children": unexpected_children,
                               "cleanup": "process-group-reaped" if cleanup_error is None else "process-group-cleanup-failed",
                               "cleanup_error": None if cleanup_error is None else f"{type(cleanup_error).__name__}: {cleanup_error}"})
        except BaseException as exc_log:
            end_log_error = exc_log
    if primary_error is not None:
        if cleanup_error is not None:
            primary_error.add_note(f"secondary process-group cleanup error: {type(cleanup_error).__name__}: {cleanup_error}")
        if end_log_error is not None:
            primary_error.add_note(f"secondary end-event log error: {type(end_log_error).__name__}: {end_log_error}")
        raise primary_error
    if cleanup_error is not None:
        error = RuntimeFailure(f"Mermaid phase cleanup failed: {phase}")
        if end_log_error is not None:
            error.add_note(f"secondary end-event log error: {type(end_log_error).__name__}: {end_log_error}")
        raise error from cleanup_error
    if end_log_error is not None:
        raise RuntimeFailure(f"Mermaid phase end-event logging failed: {phase}") from end_log_error
    if proc.returncode:
        raise RuntimeFailure(
            f"Mermaid phase failed: {phase} (exit {proc.returncode}); "
            f"stderr_path={stderr_path}; stderr_tail={diagnostic_excerpt(stderr_path)}"
        )
    if unexpected_children:
        raise RuntimeFailure(f"Mermaid phase left unexpected children: {phase}")


def _shell_exit_code(returncode: int | None) -> int | None:
    if returncode is None:
        return None
    return 128 - returncode if returncode < 0 else returncode


def chrome_profile_probe(args: list[str]) -> dict:
    """Capture a bounded persistent readiness clue without reading profile data."""
    profiles = [Path(value.split("=", 1)[1]) for value in args if value.startswith("--user-data-dir=")]
    if len(profiles) != 1 or not profiles[0].is_absolute():
        return {"state": "invalid-profile-argument", "devtools_active_port": None, "top_level_entries": []}
    profile = profiles[0]
    try:
        entries = sorted(path.name for path in profile.iterdir())[:64] if profile.is_dir() and not profile.is_symlink() else []
        port = profile / "DevToolsActivePort"
        active = port.read_text(encoding="utf-8", errors="replace")[:4096] if port.is_file() and not port.is_symlink() else None
    except OSError as exc:
        return {"state": f"unavailable:{type(exc).__name__}", "devtools_active_port": None, "top_level_entries": []}
    return {"state": "present" if profile.is_dir() else "absent", "devtools_active_port": active,
            "top_level_entries": entries}


def browser_process_preflight(run_id: str, chrome: Path, args: list[str], log_dir: Path, *, cwd: Path,
                              env: dict[str, str], timeout: float | None = None) -> None:
    """Prove a real Chrome process reaches DevTools readiness, then reap it.

    Running Chrome directly is deliberate: if Darwin's outer execution sandbox
    rejects Crashpad's Mach bootstrap, this records Chrome's real signal/exit
    rather than reducing it to Puppeteer's later TargetCloseError.
    """
    timeout = PHASE_TIMEOUTS["chrome"] if timeout is None else timeout
    stdout_path = log_dir / f"{run_id}-chrome.stdout"
    stderr_path = log_dir / f"{run_id}-chrome.stderr"
    events = log_dir / f"{run_id}.jsonl"
    command = [str(chrome), "--headless", "--remote-debugging-port=0", *args, "about:blank"]
    started = time.monotonic()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
        pgid = os.getpgid(proc.pid)
        base = {"schema_version": "mermaid-phase-log.v1", "run_id": run_id, "phase": "chrome",
                "command_id": hashlib.sha256(canonical(command)).hexdigest(), "pid": proc.pid, "pgid": pgid,
                "timeout_seconds": timeout, "stdout": str(stdout_path), "stderr": str(stderr_path)}
        log_event(events, {**base, "event": "start", "monotonic_seconds": started})
        ready_at: float | None = None
        primary_error: BaseException | None = None
        try:
            while time.monotonic() - started < timeout:
                returncode = proc.poll()
                if returncode is not None:
                    break
                try:
                    ready = CHROME_READY_MARKER in stderr_path.read_bytes()
                except OSError:
                    ready = False
                if ready:
                    if ready_at is None:
                        ready_at = time.monotonic()
                        log_event(events, {**base, "event": "ready", "monotonic_seconds": ready_at,
                                  "stability_seconds": CHROME_STABILITY_SECONDS})
                    if time.monotonic() - ready_at >= CHROME_STABILITY_SECONDS:
                        break
                time.sleep(0.05)
        except BaseException as exc:
            primary_error = exc
        stable = ready_at is not None and proc.poll() is None and time.monotonic() - ready_at >= CHROME_STABILITY_SECONDS
        timed_out = not stable and proc.poll() is None
        cleanup_error: BaseException | None = None
        term_sent = False
        kill_sent = False
        try:
            if process_group_state(pgid) != PROCESS_GROUP_EMPTY:
                term_sent, kill_sent = terminate_group(pgid, proc)
            else:
                proc.wait(timeout=0)
        except BaseException as exc:
            cleanup_error = exc
        ended = time.monotonic()
        profile_probe = chrome_profile_probe(args)
        log_event(events, {**base, "event": "end", "monotonic_seconds": ended,
                  "duration_seconds": round(ended - started, 6), "exit_code": proc.returncode,
                  "shell_exit_code": _shell_exit_code(proc.returncode), "timeout": timed_out,
                  "devtools_ready": ready_at is not None, "stable": stable,
                  "interrupted": primary_error is not None,
                  "controlled_termination": stable, "term_sent": term_sent, "kill_sent": kill_sent,
                  "profile_probe": profile_probe,
                  "cleanup": "process-group-reaped" if cleanup_error is None else "process-group-cleanup-failed",
                  "cleanup_error": None if cleanup_error is None else f"{type(cleanup_error).__name__}: {cleanup_error}"})
    if primary_error is not None:
        if cleanup_error is not None:
            primary_error.add_note(f"secondary Chrome preflight cleanup error: {type(cleanup_error).__name__}: {cleanup_error}")
        raise primary_error
    if cleanup_error is not None:
        raise RuntimeFailure("Chrome preflight process-group cleanup failed") from cleanup_error
    if not stable:
        reason = "timed out before DevTools readiness" if timed_out else "exited before DevTools readiness"
        raise RuntimeFailure(
            f"Chrome preflight {reason} (exit {proc.returncode}, shell exit {_shell_exit_code(proc.returncode)}); "
            f"stderr_path={stderr_path}; stderr_tail={diagnostic_excerpt(stderr_path)}"
        )


def confined(command: list[str]) -> list[str]:
    if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise RuntimeFailure("network-denied sandbox-exec is required")
    return ["/usr/bin/sandbox-exec", "-p", "(version 1) (allow default) (deny network*)", *command]


def safe_destination(path: Path, parent: Path) -> None:
    if not path.is_absolute() or path.parent != parent or path.exists() or path.is_symlink():
        raise RuntimeFailure("runtime destination must be a fresh direct child of its declared parent")
    resolved_parent = parent.resolve(strict=True)
    if resolved_parent != parent or parent.is_symlink():
        raise RuntimeFailure("runtime parent must be an absolute, physical directory")


def normalize_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            continue
        if stat.S_ISDIR(mode):
            path.chmod(0o555)
        elif stat.S_ISREG(mode):
            path.chmod(0o555 if mode & 0o111 else 0o444)
        else:
            raise RuntimeFailure(f"unsupported runtime entry: {path}")
    root.chmod(0o555)


def fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remove_partial_tree(root: Path) -> str:
    """Remove a run-owned partial tree even after it was made read-only."""
    if not root.exists():
        return "partial-stage-absent"
    for current, directories, files in os.walk(root, topdown=False, followlinks=False):
        current_path = Path(current)
        for name in files:
            path = current_path / name
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o600)
        for name in directories:
            path = current_path / name
            if not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o700)
        current_path.chmod(stat.S_IMODE(current_path.stat().st_mode) | 0o700)
    shutil.rmtree(root)
    return "partial-stage-removed"


def inventory(root: Path) -> list[dict]:
    entries = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            target = os.readlink(path)
            _normalize_relative_link(PurePosixPath(rel), target)
            entries.append({"path": rel, "type": "symlink", "mode": mode, "target": target})
        elif path.is_dir():
            entries.append({"path": rel, "type": "directory", "mode": mode})
        elif path.is_file():
            entries.append({"path": rel, "type": "file", "mode": mode, "size": path.stat().st_size, "sha256": digest(path)})
        else:
            raise RuntimeFailure(f"unsupported runtime entry: {rel}")
    return sorted(entries, key=lambda entry: entry["path"])


def payload_inventory(root: Path) -> list[dict]:
    excluded = {"runtime-manifest.json", "runtime-manifest.sha256"}
    return [entry for entry in inventory(root) if entry["path"] not in excluded]


def verify_image_contents(runtime: Path, manifest_bytes: bytes, expected_hash: str, *, verify_payload: bool = True) -> dict:
    """Verify every payload entry plus the two non-self-referential seal files."""
    manifest_path = runtime / "runtime-manifest.json"
    hash_path = runtime / "runtime-manifest.sha256"
    if runtime.is_symlink() or not runtime.is_dir() or runtime.resolve(strict=True) != runtime:
        raise RuntimeFailure("runtime image is absent, linked, or non-physical")
    if stat.S_IMODE(runtime.stat().st_mode) & 0o222:
        raise RuntimeFailure("runtime image is writable")
    if manifest_path.is_symlink() or hash_path.is_symlink() or not manifest_path.is_file() or not hash_path.is_file():
        raise RuntimeFailure("runtime manifest is missing or linked")
    actual_bytes = manifest_path.read_bytes()
    actual_hash = hashlib.sha256(actual_bytes).hexdigest()
    if actual_bytes != manifest_bytes or actual_hash != expected_hash:
        raise RuntimeFailure("runtime manifest bytes differ")
    if hash_path.read_bytes() != (expected_hash + "\n").encode("ascii"):
        raise RuntimeFailure("runtime manifest hash file differs")
    if stat.S_IMODE(manifest_path.stat().st_mode) != 0o444 or stat.S_IMODE(hash_path.stat().st_mode) != 0o444:
        raise RuntimeFailure("runtime seal files are not read-only")
    manifest = json.loads(actual_bytes)
    if verify_payload and payload_inventory(runtime) != manifest["entries"]:
        raise RuntimeFailure("runtime payload inventory differs")
    return manifest


def build_runtime(parent: Path, bundle: Path, log_dir: Path) -> Path:
    lock = load_lock(); ensure_python(lock)
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink() or parent.resolve() != parent:
        raise RuntimeFailure("runtime parent must not be a symlink")
    run_id = f"build-{uuid.uuid4().hex}"
    stage = parent / f".stage-{run_id}"
    safe_destination(stage, parent)
    stage.mkdir(mode=0o700)
    try:
        env = {**os.environ, "PYTHON_BIN": str(PYTHON), "HTTP_PROXY": DEAD_PROXY, "HTTPS_PROXY": DEAD_PROXY,
               "ALL_PROXY": DEAD_PROXY, "NO_PROXY": "localhost,127.0.0.1", "PUPPETEER_SKIP_DOWNLOAD": "true"}
        process_phase(run_id, "verify-bundle", [str(PYTHON), str(ROOT / "toolchain/mermaid-svg/verify-bundle"), str(bundle)], log_dir, env=env)
        shutil.copy2(ROOT / "toolchain/mermaid-svg/package.json", stage / "package.json")
        shutil.copy2(ROOT / "toolchain/mermaid-svg/package-lock.json", stage / "package-lock.json")
        shutil.copy2(ROOT / "toolchain/mermaid-svg/MERMAID_CONFIG.json", stage / "MERMAID_CONFIG.json")
        shutil.copy2(ROOT / "toolchain/mermaid-svg/PUPPETEER_CONFIG.json", stage / "PUPPETEER_CONFIG.json")
        (stage / "browser").mkdir(); (stage / "node").mkdir()
        process_phase(run_id, "extract-browser", confined(["/usr/bin/unzip", "-q", str(bundle / lock["browser"]["archive_path"]), "-d", str(stage / "browser")]), log_dir, env=env)
        process_phase(run_id, "extract-node", confined(["/usr/bin/tar", "-xzf", str(bundle / lock["node"]["archive_path"]), "-C", str(stage / "node")]), log_dir, env=env)
        for config_name in ("package.json", "package-lock.json", "MERMAID_CONFIG.json", "PUPPETEER_CONFIG.json"):
            (stage / config_name).chmod(0o444)
            os.utime(stage / config_name, (0, 0), follow_symlinks=False)
        normalize_tree(stage / "browser")
        normalize_tree(stage / "node")
        base_entries = payload_inventory(stage)
        npm_inventory_path = log_dir / f"{run_id}-npm-inventory.json"
        if npm_inventory_path.exists() or npm_inventory_path.is_symlink():
            raise RuntimeFailure("npm materializer inventory path is not fresh")
        process_phase(run_id, "materialize-npm",
                      confined([str(PYTHON), str(ROOT / "toolchain/mermaid-svg/runtime.py"), "materialize-npm",
                                str(stage), str(bundle), str(npm_inventory_path)]),
                      log_dir, env=env)
        npm_evidence = json.loads(npm_inventory_path.read_text(encoding="utf-8"))
        if npm_evidence.get("schema_version") != "mermaid-npm-materialization.v1" or \
           npm_evidence.get("package_lock_sha256") != lock["package_lock"]["sha256"] or \
           npm_evidence.get("entry_count") != len(npm_evidence.get("entries", [])):
            raise RuntimeFailure("npm materializer evidence is invalid")
        entries = sorted([*base_entries, *npm_evidence["entries"]], key=lambda entry: entry["path"])
        if len({entry["path"] for entry in entries}) != len(entries):
            raise RuntimeFailure("runtime materializer inventories overlap")
        manifest = {"schema_version": SCHEMA, "platform": lock["platform"], "source_bundle": lock["artifact_bundle"],
                    "python": lock["python"], "node": lock["node"], "browser": lock["browser"],
                    "package_json_sha256": digest(stage / "package.json"), "package_lock_sha256": digest(stage / "package-lock.json"),
                    "mermaid_config_sha256": digest(stage / "MERMAID_CONFIG.json"), "puppeteer_config_sha256": digest(stage / "PUPPETEER_CONFIG.json"),
                    "entries": entries}
        manifest_bytes = canonical(manifest)
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        manifest_path = stage / "runtime-manifest.json"
        hash_path = stage / "runtime-manifest.sha256"
        manifest_path.write_bytes(manifest_bytes)
        hash_path.write_bytes((manifest_hash + "\n").encode("ascii"))
        manifest_path.chmod(0o444); hash_path.chmod(0o444)
        fsync_file(manifest_path); fsync_file(hash_path); fsync_directory(stage)
        stage.chmod(0o555)
        verify_image_contents(stage, manifest_bytes, manifest_hash, verify_payload=False)
        destination = parent / manifest_hash
        reservation = parent / f".publish-{manifest_hash}.lock"
        try:
            reservation.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise RuntimeFailure("runtime publication is already in progress") from exc
        publish_mode = "stage-atomically-renamed"
        try:
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or destination.name != manifest_hash:
                    raise RuntimeFailure("existing runtime destination is invalid")
                verify_image_contents(destination, manifest_bytes, manifest_hash)
                remove_partial_tree(stage)
                publish_mode = "existing-runtime-fully-verified"
            else:
                stage.rename(destination)
                fsync_directory(parent)
                verify_image_contents(destination, manifest_bytes, manifest_hash, verify_payload=False)
        finally:
            reservation.rmdir()
            fsync_directory(parent)
        log_event(log_dir / f"{run_id}.jsonl", {"schema_version": "mermaid-phase-log.v1", "run_id": run_id,
                  "event": "publish", "phase": "publish-runtime", "runtime": str(destination), "manifest_sha256": manifest_hash,
                  "cleanup": publish_mode})
        return destination
    except BaseException as exc:
        cleanup = "partial-stage-cleanup-failed"
        cleanup_error = None
        try:
            cleanup = remove_partial_tree(stage)
        except BaseException as cleanup_exc:
            cleanup_error = f"{type(cleanup_exc).__name__}: {cleanup_exc}"
        try:
            log_event(log_dir / f"{run_id}.jsonl", {"schema_version": "mermaid-phase-log.v1", "run_id": run_id,
                      "event": "failure", "phase": "build-runtime", "error_type": type(exc).__name__, "error": str(exc),
                      "cleanup": cleanup, "cleanup_error": cleanup_error})
        except BaseException as log_exc:
            exc.add_note(f"failure log error: {type(log_exc).__name__}: {log_exc}")
        raise


def _critical_actual(root: Path, record: dict) -> dict:
    path = root / record["path"]
    if path.is_symlink():
        return {"path": record["path"], "type": "symlink", "mode": stat.S_IMODE(path.lstat().st_mode), "target": os.readlink(path)}
    if not path.is_file():
        raise RuntimeFailure(f"critical runtime file missing: {record['path']}")
    return {"path": record["path"], "type": "file", "mode": stat.S_IMODE(path.stat().st_mode),
            "size": path.stat().st_size, "sha256": digest(path)}


def verify_runtime(runtime: Path, lock: dict | None = None) -> dict:
    lock = load_lock() if lock is None else lock; ensure_python(lock)
    pin = lock.get("runtime_image")
    if not isinstance(pin, dict) or str(runtime) != pin.get("path") or not runtime.is_absolute() or runtime.is_symlink():
        raise RuntimeFailure("runtime path differs from the pinned image")
    resolved = runtime.resolve(strict=True)
    if resolved != runtime or stat.S_IMODE(runtime.stat().st_mode) & 0o222:
        raise RuntimeFailure("runtime image is linked or writable")
    manifest_path = runtime / "runtime-manifest.json"
    hash_path = runtime / "runtime-manifest.sha256"
    if manifest_path.is_symlink() or hash_path.is_symlink() or not manifest_path.is_file() or not hash_path.is_file():
        raise RuntimeFailure("runtime manifest is missing or linked")
    manifest_bytes = manifest_path.read_bytes(); actual_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_hash != pin.get("manifest_sha256") or hash_path.read_text(encoding="ascii") != actual_hash + "\n" or runtime.name != actual_hash:
        raise RuntimeFailure("runtime manifest hash/pin/path differs")
    manifest = json.loads(manifest_bytes)
    if set(manifest) != {"schema_version", "platform", "source_bundle", "python", "node", "browser", "package_json_sha256", "package_lock_sha256", "mermaid_config_sha256", "puppeteer_config_sha256", "entries"} or manifest["schema_version"] != SCHEMA:
        raise RuntimeFailure("runtime manifest is not closed")
    if manifest["source_bundle"] != lock["artifact_bundle"] or manifest["python"] != lock["python"] or manifest["node"] != lock["node"] or manifest["browser"] != lock["browser"]:
        raise RuntimeFailure("runtime manifest identity differs from lock")
    verify_image_contents(runtime, manifest_bytes, actual_hash)
    if pin.get("entry_count") != len(manifest["entries"]):
        raise RuntimeFailure("runtime entry count differs from pin")
    return manifest


def render_svg(runtime: Path, source: Path, output: Path, log_dir: Path, timeout: float | None = None) -> str:
    lock = load_lock(); verify_runtime(runtime, lock)
    if source.is_symlink() or not source.is_file() or output.exists() or output.is_symlink():
        raise RuntimeFailure("Mermaid input/output boundary failed")
    run_id = f"render-{uuid.uuid4().hex}"
    config = json.loads((runtime / "PUPPETEER_CONFIG.json").read_text(encoding="utf-8"))
    config["executablePath"] = str(runtime / lock["browser"]["executable_relative_path"])
    runtime_config = log_dir / f"{run_id}-puppeteer.json"
    log_dir.mkdir(parents=True, exist_ok=True)
    runtime_config.write_bytes(canonical(config)); fsync_file(runtime_config)
    env = {**os.environ, "PATH": f"{runtime / 'node/node-v22.18.0-darwin-arm64/bin'}:/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "TZ": "UTC",
           "SOURCE_DATE_EPOCH": "0", "HTTP_PROXY": DEAD_PROXY, "HTTPS_PROXY": DEAD_PROXY, "ALL_PROXY": DEAD_PROXY, "NO_PROXY": "localhost,127.0.0.1"}
    command = confined([str(runtime / "node_modules/.bin/mmdc"), "-i", str(source.resolve()), "-o", str(output.resolve()), "-b", "transparent", "-t", "neutral",
                        "-c", str(runtime / "MERMAID_CONFIG.json"), "-p", str(runtime_config), "--quiet"])
    try:
        process_phase(run_id, "mmdc", command, log_dir, cwd=runtime, env=env, timeout=timeout)
    finally:
        runtime_config.unlink(missing_ok=True)
    if output.is_symlink() or not output.is_file() or not output.read_bytes().lstrip().startswith(b"<svg"):
        raise RuntimeFailure("mmdc did not produce a regular SVG")
    return run_id


def _legacy_config(base: dict, chrome: Path, profile: Path) -> dict:
    if base.get("headless") is not True or base.get("pipe") is not True or not isinstance(base.get("args"), list):
        raise RuntimeFailure("legacy Puppeteer launch policy is incomplete")
    args = list(base["args"])
    if any(not isinstance(value, str) for value in args):
        raise RuntimeFailure("legacy Puppeteer arguments are invalid")
    if any(value in {"--no-sandbox", "--disable-setuid-sandbox"} for value in args):
        raise RuntimeFailure("legacy Puppeteer sandbox-disable flag is forbidden")
    if any(value.startswith("--user-data-dir") for value in args):
        raise RuntimeFailure("legacy Puppeteer base config must not share a browser profile")
    return {**base, "executablePath": str(chrome), "args": [*args, f"--user-data-dir={profile}"]}


def legacy_render(install: Path, source: Path, output: Path, log_dir: Path, timeout: float | None = None) -> str:
    """Render with a real, isolated Chrome preflight and persistent launch evidence.

    The host must permit Chrome's Darwin Mach bootstrap.  Network remains denied
    by the locked dead-proxy, resolver-map, background-networking, and source
    subset controls.  There is intentionally no retry and no sandbox-disable
    flag: a failed preflight is the primary, preserved error.
    """
    lock = load_lock(); ensure_python(lock)
    install = install.resolve(strict=True)
    if install.is_symlink() or not install.is_dir():
        raise RuntimeFailure("legacy Mermaid install is absent, linked, or non-physical")
    if source.is_symlink() or not source.is_file() or output.exists() or output.is_symlink():
        raise RuntimeFailure("legacy Mermaid input/output boundary failed")
    if log_dir.exists() or log_dir.is_symlink() or not log_dir.is_absolute():
        raise RuntimeFailure("legacy Mermaid diagnostics directory must be fresh and absolute")
    log_dir.mkdir(parents=True)
    chrome = install / lock["browser"]["executable_relative_path"]
    node = install / lock["node"]["executable_relative_path"]
    npm_cli = install / lock["node"]["npm_cli_relative_path"]
    for path, expected, label in (
        (chrome, lock["browser"]["executable_sha256"], "Chrome"),
        (node, lock["node"]["executable_sha256"], "Node"),
        (npm_cli, lock["node"]["npm_cli_sha256"], "npm CLI"),
    ):
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise RuntimeFailure(f"legacy installed {label} hash differs")
    config_pins = {record["path"].rsplit("/", 1)[-1]: record["sha256"] for record in lock["configs"]}
    for name in ("MERMAID_CONFIG.json", "PUPPETEER_CONFIG.json"):
        path = install / name
        if path.is_symlink() or not path.is_file() or digest(path) != config_pins.get(name):
            raise RuntimeFailure(f"legacy installed config hash differs: {name}")
    mmdc = install / "node_modules/.bin/mmdc"
    if not mmdc.is_symlink() or not mmdc.resolve(strict=True).is_file() or install not in mmdc.resolve(strict=True).parents:
        raise RuntimeFailure("legacy installed mmdc entrypoint is absent or escapes the clean room")

    run_id = f"legacy-{uuid.uuid4().hex}"
    host_home_value = os.environ.get("HOME")
    if not host_home_value or not Path(host_home_value).is_absolute() or not Path(host_home_value).is_dir():
        raise RuntimeFailure("legacy Chrome requires an existing absolute host HOME for the macOS trust store")
    state = log_dir / "state"
    xdg = state / "xdg-cache"
    temporary = state / "tmp"
    node_cache = state / "node-compile"
    puppeteer_cache = state / "puppeteer-cache"
    preflight_profile = state / "preflight-profile"
    render_profile = state / "render-profile"
    for path in (xdg, temporary, node_cache, puppeteer_cache, preflight_profile, render_profile):
        path.mkdir(parents=True)
    base_config = json.loads((install / "PUPPETEER_CONFIG.json").read_text(encoding="utf-8"))
    expected_executable = "./" + lock["browser"]["executable_relative_path"]
    if base_config.get("executablePath") != expected_executable:
        raise RuntimeFailure("legacy Puppeteer browser binding differs from the locked relative path")
    preflight_config = log_dir / "preflight-puppeteer.json"
    render_config = log_dir / "render-puppeteer.json"
    preflight_config.write_bytes(canonical(_legacy_config(base_config, chrome, preflight_profile)))
    render_config.write_bytes(canonical(_legacy_config(base_config, chrome, render_profile)))
    fsync_file(preflight_config); fsync_file(render_config)
    metadata = {
        "schema_version": "mermaid-legacy-launch.v1",
        "run_id": run_id,
        "retry_count": 0,
        "platform": platform.platform(),
        "outer_host_requirement": "Darwin Mach bootstrap permitted",
        "darwin_native_home": {"policy": "retain host HOME for Chrome trust-store bootstrap",
                               "path": host_home_value},
        "network_controls": lock["network"]["controls"],
        "shared_memory": "/dev/shm not used on darwin-arm64",
        "chrome": {"path": str(chrome), "sha256": digest(chrome)},
        "node": {"path": str(node), "sha256": digest(node)},
        "configs": {
            "mermaid_sha256": digest(install / "MERMAID_CONFIG.json"),
            "puppeteer_base_sha256": digest(install / "PUPPETEER_CONFIG.json"),
            "puppeteer_preflight_sha256": digest(preflight_config),
            "puppeteer_render_sha256": digest(render_config),
        },
        "isolated_roots": {"xdg_cache": str(xdg), "tmp": str(temporary),
                           "node_compile_cache": str(node_cache), "puppeteer_cache": str(puppeteer_cache),
                           "preflight_profile": str(preflight_profile), "render_profile": str(render_profile)},
    }
    metadata_path = log_dir / "launch-metadata.json"
    metadata_path.write_bytes(canonical(metadata)); fsync_file(metadata_path)
    env = {
        **os.environ,
        "PATH": f"{install / 'node/node-v22.18.0-darwin-arm64/bin'}:/usr/bin:/bin",
        "XDG_CACHE_HOME": str(xdg),
        "TMPDIR": str(temporary),
        "TMP": str(temporary),
        "TEMP": str(temporary),
        "NODE_COMPILE_CACHE": str(node_cache),
        "PUPPETEER_CACHE_DIR": str(puppeteer_cache),
        "PUPPETEER_SKIP_DOWNLOAD": "true",
        "LANG": "C", "LC_ALL": "C", "TZ": "UTC", "SOURCE_DATE_EPOCH": "0",
        "HTTP_PROXY": DEAD_PROXY, "HTTPS_PROXY": DEAD_PROXY, "ALL_PROXY": DEAD_PROXY,
        "NO_PROXY": "localhost,127.0.0.1",
    }
    env.pop("CFFIXED_USER_HOME", None)
    primary_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    try:
        browser_process_preflight(run_id, chrome, _legacy_config(base_config, chrome, preflight_profile)["args"],
                                  log_dir, cwd=install, env=env, timeout=timeout)
        process_phase(
            run_id,
            "mmdc",
            [str(mmdc), "-i", str(source.resolve()), "-o", str(output.resolve()), "-b", "transparent", "-t", "neutral",
             "-c", str(install / "MERMAID_CONFIG.json"), "-p", str(render_config), "--quiet"],
            log_dir,
            cwd=install,
            env=env,
            timeout=timeout,
        )
    except BaseException as exc:
        primary_error = exc
    finally:
        try:
            cleanup = remove_partial_tree(state)
            log_event(log_dir / f"{run_id}.jsonl", {"schema_version": "mermaid-phase-log.v1", "run_id": run_id,
                      "event": "cleanup", "phase": "legacy-render", "cleanup": cleanup})
        except BaseException as exc:
            cleanup_error = exc
    if primary_error is not None:
        if output.exists() and not output.is_symlink():
            output.unlink()
        if cleanup_error is not None:
            primary_error.add_note(f"secondary legacy state cleanup error: {type(cleanup_error).__name__}: {cleanup_error}")
        raise primary_error
    if cleanup_error is not None:
        raise RuntimeFailure("legacy Mermaid state cleanup failed") from cleanup_error
    if output.is_symlink() or not output.is_file() or not output.read_bytes().lstrip().startswith(b"<svg"):
        raise RuntimeFailure("legacy mmdc did not produce a regular SVG")
    return run_id


def raster_svg(runtime: Path, source: Path, output: Path, log_dir: Path, timeout: float | None = None) -> str:
    lock = load_lock(); verify_runtime(runtime, lock)
    if source.is_symlink() or not source.is_file() or output.exists() or output.is_symlink():
        raise RuntimeFailure("SVG raster input/output boundary failed")
    if not source.read_bytes().lstrip().startswith(b"<svg"):
        raise RuntimeFailure("SVG raster input is not SVG")
    run_id = f"raster-{uuid.uuid4().hex}"
    chrome = runtime / lock["browser"]["executable_relative_path"]
    profile = log_dir / f"{run_id}-profile"
    profile.mkdir(parents=True)
    env = {**os.environ, "LANG": "C", "LC_ALL": "C", "TZ": "UTC", "SOURCE_DATE_EPOCH": "0",
           "HTTP_PROXY": DEAD_PROXY, "HTTPS_PROXY": DEAD_PROXY, "ALL_PROXY": DEAD_PROXY,
           "NO_PROXY": "localhost,127.0.0.1", "TMPDIR": str(profile)}
    command = confined([str(chrome), "--headless", "--disable-gpu", "--disable-background-networking",
                        "--host-resolver-rules=MAP * 0.0.0.0, EXCLUDE localhost", "--no-pdf-header-footer",
                        "--hide-scrollbars", "--window-size=1200,500", f"--user-data-dir={profile}",
                        f"--screenshot={output.resolve()}", source.resolve().as_uri()])
    try:
        process_phase(run_id, "chrome", command, log_dir, env=env, timeout=timeout)
    finally:
        remove_partial_tree(profile)
    if output.is_symlink() or not output.is_file() or not output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeFailure("Chrome did not produce a regular PNG")
    return run_id


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build-runtime"); build.add_argument("parent", type=Path); build.add_argument("bundle", type=Path); build.add_argument("log_dir", type=Path)
    materialize = sub.add_parser("materialize-npm"); materialize.add_argument("stage", type=Path); materialize.add_argument("bundle", type=Path); materialize.add_argument("inventory", type=Path); materialize.add_argument("--target-os", choices=("darwin", "linux"), default="darwin"); materialize.add_argument("--target-cpu", choices=("arm64", "x64"), default="arm64")
    verify = sub.add_parser("verify-runtime"); verify.add_argument("runtime", type=Path)
    render = sub.add_parser("render-svg"); render.add_argument("runtime", type=Path); render.add_argument("source", type=Path); render.add_argument("output", type=Path); render.add_argument("log_dir", type=Path)
    raster = sub.add_parser("raster-svg"); raster.add_argument("runtime", type=Path); raster.add_argument("source", type=Path); raster.add_argument("output", type=Path); raster.add_argument("log_dir", type=Path)
    legacy = sub.add_parser("legacy-render"); legacy.add_argument("install", type=Path); legacy.add_argument("source", type=Path); legacy.add_argument("output", type=Path); legacy.add_argument("log_dir", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "build-runtime": print(build_runtime(args.parent, args.bundle, args.log_dir))
        elif args.command == "materialize-npm": print(json.dumps({key: value for key, value in materialize_npm_packages(args.stage, args.bundle, args.inventory, target_os=args.target_os, target_cpu=args.target_cpu).items() if key != "entries"}, sort_keys=True))
        elif args.command == "verify-runtime": verify_runtime(args.runtime); print(load_lock()["runtime_image"]["manifest_sha256"])
        elif args.command == "render-svg": print(render_svg(args.runtime, args.source, args.output, args.log_dir))
        elif args.command == "raster-svg": print(raster_svg(args.runtime, args.source, args.output, args.log_dir))
        else: print(legacy_render(args.install, args.source, args.output, args.log_dir))
        return 0
    except (RuntimeFailure, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"mermaid-runtime: {exc}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
