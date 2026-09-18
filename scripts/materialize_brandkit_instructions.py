#!/usr/bin/env python3
"""Materialize one canonical project instruction source for Claude and Hermes."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "skills/brandkit-build/assets/AGENTS.md.template"
HERMES_ROOT = Path.home() / ".hermes/hermes-agent"
HERMES_PYTHON = HERMES_ROOT / "venv/bin/python"
HERMES_VERSION = "0.20.5"
HERMES_REVISION = "1bbb6e5b"


def _clean_git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        environment.pop(name, None)
    return environment


def _lstat_parent_chain(path: Path) -> None:
    absolute = path.absolute()
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor = cursor / part
        mode = cursor.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"symlink in project-root path component chain: {cursor}")
        if cursor != absolute and not stat.S_ISDIR(mode):
            raise ValueError(f"non-directory in project-root parent chain: {cursor}")


def _git_root(path: Path) -> Path:
    completed = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        text=True, capture_output=True, env=_clean_git_environment(),
    )
    if completed.returncode:
        raise ValueError("project-root must be an existing Git repository root")
    return Path(completed.stdout.strip()).resolve(strict=True)


def _lstat_regular(path: Path, label: str) -> None:
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ValueError(f"{label} must be a non-symlink regular file")


def _check_target(root: Path, path: Path, *, claude: bool = False) -> None:
    if path.parent != root:
        raise ValueError("instruction target escapes Git root")
    if not (path.exists() or path.is_symlink()):
        return
    mode = path.lstat().st_mode
    if claude and stat.S_ISLNK(mode) and os.readlink(path) == "AGENTS.md":
        return
    if stat.S_ISLNK(mode):
        raise ValueError(f"{path.name} symlink is not allowed")
    if not stat.S_ISREG(mode):
        raise ValueError(f"{path.name} must be a regular file")
    if claude:
        raise ValueError("CLAUDE.md exists but is not the exact AGENTS.md symlink")


def _check_priority_sources(project_root: Path) -> None:
    forbidden = {".hermes.md", "HERMES.md", "AGENTS.override.md"}
    present = sorted(entry.name for entry in project_root.iterdir() if entry.name in forbidden)
    divergent_agents = sorted(
        entry.name
        for entry in project_root.iterdir()
        if entry.name.casefold() == "agents.md" and entry.name != "AGENTS.md"
    )
    present.extend(divergent_agents)
    if present:
        raise ValueError(f"Hermes shadowing/divergent instruction source: {', '.join(present)}")


def _atomic_bytes(path: Path, payload: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _reserve_path(parent: Path, name: str) -> Path:
    descriptor, value = tempfile.mkstemp(prefix=f".{name}.", dir=parent)
    os.close(descriptor)
    path = Path(value)
    path.unlink()
    return path


def _parse_hermes_version(text: str) -> tuple[int, int, int] | None:
    """Die erste ``vMAJOR.MINOR.PATCH``-Angabe eines Versionstextes.

    Gelesen wird die Zahl, nicht die Zeichenkette: Ein Vergleich per Teilstring
    kann ``v0.20.5`` nicht von ``v0.20.50`` unterscheiden und ordnet gar nichts.
    """
    found = re.search(r"\bv(\d+)\.(\d+)\.(\d+)\b", text)
    if not found:
        return None
    return (int(found.group(1)), int(found.group(2)), int(found.group(3)))


def _verify_hermes_installation() -> None:
    """Die installierte Hermes-Fassung muss die Prompt-Auswahl tragen können.

    Geprüft wird eine MINDESTVERSION innerhalb derselben Hauptversion, nicht
    Zeichengleichheit mit einem eingefrorenen Commit. Der frühere Vergleich
    (``f"v{HERMES_VERSION}" not in version_text or HERMES_REVISION not in
    version_text``) sperrte jede neuere Fassung aus - gemessen an v0.20.6, die
    ``build_context_files_prompt`` unverändert bereitstellt und die vorgeschlagene
    ``AGENTS.md`` korrekt auswählt. Ein Werkzeug, das sich bei jedem Patch des
    überprüften Programms selbst aussperrt, prüft nicht mehr, es blockiert nur.

    Was die Strenge trägt, bleibt: Eine ältere Fassung wird abgewiesen, weil ihre
    Prioritätskette nicht zugesichert ist, und eine fremde Hauptversion ebenso,
    weil dort mit brechenden Änderungen zu rechnen ist. Die eigentliche Zusage
    wird ohnehin nicht aus der Versionsnummer abgeleitet, sondern in
    ``_verify_effective_prompt`` am echten ``prompt_builder`` gemessen: Dort muss
    die vorgeschlagene Datei im effektiven Prompt wirklich auftauchen.

    Die beobachtete Revision wird weiterhin gelesen, aber als Angabe geführt und
    nicht als Bedingung - sie gehört in die Fehlermeldung, damit eine
    Abweichung nachvollziehbar bleibt.
    """
    binary = shutil.which("hermes")
    if not binary:
        raise ValueError("installed Hermes executable is unavailable")
    version = subprocess.run([binary, "--version"], text=True, capture_output=True, env=_clean_git_environment())
    version_text = version.stdout + version.stderr
    if version.returncode:
        raise ValueError("installed Hermes does not report its version")
    observed = _parse_hermes_version(version_text)
    minimum = _parse_hermes_version(f"v{HERMES_VERSION}")
    if observed is None or minimum is None:
        raise ValueError(f"installed Hermes reports no readable version: {version_text.strip()!r}")
    if observed[0] != minimum[0] or observed < minimum:
        raise ValueError(
            f"installed Hermes {observed[0]}.{observed[1]}.{observed[2]} is outside the supported range "
            f"(>= {HERMES_VERSION}, same major); reference upstream {HERMES_REVISION}"
        )
    if not HERMES_PYTHON.is_file() or not (HERMES_ROOT / "agent/prompt_builder.py").is_file():
        raise ValueError("installed Hermes prompt_builder is unavailable")


def _verify_effective_prompt(project_root: Path, expected: bytes) -> None:
    expected_text = expected.decode("utf-8")
    probe = (
        "import sys; "
        f"sys.path.insert(0, {str(HERMES_ROOT)!r}); "
        "from agent.prompt_builder import build_context_files_prompt; "
        f"print(build_context_files_prompt(cwd={str(project_root)!r}, skip_soul=True))"
    )
    completed = subprocess.run(
        [str(HERMES_PYTHON), "-c", probe],
        text=True, capture_output=True, env=_clean_git_environment(),
    )
    if completed.returncode:
        raise ValueError(f"Hermes prompt_builder verification failed: {completed.stderr.strip()}")
    if "## AGENTS.md" not in completed.stdout or expected_text.strip() not in completed.stdout:
        raise ValueError("Hermes effective prompt does not select the proposed AGENTS.md")


def _shadow_prompt_preflight(payload: bytes) -> None:
    with tempfile.TemporaryDirectory(prefix="brandkit-hermes-shadow-") as temporary:
        shadow = Path(temporary).resolve(strict=True)
        (shadow / "AGENTS.md").write_bytes(payload)
        (shadow / "CLAUDE.md").symlink_to("AGENTS.md")
        _verify_effective_prompt(shadow, payload)


def _rollback_target(path: Path, backup: Path, backed_up: bool, installed: bool) -> None:
    if installed and (path.exists() or path.is_symlink()):
        path.unlink()
    if backed_up:
        os.replace(backup, path)


def _install_transaction(project_root: Path, payload: bytes) -> None:
    agents = project_root / "AGENTS.md"
    claude = project_root / "CLAUDE.md"
    staged_agents = _reserve_path(project_root, "AGENTS.md.stage")
    staged_claude = _reserve_path(project_root, "CLAUDE.md.stage")
    backup_agents = _reserve_path(project_root, "AGENTS.md.preimage")
    backup_claude = _reserve_path(project_root, "CLAUDE.md.preimage")
    agents_existed = agents.exists()
    claude_existed = claude.exists() or claude.is_symlink()
    agents_backed_up = False
    claude_backed_up = False
    agents_installed = False
    claude_installed = False
    try:
        _atomic_bytes(staged_agents, payload)
        staged_claude.symlink_to("AGENTS.md")
        if agents_existed:
            os.replace(agents, backup_agents)
            agents_backed_up = True
        os.replace(staged_agents, agents)
        agents_installed = True
        if claude_existed:
            os.replace(claude, backup_claude)
            claude_backed_up = True
        os.replace(staged_claude, claude)
        claude_installed = True
        if (
            not claude.is_symlink()
            or os.readlink(claude) != "AGENTS.md"
            or claude.resolve(strict=True) != agents.resolve(strict=True)
            or agents.read_bytes() != payload
            or claude.read_bytes() != payload
        ):
            raise ValueError("materialized Claude bridge is not byte-identical to AGENTS.md")
        _verify_hermes_installation()
        _verify_effective_prompt(project_root, payload)
    except BaseException:
        rollback_errors: list[Exception] = []
        try:
            _rollback_target(claude, backup_claude, claude_backed_up, claude_installed)
        except Exception as error:
            rollback_errors.append(error)
        try:
            _rollback_target(agents, backup_agents, agents_backed_up, agents_installed)
        except Exception as error:
            rollback_errors.append(error)
        if rollback_errors:
            raise RuntimeError(f"instruction transaction rollback failed: {rollback_errors}") from rollback_errors[0]
        raise
    finally:
        for path in (staged_agents, staged_claude, backup_agents, backup_claude):
            if path.exists() or path.is_symlink():
                path.unlink()


def materialize(project_root: Path, agents_source: Path | None = None) -> None:
    if not project_root.exists() or project_root.is_symlink() or not project_root.is_dir():
        raise ValueError("project-root must be an existing non-symlink directory")
    _lstat_parent_chain(project_root)
    project_root = project_root.resolve(strict=True)
    if _git_root(project_root) != project_root:
        raise ValueError("project-root must be the actual Git root, not a subdirectory")
    agents = project_root / "AGENTS.md"
    claude = project_root / "CLAUDE.md"
    source = agents_source or TEMPLATE
    _lstat_regular(source, "agents-source")
    _check_target(project_root, agents)
    _check_target(project_root, claude, claude=True)
    _check_priority_sources(project_root)
    payload = source.read_bytes()
    payload.decode("utf-8")
    _verify_hermes_installation()
    _shadow_prompt_preflight(payload)
    _install_transaction(project_root, payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--agents-source", type=Path)
    args = parser.parse_args()
    try:
        materialize(args.project_root, args.agents_source)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    print(f"AGENTS.md={args.project_root / 'AGENTS.md'}")
    print("CLAUDE.md -> AGENTS.md")
    print("Hermes: launch with cwd=project root; AGENTS.md is the selected project context")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
