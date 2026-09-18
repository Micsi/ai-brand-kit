#!/usr/bin/env python3
"""Verify the optional discovery bundle and authenticate a project handoff."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import importlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "brandkit-skill-contract.v1.json"
HANDOFF_SCHEMA_PATH = ROOT / "schemas/brandkit-discovery-handoff.v1.schema.json"
RECEIPT_SCHEMA_PATH = ROOT / "schemas/brandkit-discovery-owner-receipt.v1.schema.json"
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
# The sealed run.  The core keeps it out of its own workspace walk; this module
# did not know the name at all, so a declared CLI command - ``progress --run-id
# first-ci`` - created and wrote ``.brandkit/work/first-ci/`` while the core
# refused the very same identifier one module away.  The constant, the fold and
# the two guards below close that from this side too, with the *same* rules as
# the core rather than a second, weaker spelling of them.
PROTECTED_RUN_ID = "first-ci"
# The one artifact the core resolves candidate sources from.  It is a release
# member of *this* repository, not of the run's project, and the core pins its
# name to this literal in ``validate_source_contract``.
SOURCE_CONTRACT_NAME = "visual-decision-sources.v1.json"
BRAND_AXES = ("core-audience", "personality", "anti-character", "brand-architecture", "color", "typography", "grid-spacing", "components", "language-prohibitions", "target-formats", "image-rules")
VISUAL_DOMAINS = ("color", "typography", "code-font", "logo", "lockup", "icon")


class DiscoveryError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def load_json_bytes(raw: bytes, label: str) -> dict[str, Any]:
    def closed(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DiscoveryError(f"{label}: duplicate key {key}")
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=closed)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DiscoveryError(f"{label}: invalid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise DiscoveryError(f"{label}: expected JSON object")
    return value


def load_contract() -> dict[str, Any]:
    return load_json_bytes(CONTRACT_PATH.read_bytes(), "discovery contract")["optional_discovery"]["grill-with-docs"]


def bundle_digest(members: list[dict[str, Any]]) -> str:
    ordered = sorted(members, key=lambda item: item["path"])
    return hashlib.sha256(canonical(ordered)).hexdigest()


def _exact_root(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise DiscoveryError(f"{label} does not exist") from exc
    if absolute != resolved or not resolved.is_dir():
        raise DiscoveryError(f"{label} must be an exact real directory path")
    return resolved


def _names_protected_run(parts: tuple[str, ...]) -> bool:
    """Does any component name the sealed run, however it is spelled?

    The same NFC + full case fold the core uses, and for the same reason: the
    file systems this ships on decide that ``First-CI`` and ``first-ci`` are one
    directory, so a case-sensitive ``in`` test answers ``False`` about a name
    that reaches the sealed tree anyway.  ``str.casefold`` folds strictly more
    than any file system does, so every disagreement between the two refuses a
    name the file system might still have accepted - the fail-closed direction.
    """
    return any(unicodedata.normalize("NFC", part).casefold() == PROTECTED_RUN_ID for part in parts)


def _open_run_id(run_id: str, label: str = "run id") -> str:
    """The one gate every run identifier this module accepts passes through.

    Two rules, in this order: the identifier is a well-formed run id, and it is
    not the sealed run under any spelling.  The second rule is what was missing
    - ``progress --run-id first-ci`` is a declared CLI command, and without it
    that command created and wrote ``.brandkit/work/first-ci/`` while the core
    refused the identical identifier.  Refusing here rather than at the write
    means nothing is created before the refusal.
    """
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise DiscoveryError(f"{label} is invalid")
    if _names_protected_run((run_id,)):
        raise DiscoveryError(f"{label} is protected")
    return run_id


def _identity_contains(container: Path, candidate: Path) -> bool:
    """Is ``candidate`` the directory ``container`` itself, or below it?

    Judged by ``(st_dev, st_ino)``, never by text.  A lexical answer is wrong on
    exactly the file systems this ships on: ``/tmp/Project/.brandkit/x`` is not
    textually below ``/tmp/project`` and is the very same file.  The core made
    this a class of defect twice and settled it with identity; the adapter used
    a text comparison and therefore accepted a case-flipped path *inside* the
    project as "external", leaving the refusal to the core alone.

    ``os.path.realpath`` first, so symlinks and ``..`` resolve the way the
    kernel resolves them; then every ancestor of the result is stat-ed against
    the container's identity.  An ancestor that cannot be stat-ed is skipped
    rather than ending the walk, so an unreadable intermediate directory cannot
    hide containment.  A container that does not exist contains nothing.
    """
    try:
        marker = os.stat(container)
    except OSError:
        return False
    anchor = (marker.st_dev, marker.st_ino)
    current = Path(os.path.realpath(candidate))
    seen: set[Path] = set()
    while current not in seen:
        seen.add(current)
        try:
            info = os.stat(current)
            if (info.st_dev, info.st_ino) == anchor:
                return True
        except OSError:
            pass
        parent = current.parent
        if parent == current:
            return False
        current = parent
    return False


def _relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts) or path.as_posix() != value:
        raise DiscoveryError(f"unsafe relative path: {value}")
    return path


def _fold_path(value: str) -> str:
    """The spelling-independent name of a relative path, for set membership.

    The question every caller below asks is "is this the same *file* as that
    one", and the only complete answer is the one ``_identity_contains`` and
    ``_outside_project`` give: ``(st_dev, st_ino)``.  That answer is not
    available to a rule that has to decide before anything exists - the seam
    rule in ``prepare_visual_confirmation`` compares a path the owner bound
    against paths the operation has not written yet, so there is no inode to
    ask about.  This is the fail-closed approximation for that case, and it is
    named as an approximation rather than sold as completeness.

    The fold is the one ``_names_protected_run`` has used since r1d, per
    component and for the same reason: the file systems this ships on decide
    that ``HANDOFF.JSON`` and ``handoff.json`` are one file, so a raw string
    lookup answers ``False`` about a path the operation overwrites anyway.
    ``str.casefold`` after NFC folds strictly more than any file system does,
    so every disagreement between the two refuses a binding the file system
    might still have collided - the fail-closed direction.  It is therefore
    only ever used to *find* a collision, never to grant membership: a rule
    that must find a path present keeps comparing text, where a mis-spelling
    refuses rather than passes.
    """
    return "/".join(unicodedata.normalize("NFC", part).casefold() for part in value.split("/"))


def secure_read(root: Path, relative: str) -> tuple[bytes, os.stat_result]:
    """Read one regular file through component-relative O_NOFOLLOW descriptors."""
    parts = _relative(relative).parts
    # O_NONBLOCK for the reason the core states in its own ``secure_read``:
    # O_NOFOLLOW stops a symlink but not a FIFO, and open(O_RDONLY) on a FIFO
    # with no writer blocks for ever.  A FIFO left in the place of
    # ``sheet/manifest.json``, the visual brief or the Affinity observation
    # would hang every reader of the handoff - render-review, confirm and
    # consume - instead of failing closed.  With O_NONBLOCK the open returns at
    # once and the S_ISREG test below turns it into a refusal.
    flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags_file = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptors: list[int] = []
    try:
        current = os.open(root, flags_dir)
        descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(part, flags_dir, dir_fd=current)
            descriptors.append(current)
        fd = os.open(parts[-1], flags_file, dir_fd=current)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise DiscoveryError(f"not a regular file: {relative}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise DiscoveryError(f"file changed while reading: {relative}")
        raw = b"".join(chunks)
        if len(raw) != before.st_size:
            raise DiscoveryError(f"short read: {relative}")
        return raw, before
    except OSError as exc:
        raise DiscoveryError(f"cannot safely read {relative}: {exc.strerror}") from exc
    finally:
        for fd in reversed(descriptors):
            try:
                os.close(fd)
            except OSError:
                pass


def _tree_members(root: Path, bases: list[str]) -> list[str]:
    members: list[str] = []
    for base in bases:
        start = root / _relative(base).as_posix()
        if not start.is_dir() or start.is_symlink():
            raise DiscoveryError(f"bundle subtree is missing or symlinked: {base}")
        for current, directories, files in os.walk(start, followlinks=False):
            current_path = Path(current)
            for name in [*directories, *files]:
                candidate = current_path / name
                if candidate.is_symlink():
                    raise DiscoveryError(f"bundle contains symlink: {candidate.relative_to(root).as_posix()}")
            for name in files:
                candidate = current_path / name
                if not stat.S_ISREG(candidate.stat(follow_symlinks=False).st_mode):
                    raise DiscoveryError(f"bundle contains nonregular file: {candidate.relative_to(root).as_posix()}")
                members.append(candidate.relative_to(root).as_posix())
    return sorted(members)


def verify_bundle_checkout(bundle_root: Path, contract: dict[str, Any] | None = None) -> dict[str, Any]:
    contract = contract or load_contract()
    root = _exact_root(bundle_root, "bundle root")
    expected = contract["bundle"]
    members = expected["members"]
    if bundle_digest(members) != expected["content_sha256"]:
        raise DiscoveryError("bundle contract derivation mismatch")
    git = lambda *args: subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True)
    head = git("rev-parse", "HEAD")
    if head.returncode or head.stdout.strip() != contract["revision"]:
        raise DiscoveryError("bundle checkout revision differs from pin")
    if git("symbolic-ref", "-q", "HEAD").returncode == 0:
        raise DiscoveryError("bundle checkout must be detached")
    remote = git("remote", "get-url", "origin")
    expected_remote = contract["repository"].removeprefix("github:")
    expected_remote = "https" + f"://github.com/{expected_remote}.git"
    if remote.returncode or remote.stdout.strip() != expected_remote:
        raise DiscoveryError("bundle checkout origin differs from approved source")
    status_result = git("status", "--porcelain=v1", "--untracked-files=all")
    if status_result.returncode or status_result.stdout:
        raise DiscoveryError("bundle checkout is not clean")
    bases = sorted(contract["canonical_subtrees"].values())
    actual_paths = _tree_members(root, bases)
    expected_paths = [item["path"] for item in members]
    if actual_paths != expected_paths:
        raise DiscoveryError("bundle checkout member set differs from closed contract")
    tree = git("ls-tree", "-r", "--name-only", contract["revision"], "--", *bases)
    if tree.returncode or sorted(tree.stdout.splitlines()) != expected_paths:
        raise DiscoveryError("pinned Git tree member set differs from closed contract")
    observed: list[dict[str, Any]] = []
    opened: dict[str, bytes] = {}
    for item in members:
        raw, info = secure_read(root, item["path"])
        actual = {"path": item["path"], "size": info.st_size, "sha256": hashlib.sha256(raw).hexdigest()}
        if actual != item:
            raise DiscoveryError(f"bundle member differs from pin: {item['path']}")
        observed.append(actual)
        opened[item["path"]] = raw
    wrapper = opened["skills/engineering/grill-with-docs/SKILL.md"].decode("utf-8")
    provider = opened["skills/engineering/grill-with-docs/agents/openai.yaml"].decode("utf-8")
    if "disable-model-invocation: true" not in wrapper or '"grilling" and "domain-modeling"' not in wrapper or "allow_implicit_invocation: false" not in provider:
        raise DiscoveryError("bundle wrapper is not explicit-only or does not delegate to both dependencies")
    return {"revision": contract["revision"], "content_sha256": bundle_digest(observed), "members": observed}


def verify_project_activation(project_root: Path, bundle_root: Path) -> dict[str, Any]:
    """Verify the documented project-local Claude/Codex skill bridge."""
    project = _exact_root(project_root, "project root")
    bundle = _exact_root(bundle_root, "bundle root")
    verified = verify_bundle_checkout(bundle)
    skill_root = project / ".agents/skills"
    if skill_root.is_symlink() or not skill_root.is_dir():
        raise DiscoveryError("project .agents/skills must be a real directory")
    full_contract = load_json_bytes(CONTRACT_PATH.read_bytes(), "contract")
    brandkit_skills = {route["skill"] for route in full_contract["progressive_disclosure"]["routes"].values()}
    targets = {name: ROOT / "skills" / name for name in brandkit_skills}
    targets.update({
        "grill-with-docs": bundle / "skills/engineering/grill-with-docs",
        "grilling": bundle / "skills/productivity/grilling",
        "domain-modeling": bundle / "skills/engineering/domain-modeling",
    })
    for name, target in targets.items():
        link = skill_root / name
        if not link.is_symlink() or link.resolve(strict=True) != target.resolve(strict=True):
            raise DiscoveryError(f"project skill bridge differs from verified source: {name}")
    claude = project / ".claude/skills"
    if not claude.is_symlink() or os.readlink(claude) != "../.agents/skills" or claude.resolve(strict=True) != skill_root.resolve(strict=True):
        raise DiscoveryError("Claude project bridge must be .claude/skills -> ../.agents/skills")
    return {"bundle": verified, "skill_root": str(skill_root), "providers": ["claude", "codex"]}


def _ensure_workspace(project: Path, run_id: str) -> Path:
    _open_run_id(run_id)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current = os.open(project, flags)
    descriptors = [current]
    try:
        for part, mode in ((".brandkit", 0o700), ("work", 0o700), (run_id, 0o700), ("discovery", 0o700)):
            try: os.mkdir(part, mode, dir_fd=current)
            except FileExistsError: pass
            current = os.open(part, flags, dir_fd=current); descriptors.append(current)
    except OSError as exc:
        raise DiscoveryError(f"cannot safely create discovery workspace: {exc.strerror}") from exc
    finally:
        for fd in reversed(descriptors): os.close(fd)
    return project / ".brandkit/work" / run_id / "discovery"


def prepare_orchestration(project_root: Path, bundle_root: Path, run_id: str, owner_key: Path) -> dict[str, Any]:
    """Validate activation and return the exact model-skill execution trace."""
    project = _exact_root(project_root, "project root")
    verify_project_activation(project, bundle_root)
    workspace = _ensure_workspace(project, run_id)
    progress = project / ".brandkit/work" / run_id / "interview-progress.md"
    if not progress.exists():
        initialize_progress(project, run_id, owner_key)
    state = read_progress(project, run_id, owner_key)
    frontier_complete = not state["open"] and state["answer_turns"] >= 2
    next_steps = ([{"skill": "grilling", "mode": "model-invoked", "action": "continue-frontier"}]
                  if not frontier_complete else [{"skill": "domain-modeling", "mode": "model-invoked", "required_outputs": ["CONTEXT.md", "docs/adr/*.md"]}])
    return {
        "schema_version": "brandkit-discovery-orchestration-trace.v1",
        "entrypoint": "$brandkit-interview",
        "cwd": str(workspace),
        "phase": "grilling" if not frontier_complete else "domain-modeling",
        "progress": str(progress), "answer_turns": state["answer_turns"], "open_frontier": state["open"], "steps": next_steps,
        "wrapper": {"skill": "grill-with-docs", "policy": "reference-only-never-invoke"},
        "fallback": "brandkit-interview-native-11-axes",
    }


def _safe_write_target(project: Path, relative: str) -> Path:
    """Decide every refusal of ``_safe_write`` and return the resolved target.

    The sealed-run test stands here as well as at every run-id gate because
    this is the single funnel every write in this module passes through: a
    caller that reaches a sealed path by some route other than a run id - a
    handoff path, a receipt path, a future one - is refused by the write itself
    rather than by whichever gate happened to be in front of it.

    Two tests, and the second is the one that makes the sentence above true.
    The spelling test refuses a *named* sealed component; on its own it refuses
    only callers that spell the seal, so a symlink ``work/alias -> first-ci``
    walked past it and the file landed inside the sealed tree.  The second test
    therefore asks the same question the way the rest of this module asks it
    since r1d: by identity, of the *resolved* parent directory.  ``resolve``
    has already followed every link, so the alias answers with the components
    of its target and is refused - and a directory named ``first-ci`` that the
    project genuinely owns is refused by both tests alike, which is the
    fail-closed direction.

    All four rules are pure predicates over the path and over the directory the
    file lands in, which is why they are readable apart from the write since
    r1g: a caller that must not refuse *after* it has spent something asks this
    function about every path it intends to write while the receipts are still
    unspent, and then writes knowing the writer has nothing left to refuse.
    """
    parts = _relative(relative).parts
    if _names_protected_run(parts):
        raise DiscoveryError("write target names the protected first-ci run")
    target = project / _relative(relative).as_posix()
    parent = target.parent.resolve(strict=True)
    if project not in parent.parents and parent != project:
        raise DiscoveryError("write target escapes project")
    if _names_protected_run(parent.relative_to(project).parts):
        raise DiscoveryError("write target resolves into the protected first-ci run")
    if target.exists() and (target.is_symlink() or not target.is_file()):
        raise DiscoveryError("write target is not a regular file")
    return target


def _safe_write(project: Path, relative: str, data: bytes) -> None:
    """Write one regular file below the project, never into the sealed run.

    Every refusal of this write lives in ``_safe_write_target``; what remains
    here is the write itself, which cannot refuse.
    """
    target = _safe_write_target(project, relative)
    parent = target.parent.resolve(strict=True)
    temporary = parent / ("." + target.name + "." + secrets.token_hex(12))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, data); os.fsync(fd)
    finally: os.close(fd)
    os.replace(temporary, target)


def _progress_payload(project: Path, run_id: str, open_axes: list[str], explicit_open: list[str], covered: list[str], answer_turns: int, predecessor_sha256: str, event_sha256: str, nonce: str) -> dict[str, Any]:
    sets = [set(open_axes), set(explicit_open), set(covered)]
    if any(not group <= set(BRAND_AXES) for group in sets) or any(sets[a] & sets[b] for a in range(3) for b in range(a + 1, 3)) or set().union(*sets) != set(BRAND_AXES):
        raise DiscoveryError("progress axes must form one closed partition")
    if not isinstance(answer_turns, int) or isinstance(answer_turns, bool) or answer_turns < 0:
        raise DiscoveryError("progress answer-turn count is invalid")
    if not HEX64.fullmatch(predecessor_sha256) or not HEX64.fullmatch(event_sha256) or not re.fullmatch(r"[0-9a-f]{64}", nonce):
        raise DiscoveryError("progress chain fields are invalid")
    return {"domain": "brandkit-interview-progress-v1", "project_realpath_sha256": _project_identity(project), "run_id": run_id,
            "answer_turns": answer_turns, "predecessor_sha256": predecessor_sha256, "event_sha256": event_sha256,
            "nonce": nonce, "open": open_axes, "explicit_open": explicit_open, "covered": covered}


def progress_markdown(payload: dict[str, Any], signature: str) -> bytes:
    open_axes, explicit_open, covered, answer_turns = payload["open"], payload["explicit_open"], payload["covered"], payload["answer_turns"]
    phase = "grilling" if open_axes or answer_turns < 2 else "docs-ready"
    def section(title: str, values: list[str]) -> str:
        return f"## {title}\n" + ("\n".join(f"- {item}" for item in values) if values else "- none")
    return (f"# Interview Progress\n\nRun: {payload['run_id']}\nProject: {payload['project_realpath_sha256']}\nAnswer turns: {answer_turns}\nPrevious: {payload['predecessor_sha256']}\nEvent: {payload['event_sha256']}\nNonce: {payload['nonce']}\nAuthentication: {signature}\nPhase: {phase}\n\n" + section("Open frontier", open_axes) + "\n\n" + section("Explicitly open", explicit_open) + "\n\n" + section("Covered", covered) + "\n").encode()


def _progress_ledger(project: Path, run_id: str, key: bytes, turn: int, head: str) -> bytes:
    value = {"domain": "brandkit-interview-progress-ledger-v1", "project_realpath_sha256": _project_identity(project), "run_id": run_id, "turn": turn, "head_sha256": head}
    return canonical({**value, "hmac_sha256": hmac.new(key, canonical(value), hashlib.sha256).hexdigest()})


def _ledger_path(owner_key: Path, project: Path, run_id: str) -> Path:
    absolute = Path(os.path.abspath(owner_key)); parent = _exact_root(absolute.parent, "owner key directory")
    return parent / f".{absolute.name}.brandkit-progress-{_project_identity(project)}-{run_id}.json"


def _write_external_ledger(path: Path, raw: bytes) -> None:
    parent = _exact_root(path.parent, "progress ledger directory"); temporary = "." + path.name + "." + secrets.token_hex(12)
    directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
        try: os.write(fd, raw); os.fsync(fd)
        finally: os.close(fd)
        os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd); os.fsync(directory_fd)
    finally:
        try: os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError: pass
        os.close(directory_fd)


def initialize_progress(project_root: Path, run_id: str, owner_key: Path) -> dict[str, Any]:
    project = _exact_root(project_root, "project root"); _ensure_workspace(project, run_id); key = _external_owner_key(project, owner_key)
    ledger_path = _ledger_path(owner_key, project, run_id)
    if ledger_path.exists() or ledger_path.is_symlink():
        raise DiscoveryError("progress head already exists; use a fresh run-id")
    payload = _progress_payload(project, run_id, list(BRAND_AXES), [], [], 0, "0" * 64, "0" * 64, secrets.token_hex(32))
    signature = hmac.new(_derive_key(key, b"progress-state"), canonical(payload), hashlib.sha256).hexdigest()
    raw = progress_markdown(payload, signature); head = hashlib.sha256(raw).hexdigest()
    _safe_write(project, f".brandkit/work/{run_id}/interview-progress.md", raw)
    _write_external_ledger(ledger_path, _progress_ledger(project, run_id, _derive_key(key, b"progress-ledger"), 0, head))
    return {"path": f".brandkit/work/{run_id}/interview-progress.md", "phase": "grilling", "answer_turns": 0, "sha256": head}


def write_progress(project_root: Path, run_id: str, open_axes: list[str], explicit_open: list[str], covered: list[str], answer_sha256: str, owner_key: Path) -> dict[str, Any]:
    project = _exact_root(project_root, "project root"); _ensure_workspace(project, run_id)
    progress_path = project / ".brandkit/work" / run_id / "interview-progress.md"
    if not progress_path.exists(): initialize_progress(project, run_id, owner_key)
    prior = read_progress(project, run_id, owner_key); key = _external_owner_key(project, owner_key); answer_turns = prior["answer_turns"] + 1
    payload = _progress_payload(project, run_id, open_axes, explicit_open, covered, answer_turns, hashlib.sha256(prior["raw"]).hexdigest(), answer_sha256, secrets.token_hex(32))
    signature = hmac.new(_derive_key(key, b"progress-state"), canonical(payload), hashlib.sha256).hexdigest()
    raw = progress_markdown(payload, signature)
    relative = f".brandkit/work/{run_id}/interview-progress.md"; _safe_write(project, relative, raw)
    head = hashlib.sha256(raw).hexdigest(); _write_external_ledger(_ledger_path(owner_key, project, run_id), _progress_ledger(project, run_id, _derive_key(key, b"progress-ledger"), answer_turns, head))
    return {"path": relative, "phase": "grilling" if open_axes or answer_turns < 2 else "docs-ready", "answer_turns": answer_turns, "sha256": head}


def read_progress(project_root: Path, run_id: str, owner_key: Path) -> dict[str, Any]:
    _open_run_id(run_id)
    project = _exact_root(project_root, "project root"); key = _external_owner_key(project, owner_key)
    raw, _ = secure_read(project, f".brandkit/work/{run_id}/interview-progress.md")
    text = raw.decode("utf-8")
    identity = re.match(rf"^# Interview Progress\n\nRun: {re.escape(run_id)}\nProject: (?P<project>[0-9a-f]{{64}})\nAnswer turns: (?P<count>[0-9]+)\nPrevious: (?P<previous>[0-9a-f]{{64}})\nEvent: (?P<event>[0-9a-f]{{64}})\nNonce: (?P<nonce>[0-9a-f]{{64}})\nAuthentication: (?P<signature>[0-9a-f]{{64}})\nPhase: ", text)
    if not identity:
        raise DiscoveryError("interview progress identity is invalid")
    answer_turns = int(identity.group("count"))
    def values(title: str) -> list[str]:
        match = re.search(rf"^## {re.escape(title)}\n(?P<body>(?:- .*\n?)+)", text, re.M)
        if not match: raise DiscoveryError("interview progress section is missing")
        result = [line[2:] for line in match.group("body").splitlines()]
        return [] if result == ["none"] else result
    opened, explicit, covered = values("Open frontier"), values("Explicitly open"), values("Covered")
    payload = _progress_payload(project, run_id, opened, explicit, covered, answer_turns, identity.group("previous"), identity.group("event"), identity.group("nonce"))
    signature = hmac.new(_derive_key(key, b"progress-state"), canonical(payload), hashlib.sha256).hexdigest()
    if identity.group("project") != _project_identity(project) or not hmac.compare_digest(identity.group("signature"), signature) or raw != progress_markdown(payload, signature):
        raise DiscoveryError("interview progress is not canonical")
    ledger_path = _ledger_path(owner_key, project, run_id); ledger_raw, ledger_info = secure_read(ledger_path.parent, ledger_path.name); ledger = load_json_bytes(ledger_raw, "progress ledger")
    if stat.S_IMODE(ledger_info.st_mode) != 0o600 or ledger_info.st_uid != os.geteuid():
        raise DiscoveryError("progress ledger must be an owner-only regular file")
    ledger_signature = ledger.pop("hmac_sha256", None)
    if set(ledger) != {"domain", "project_realpath_sha256", "run_id", "turn", "head_sha256"} or not isinstance(ledger_signature, str) or not hmac.compare_digest(ledger_signature, hmac.new(_derive_key(key, b"progress-ledger"), canonical(ledger), hashlib.sha256).hexdigest()):
        raise DiscoveryError("progress ledger authentication failed")
    if ledger != {"domain": "brandkit-interview-progress-ledger-v1", "project_realpath_sha256": _project_identity(project), "run_id": run_id, "turn": answer_turns, "head_sha256": hashlib.sha256(raw).hexdigest()}:
        raise DiscoveryError("progress state is stale or replayed")
    return {"open": opened, "explicit_open": explicit, "covered": covered, "answer_turns": answer_turns, "head_sha256": hashlib.sha256(raw).hexdigest(), "chain_id": hashlib.sha256(("brandkit-progress-chain-v1\n" + _project_identity(project) + "\n" + run_id + "\n").encode()).hexdigest(), "raw": raw}


def _workspace_paths(project: Path, workspace: str, handoff: str) -> list[str]:
    flags_dir = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    result: list[str] = []
    try:
        current = os.open(project, flags_dir); descriptors.append(current)
        for part in _relative(workspace).parts:
            current = os.open(part, flags_dir, dir_fd=current); descriptors.append(current)

        def visit(directory_fd: int, relative_dir: str) -> None:
            with os.scandir(directory_fd) as entries:
                for entry in entries:
                    relative = f"{relative_dir}/{entry.name}"
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise DiscoveryError(f"discovery workspace contains symlink: {relative}")
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, flags_dir, dir_fd=directory_fd)
                        descriptors.append(child)
                        visit(child, relative)
                    elif stat.S_ISREG(info.st_mode):
                        if relative != handoff:
                            result.append(relative)
                    else:
                        raise DiscoveryError(f"discovery workspace contains nonregular file: {relative}")
        visit(current, workspace)
        return sorted(result)
    except OSError as exc:
        raise DiscoveryError(f"cannot safely enumerate discovery workspace: {exc.strerror}") from exc
    finally:
        for fd in reversed(descriptors):
            try: os.close(fd)
            except OSError: pass


def meaningful_markdown(raw: bytes, heading: str, label: str, *, prefix: bool = False) -> None:
    """Require a real heading plus human-readable content beyond Markdown syntax."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DiscoveryError(f"{label} is not UTF-8") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    without_comments = re.sub(r"<!--.*?-->", "", normalized, flags=re.DOTALL)
    lines = without_comments.splitlines()
    while lines and not lines[0].strip(): lines.pop(0)
    first = lines[0].strip() if lines else ""
    heading_ok = first == heading or (prefix and first.startswith(heading + " "))
    if not heading_ok:
        raise DiscoveryError(f"{label} lacks its canonical heading")
    payload_lines: list[str] = []
    fence: tuple[str, int] | None = None
    for line in lines[1:]:
        if fence is None:
            opener = re.match(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$", line)
            if opener:
                fence = (opener.group(1)[0], len(opener.group(1)))
            else:
                payload_lines.append(line)
        else:
            marker, width = fence
            if re.fullmatch(rf"[ ]{{0,3}}{re.escape(marker)}{{{width},}}[ \t]*", line):
                fence = None
    if fence is not None:
        raise DiscoveryError(f"{label} contains an unclosed fenced block")
    payload = "\n".join(payload_lines)
    payload = re.sub(r"!?\[([^]]*)\]\([^)]*\)", r"\1", payload)
    words = re.findall(r"[^\W_]+", payload, flags=re.UNICODE)
    if len(words) < 3 or len(" ".join(words)) < 12:
        raise DiscoveryError(f"{label} has no substantive content beyond its heading")


def _closed_binding(binding: dict[str, Any], label: str, *, prefix: str | None = None) -> None:
    """The two rules a descriptor answers before anything is read for it."""
    if not isinstance(binding, dict) or set(binding) != {"path", "size", "sha256"} or not isinstance(binding["size"], int) or binding["size"] < 0 or not HEX64.fullmatch(str(binding["sha256"])):
        raise DiscoveryError(f"{label} descriptor is not closed")
    if prefix is not None and not binding["path"].startswith(prefix):
        raise DiscoveryError(f"{label} path is outside its canonical workspace")


def _bound_file(project: Path, binding: dict[str, Any], label: str, *, prefix: str | None = None) -> bytes:
    _closed_binding(binding, label, prefix=prefix)
    raw, info = secure_read(project, binding["path"])
    if info.st_size != binding["size"] or hashlib.sha256(raw).hexdigest() != binding["sha256"]:
        raise DiscoveryError(f"{label} differs from its descriptor")
    return raw


def _bound_bytes(raw: bytes, binding: dict[str, Any], label: str, *, prefix: str | None = None) -> bytes:
    """``_bound_file``'s rules, decided against bytes the caller already holds.

    The operation that *builds* a handoff knows every member's bytes before it
    writes any of them, so it can answer the same three refusals - closed
    descriptor, canonical workspace, size and digest - while the file is still
    only a value.  That is what lets the inventory rules be decided before the
    receipts are spent rather than after.
    """
    _closed_binding(binding, label, prefix=prefix)
    if len(raw) != binding["size"] or hashlib.sha256(raw).hexdigest() != binding["sha256"]:
        raise DiscoveryError(f"{label} differs from its descriptor")
    return raw


def _outside_project(project: Path, path: Path, label: str) -> Path:
    """Return an absolute external path, refusing anything the project can write.

    The lexical comparison is kept as the cheap first answer; the identity test
    is the one that decides, because it is the only one a case-flipped or
    NFD-spelled project component cannot walk past.  Before this, the adapter
    accepted ``…/Project/.brandkit/inside.key`` as external on a case-folding
    file system and the refusal existed only because the core repeated the check
    by identity - a defence that reads as two layers and was one.

    A hard link is the third spelling of the same trick and neither of those
    two tests sees it: it is a second *name* for one inode, so every path
    component of the external name is genuinely outside the project while the
    bytes are a file inside it, and ``realpath`` has nothing to resolve.  The
    core catches it one layer down (``_external_0600`` requires ``st_nlink ==
    1``); the adapter used to be blind to it, so the same defence read as two
    layers and was one here too.  The link count is asked of regular files
    only: a directory legitimately carries more than one link, and the external
    inputs that are directories - the source sidecar - are judged by
    containment, not by link count.
    """
    absolute = Path(os.path.abspath(path))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise DiscoveryError(f"{label} is absent or unreadable") from exc
    if resolved == project or project in resolved.parents or _identity_contains(project, absolute):
        raise DiscoveryError(f"{label} must remain outside the project")
    try:
        info = os.stat(resolved)
    except OSError as exc:
        raise DiscoveryError(f"{label} is absent or unreadable") from exc
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise DiscoveryError(f"{label} must be an exclusive external file")
    return absolute


def _closed_affinity_auth(project: Path, run_id: str, affinity_auth: Any) -> dict[str, Any]:
    """Retype the optional Affinity descriptor before the core ever sees it.

    The descriptor names two *external* files - the 32-byte owner key and the
    persistent replay ledger - plus one workspace-relative receipt binding per
    Affinity-authored candidate.  The core re-checks every one of these; this
    adapter check is deliberately additive and never a substitute:

    * it is a keyword argument of ``prepare_visual_confirmation``, never a field
      of the caller-supplied request state, so a writer inside the project
      cannot name which external key or ledger a run authenticates against;
    * the two external paths must be absolute, distinct and outside the project
      - the core enforces the last of these too, the distinctness only
      indirectly through the 32-byte key rule;
    * every receipt binding is closed ``{path, size, sha256}``, confined to this
      run's visual-decisions workspace and verified by size *and* digest, while
      the core verifies the digest alone.

    Nothing here relaxes a core promise: a descriptor this function accepts is
    still validated in full by ``validate_bundle`` under its external lock.
    """
    if not isinstance(affinity_auth, dict) or set(affinity_auth) != {"owner_key_path", "replay_ledger_path", "receipts"} or not isinstance(affinity_auth["receipts"], dict):
        raise DiscoveryError("Affinity authentication descriptor is not closed")
    external: list[Path] = []
    for field in ("owner_key_path", "replay_ledger_path"):
        value = affinity_auth[field]
        if not isinstance(value, (str, Path)) or not str(value) or not os.path.isabs(str(value)):
            raise DiscoveryError("Affinity authentication path is not an absolute external path")
        external.append(_outside_project(project, Path(str(value)), "Affinity keys and replay ledger"))
    if external[0] == external[1]:
        raise DiscoveryError("Affinity owner key and replay ledger must be different files")
    workspace = f".brandkit/work/{run_id}/visual-decisions"
    receipts: dict[str, Any] = {}
    for candidate_id, binding in affinity_auth["receipts"].items():
        if not isinstance(candidate_id, str) or not RUN_ID.fullmatch(candidate_id):
            raise DiscoveryError("Affinity receipt candidate id is invalid")
        if not isinstance(binding, dict) or set(binding) != {"path", "size", "sha256"} or not isinstance(binding["path"], str):
            raise DiscoveryError("Affinity receipt binding is not closed")
        relative = PurePosixPath(binding["path"])
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise DiscoveryError("Affinity receipt path is not a confined workspace-relative path")
        _bound_file(project, {**binding, "path": f"{workspace}/{relative.as_posix()}"}, f"Affinity receipt {candidate_id}", prefix=workspace + "/")
        receipts[candidate_id] = {"path": relative.as_posix(), "size": binding["size"], "sha256": binding["sha256"]}
    return {"owner_key_path": external[0], "replay_ledger_path": external[1], "receipts": receipts}


def _visual_core() -> Any:
    """The one place this module reaches the core, with its error class in hand.

    The core signals refusals with ``VisualDecisionError``, which derives from
    ``RuntimeError`` and is therefore *not* one of the exceptions ``main()``
    turns into a contract message.  A core refusal on the CLI printed a
    traceback instead of a sentence.  Fetching the class here lets both call
    sites re-raise it as a ``DiscoveryError`` that carries the core's own words,
    so the refusal is the core's and the shape of the failure is this module's.
    """
    # The core is reached as ``scripts.brandkit_visual_decisions`` so that a
    # test which imports it under that name and this module see one object.  As
    # a *script*, though, only ``…/scripts`` is on the path, not the repository
    # root, and the declared subcommand ``prepare-visual-confirmation`` died on
    # a ModuleNotFoundError before it ever reached a contract rule.  The entry
    # is added here, once, from this file's own location - not inherited from an
    # environment the caller controls.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    module = importlib.import_module("scripts.brandkit_visual_decisions")
    failure = getattr(module, "VisualDecisionError", None)
    if not isinstance(failure, type) or not issubclass(failure, BaseException):
        raise DiscoveryError("visual candidate validator hook is unavailable")
    return module, failure


def _release_bound_source_contract(binding: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    """Read the release-bound source contract the core has just validated.

    The core resolves candidate sources from exactly one artifact:
    ``visual-decision-sources.v1.json`` at the *skill repository* root, pinned
    to that literal name in ``validate_source_contract``, checked against a
    closed schema and re-checked by an independently hash-bound validator.  Its
    records are ``candidates[].name``; the single usable one is named by
    ``selected_dependency``.

    This adapter used to invent a different document - ``{"sources":[{"id": …}]}``
    read from a copy inside the run workspace - which nothing produces and the
    core's closed schema forbids.  No document satisfied both sides, so
    ``prepare_visual_confirmation`` could not complete for any real bundle.

    The read here is a *read*, not a second validation: it runs after
    ``validate_bundle`` has accepted the whole bundle, and it re-checks the
    digest the core pinned, so the bytes parsed are the bytes the core
    validated.  Re-running the independent source validator would additionally
    have meant re-deriving the core's sidecar default in this module, which is
    the kind of second spelling that drifts.
    """
    module, _failure = _visual_core()
    root = getattr(module, "ROOT", None); reader = getattr(module, "secure_read", None)
    if not isinstance(root, Path) or not callable(reader):
        raise DiscoveryError("visual source contract location is unavailable")
    if binding["path"] != SOURCE_CONTRACT_NAME:
        raise DiscoveryError("candidate source contract path differs from the release-bound artifact")
    raw, _info = reader(root, SOURCE_CONTRACT_NAME, "visual-decision source contract")
    if hashlib.sha256(raw).hexdigest() != binding["sha256"]:
        raise DiscoveryError("release-bound source contract differs from its binding")
    return load_json_bytes(raw, "visual-decision source contract"), raw


def _visual_source_records(contract: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The selected dependency and the source records a handoff may cite.

    Kept pure and separate so the shape rules can be measured directly, without
    a mock anywhere near the adapter/core seam.
    """
    selected = contract.get("selected_dependency")
    if not isinstance(selected, str) or not RUN_ID.fullmatch(selected):
        raise DiscoveryError("release-bound source contract names no usable selected dependency")
    if selected == "owner-visual-selection":
        raise DiscoveryError("candidate source id collides with the reserved owner source")
    candidates = contract.get("candidates")
    if not isinstance(candidates, list):
        raise DiscoveryError("release-bound source contract carries no candidate list")
    records = {item["name"]: item for item in candidates
               if isinstance(item, dict) and item.get("decision") == "selected" and isinstance(item.get("name"), str)}
    if set(records) != {selected}:
        raise DiscoveryError("release-bound source contract does not select exactly its named dependency")
    return selected, records


def _validated_visual_bundle(project: Path, run_id: str, now: int, *, sidecar_root: Path | None = None, affinity_auth: dict[str, Any] | None = None, commit: bool = True) -> dict[str, Any]:
    """Reach the core's bundle validation, either verifying or consuming.

    ``commit=False`` selects the core's ``verify_bundle``, which runs the same
    locked body and answers the same result without advancing the replay
    ledger.  It exists so that an operation over the one-shot Affinity receipts
    can be *decided* before it is *paid for*; see the ordering note in
    ``prepare_visual_confirmation``.  Both names are fetched the same way and
    both results pass the same closed-result gate, so the seam to the core has
    not widened: it is one more function of the core, not one check less.
    """
    module, failure = _visual_core()
    validator = getattr(module, "validate_bundle" if commit else "verify_bundle", None)
    if not callable(validator):
        raise DiscoveryError("visual candidate validator hook is unavailable")
    try:
        result = validator(project, run_id, now, sidecar_root=sidecar_root, affinity_auth=affinity_auth)
    except failure as exc:
        raise DiscoveryError(f"visual candidate validator refused the bundle: {exc}") from exc
    return _closed_core_result(result)


def _closed_core_result(result: Any) -> dict[str, Any]:
    """Re-derive what the core returned instead of believing it.

    Kept as its own pure function so the rule can be measured directly.  The
    alternative - reaching this code by substituting the core - is exactly the
    seam that hid the R1C blocker for eleven rounds, so no test of this module
    replaces ``validate_bundle`` any more.  The value hash in particular is
    *re-computed* here: it is the number the adapter then carries into the
    handoff, the receipt and ``brand-raw.md``, and a number that is only ever
    copied is a number nothing checks.
    """
    if not isinstance(result, dict) or set(result) != {"candidate_set_sha256", "eligible_candidates", "blockers"} or not HEX64.fullmatch(str(result["candidate_set_sha256"])) or not isinstance(result["eligible_candidates"], list) or not isinstance(result["blockers"], list):
        raise DiscoveryError("visual candidate validator returned an invalid closed result")
    for candidate in result["eligible_candidates"]:
        if (not isinstance(candidate, dict) or set(candidate) != {"id", "kind", "value", "value_sha256"}
                or candidate["kind"] not in VISUAL_DOMAINS
                or not HEX64.fullmatch(str(candidate["value_sha256"]))
                or hashlib.sha256(canonical(candidate["value"])).hexdigest() != candidate["value_sha256"]):
            raise DiscoveryError("visual candidate validator returned an invalid candidate")
    return result


def prepare_visual_confirmation(project_root: Path, run_id: str, owner_key: Path, state: dict[str, Any], now: int | None = None, *, sidecar_root: Path | None = None, affinity_auth: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve one owner visual selection against the pinned candidate bundle.

    ``sidecar_root`` and ``affinity_auth`` are the trust inputs of the final
    core API ``validate_bundle(project_root, run_id, now, *, sidecar_root=None,
    affinity_auth=None)``.  They are keyword arguments of *this* function rather
    than fields of ``state`` on purpose: ``state`` is a run-local document
    inside the project and therefore writable by whoever the run is protecting
    itself against, while these two inputs decide which independent sidecar and
    which external key/ledger pair the run authenticates against.
    """
    project = _exact_root(project_root, "project root"); progress = read_progress(project, run_id, owner_key)
    if progress["open"] or progress["answer_turns"] < 2:
        raise DiscoveryError("interview frontier is incomplete; visual confirmation is unavailable")
    required = {"operation", "brand_raw_preimage", "candidate_bundle", "candidate_review", "owner_selection"}
    if not isinstance(state, dict) or set(state) != required or state.get("operation") != "prepare-visual-confirmation":
        raise DiscoveryError("visual confirmation request is not closed")
    workspace = f".brandkit/work/{run_id}/visual-decisions"
    preimage = _bound_file(project, state["brand_raw_preimage"], "brand-raw preimage", prefix=f".brandkit/work/{run_id}/")
    bundle_raw = _bound_file(project, state["candidate_bundle"], "candidate bundle", prefix=workspace + "/")
    review_raw = _bound_file(project, state["candidate_review"], "candidate review", prefix=workspace + "/")
    selection = state["owner_selection"]
    if not isinstance(selection, dict) or set(selection) != {"selection_id", "chat_statement", "choices"} or not RUN_ID.fullmatch(str(selection["selection_id"])):
        raise DiscoveryError("owner visual selection is not closed")
    statement_raw = _bound_file(project, selection["chat_statement"], "owner selection statement", prefix=workspace + "/")
    if not statement_raw.strip() or b"\r" in statement_raw:
        raise DiscoveryError("owner selection statement must be non-empty canonical UTF-8/LF")
    try: statement_raw.decode("utf-8")
    except UnicodeError as exc: raise DiscoveryError("owner selection statement is not UTF-8") from exc
    sidecar = None if sidecar_root is None else _outside_project(project, Path(sidecar_root), "visual source sidecar")
    descriptor = None if affinity_auth is None else _closed_affinity_auth(project, run_id, affinity_auth)
    moment = int(time.time()) if now is None else now
    # Verify first, spend last.  Until r1f this single call was consuming, and
    # it stood *above* every rule that inspects the owner's selection - so a
    # typo in one candidate id, an extra field on one choice, a misspelt
    # outcome or a domain answered twice each burned the Affinity mutation
    # receipts on their way to being refused.  The receipts are single-use, so
    # the corrected selection was then refused too ("already consumed"), and
    # with it the sheet render: one owner typo made the whole run unusable,
    # which is the false refusal this module exists to avoid, and the exact
    # opposite of what the skill promises about corrections.  The verification
    # answers the same result the consuming call answers - same lock, same
    # body, same refusals - so every rule below can now be decided before
    # anything is spent.
    validated = _validated_visual_bundle(project, run_id, moment, sidecar_root=sidecar, affinity_auth=descriptor, commit=False)
    if validated["blockers"] or validated["candidate_set_sha256"] != hashlib.sha256(bundle_raw).hexdigest():
        raise DiscoveryError("candidate bundle is blocked or differs from the validated bundle")
    # The review is bound to the one artifact the owner decides on - the sheet
    # the core rendered for exactly this bundle - here, right after the bundle
    # is known to be the validated one and before ``selection-sources/`` is
    # created or anything is spent.  ``_visual_handoff_rules`` states the same
    # rule again for every reader of the handoff; see ``_bound_sheet_review``.
    _bound_sheet_review(project, workspace, state["candidate_review"]["path"], review_raw, bundle_raw)
    candidates = {item["id"]: item for item in validated["eligible_candidates"]}
    bundle = load_json_bytes(bundle_raw, "visual candidate bundle")
    bundle_candidates = {item.get("id"): item for item in bundle.get("candidates", []) if isinstance(item, dict)}
    recommendations = bundle.get("recommendations")
    if not isinstance(recommendations, list) or any(
        not isinstance(item, dict)
        or set(item) != {"kind", "candidate_id", "rationale", "source_ids"}
        or item.get("candidate_id") not in candidates
        or not isinstance(item.get("rationale"), str)
        or not isinstance(item.get("source_ids"), list)
        for item in recommendations
    ):
        raise DiscoveryError("candidate recommendations are not a closed non-normative list")
    brief_raw, _ = secure_read(project, workspace + "/visual-brief.json"); brief = load_json_bytes(brief_raw, "visual brief")
    open_domains = brief.get("open_decisions")
    if not isinstance(open_domains, list) or len(open_domains) != len(set(open_domains)) or set(open_domains) - set(VISUAL_DOMAINS):
        raise DiscoveryError("visual brief has invalid open domains")
    choices = selection["choices"]
    if not isinstance(choices, list) or len(choices) != len(open_domains) or {item.get("domain") for item in choices if isinstance(item, dict)} != set(open_domains):
        raise DiscoveryError("visual selection must cover each open domain exactly once")
    decisions: list[dict[str, Any]] = []; outcomes: list[dict[str, str]] = []; resolved: list[dict[str, Any]] = []
    for choice in choices:
        if not isinstance(choice, dict) or choice.get("domain") not in open_domains or choice.get("outcome") not in {"candidate", "open", "not-applicable"}:
            raise DiscoveryError("visual choice is invalid")
        domain, outcome = choice["domain"], choice["outcome"]
        expected = {"domain", "outcome", "candidate_id"} if outcome == "candidate" else {"domain", "outcome"}
        if set(choice) != expected:
            raise DiscoveryError("visual choice contains caller-supplied value or extra fields")
        if outcome != "candidate": outcomes.append({"domain": domain, "outcome": outcome}); resolved.append({"domain": domain, "outcome": outcome}); continue
        candidate = candidates.get(choice["candidate_id"])
        if candidate is None or candidate["kind"] != domain:
            raise DiscoveryError("visual candidate is unknown, ineligible, or belongs to another domain")
        decision_id = f"visual-{domain}"
        bundle_candidate = bundle_candidates.get(candidate["id"])
        if not isinstance(bundle_candidate, dict) or bundle_candidate.get("value") != candidate["value"] or bundle_candidate.get("value_sha256") != candidate["value_sha256"] or not isinstance(bundle_candidate.get("source_ids"), list):
            raise DiscoveryError("eligible candidate differs from its bound candidate-set record")
        decisions.append({"id": decision_id, "domain": domain, "candidate_id": candidate["id"], "candidate_value": candidate["value"], "candidate_value_sha256": candidate["value_sha256"], "source_ids": ["owner-visual-selection", *bundle_candidate["source_ids"]]})
        resolved.append({"domain": domain, "outcome": "candidate", "candidate_id": candidate["id"], "candidate_value_sha256": candidate["value_sha256"]})
    handoff_path = f"{workspace}/handoff.json"; filled_review_path = f"{workspace}/filled-review.md"
    source_binding = bundle.get("source_review")
    if not isinstance(source_binding, dict) or set(source_binding) != {"path", "sha256"} or not HEX64.fullmatch(str(source_binding.get("sha256", ""))):
        raise DiscoveryError("candidate source review binding is invalid")
    contract, contract_raw = _release_bound_source_contract(source_binding)
    selected_source, source_records = _visual_source_records(contract)
    used_source_ids = sorted({source_id for item in decisions for source_id in item["source_ids"] if source_id != "owner-visual-selection"})
    if any(source_id != selected_source for source_id in used_source_ids):
        raise DiscoveryError("selected candidate references a source the release-bound contract did not select")
    sources = [{"id": "owner-visual-selection", "classification": "owner-supplied", "file": selection["chat_statement"]["path"]}]
    source_files: list[dict[str, Any]] = []
    source_output = project / workspace / "selection-sources"
    source_output.mkdir(mode=0o700, exist_ok=True)
    if source_output.is_symlink() or not source_output.is_dir() or source_output.resolve(strict=True) != source_output:
        raise DiscoveryError("visual selection source directory is unsafe")
    # The contract lives in the skill repository, the handoff inventory is
    # confined to this run's workspace, so the exact validated bytes are copied
    # in as the handoff's evidence member under the digest the core pinned.
    evidence_path = f"{workspace}/selection-sources/{SOURCE_CONTRACT_NAME}"
    payloads: list[tuple[str, bytes]] = [(evidence_path, contract_raw)]
    source_raws: list[bytes] = []
    for source_id in used_source_ids:
        record = source_records.get(source_id)
        if record is None:
            raise DiscoveryError("selected candidate references an absent source record")
        relative = f"{workspace}/selection-sources/{source_id}.json"
        raw = canonical(record); payloads.append((relative, raw)); source_raws.append(raw)
        sources.append({"id": source_id, "classification": "observed", "file": relative})
        source_files.append({"path": relative, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "role": "source"})
    # The handoff is built here, above the commit, because everything it is
    # built from is already known here.  Until r1g it was built below, and the
    # rules ``handoff_snapshot`` states about it therefore ran *after* the
    # receipts had been spent: an owner who bound the chat statement to a file
    # the inventory already lists - the candidate set, the candidate review -
    # reached "visual handoff inventory is not closed" with the receipts gone,
    # handoff.json on disk, filled-review.md missing, and neither a corrected
    # selection nor a second sheet render still possible.  A pure owner input
    # error destroyed the run, which is the burn-on-refusal class this module
    # exists to close, one call level below where r1f closed it.
    members: list[tuple[dict[str, Any], bytes]] = [
        ({**state["candidate_bundle"], "role": "candidate-bundle"}, bundle_raw),
        ({**state["candidate_review"], "role": "candidate-review"}, review_raw),
        ({**selection["chat_statement"], "role": "source"}, statement_raw),
        ({"path": evidence_path, "size": len(contract_raw), "sha256": source_binding["sha256"], "role": "evidence"}, contract_raw),
        *zip(source_files, source_raws),
    ]
    files = [item for item, _ in members]
    document = {"schema_version": "brandkit-discovery-handoff.v2", "handoff_kind": "visual-decisions", "handoff_id": f"visual-{selection['selection_id']}", "run_id": run_id, "workspace": workspace,
                "selection_id": selection["selection_id"], "brand_raw_preimage_sha256": hashlib.sha256(preimage).hexdigest(), "candidate_bundle_sha256": validated["candidate_set_sha256"], "candidate_review_sha256": hashlib.sha256(review_raw).hexdigest(),
                "decisions": decisions, "outcomes": outcomes, "recommendations": recommendations, "sources": sources, "files": files}
    # The rehearsal.  Every rule of the finished handoff is decided against the
    # bytes this function is about to write, under the same rule function and
    # the same descriptor rules the post-commit read uses; every path it intends
    # to write is put to the write's own four refusals without writing; and the
    # review is rendered once from the rehearsed snapshot, so the two callees
    # that stand behind the commit - ``handoff_snapshot`` and ``review_markdown``
    # - have nothing left to say that has not already been said for free.
    raw_document = canonical(document)
    # The rehearsal shares its rules with the post-commit read but not its
    # state: ``_staged_member_reader`` resolves each inventory member against
    # the bytes staged here, ``_bound_file`` resolves it against the disk
    # *after* this operation has written to it.  Two of the paths it writes -
    # ``handoff.json`` and ``filled-review.md`` - are no inventory members of
    # their own, so the duplicate-path rule of ``_visual_handoff_rules`` cannot
    # see them, and both are names the owner may give his ``chat_statement`` or
    # his ``candidate_review``: the only condition on those bindings is the
    # workspace prefix.  Without this rule such a binding rehearses green,
    # the receipts are spent, the operation overwrites the owner's own file and
    # then reads it back as an inventory member - ``visual handoff file differs
    # from its descriptor``, raised in ``_bound_file`` behind the commit, with
    # handoff.json on disk and filled-review.md missing; and for the name
    # ``filled-review.md`` the operation does not even refuse, it returns a
    # handoff that every one of its own readers rejects from then on.  So: no
    # inventory member may sit on a path this operation writes, unless it is
    # bound to exactly the bytes that land there.  The payload members satisfy
    # that by construction - they *are* their own staged bytes - which leaves
    # the two paths above, and leaves the rehearsal a faithful one.
    #
    # The handoff is compared against the bytes it will hold rather than simply
    # forbidden, because the exception is the rule's whole content; that it is
    # unreachable in practice is a property of the document (its inventory
    # carries the digest of those very bytes, so the binding is a fixed point)
    # and not an assumption made here.  The filled review has no such bytes to
    # compare against yet - it is rendered from the snapshot read back after the
    # commit - so ``None`` stands for "nothing a member may be bound to".
    #
    # Both sides of that lookup are folded, because the question is which
    # *file* a binding names and not how it is spelled.  r1h asked it as a raw
    # string lookup and the r1g finding ran again one spelling further: an
    # owner who names his chat statement ``HANDOFF.JSON`` binds the file this
    # operation writes, the rule saw two paths where the file system sees one,
    # and the run burned exactly as before - while ``Filled-Review.md`` did not
    # even refuse.  That is register point 26 for the fourth time in this loop,
    # and the module states the fix in three docstrings of its own:
    # ``_fold_path`` is ``_names_protected_run``'s fold, applied here.
    written: dict[str, bytes | None] = {_fold_path(relative): raw for relative, raw in payloads}
    written[_fold_path(handoff_path)] = raw_document
    written[_fold_path(filled_review_path)] = None
    if any(_fold_path(item["path"]) in written and written[_fold_path(item["path"])] != raw for item, raw in members):
        raise DiscoveryError("visual handoff inventory member lies on a path this operation overwrites")
    _visual_handoff_rules(document, run_id, _staged_member_reader({item["path"]: raw for item, raw in members}, workspace + "/"), project)
    for relative, _ in (*payloads, (handoff_path, b""), (filled_review_path, b"")):
        _safe_write_target(project, relative)
    review_markdown({"document": document, "inventory": files, "progress": progress, "run_id": run_id, "handoff_kind": "visual-decisions", "resolved": _visual_resolved_values(project, document)})
    # Every refusal of this operation is now behind us, so this is the point at
    # which the operation is known to succeed and may pay for itself: the same
    # bundle is validated once more, this time consuming.  It runs under the
    # core's ledger lock, it is the only consuming call in this module, and it
    # is the same call this function has always made - only its position moved.
    # A bundle that changed between the two passes is refused here rather than
    # bound: the verification decided about the bytes that were on disk then,
    # and only an identical answer may be committed.
    committed = _validated_visual_bundle(project, run_id, moment, sidecar_root=sidecar, affinity_auth=descriptor)
    if committed != validated:
        raise DiscoveryError("candidate bundle changed between verification and commit")
    for relative, raw in payloads:
        _safe_write(project, relative, raw)
    _safe_write(project, handoff_path, raw_document)
    # Read back and re-decided from disk: the rehearsal proved the rules hold
    # for the bytes intended, this proves they hold for the bytes that landed.
    snapshot = handoff_snapshot(project, handoff_path, owner_key); filled = review_markdown(snapshot); _safe_write(project, filled_review_path, filled)
    return {"operation": "prepare-visual-confirmation", "state": "visual-confirmation-prepared", "selection_id": selection["selection_id"], "selection_statement_sha256": hashlib.sha256(statement_raw).hexdigest(), "brand_raw_preimage_sha256": hashlib.sha256(preimage).hexdigest(), "candidate_bundle_sha256": validated["candidate_set_sha256"], "candidate_review_sha256": hashlib.sha256(review_raw).hexdigest(), "visual_handoff": {"path": handoff_path, "size": len(canonical(document)), "sha256": hashlib.sha256(canonical(document)).hexdigest()}, "filled_review": {"path": filled_review_path, "size": len(filled), "sha256": hashlib.sha256(filled).hexdigest()}, "resolved_choices": resolved, "normative": False, "next_operation": "owner-review-then-confirm-or-correct"}


def _bound_sheet_review(project: Path, workspace: str, path: str, review_raw: bytes, bundle_raw: bytes) -> None:
    """The candidate review is the decision sheet rendered for the bound bundle.

    The owner decided that the static sheet is the byte-bound review artifact of
    the confirmation.  Until this rule the adapter only *refused* a few things
    as review - a path under ``playground/``, bytes carrying the playground
    marker or a ``<script`` opener - and otherwise bound any file of the
    workspace: a sanitized playground copy, an invented page, the brief, the
    sheet's own manifest, and a sheet the core really rendered, but for a bundle
    that has since been rebuilt.  In that last case the owner confirmed values
    no page he was shown had carried, and nothing needed to go wrong on his
    side: rebuilding the bundle after a correction without rendering the sheet
    again was enough.  So the binding is stated positively, and every statement
    has to hold:

    * **place** - the path is ``<workspace>/sheet/index.html``, compared
      component for component with that one spelling.  A second spelling of the
      same file (``SHEET/index.html`` on a folding file system) is refused, not
      resolved: the handoff carries exactly one name for the review, and every
      read of it goes through ``O_NOFOLLOW`` components, so the name cannot be
      redirected.  No prefix, suffix or substring test is involved;
    * **form** - ``sheet/manifest.json`` passes the core's
      ``validate_sheet_manifest``, which re-hashes every member of the sheet on
      disk, holds ``index.html`` to the sheet's closed document form and refuses
      a hard-linked member;
    * **bytes** - the manifest's ``index.html`` digest is the digest of the
      review bytes this caller binds, so the page the core validated is the page
      the handoff carries;
    * **provenance** - the manifest's ``candidate_set_sha256`` is the digest of
      the bound candidate set, its ``brief_sha256`` is the digest of the brief on
      disk and the brief the candidate set binds, and its
      ``affinity_observation_sha256`` is the digest of the observation on disk.
      A sheet rendered before the bundle, the brief or the observation changed
      is refused here, and its remedy is to render the sheet again.

    This replaces the playground blocklist rather than standing beside it.  Every
    path under ``playground/`` fails the place statement.  Every byte string the
    content half refused fails the form statement: the sheet's document form
    admits ``<`` only as the opener of a lowercase tag from a closed set that
    has no ``script`` - attribute values exclude ``<`` - so no ``<script`` in any
    ASCII case and no ``<!--`` comment, the playground marker's opener, survives
    ``validate_sheet_manifest``.  The blocklist also refused legitimate files
    that merely *mention* the word; those are no sheet and fail the place
    statement now, with a sentence that says what the review has to be.

    Decided twice, like every rule of the handoff: in
    ``prepare_visual_confirmation`` before anything is created or spent, and in
    ``_visual_handoff_rules`` for the rehearsal and for every reader of the
    handoff - ``handoff_snapshot`` and through it ``render_review``,
    ``make_owner_receipt`` and ``verify_owner_receipt``.

    What it does not decide: the manifest is a workspace file and carries no
    signature, so a writer inside the run workspace can forge a manifest and a
    page together (register point 19).  The filled review is the second
    statement against that case - it shows the values the owner confirms,
    resolved from the bound reports rather than from the sheet.
    """
    sheet = f"{workspace}/sheet"
    if _relative(path).parts != _relative(f"{sheet}/index.html").parts:
        raise DiscoveryError("candidate review is not the decision sheet of this run; only <workspace>/sheet/index.html can be reviewed")
    module, failure = _visual_core()
    hooks = tuple(getattr(module, name, None) for name in ("validate_sheet_manifest", "load_json_bytes", "canonical_json", "sha256_bytes"))
    if not all(callable(hook) for hook in hooks):
        raise DiscoveryError("visual sheet validator hook is unavailable")
    validate_sheet, parse, encode, digest = hooks
    manifest_raw, _ = secure_read(project, f"{sheet}/manifest.json")
    manifest = load_json_bytes(manifest_raw, "decision sheet manifest")
    brief_raw, _ = secure_read(project, f"{workspace}/visual-brief.json")
    observation_raw, _ = secure_read(project, f"{workspace}/affinity-observation.json")
    try:
        validate_sheet(manifest, project / sheet)
        brief_sha256 = digest(encode(parse(brief_raw, "visual brief")))
        observation_sha256 = digest(encode(parse(observation_raw, "Affinity observation")))
    except failure as exc:
        raise DiscoveryError(f"decision sheet is refused by the visual core: {exc}") from exc
    members = {item["path"]: item["sha256"] for item in manifest["files"]}
    if members.get("index.html") != hashlib.sha256(review_raw).hexdigest():
        raise DiscoveryError("candidate review differs from the decision sheet its manifest binds")
    if manifest["candidate_set_sha256"] != hashlib.sha256(bundle_raw).hexdigest():
        raise DiscoveryError("candidate review is a decision sheet of another candidate bundle; render the sheet again")
    bundle = load_json_bytes(bundle_raw, "visual candidate bundle")
    if manifest["brief_sha256"] != brief_sha256 or bundle.get("brief_sha256") != brief_sha256:
        raise DiscoveryError("candidate review is a decision sheet of another visual brief; render the sheet again")
    if manifest["affinity_observation_sha256"] != observation_sha256:
        raise DiscoveryError("candidate review is a decision sheet of another Affinity observation; render the sheet again")


#: The candidate ids a decision value names, per domain, and whether the field
#: holds one id or a list.
_VALUE_CANDIDATE_FIELDS = {"typography": ("font_candidate_ids", True), "code-font": ("code_font_id", False),
                           "logo": ("logo_candidate_ids", True), "lockup": ("lockup_candidate_ids", True)}


def _visual_resolved_values(project: Path, document: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """What each decision resolves to, in the words the owner checks.

    Architecture R3: the owner names ids, the interview resolves the concrete
    values, and the owner confirms exactly those.  The filled review used to
    show the candidate value only, and for typography and code font that value
    is a list of ids and a report digest - no family name.  A serif swapped
    under a rebuilt bundle was therefore invisible in the second confirmation
    too.  So every decision is resolved here into the values it stands for:

    * colour - every role and its colour, straight from the value;
    * typography and code font - id, class and family of each font the value
      names, read from the font report the value binds;
    * logo and lockup - id, variant, asset id and asset digest of each
      candidate the value names, read from the logo report the value binds.

    A report is read under the digest its decision value pins, and that value is
    pinned by the handoff, so what is shown is what the receipt will bind.  Any
    value that cannot be resolved refuses instead of being left out of the
    review.  The strings - family names from a font's ``name`` table above all -
    are owner-supplied; ``review_markdown`` shows them only as JSON strings
    inside a fenced block, never as Markdown.

    Called for the rehearsal in ``prepare_visual_confirmation`` and by
    ``handoff_snapshot``, so it refuses before the commit as well as after it.
    """
    workspace = document["workspace"]; resolved: dict[str, list[dict[str, str]]] = {}
    for decision in document["decisions"]:
        domain, value = decision["domain"], decision["candidate_value"]
        if not isinstance(value, dict):
            raise DiscoveryError("visual decision value cannot be resolved for the review")
        if domain == "color":
            roles = value.get("roles")
            if not isinstance(roles, dict) or not roles or not all(isinstance(role, str) and isinstance(colour, str) for role, colour in roles.items()):
                raise DiscoveryError("visual decision value cannot be resolved for the review")
            resolved[decision["id"]] = [{"rolle": role, "farbe": colour} for role, colour in sorted(roles.items())]
            continue
        binding = value.get("report")
        if not isinstance(binding, dict) or not isinstance(binding.get("path"), str) or not HEX64.fullmatch(str(binding.get("sha256", ""))):
            raise DiscoveryError("visual decision value binds no readable report")
        raw, _ = secure_read(project, f"{workspace}/{binding['path']}")
        if hashlib.sha256(raw).hexdigest() != binding["sha256"]:
            raise DiscoveryError("visual decision report differs from the value that binds it")
        report = load_json_bytes(raw, "visual decision report")
        listed = report.get("candidates") if isinstance(report, dict) else None
        candidates = {item.get("id"): item for item in listed if isinstance(item, dict)} if isinstance(listed, list) else {}
        field, many = _VALUE_CANDIDATE_FIELDS[domain]
        identifiers = value.get(field) if many else [value.get(field)]
        if not isinstance(identifiers, list) or not identifiers or any(not isinstance(identifier, str) or not isinstance(candidates.get(identifier), dict) for identifier in identifiers):
            raise DiscoveryError("visual decision names a candidate its report does not carry")
        rows: list[dict[str, str]] = []
        for identifier in identifiers:
            item = candidates[identifier]
            if domain in {"typography", "code-font"}:
                row = {"kandidat": identifier, "klasse": item.get("classification"), "familie": item.get("family")}
            else:
                asset = item.get("asset")
                row = {"kandidat": identifier, "variante": item.get("variant"), "asset": item.get("asset_id"),
                       "asset_sha256": asset.get("sha256") if isinstance(asset, dict) else None}
            if not all(isinstance(text, str) for text in row.values()):
                raise DiscoveryError("visual decision report lacks a value the review has to show")
            rows.append(row)
        resolved[decision["id"]] = rows
    return resolved


def _review_value_block(rows: list[dict[str, str]]) -> list[str]:
    """Resolved values as a fenced JSON list, one record per line.

    The records carry owner-supplied text, so none of it may reach Markdown as
    Markdown.  A fenced code block admits no inline markup, no HTML and no link;
    it ends only at a line that opens with a backtick fence after at most three
    spaces, and every line here opens with ``[``, ``]`` or two spaces and
    ``{``.  Inside, each value is a JSON string, so a quote, a backslash or a
    line break in a family name is an escape sequence and not a boundary.

    ``ensure_ascii=True`` closes the rest of that class.  A family name comes
    out of an owner-supplied ``name`` table and may carry U+2028, U+2029,
    U+0085 or a bidi override; those are not C0 controls, so the sheet's control
    rule lets them pass, and they are line breaks or direction switches for some
    readers while being ordinary content for others.  Escaped to ``\u2028`` the
    record is one line for every reader, and a name that reads right-to-left in
    a terminal cannot rearrange the record around it.
    """
    body = ["  " + json.dumps(row, ensure_ascii=True) + ("," if index < len(rows) - 1 else "") for index, row in enumerate(rows)]
    return ["```json", "[", *body, "]", "```"]


def _staged_member_reader(staged: dict[str, bytes], prefix: str) -> Callable[[dict[str, Any]], bytes]:
    """Resolve a handoff inventory member against bytes staged for writing.

    The counterpart of ``handoff_snapshot``'s reader, for the operation that
    builds the handoff rather than the one that reads it back.  It answers the
    same three descriptor refusals as ``_bound_file``, so the rehearsal reaches
    exactly the verdicts the post-commit read would reach.  A member the
    operation did not stage cannot occur - the inventory is built from the same
    list - and is refused rather than assumed.
    """
    def read(binding: dict[str, Any]) -> bytes:
        raw = staged.get(binding["path"])
        if raw is None:
            raise DiscoveryError("visual handoff file is not a staged member of this operation")
        return _bound_bytes(raw, binding, "visual handoff file", prefix=prefix)
    return read


def _visual_handoff_rules(document: dict[str, Any], run_id: str, open_member: Callable[[dict[str, Any]], bytes], project: Path) -> dict[str, bytes]:
    """Every rule a visual-decisions handoff answers, over the document alone.

    Until r1g these rules lived inside ``handoff_snapshot`` and therefore ran
    only against a handoff already on disk.  ``prepare_visual_confirmation``
    calls that function *after* the consuming validation, so a rule stated here
    used to refuse behind the commit: an owner who bound the ``chat_statement``
    to a file the inventory already lists - the candidate set or the candidate
    review - reached the duplicate-path rule below with the Affinity receipts
    already spent, a half-written handoff on disk and no way to correct the
    selection or re-render the sheet.  That is the burn-on-refusal class this
    module exists to close, one call level below where it was closed.

    Every rule here is a predicate over the document and over bytes the caller
    holds, so the operation that *builds* the handoff can decide all of them
    while the receipts are still unspent.  ``open_member`` is the one seam:
    ``handoff_snapshot`` resolves each inventory member by reading the file,
    the builder resolves it against the bytes it is about to write.  Both use
    the same descriptor rules and answer the same refusals, which is what makes
    the pre-commit pass a faithful rehearsal of the post-commit one rather than
    a second, weaker opinion.

    ``project`` is the root the decision sheet is read from.  The sheet is no
    inventory member and this operation never writes it, so both readers read
    it from the disk: the rehearsal and the read-back decide the review binding
    over the same files.
    """
    required = {"schema_version", "handoff_kind", "handoff_id", "run_id", "workspace", "selection_id", "brand_raw_preimage_sha256", "candidate_bundle_sha256", "candidate_review_sha256", "decisions", "outcomes", "recommendations", "sources", "files"}
    if set(document) != required or document.get("schema_version") != "brandkit-discovery-handoff.v2" or document.get("handoff_kind") != "visual-decisions" or document.get("run_id") != run_id or document.get("workspace") != f".brandkit/work/{run_id}/visual-decisions" or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", str(document.get("handoff_id", ""))):
        raise DiscoveryError("visual handoff is not closed V2 or differs from its canonical path")
    if not isinstance(document["decisions"], list) or any(not isinstance(item, dict) or set(item) != {"id", "domain", "candidate_id", "candidate_value", "candidate_value_sha256", "source_ids"} or item["domain"] not in VISUAL_DOMAINS or not isinstance(item["source_ids"], list) or not item["source_ids"] or hashlib.sha256(canonical(item["candidate_value"])).hexdigest() != item["candidate_value_sha256"] for item in document["decisions"]):
        raise DiscoveryError("visual handoff decision is invalid")
    if not isinstance(document["outcomes"], list) or any(not isinstance(item, dict) or set(item) != {"domain", "outcome"} or item["domain"] not in VISUAL_DOMAINS or item["outcome"] not in {"open", "not-applicable"} for item in document["outcomes"]):
        raise DiscoveryError("visual handoff outcome is invalid")
    domains = [item["domain"] for item in document["decisions"]] + [item["domain"] for item in document["outcomes"]]
    if len(domains) != len(set(domains)):
        raise DiscoveryError("visual handoff contains duplicate domains")
    if not isinstance(document["recommendations"], list) or any(not isinstance(item, dict) or set(item) != {"kind", "candidate_id", "rationale", "source_ids"} for item in document["recommendations"]):
        raise DiscoveryError("visual handoff recommendations are not closed")
    if not isinstance(document["sources"], list) or not document["sources"] or any(not isinstance(item, dict) or set(item) != {"id", "classification", "file"} or item["classification"] not in {"owner-supplied", "observed"} for item in document["sources"]) or document["sources"][0]["id"] != "owner-visual-selection" or document["sources"][0]["classification"] != "owner-supplied":
        raise DiscoveryError("visual handoff sources are not closed owner/observed records")
    inventory = document["files"]; opened: dict[str, bytes] = {}
    # Folded, for the reason ``_fold_path`` states: two inventory members whose
    # paths differ only in spelling are two members of one file on the file
    # systems this ships on, and the closure this rule asserts is about files.
    # Both member paths the owner controls - the chat statement and the
    # candidate review - reach this rule, and it is decided in the rehearsal,
    # so the refusal costs nothing.
    if len({_fold_path(str(item.get("path"))) for item in inventory if isinstance(item, dict)}) != len(inventory): raise DiscoveryError("visual handoff inventory is not closed")
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256", "role"} or item["role"] not in {"candidate-bundle", "candidate-review", "source", "evidence"}: raise DiscoveryError("visual handoff inventory item is invalid")
        opened[item["path"]] = open_member({key: item[key] for key in ("path", "size", "sha256")})
    roles = [item["role"] for item in inventory]
    if roles.count("candidate-bundle") != 1 or roles.count("candidate-review") != 1 or roles.count("source") < 1 or roles.count("evidence") < 1:
        raise DiscoveryError("visual handoff requires bundle, review, owner/candidate sources, and evidence")
    bundle_item = next(item for item in inventory if item["role"] == "candidate-bundle")
    review_item = next(item for item in inventory if item["role"] == "candidate-review")
    if bundle_item["sha256"] != document["candidate_bundle_sha256"] or review_item["sha256"] != document["candidate_review_sha256"]:
        raise DiscoveryError("visual handoff top-level hashes differ from inventory")
    _bound_sheet_review(project, document["workspace"], review_item["path"], opened[review_item["path"]], opened[bundle_item["path"]])
    source_paths = {item["path"] for item in inventory if item["role"] == "source"}
    if any(item["file"] not in source_paths for item in document["sources"]):
        raise DiscoveryError("visual handoff source differs from inventory")
    source_ids = {item.get("id") for item in document["sources"] if isinstance(item, dict)}
    if len(source_ids) != len(document["sources"]): raise DiscoveryError("visual handoff source ids are not unique")
    if any(set(item["source_ids"]) - source_ids for item in document["decisions"]): raise DiscoveryError("visual decision lacks a bound source")
    return opened


def handoff_snapshot(project_root: Path, handoff_path: str, owner_key: Path) -> dict[str, Any]:
    project = _exact_root(project_root, "project root")
    parts = _relative(handoff_path).parts
    if len(parts) != 5 or parts[:2] != (".brandkit", "work") or parts[3:] not in (("discovery", "handoff.json"), ("visual-decisions", "handoff.json")) or not RUN_ID.fullmatch(parts[2]):
        raise DiscoveryError("handoff path is not the canonical run-scoped path")
    _open_run_id(parts[2])
    run_id = parts[2]
    progress = read_progress(project, run_id, owner_key)
    if progress["open"] or progress["answer_turns"] < 2:
        raise DiscoveryError("interview frontier is incomplete; remain in grilling")
    handoff_raw, _ = secure_read(project, handoff_path)
    document = load_json_bytes(handoff_raw, "discovery handoff")
    if parts[3] == "visual-decisions":
        opened = _visual_handoff_rules(document, run_id, lambda binding: _bound_file(project, binding, "visual handoff file", prefix=document["workspace"] + "/"), project)
        inventory = document["files"]
        return {"project": project, "run_id": run_id, "handoff_raw": handoff_raw, "handoff_sha256": hashlib.sha256(handoff_raw).hexdigest(), "document": document, "inventory": inventory, "opened": opened, "progress": progress, "handoff_kind": "visual-decisions", "handoff_id": document["handoff_id"], "review_path": f".brandkit/work/{run_id}/visual-decisions/filled-review.md", "receipt_path": f".brandkit/owner-receipts/{run_id}/{document['handoff_id']}.json", "resolved": _visual_resolved_values(project, document)}
    jsonschema.Draft202012Validator(load_json_bytes(HANDOFF_SCHEMA_PATH.read_bytes(), "handoff schema")).validate(document)
    workspace = f".brandkit/work/{run_id}/discovery"
    if document["run_id"] != run_id or document["workspace"] != workspace:
        raise DiscoveryError("handoff identity differs from its canonical path")
    inventory = document["files"]
    expected_paths = [item["path"] for item in inventory]
    if len(expected_paths) != len(set(expected_paths)) or _workspace_paths(project, workspace, handoff_path) != sorted(expected_paths):
        raise DiscoveryError("discovery file inventory is not closed")
    roles = [item["role"] for item in inventory]
    if roles.count("context") != 1 or "adr" not in roles or "source" not in roles:
        raise DiscoveryError("discovery inventory requires one context, an ADR, and a source")
    opened: dict[str, bytes] = {}
    for item in inventory:
        path, role = item["path"], item["role"]
        relative = _relative(path)
        if not str(relative).startswith(workspace + "/"):
            raise DiscoveryError(f"discovery file escapes workspace: {path}")
        suffix = relative.relative_to(PurePosixPath(workspace)).as_posix()
        role_ok = ((role == "context" and suffix == "CONTEXT.md") or
                   (role == "adr" and suffix.startswith("docs/adr/") and suffix.endswith(".md")) or
                   (role == "source" and suffix.startswith("sources/") and "/" not in suffix.removeprefix("sources/")))
        if not role_ok:
            raise DiscoveryError(f"discovery file role/path mismatch: {path}")
        raw, info = secure_read(project, path)
        if info.st_size != item["size"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise DiscoveryError(f"discovery file differs from inventory: {path}")
        if role == "context":
            meaningful_markdown(raw, "# Context", "CONTEXT.md")
        if role == "adr":
            meaningful_markdown(raw, "# ADR", f"ADR {path}", prefix=True)
        if role == "source" and not raw.strip():
            raise DiscoveryError(f"discovery source is empty: {path}")
        opened[path] = raw
    sources = {item["id"]: item for item in document["sources"]}
    if len(sources) != len(document["sources"]):
        raise DiscoveryError("duplicate discovery source id")
    inventory_by_path = {item["path"]: item for item in inventory}
    if any(source["file"] not in inventory_by_path or inventory_by_path[source["file"]]["role"] != "source" for source in sources.values()):
        raise DiscoveryError("discovery source is not bound to a source file")
    decisions = document["decisions"]
    if len({item["id"] for item in decisions}) != len(decisions) or any(not item["source_ids"] or any(source not in sources for source in item["source_ids"]) for item in decisions):
        raise DiscoveryError("discovery decision lacks a bound source")
    if set(item["id"] for item in decisions) & set(item["id"] for item in document["recommendations"]):
        raise DiscoveryError("recommendation and decision ids overlap")
    return {
        "project": project, "run_id": run_id, "handoff_raw": handoff_raw,
        "handoff_sha256": hashlib.sha256(handoff_raw).hexdigest(), "document": document,
        "inventory": inventory, "opened": opened, "progress": progress, "handoff_kind": "discovery", "handoff_id": "discovery", "review_path": f".brandkit/work/{run_id}/review.md", "receipt_path": f".brandkit/owner-receipts/{run_id}.json",
    }


def review_markdown(snapshot: dict[str, Any]) -> bytes:
    document = snapshot["document"]
    sources = {item["id"]: item for item in document["sources"]}
    lines = ["# Brand Interview Review", "", f"Run: {snapshot['run_id']}", "", "## Entscheidungen"]
    for decision in document["decisions"]:
        if snapshot["handoff_kind"] == "visual-decisions":
            lines += [f"### {decision['domain']}: {decision['candidate_id']}", "Bestätigt werden diese Werte:", *_review_value_block(snapshot["resolved"][decision["id"]]),
                      "", "Gebundener Kandidatenwert:", "```json", json.dumps(decision["candidate_value"], ensure_ascii=False, sort_keys=True, indent=2), "```", f"Wert-SHA-256: {decision['candidate_value_sha256']}", "", "Quellen:"]
        else:
            lines += [f"### {decision['id']}", decision["value"], "", "Quellen:"]
        for source_id in decision["source_ids"]:
            source = sources[source_id]
            lines.append(f"- {source_id} ({source['classification']}): `{source['file']}`")
        lines.append("")
    progress = snapshot["progress"]
    lines += [f"Antwort-Turns: {progress['answer_turns']}", f"Fortschrittskette: {progress['chain_id']}", f"Aktueller Head: {progress['head_sha256']}", ""]
    if snapshot["handoff_kind"] == "visual-decisions":
        lines += ["## Offene oder nicht anwendbare visuelle Punkte", *([f"- {item['domain']}: {item['outcome']}" for item in document["outcomes"]] or ["- keine"]), "", "## Unbestätigte Empfehlungen"]
        # The rationale is prose an agent wrote, and it used to stand as raw
        # Markdown under its own heading.  A rationale may therefore spell a
        # second, deceptively real "Bestätigt werden diese Werte:" list naming
        # another family; what is confirmed and consumed is still the real block
        # above, but the owner reads both and cannot tell them apart.  As a JSON
        # string inside the same fenced block the whole rationale is one value:
        # no heading, no list, no fence of its own.
        lines += (_review_value_block([{"kandidat": str(item.get("candidate_id", "unbekannt")), "begruendung": str(item.get("rationale", ""))}
                                       for item in document["recommendations"]]) if document["recommendations"] else ["- keine"])
    else:
        lines += ["## Explizit offene Punkte", *([f"- {item}" for item in progress["explicit_open"]] or ["- keine"]), "", "## Unbestätigte Empfehlungen"]
        lines += [*(f"- {item['id']}: {item['text']}" for item in document["recommendations"])] or ["- keine"]
    lines += ["", "## Dokumente"]
    lines += [f"- [{item['role']}] `{item['path']}`" for item in snapshot["inventory"] if item["role"] in {"context", "adr"}]
    lines += ["", "Diese Entscheidungen bestätigen? Korrekturen?", ""]
    return "\n".join(lines).encode("utf-8")


def render_review(project_root: Path, handoff_path: str, owner_key: Path) -> dict[str, Any]:
    snapshot = handoff_snapshot(project_root, handoff_path, owner_key)
    raw = review_markdown(snapshot)
    relative = snapshot["review_path"]
    _safe_write(snapshot["project"], relative, raw)
    return {"path": relative, "sha256": hashlib.sha256(raw).hexdigest(), "markdown": raw.decode("utf-8")}


def verified_review(snapshot: dict[str, Any], review_path: str) -> tuple[bytes, str]:
    expected = snapshot["review_path"]
    if review_path != expected:
        raise DiscoveryError("review path is not canonical")
    raw, _ = secure_read(snapshot["project"], review_path)
    if raw != review_markdown(snapshot):
        raise DiscoveryError("review differs from current verified handoff")
    return raw, hashlib.sha256(raw).hexdigest()


def _project_identity(project: Path) -> str:
    return hashlib.sha256((str(project) + "\n").encode("utf-8")).hexdigest()


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def _derive_key(key: bytes, purpose: bytes) -> bytes:
    return hmac.new(key, b"brandkit-discovery-subkey-v1\0" + purpose, hashlib.sha256).digest()


def read_owner_key(owner_key: Path) -> bytes:
    """Read an owner-only regular key without following a symlink."""
    absolute = Path(os.path.abspath(owner_key))
    parent = _exact_root(absolute.parent, "owner key directory")
    try:
        raw, info = secure_read(parent, absolute.name)
    except DiscoveryError as exc:
        raise DiscoveryError(f"owner key is unsafe: {exc}") from exc
    if info.st_mode & 0o077:
        raise DiscoveryError("owner key must not grant group or other permissions")
    if info.st_uid != os.geteuid():
        raise DiscoveryError("owner key must be owned by the current user")
    if len(raw) < 32:
        raise DiscoveryError("owner key must contain at least 32 bytes")
    return raw


def _external_owner_key(project: Path, owner_key: Path) -> bytes:
    """The owner key, refused if it lies inside the project - judged by identity.

    The same question ``_outside_project`` answers, and until r1i it answered it
    with the lexical half alone: ``project in resolved.parents`` is a text test,
    and ``os.path.realpath`` does not fold the case of the components it
    returns.  On the file systems this ships on, a key at
    ``<project>/owner.key`` handed over as ``<Project>/owner.key`` is therefore
    not textually below the project and is the very same file - measured, not
    argued.  The rule is what keeps the progress ledger and the owner receipt
    anchored outside the tree the run itself may write, so answering it lexically
    lets exactly the writer this rule protects against hold the key.

    Register point 26, in ``_outside_project`` since r1d and here only since
    r1i: the identity disjunct is the same one, spelled the same way.

    A hard link is the third spelling of the same trick, and r1i copied the
    identity disjunct without it.  Both of the tests above answer "outside" for
    a second *name* of a file whose bytes live in the project: every component
    of the external name is genuinely outside, and ``realpath`` has nothing to
    resolve.  Unlike the Affinity key and the replay ledger, which the core
    re-reads through ``_external_0600`` (``st_nlink == 1``), the discovery owner
    key never reaches the core - this function is the only gate, so a missing
    link count here is not one layer of two, it is none.  The writer inside the
    project does not need an owner mistake to use it: it already knows the key's
    path, ``link()`` needs no read permission on the source, and afterwards the
    owner keeps calling exactly as before.  Register point 32, reopened in r1j:
    the question "inside or outside" is answered by the full triad - text,
    identity, link count - or it is not answered.  As in ``_outside_project``
    the link count is asked of regular files only.
    """
    absolute = Path(os.path.abspath(owner_key)); resolved = absolute.resolve(strict=True)
    if resolved == project or project in resolved.parents or _identity_contains(project, absolute):
        raise DiscoveryError("owner key and progress ledger must be outside the project")
    try:
        info = os.stat(resolved)
    except OSError as exc:
        raise DiscoveryError("owner key is absent or unreadable") from exc
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise DiscoveryError("owner key must be an exclusive external file")
    return read_owner_key(absolute)


def make_owner_receipt(project_root: Path, handoff_path: str, review_path: str, owner_key: Path, issuer: str, confirmation_id: str, confirmation_statement: str, ttl: int, now: int | None = None) -> dict[str, Any]:
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._@:-]{0,127}", issuer) or ttl < 1 or ttl > 86400
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,127}", confirmation_id)
            or confirmation_statement != "Diese Entscheidungen bestätige ich."):
        raise DiscoveryError("owner receipt issuer/ttl is invalid")
    snapshot = handoff_snapshot(project_root, handoff_path, owner_key)
    _review_raw, review_sha256 = verified_review(snapshot, review_path)
    key = _derive_key(_external_owner_key(snapshot["project"], owner_key), b"owner-receipt")
    issued = int(time.time()) if now is None else now
    receipt = {
        "schema_version": "brandkit-discovery-owner-receipt.v1", "issuer": issuer,
        "handoff_kind": snapshot["handoff_kind"], "handoff_id": snapshot["handoff_id"],
        "key_id": _key_id(key), "project_realpath_sha256": _project_identity(snapshot["project"]),
        "run_id": snapshot["run_id"], "handoff_sha256": snapshot["handoff_sha256"],
        "review_sha256": review_sha256, "confirmation_id": confirmation_id, "confirmation_statement": confirmation_statement,
        "progress_chain_id": snapshot["progress"]["chain_id"], "progress_head_sha256": snapshot["progress"]["head_sha256"], "progress_turn": snapshot["progress"]["answer_turns"],
        "decisions": snapshot["document"]["decisions"], "source_inventory": [item for item in snapshot["inventory"] if item["role"] == "source"],
        "artifact_inventory": [item for item in snapshot["inventory"] if item["role"] != "source"],
        "nonce": secrets.token_hex(32), "issued_at": issued, "expires_at": issued + ttl,
        "algorithm": "HMAC-SHA256",
    }
    receipt["hmac_sha256"] = hmac.new(key, canonical(receipt), hashlib.sha256).hexdigest()
    return receipt


def verify_owner_receipt(project_root: Path, handoff_path: str, receipt_path: str, owner_key: Path, now: int | None = None) -> dict[str, Any]:
    snapshot = handoff_snapshot(project_root, handoff_path, owner_key)
    _review_raw, review_sha256 = verified_review(snapshot, snapshot["review_path"])
    expected_receipt = snapshot["receipt_path"]
    if receipt_path != expected_receipt:
        raise DiscoveryError("owner receipt path is not canonical for the run")
    raw, _ = secure_read(snapshot["project"], receipt_path)
    receipt = load_json_bytes(raw, "owner receipt")
    jsonschema.Draft202012Validator(load_json_bytes(RECEIPT_SCHEMA_PATH.read_bytes(), "receipt schema")).validate(receipt)
    key = _derive_key(_external_owner_key(snapshot["project"], owner_key), b"owner-receipt")
    signature = receipt.pop("hmac_sha256")
    if receipt["key_id"] != _key_id(key) or not hmac.compare_digest(signature, hmac.new(key, canonical(receipt), hashlib.sha256).hexdigest()):
        raise DiscoveryError("owner receipt authentication failed")
    instant = int(time.time()) if now is None else now
    if not receipt["issued_at"] <= instant < receipt["expires_at"]:
        raise DiscoveryError("owner receipt is expired or not yet valid")
    if receipt["expires_at"] <= receipt["issued_at"] or receipt["expires_at"] - receipt["issued_at"] > 86400:
        raise DiscoveryError("owner receipt validity interval is invalid")
    expected = {
        "project_realpath_sha256": _project_identity(snapshot["project"]), "run_id": snapshot["run_id"],
        "handoff_kind": snapshot["handoff_kind"], "handoff_id": snapshot["handoff_id"],
        "handoff_sha256": snapshot["handoff_sha256"], "decisions": snapshot["document"]["decisions"],
        "review_sha256": review_sha256,
        "progress_chain_id": snapshot["progress"]["chain_id"], "progress_head_sha256": snapshot["progress"]["head_sha256"], "progress_turn": snapshot["progress"]["answer_turns"],
        "source_inventory": [item for item in snapshot["inventory"] if item["role"] == "source"],
        "artifact_inventory": [item for item in snapshot["inventory"] if item["role"] != "source"],
    }
    if any(receipt[key_name] != value for key_name, value in expected.items()):
        raise DiscoveryError("owner receipt binding differs from current handoff")
    sources = []
    artifacts = []
    source_by_path = {item["file"]: item for item in snapshot["document"]["sources"]}
    for path, content in snapshot["opened"].items():
        if path in source_by_path:
            descriptor = source_by_path[path]
            sources.append({"id": descriptor["id"], "classification": descriptor["classification"], "path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest(), "utf8_base64": base64.b64encode(content).decode("ascii")})
        else:
            role = next(item["role"] for item in snapshot["inventory"] if item["path"] == path)
            artifacts.append({"path": path, "role": role, "sha256": hashlib.sha256(content).hexdigest(), "utf8_base64": base64.b64encode(content).decode("ascii")})
    output_ids = [item["id"] for item in sources]
    required_ids = {source for decision in snapshot["document"]["decisions"] for source in decision["source_ids"]}
    if len(output_ids) != len(set(output_ids)) or required_ids - set(output_ids):
        raise DiscoveryError("consumed decision source ids do not resolve uniquely to verified source bytes")
    result = {"schema_version": "brandkit-discovery-consumption.v1", "run_id": snapshot["run_id"], "handoff_sha256": snapshot["handoff_sha256"], "decisions": snapshot["document"]["decisions"], "sources": sources, "artifacts": artifacts}
    if snapshot["handoff_kind"] == "visual-decisions":
        receipt_sha256 = hashlib.sha256(raw).hexdigest()
        result.update(handoff_kind="visual-decisions", receipt_sha256=receipt_sha256, brand_raw_block=visual_brand_raw_block(snapshot["document"]["decisions"], snapshot["document"]["outcomes"], snapshot["handoff_sha256"], receipt_sha256))
    return result


def visual_brand_raw_block(decisions: list[dict[str, Any]], outcomes: list[dict[str, str]], handoff_sha256: str, receipt_sha256: str) -> str:
    lines = ["<!-- brandkit:visual-decisions:v1 -->", "## Bestätigte visuelle Entscheidungen", "", f"Handoff-SHA-256: `{handoff_sha256}`", f"Receipt-SHA-256: `{receipt_sha256}`", ""]
    for item in sorted(decisions, key=lambda value: value["domain"]):
        lines += [f"### {item['domain']}", f"Candidate: `{item['candidate_id']}`", f"Value-SHA-256: `{item['candidate_value_sha256']}`", "```json", json.dumps(item["candidate_value"], ensure_ascii=False, sort_keys=True, indent=2), "```", ""]
    for item in sorted(outcomes, key=lambda value: value["domain"]): lines.append(f"- {item['domain']}: TODO(owner) — {item['outcome']}")
    return "\n".join(lines + ["", "<!-- /brandkit:visual-decisions:v1 -->", ""])


def write_owner_receipt(project_root: Path, relative: str, receipt: dict[str, Any]) -> None:
    _open_run_id(str(receipt.get("run_id", "")), "owner receipt run id")
    project = _exact_root(project_root, "project root")
    expected = (f".brandkit/owner-receipts/{receipt['run_id']}/{receipt['handoff_id']}.json" if receipt.get("handoff_kind") == "visual-decisions" else f".brandkit/owner-receipts/{receipt['run_id']}.json")
    if relative != expected:
        raise DiscoveryError("owner receipt output path is not canonical")
    parent = project / PurePosixPath(relative).parent
    current = project
    for part in PurePosixPath(relative).parent.parts:
        current = current / part
        try: current.mkdir(mode=0o700)
        except FileExistsError: pass
        if current.is_symlink() or not current.is_dir() or current.resolve(strict=True) != current:
            raise DiscoveryError("owner receipt directory is unsafe")
    _safe_write(project, relative, canonical(receipt))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    bundle = sub.add_parser("check-bundle"); bundle.add_argument("--bundle-root", required=True, type=Path)
    prepare = sub.add_parser("prepare"); prepare.add_argument("--project-root", required=True, type=Path); prepare.add_argument("--bundle-root", required=True, type=Path); prepare.add_argument("--run-id", required=True); prepare.add_argument("--owner-key", required=True, type=Path)
    visual = sub.add_parser("prepare-visual-confirmation"); visual.add_argument("--project-root", required=True, type=Path); visual.add_argument("--run-id", required=True); visual.add_argument("--owner-key", required=True, type=Path); visual.add_argument("--state", required=True); visual.add_argument("--sidecar-root", type=Path); visual.add_argument("--affinity-auth", type=Path)
    progress = sub.add_parser("progress"); progress.add_argument("--project-root", required=True, type=Path); progress.add_argument("--run-id", required=True); progress.add_argument("--answer-sha256", required=True); progress.add_argument("--owner-key", required=True, type=Path); progress.add_argument("--open-axis", action="append", default=[]); progress.add_argument("--explicit-open-axis", action="append", default=[]); progress.add_argument("--covered-axis", action="append", default=[])
    review = sub.add_parser("render-review"); review.add_argument("--project-root", required=True, type=Path); review.add_argument("--handoff", required=True); review.add_argument("--owner-key", required=True, type=Path)
    confirm = sub.add_parser("confirm"); confirm.add_argument("--project-root", required=True, type=Path); confirm.add_argument("--handoff", required=True); confirm.add_argument("--review", required=True); confirm.add_argument("--owner-key", required=True, type=Path); confirm.add_argument("--receipt", required=True); confirm.add_argument("--issuer", required=True); confirm.add_argument("--confirmation-id", required=True); confirm.add_argument("--confirmation-statement", required=True); confirm.add_argument("--ttl", required=True, type=int)
    consume = sub.add_parser("consume"); consume.add_argument("--project-root", required=True, type=Path); consume.add_argument("--handoff", required=True); consume.add_argument("--owner-receipt", required=True); consume.add_argument("--owner-key", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "check-bundle":
            result = verify_bundle_checkout(args.bundle_root)
        elif args.command == "prepare":
            result = prepare_orchestration(args.project_root, args.bundle_root, args.run_id, args.owner_key)
        elif args.command == "prepare-visual-confirmation":
            project = _exact_root(args.project_root, "project root"); state_raw, _ = secure_read(project, args.state)
            affinity_auth = None
            if args.affinity_auth is not None:
                # The descriptor names external secrets, so it is read from an
                # operator-named absolute path *outside* the project rather than
                # from the run workspace the request state lives in.
                descriptor_path = _outside_project(project, args.affinity_auth, "Affinity authentication descriptor")
                descriptor_parent = _exact_root(descriptor_path.parent, "Affinity authentication directory")
                affinity_auth = load_json_bytes(secure_read(descriptor_parent, descriptor_path.name)[0], "Affinity authentication descriptor")
            result = prepare_visual_confirmation(project, args.run_id, args.owner_key, load_json_bytes(state_raw, "visual confirmation state"), sidecar_root=args.sidecar_root, affinity_auth=affinity_auth)
        elif args.command == "progress":
            result = write_progress(args.project_root, args.run_id, args.open_axis, args.explicit_open_axis, args.covered_axis, args.answer_sha256, args.owner_key)
        elif args.command == "render-review":
            result = render_review(args.project_root, args.handoff, args.owner_key)
            print(result["markdown"], end="")
            return 0
        elif args.command == "confirm":
            result = make_owner_receipt(args.project_root, args.handoff, args.review, args.owner_key, args.issuer, args.confirmation_id, args.confirmation_statement, args.ttl)
            write_owner_receipt(args.project_root, args.receipt, result)
        else:
            result = verify_owner_receipt(args.project_root, args.handoff, args.owner_receipt, args.owner_key)
        print(canonical(result).decode("utf-8"), end="")
        return 0
    except (DiscoveryError, OSError, jsonschema.ValidationError, subprocess.SubprocessError) as exc:
        print(f"brandkit discovery: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
