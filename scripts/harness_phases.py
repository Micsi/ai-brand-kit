#!/usr/bin/env python3
"""Fail-closed phase DAG, event log, and immutable resume receipts."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Iterable


class PhaseError(RuntimeError):
    pass


def valid_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and not (set(value) - set("0123456789abcdef"))


def require_single_link_file(path: pathlib.Path, label: str) -> os.stat_result:
    if path.is_symlink() or not path.is_file():
        raise PhaseError(f"{label} is absent, linked, or non-regular: {path}")
    info = path.lstat()
    if info.st_nlink != 1:
        raise PhaseError(f"{label} has external hardlinks: {path}")
    return info


def canonical_digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def file_digest(path: pathlib.Path) -> str:
    require_single_link_file(path, "digested file")
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def ensure_directory(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise PhaseError(f"phase state directory is linked or invalid: {path}")


def scan_tree(root: pathlib.Path) -> tuple[str, int, int]:
    """Hash a closed regular-file tree. Symlinks and special files are forbidden."""
    if root.is_symlink() or not root.is_dir():
        raise PhaseError(f"immutable phase output is absent, linked, or not a directory: {root}")
    records: list[dict[str, object]] = []
    files = total = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if path.is_symlink():
            raise PhaseError(f"immutable phase output contains symlink: {relative}")
        if path.is_dir():
            records.append({"path": relative, "type": "directory", "mode": stat.S_IMODE(info.st_mode)})
        elif path.is_file():
            if info.st_nlink != 1:
                raise PhaseError(f"immutable phase output contains hardlink: {relative}")
            files += 1
            total += info.st_size
            records.append({"path": relative, "type": "file", "mode": stat.S_IMODE(info.st_mode),
                            "size": info.st_size, "sha256": file_digest(path)})
        else:
            raise PhaseError(f"immutable phase output contains special file: {relative}")
    return canonical_digest(records), files, total


def atomic_json(path: pathlib.Path, value: dict) -> None:
    ensure_directory(path.parent)
    if path.exists():
        require_single_link_file(path, "phase JSON target")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    replaced = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        replaced = True
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        if replaced:
            # A failed parent-directory sync is not a completed commit.  Do
            # not leave the merely visible rename available to resume logic.
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def remove_tree(root: pathlib.Path) -> None:
    if not root.exists() or root.is_symlink():
        return
    root.chmod(0o700)
    for path in sorted(root.rglob("*")):
        if not path.is_symlink():
            path.chmod(0o700 if path.is_dir() else 0o600)
    shutil.rmtree(root)


class EventLog:
    def __init__(self, path: pathlib.Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        ensure_directory(path.parent)
        if path.exists():
            require_single_link_file(path, "phase event log")
        self.descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0), 0o600)
        info = os.fstat(self.descriptor)
        actual = require_single_link_file(path, "phase event log")
        if info.st_nlink != 1 or (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino):
            os.close(self.descriptor)
            raise PhaseError("phase event log descriptor identity differs")
        self.synced_size = 0
        self.closed = False

    def _verify_descriptor(self) -> os.stat_result:
        if self.closed:
            raise PhaseError("phase event log is closed")
        descriptor_info = os.fstat(self.descriptor)
        path_info = require_single_link_file(self.path, "phase event log")
        if descriptor_info.st_nlink != 1 or (descriptor_info.st_dev, descriptor_info.st_ino) != (path_info.st_dev, path_info.st_ino):
            raise PhaseError("phase event log descriptor identity changed")
        return descriptor_info

    def append(self, value: dict) -> None:
        event = {"schema_version": "brandkit-harness-phase-event.v1", "run_id": self.run_id,
                 "timestamp_ns": time.time_ns(), **value}
        raw = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
        self._verify_descriptor()
        written = os.write(self.descriptor, raw)
        if written != len(raw):
            raise PhaseError("short atomic JSONL event write")

    def sync(self) -> int:
        """Durably commit all appended events before a phase/run commit."""
        info = self._verify_descriptor()
        sync = getattr(os, "fdatasync", os.fsync)
        sync(self.descriptor)
        committed = os.fstat(self.descriptor)
        if (committed.st_dev, committed.st_ino) != (info.st_dev, info.st_ino) or committed.st_size < self.synced_size:
            raise PhaseError("phase event log changed during sync")
        self.synced_size = committed.st_size
        return self.synced_size

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            self.closed = True


EVENT_FIELDS = {"schema_version", "run_id", "timestamp_ns", "event", "phase", "predecessors",
                "input_digest", "code_fingerprint", "environment_digest", "output_digest",
                "input_files", "input_bytes", "output_files", "output_bytes", "wall_seconds",
                "receipt_digest", "detail"}


def validate_event_log(path: pathlib.Path, *, expected_run_id: str | None = None,
                       expected_phases: set[str] | None = None) -> dict:
    info = require_single_link_file(path, "phase event log")
    raw = path.read_bytes()
    if not raw or not raw.endswith(b"\n"):
        raise PhaseError("phase event log is empty or truncated")
    try:
        lines = raw.decode("utf-8").splitlines()
        events = [json.loads(line) for line in lines]
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PhaseError("phase event log is invalid JSONL") from exc
    open_phases: dict[tuple[str, str], dict] = {}
    ended: dict[tuple[str, str], dict] = {}
    for event in events:
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            raise PhaseError("phase event fields differ")
        if (event["schema_version"] != "brandkit-harness-phase-event.v1"
                or event["event"] not in {"start", "invalidate", "end"}
                or not isinstance(event["run_id"], str) or not event["run_id"]
                or not isinstance(event["phase"], str) or not event["phase"]
                or not isinstance(event["timestamp_ns"], int) or event["timestamp_ns"] <= 0
                or not valid_digest(event["input_digest"]) or not valid_digest(event["code_fingerprint"])
                or not valid_digest(event["environment_digest"])
                or not isinstance(event["predecessors"], dict)
                or any(not isinstance(name, str) or not valid_digest(value)
                       for name, value in event["predecessors"].items())
                or not isinstance(event["input_files"], int) or event["input_files"] < 0
                or not isinstance(event["input_bytes"], int) or event["input_bytes"] < 0
                or not isinstance(event["output_files"], int) or event["output_files"] < 0
                or not isinstance(event["output_bytes"], int) or event["output_bytes"] < 0
                or not isinstance(event["wall_seconds"], (int, float)) or event["wall_seconds"] < 0):
            raise PhaseError("phase event value differs")
        key = (event["run_id"], event["phase"])
        if event["event"] == "start":
            if key in open_phases or key in ended or event["output_digest"] is not None or event["receipt_digest"] is not None:
                raise PhaseError("phase event start state differs")
            if event["output_files"] or event["output_bytes"] or event["wall_seconds"] or event["detail"] is not None:
                raise PhaseError("phase event start counters differ")
            open_phases[key] = event
            continue
        if key not in open_phases or key in ended:
            raise PhaseError("phase event has no unique start")
        start = open_phases[key]
        for field in ("predecessors", "input_digest", "code_fingerprint", "environment_digest",
                      "input_files", "input_bytes"):
            if event[field] != start[field]:
                raise PhaseError("phase event binding changed within phase")
        if event["event"] == "invalidate":
            if event["output_digest"] is not None or event["receipt_digest"] is not None or not isinstance(event["detail"], str):
                raise PhaseError("phase invalidation event differs")
            continue
        detail = event["detail"]
        if detail in {"pass", "resumed"}:
            if not valid_digest(event["output_digest"]) or not valid_digest(event["receipt_digest"]):
                raise PhaseError("successful phase end digest differs")
        elif not isinstance(detail, str) or not detail.startswith("error:"):
            raise PhaseError("phase end status differs")
        elif event["output_digest"] is not None or event["receipt_digest"] is not None:
            raise PhaseError("failed phase end claims output")
        ended[key] = event
        del open_phases[key]
    if open_phases:
        raise PhaseError("phase event log has unterminated phase")
    run_ids = {event["run_id"] for event in events}
    if len(run_ids) != 1 or (expected_run_id is not None and run_ids != {expected_run_id}):
        raise PhaseError("phase event log run id differs")
    run_id = next(iter(run_ids))
    successful = {phase:event for (candidate, phase), event in ended.items()
                  if candidate == run_id and event["detail"] in {"pass", "resumed"}}
    if expected_phases is not None and set(successful) != expected_phases:
        raise PhaseError("phase event log completed phase set differs")
    return {"run_id":run_id, "sha256":hashlib.sha256(raw).hexdigest(), "size":info.st_size,
            "events":len(events), "phase_receipts":{name:event["receipt_digest"] for name, event in successful.items()}}


@dataclass(frozen=True)
class PhaseSpec:
    name: str
    predecessors: tuple[str, ...] = ()
    resumable: bool = False


class PhaseDAG:
    """Supervise phase bindings; only sealed object outputs may be resumed."""
    RECEIPT_FIELDS = {"schema_version", "phase", "predecessors", "input_digest", "code_fingerprint",
                      "environment_digest", "output_digest", "output_files", "output_bytes", "object_path",
                      "receipt_digest"}
    CLOSURE_FIELDS = {"schema_version", "run_id", "event_log_sha256", "event_log_size", "event_count",
                      "phase_receipts", "closure_digest"}

    def __init__(self, root: pathlib.Path, specs: Iterable[PhaseSpec], *, resume: bool = False,
                 fresh_release: bool = False, environment: dict[str, str] | None = None) -> None:
        if resume and fresh_release:
            raise PhaseError("--fresh-release forbids --resume")
        if root.is_symlink():
            raise PhaseError("phase DAG root must not be a symlink")
        self.root = root.resolve()
        self.resume = resume
        self.fresh_release = fresh_release
        self.environment_digest = canonical_digest(environment or {})
        spec_list = list(specs)
        self.specs = {spec.name: spec for spec in spec_list}
        if len(self.specs) != len(spec_list):
            raise PhaseError("phase DAG contains duplicate names")
        for spec in spec_list:
            if (spec.name in spec.predecessors or len(set(spec.predecessors)) != len(spec.predecessors)
                    or any(name not in self.specs for name in spec.predecessors)):
                raise PhaseError(f"phase DAG predecessor differs: {spec.name}")
        self._validate_acyclic()
        self.run_id = uuid.uuid4().hex
        if resume:
            self._validate_history()
        self.events = EventLog(self.root / "events" / f"{self.run_id}.jsonl", self.run_id)
        self.completed: dict[str, str] = {}
        # Immutable receipts are only useful as resume authority once their
        # run has a durable, matching event-log closure.  Keep new receipts in
        # memory until finalize() establishes that durability boundary.
        self.pending_receipts: dict[str, tuple[pathlib.Path, dict]] = {}

    def _validate_history(self) -> None:
        events_dir = self.root / "events"
        closures_dir = self.root / "closures"
        if not events_dir.exists() and not closures_dir.exists():
            return
        ensure_directory(events_dir); ensure_directory(closures_dir)
        event_files = {path.stem:path for path in events_dir.iterdir()
                       if path.is_file() and not path.is_symlink() and path.suffix == ".jsonl"}
        closure_files = {path.stem:path for path in closures_dir.iterdir()
                         if path.is_file() and not path.is_symlink() and path.suffix == ".json"}
        if (len(event_files) != len(list(events_dir.iterdir()))
                or len(closure_files) != len(list(closures_dir.iterdir()))
                or set(event_files) != set(closure_files)):
            raise PhaseError("phase history is not a closed event/closure set")
        for run_id in sorted(event_files):
            closure_path = closure_files[run_id]
            require_single_link_file(closure_path, "phase run closure")
            closure = json.loads(closure_path.read_text(encoding="utf-8"))
            if set(closure) != self.CLOSURE_FIELDS or closure["schema_version"] != "brandkit-harness-run-closure.v1":
                raise PhaseError("phase run closure fields differ")
            if (not isinstance(closure["run_id"], str) or not valid_digest(closure["event_log_sha256"])
                    or not isinstance(closure["event_log_size"], int) or closure["event_log_size"] < 1
                    or not isinstance(closure["event_count"], int) or closure["event_count"] < 2
                    or not isinstance(closure["phase_receipts"], dict)
                    or any(not isinstance(name, str) or not valid_digest(value)
                           for name, value in closure["phase_receipts"].items())):
                raise PhaseError("phase run closure value differs")
            unsigned = {key:value for key, value in closure.items() if key != "closure_digest"}
            if not valid_digest(closure["closure_digest"]) or canonical_digest(unsigned) != closure["closure_digest"]:
                raise PhaseError("phase run closure digest differs")
            actual = validate_event_log(event_files[run_id], expected_run_id=run_id,
                                        expected_phases=set(closure["phase_receipts"]))
            expected = {"run_id":closure["run_id"], "sha256":closure["event_log_sha256"],
                        "size":closure["event_log_size"], "events":closure["event_count"],
                        "phase_receipts":closure["phase_receipts"]}
            if actual != expected or closure["run_id"] != run_id:
                raise PhaseError("phase run closure binding differs")

    def _validate_acyclic(self) -> None:
        visiting: set[str] = set(); visited: set[str] = set()
        def visit(name: str) -> None:
            if name in visiting:
                raise PhaseError("phase DAG contains a cycle")
            if name in visited:
                return
            visiting.add(name)
            for predecessor in self.specs[name].predecessors:
                visit(predecessor)
            visiting.remove(name); visited.add(name)
        for name in self.specs:
            visit(name)

    def finalize(self) -> dict:
        if set(self.completed) != set(self.specs):
            raise PhaseError("cannot finalize incomplete phase DAG")
        try:
            # This is the authoritative run commit boundary: events first,
            # then resumable phase receipts, and the run closure last.  A
            # crash at either earlier boundary leaves no closed history and
            # therefore fails closed on the next --resume.
            self.events.sync()
            result = validate_event_log(self.events.path, expected_run_id=self.run_id,
                                        expected_phases=set(self.specs))
            if result["phase_receipts"] != self.completed:
                raise PhaseError("event log receipts differ from live DAG")
            for name in sorted(self.pending_receipts):
                path, receipt = self.pending_receipts[name]
                atomic_json(path, receipt)
            unsigned = {"schema_version":"brandkit-harness-run-closure.v1", "run_id":self.run_id,
                        "event_log_sha256":result["sha256"], "event_log_size":result["size"],
                        "event_count":result["events"], "phase_receipts":result["phase_receipts"]}
            closure = {**unsigned, "closure_digest":canonical_digest(unsigned)}
            closure_path = self.root / "closures" / f"{self.run_id}.json"
            atomic_json(closure_path, closure)
            require_single_link_file(closure_path, "phase run closure")
        finally:
            self.events.close()
        return {**closure, "event_log_path":str(self.events.path), "closure_path":str(closure_path)}

    def _bindings(self, name: str, inputs: dict, code_paths: Iterable[pathlib.Path]) -> tuple[dict, str, str, int, int]:
        if name not in self.specs:
            raise PhaseError(f"phase is not declared in DAG: {name}")
        spec = self.specs[name]
        if any(predecessor not in self.completed for predecessor in spec.predecessors):
            raise PhaseError(f"phase predecessors are incomplete: {name}")
        code = []
        resolved_code = []
        for item in [pathlib.Path(__file__), *code_paths]:
            if item.is_symlink() or not item.is_file():
                raise PhaseError(f"phase code input differs: {item}")
            resolved_code.append(item.resolve(strict=True))
        for path in sorted(set(resolved_code), key=str):
            code.append({"path": str(path), "sha256": file_digest(path), "size": path.stat().st_size})
        predecessor_receipts = {key: self.completed[key] for key in spec.predecessors}
        bound = {"inputs": inputs, "predecessor_receipts": predecessor_receipts}
        declared_files = inputs.get("input_file_count", 0)
        declared_bytes = inputs.get("input_file_bytes", 0)
        if not isinstance(declared_files, int) or declared_files < 0 or not isinstance(declared_bytes, int) or declared_bytes < 0:
            raise PhaseError(f"phase input counters differ: {name}")
        return (predecessor_receipts, canonical_digest(bound), canonical_digest(code),
                len(code) + declared_files, sum(row["size"] for row in code) + declared_bytes)

    def _event(self, event: str, name: str, predecessors: dict, input_digest: str, code_fingerprint: str,
               input_files: int, input_bytes: int, *, output_digest: str | None = None,
               output_files: int = 0, output_bytes: int = 0, wall_seconds: float = 0.0,
               receipt_digest: str | None = None, detail: str | None = None) -> None:
        self.events.append({"event": event, "phase": name, "predecessors": predecessors,
                            "input_digest": input_digest, "code_fingerprint": code_fingerprint,
                            "environment_digest": self.environment_digest, "output_digest": output_digest,
                            "input_files": input_files, "input_bytes": input_bytes,
                            "output_files": output_files, "output_bytes": output_bytes,
                            "wall_seconds": round(wall_seconds, 6), "receipt_digest": receipt_digest,
                            "detail": detail})

    def run(self, name: str, action: Callable[[], tuple[str, int, int]], *, inputs: dict,
            code_paths: Iterable[pathlib.Path],
            atomic_commit: Callable[[tuple[str, int, int], Callable[[tuple[str, int, int]], tuple[str, int, int]]],
                                    tuple[str, int, int]] | None = None) -> tuple[str, int, int]:
        """Run a non-resumable phase; installed-root verification uses this path."""
        if self.specs[name].resumable:
            raise PhaseError(f"resumable phase requires run_immutable: {name}")
        predecessors, input_digest, code, input_files, input_bytes = self._bindings(name, inputs, code_paths)
        self._event("start", name, predecessors, input_digest, code, input_files, input_bytes)
        started = time.monotonic()
        terminal_event_appended = False
        def publish(output: tuple[str, int, int]) -> tuple[str, int, int]:
            nonlocal terminal_event_appended
            output_digest, output_files, output_bytes = output
            if (not valid_digest(output_digest) or not isinstance(output_files, int) or output_files < 0
                    or not isinstance(output_bytes, int) or output_bytes < 0):
                raise PhaseError(f"phase output contract differs: {name}")
            receipt = canonical_digest({"phase": name, "predecessors": predecessors, "input_digest": input_digest,
                                        "code_fingerprint": code, "environment_digest": self.environment_digest,
                                        "output_digest": output_digest, "output_files": output_files,
                                        "output_bytes": output_bytes})
            self._event("end", name, predecessors, input_digest, code, input_files, input_bytes,
                        output_digest=output_digest, output_files=output_files, output_bytes=output_bytes,
                        wall_seconds=time.monotonic() - started, receipt_digest=receipt, detail="pass")
            terminal_event_appended = True
            self.completed[name] = receipt
            return output

        try:
            preliminary = action()
            return (atomic_commit(preliminary, publish) if atomic_commit is not None else publish(preliminary))
        except BaseException as exc:
            self.completed.pop(name, None)
            try:
                if not terminal_event_appended:
                    self._event("end", name, predecessors, input_digest, code, input_files, input_bytes,
                                wall_seconds=time.monotonic() - started, detail=f"error:{type(exc).__name__}")
                self.events.sync()
            except BaseException as log_exc:
                exc.add_note(f"phase error-event commit failed: {type(log_exc).__name__}: {log_exc}")
            self.events.close()
            raise

    def run_immutable(self, name: str, build: Callable[[pathlib.Path], None], *, inputs: dict,
                      code_paths: Iterable[pathlib.Path]) -> pathlib.Path:
        spec = self.specs[name]
        if not spec.resumable:
            raise PhaseError(f"non-resumable phase cannot create resume receipt: {name}")
        predecessors, input_digest, code, input_files, input_bytes = self._bindings(name, inputs, code_paths)
        self._event("start", name, predecessors, input_digest, code, input_files, input_bytes)
        started = time.monotonic()
        terminal_event_appended = False
        receipt_path = self.root / "receipts" / f"{name}.json"
        if self.resume and not self.fresh_release and receipt_path.is_file() and not receipt_path.is_symlink():
            try:
                require_single_link_file(receipt_path, "resume receipt")
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if set(receipt) != self.RECEIPT_FIELDS or receipt["schema_version"] != "brandkit-harness-phase-receipt.v1":
                    raise PhaseError("resume receipt fields differ")
                if (not valid_digest(receipt["output_digest"]) or not valid_digest(receipt["receipt_digest"])
                        or not isinstance(receipt["output_files"], int) or receipt["output_files"] < 0
                        or not isinstance(receipt["output_bytes"], int) or receipt["output_bytes"] < 0):
                    raise PhaseError("resume receipt digest or counters differ")
                expected = {"phase": name, "predecessors": predecessors, "input_digest": input_digest,
                            "code_fingerprint": code, "environment_digest": self.environment_digest}
                if any(receipt[key] != value for key, value in expected.items()):
                    raise PhaseError("resume receipt binding differs")
                object_path = pathlib.Path(receipt["object_path"])
                if object_path != self.root / "objects" / receipt["output_digest"]:
                    raise PhaseError("resume receipt object path differs")
                digest, files, total = scan_tree(object_path)
                unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
                if (digest != receipt["output_digest"] or files != receipt["output_files"]
                        or total != receipt["output_bytes"] or canonical_digest(unsigned) != receipt["receipt_digest"]):
                    raise PhaseError("resume receipt output differs")
                self._event("end", name, predecessors, input_digest, code, input_files, input_bytes,
                            output_digest=digest, output_files=files, output_bytes=total,
                            wall_seconds=time.monotonic() - started, receipt_digest=receipt["receipt_digest"],
                            detail="resumed")
                terminal_event_appended = True
                self.completed[name] = receipt["receipt_digest"]
                return object_path
            except (OSError, ValueError, KeyError, TypeError, PhaseError) as exc:
                self._event("invalidate", name, predecessors, input_digest, code, input_files, input_bytes,
                            wall_seconds=time.monotonic() - started, detail=type(exc).__name__)
        staging_parent = self.root / "staging"
        ensure_directory(staging_parent)
        staging = pathlib.Path(tempfile.mkdtemp(prefix=f"{name}-", dir=staging_parent))
        staging_siblings = set(staging_parent.iterdir())
        try:
            build(staging)
            unexpected = set(staging_parent.iterdir()) - staging_siblings
            if unexpected:
                for item in unexpected:
                    if item.is_symlink() or item.is_file():
                        item.unlink()
                    elif item.is_dir():
                        remove_tree(item)
                raise PhaseError("immutable phase builder escaped its staging directory")
            # Validate closure before chmod so a builder-controlled symlink can
            # never redirect the sealing operation outside the staging root.
            scan_tree(staging)
            for path in sorted(staging.rglob("*"), reverse=True):
                path.chmod(0o500 if path.is_dir() else 0o400)
            output_digest, output_files, output_bytes = scan_tree(staging)
            object_path = self.root / "objects" / output_digest
            ensure_directory(object_path.parent)
            if object_path.exists() or object_path.is_symlink():
                existing = scan_tree(object_path)
                if existing != (output_digest, output_files, output_bytes):
                    raise PhaseError("content-addressed object collision")
                remove_tree(staging)
            else:
                os.replace(staging, object_path)
                object_path.chmod(0o500)
            unsigned = {"schema_version": "brandkit-harness-phase-receipt.v1", "phase": name,
                        "predecessors": predecessors, "input_digest": input_digest, "code_fingerprint": code,
                        "environment_digest": self.environment_digest, "output_digest": output_digest,
                        "output_files": output_files, "output_bytes": output_bytes, "object_path": str(object_path)}
            receipt = {**unsigned, "receipt_digest": canonical_digest(unsigned)}
            self._event("end", name, predecessors, input_digest, code, input_files, input_bytes,
                        output_digest=output_digest, output_files=output_files, output_bytes=output_bytes,
                        wall_seconds=time.monotonic() - started, receipt_digest=receipt["receipt_digest"], detail="pass")
            terminal_event_appended = True
            self.pending_receipts[name] = (receipt_path, receipt)
            self.completed[name] = receipt["receipt_digest"]
            return object_path
        except BaseException as exc:
            if staging.exists():
                remove_tree(staging)
            try:
                if not terminal_event_appended:
                    self._event("end", name, predecessors, input_digest, code, input_files, input_bytes,
                                wall_seconds=time.monotonic() - started, detail=f"error:{type(exc).__name__}")
                self.events.sync()
            except BaseException as log_exc:
                exc.add_note(f"phase error-event commit failed: {type(log_exc).__name__}: {log_exc}")
            self.events.close()
            raise
