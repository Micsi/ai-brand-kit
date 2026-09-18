#!/usr/bin/env python3
"""Fail-closed primitives for non-normative BrandKit visual comparisons.

This module validates candidate evidence and builds a static, offline comparison
sheet.  It deliberately has no command that confirms a choice or modifies an
interview handoff / brand package.
"""

from __future__ import annotations

import argparse
import fcntl
from fractions import Fraction
import hashlib
import hmac
import html
from html.parser import HTMLParser
from io import BytesIO
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import time
import types
from typing import Any
import unicodedata
import xml.etree.ElementTree as ET

from brand_package import ContractError as BrandPackageError
from brand_package import validate_schema as validate_brand_schema
from brand_package import validate_svg, validate_woff2
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas"

# --- ASCII semantics, wherever a specification says ASCII ---------------------
#
# Python's string methods are *Unicode* methods, and every specification this
# module models - HTML5, css-syntax-3, SVG/XML - is defined over **ASCII**.  The
# two differ, and every one of the differences is a place where this apparatus
# and the browser read one document as two:
#
# * ``str.strip()`` strips 29 code points.  HTML5 (2.4.1 "ASCII whitespace")
#   knows exactly five: TAB, LF, FF, CR, SPACE.  The other 24 - U+0085, U+00A0,
#   U+1680, U+2000-200A, U+2028, U+2029, U+202F, U+205F, U+3000 and the C1-range
#   separators - are ordinary *character* tokens to Blink.  A single U+00A0
#   between the doctype and ``<html>``, or anywhere inside ``<head>``, was
#   "empty text, ignore" to this scanner and a character token to Blink, which
#   closes ``<head>`` implicitly there; the CSP ``<meta>`` then landed in
#   ``<body>``, where a meta-delivered policy is ignored outright.  Validator
#   ACCEPTED, ``cspMetas[0].inHead: false``, and the sheet fetched across
#   origins exactly like the policy-free control.
# * ``re``'s ``\s`` on ``str`` is that same 29-code-point set, so the two tag
#   grammars below matched ``</p >`` as an end tag for ``p`` while Blink
#   reads the tag name ``p ``, finds no such element and *ignores* the end
#   tag - the element stays open and the document is a different tree.
# * ``str.lower()`` folds U+212A KELVIN SIGN onto ``k`` (it is the only
#   non-ASCII code point in all of Unicode whose lowercase is pure ASCII), and
#   the Turkish ``I`` grows a combining dot.  CSS keyword and ident matching,
#   the HTML pragma and relation allowlists and the SVG paint keywords are all
#   *ASCII* case-insensitive; nothing in them may fold a non-ASCII code point
#   into an ASCII one.
# * ``str.split()`` without an argument splits on that same wider set, so an
#   SVG ``viewBox`` with a U+00A0 in it counted four numbers here and three in
#   the renderer.
#
# The answer is not sixteen local fixes but four named helpers, used at every
# site where a specification says ASCII.  Three kinds of site are deliberately
# *not* converted, and each says why where it stands: the file-system name fold
# (:func:`_names_protected_run`), which must follow the file system rather than
# a specification; splits on an explicit separator (``split(b"\0")``,
# ``split("}")``), which have no Unicode semantics to begin with; and the two
# *denylist* folds, where the wider Unicode fold can only ever match more and
# is therefore kept **beside** the ASCII one rather than replaced by it.
ASCII_WHITESPACE = " \t\n\r\f"
# XML 1.0 2.3 S: space, tab, CR, LF - no form feed, which is not even a legal
# XML character, so a well-formed SVG attribute cannot contain one.
XML_WHITESPACE = " \t\n\r"
_ASCII_LOWER_MAP = {ord(char): ord(char) + 32 for char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
_ASCII_SPACE_CHARS = re.escape(ASCII_WHITESPACE)  # the five, safe inside a character class
_ASCII_SPACE = f"[{_ASCII_SPACE_CHARS}]"


def ascii_lower(text: str) -> str:
    """Lowercase the 26 ASCII letters and nothing else.

    ``str.lower()`` would additionally fold U+212A onto ``k`` and turn ``İ``
    into two code points, which is precisely what an ASCII case-insensitive
    match in HTML5 or css-syntax-3 must not do.
    """
    return text.translate(_ASCII_LOWER_MAP)


def ascii_strip(text: str, whitespace: str = ASCII_WHITESPACE) -> str:
    """Strip the five ASCII whitespace characters - never the other 24."""
    return text.strip(whitespace)


def ascii_blank(text: str, whitespace: str = ASCII_WHITESPACE) -> bool:
    """Is this text empty or nothing but ASCII whitespace?

    ``not text.strip()`` answers ``True`` for a lone U+00A0.  A browser answers
    "character token", and that one answer closes an open ``<head>``.
    """
    return not text.strip(whitespace)


def ascii_split(text: str, whitespace: str = ASCII_WHITESPACE) -> list[str]:
    """``str.split()`` restricted to ASCII whitespace, empty runs dropped."""
    parts: list[str] = []
    current: list[str] = []
    for char in text:
        if char in whitespace:
            if current: parts.append("".join(current)); current = []
        else:
            current.append(char)
    if current: parts.append("".join(current))
    return parts


HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SPECIMEN = "ÄÖÜ äöü ß ẞ · 0123456789 · 1lI O0 · Franz jagt im komplett verwahrlosten Taxi quer durch Bayern. AVATAR Wa To fi fl — 19,90 €.\n\nconst grüße = (preis = 19.90) => `Größe: ${preis.toFixed(2)} €`; // []{}() <> != === +-*/"
SPECIMEN_SHA256 = hashlib.sha256(SPECIMEN.encode("utf-8")).hexdigest()
VIEWPORTS = (320, 768, 1440)
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
PROTECTED_RUN_ID = "first-ci"
# How long a run waits for the replay-ledger lock before it fails closed.  A
# wait without a bound is a hang, not a contract.
AFFINITY_LOCK_TIMEOUT = 30.0
SEMANTIC_ROLES = {
    "canvas", "surface", "surface-strong", "text", "text-muted", "border",
    "primary", "on-primary", "secondary", "on-secondary", "accent", "on-accent",
    "success", "on-success", "warning", "on-warning", "danger", "on-danger", "focus",
}
ROLE_TESTS = (
    ("text", "canvas", "normal-text", 4.5),
    ("text", "surface", "normal-text", 4.5),
    ("text", "surface-strong", "normal-text", 4.5),
    ("text-muted", "canvas", "normal-text", 4.5),
    ("text-muted", "surface", "normal-text", 4.5),
    ("border", "canvas", "non-text", 3.0),
    ("focus", "canvas", "non-text", 3.0),
    ("on-primary", "primary", "normal-text", 4.5),
    ("on-secondary", "secondary", "normal-text", 4.5),
    ("on-accent", "accent", "normal-text", 4.5),
    ("on-success", "success", "normal-text", 4.5),
    ("on-warning", "warning", "normal-text", 4.5),
    ("on-danger", "danger", "normal-text", 4.5),
)
SCHEMAS = {
    "brief": "brandkit-visual-brief.v1.schema.json",
    "candidates": "brandkit-visual-candidate-set.v1.schema.json",
    "color": "brandkit-color-contrast-report.v1.schema.json",
    "font": "brandkit-font-candidate-report.v1.schema.json",
    "logo": "brandkit-logo-test-report.v1.schema.json",
    "icon": "brandkit-icon-report.v1.schema.json",
    "sheet": "brandkit-visual-sheet-manifest.v1.schema.json",
    "playground": "brandkit-visual-playground-manifest.v1.schema.json",
    "affinity-observation": "brandkit-affinity-observation.v1.schema.json",
    "affinity-mutation": "brandkit-affinity-mutation-receipt.v1.schema.json",
}


class VisualDecisionError(RuntimeError):
    """A visual-decision evidence contract failed closed."""


def _pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


# --- what every ``.decode()`` in this module assumes, and what binds it -------
#
# A ``.decode()`` is an *assumption* about bytes.  It is safe only where the
# format cannot say otherwise, or where the bytes are held to saying exactly
# this - because the second reader of those bytes is a browser, and a format
# that carries its own encoding declaration lets the two read one byte stream as
# two character streams.  That defect has now been found three times in this
# file, in three formats.  There are five decode sites, and each one states its
# answer where it stands:
#
#   ``load_json_bytes``      JSON carries no in-band encoding declaration at all
#                            (RFC 8259 8.1 fixes UTF-8 and forbids a BOM), and
#                            this decode is the only reading anything ever makes
#                            of those bytes - no browser reads a manifest.
#                            Nothing to bind; the two byte-level alternatives
#                            already fail closed here (see below).
#   ``_stored_spelling``     a kernel answer, decoded with ``surrogateescape``,
#                            which is total and byte-reversible.  That is not an
#                            assumption about an encoding, it is the absence of
#                            one.
#   ``_svg_static_checks``   XML *does* declare its encoding: :func:`_bind_xml_encoding`.
#   ``index.html``           bound since r19: no BOM, exactly one declaration,
#                            spelled exactly, inside the 1024-byte window.
#   every CSS member         css-syntax-3 lets a stylesheet name its own
#                            encoding: :func:`_bind_css_encoding`.
#
# Every UTF-16 or UTF-32 form of a JSON document fails the UTF-8 decode below (a
# NUL or a lone 0xFF is not UTF-8), and a UTF-8 BOM decodes to U+FEFF, which
# ``json.loads`` refuses as an unexpected character - so both arrive as the one
# ``VisualDecisionError`` this function raises, never as a second reading.
#
# The byte order marks of every Unicode encoding form.  A BOM overrides every
# in-band declaration in HTML5, is a stray U+FEFF in CSS, and picks the encoding
# outright in XML, so no member of a bundle may carry one.  UTF-32 first: its
# little-endian mark opens with the UTF-16 little-endian one.
_BYTE_ORDER_MARKS = (b"\xef\xbb\xbf", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00", b"\xfe\xff", b"\xff\xfe")


def load_json_bytes(data: bytes, label: str) -> Any:
    try:
        text = data.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise VisualDecisionError(f"{label} is not strict JSON: {exc}") from exc


def canonical_json(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise VisualDecisionError(f"value is not finite canonical JSON: {exc}") from exc


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_sha256(value: Any) -> bool:
    """True only for a lowercase hex SHA-256 string.

    ``hmac.compare_digest`` raises ``TypeError`` for a non-string or for a
    string with non-ASCII characters, so every MAC that arrives as untyped JSON
    is screened with this first: a malformed digest must fail the contract
    closed, not escape the ``VisualDecisionError`` boundary as a crash.
    """
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def require_sha256(value: Any, label: str) -> str:
    if not is_sha256(value):
        raise VisualDecisionError(f"{label} is not a lowercase SHA-256")
    return value


# macOS/BSD ``fcntl`` command that answers with the path the kernel has stored
# for an open file description.  ``fcntl.F_GETPATH`` is not exposed by CPython
# on every build, so the numeric constant is the fallback.
# Only on the platforms that define it: elsewhere the number would name some
# other fcntl command, and this must never be a guess.
_F_GETPATH = getattr(fcntl, "F_GETPATH", 50 if sys.platform.startswith(("darwin", "freebsd", "openbsd", "netbsd")) else None)


def _file_identity(info: os.stat_result) -> tuple[int, int]:
    """The one value that names a file on this host: ``(st_dev, st_ino)``.

    Every security decision in this module that used to compare path *strings*
    compares this instead.  A path string is not an identity on the file
    systems this ships on: APFS folds case and Unicode normalization form, so
    ``.../FIRST-CI``, ``.../first-ci`` and an NFD spelling of the same name are
    three different strings for one and the same directory.  Three of the last
    four holes in this file came from a string comparison answering "different"
    where the kernel answers "same file".
    """
    return (info.st_dev, info.st_ino)


def _stored_spelling(path: Path) -> Path | None:
    """The spelling the kernel itself has stored for ``path``, or ``None``.

    ``os.path.realpath`` resolves symlinks and ``..`` but never folds case or
    Unicode normalization form, so on a case- and normalization-insensitive
    file system it happily hands back the attacker's spelling of an existing
    directory.  ``F_GETPATH`` on an open descriptor answers with the name the
    file system actually holds, which is what "canonical" has to mean here.

    ``None`` means "this host cannot answer", and the caller then falls back to
    :func:`_listed_verbatim`, which is portable but has to read directories.
    """
    if _F_GETPATH is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        # MAXPATHLEN exactly: F_GETPATH wants a buffer of that size, and
        # CPython refuses an fcntl argument longer than 1024 bytes - a larger
        # buffer raises ValueError, which would silently drop this check to the
        # portable fallback on every call.
        answer = fcntl.fcntl(fd, _F_GETPATH, b"\0" * 1024)
    except (OSError, ValueError, TypeError):
        return None
    finally:
        os.close(fd)
    # An explicit separator: this split has no Unicode semantics to get wrong.
    # ``surrogateescape`` is what keeps this decode from being an *assumption*:
    # it is total and byte-reversible, so a path the kernel stores in bytes that
    # are not UTF-8 comes back as itself rather than as a second spelling.  The
    # result is only ever compared as a path; nothing renders it.
    text = answer.split(b"\0")[0].decode("utf-8", "surrogateescape")
    return Path(text) if text.startswith("/") else None


def _listed_verbatim(path: Path) -> bool:
    """Portable check that every component of ``path`` is spelled as stored.

    A directory entry is returned by ``os.listdir`` in exactly the bytes the
    file system holds, so a component that is missing from its parent's
    listing - while the path as a whole still opens - is a case or
    normalization variant rather than the stored name.
    """
    current = Path(path.anchor or "/")
    for part in path.parts[1:] if path.anchor else path.parts:
        try:
            if part not in os.listdir(current):
                return False
        except OSError:
            return False
        current = current / part
    return True


def exact_project_root(root: Path) -> Path:
    """The project root, or a fail-closed error - spelled exactly as stored.

    ``abspath == resolve`` is *not* a canonicity test on APFS: both sides keep
    whatever case and Unicode normalization form the caller wrote, so every
    spelling of an existing directory passed it.  That is what let a case
    variant of the project root walk straight past the ``first-ci`` guard in
    :func:`visual_workspace` and past the "outside the project" guard on the
    affinity key and ledger.  The stored spelling is therefore required as
    well, from the kernel where it can be had and from the parent directory
    listing otherwise.
    """
    absolute = Path(os.path.abspath(root))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise VisualDecisionError("project root does not exist") from exc
    if absolute != resolved or not resolved.is_dir():
        raise VisualDecisionError("project root must be an exact canonical directory")
    stored = _stored_spelling(resolved)
    if stored is not None:
        if stored != resolved:
            raise VisualDecisionError("project root must be an exact canonical directory")
    elif not _listed_verbatim(resolved):
        raise VisualDecisionError("project root must be an exact canonical directory")
    return resolved


def _identity_contains(container: Path, candidate: Path) -> bool:
    """Is ``candidate`` the directory ``container`` itself, or below it?

    Judged by file identity, never by text.  ``os.path.realpath`` first, so
    symlinks and ``..`` are resolved the way the kernel resolves them, then
    every ancestor of the result is stat-ed and compared to the container's
    ``(st_dev, st_ino)``.  Ancestors that do not exist are skipped rather than
    treated as "outside": the leaf of a path being written to need not exist
    yet, but the directory it will be created in does.

    A container that does not exist contains nothing, so the answer is ``False``
    - callers use this to keep something *out* of a place, and a place that is
    absent cannot hold it.
    """
    try:
        anchor = _file_identity(os.stat(container))
    except OSError:
        return False
    return _identity_holds(anchor, candidate)


def _identity_holds(anchor: tuple[int, int] | None, candidate: Path) -> bool:
    """Is ``candidate`` at or below the file identity ``anchor``?

    The anchor is a ``(st_dev, st_ino)`` pair the caller has already taken,
    which is the whole point: :func:`_identity_contains` re-derives it from a
    *path* on every call, so a container whose name stops resolving between two
    calls silently stops containing anything.  A caller that must not lose the
    anchor takes it once, from a descriptor it holds open, and asks here.

    An ancestor that cannot be stat-ed is skipped rather than ending the walk:
    an unreadable intermediate directory must not be able to hide containment,
    and the leaf of a path being written to need not exist yet.
    """
    if anchor is None:
        return False
    current = Path(os.path.realpath(candidate))
    seen: set[Path] = set()
    while current not in seen:
        seen.add(current)
        try:
            if _file_identity(os.stat(current)) == anchor:
                return True
        except OSError:
            pass
        parent = current.parent
        if parent == current:
            return False
        current = parent
    return False


def _outside_project(project_root: Path, candidates: tuple[tuple[Path, str], ...]) -> None:
    """Fail closed unless every named path lies outside the project tree.

    The lexical test is kept as a first, cheap answer for paths that do not
    exist at all; the identity test is the one that decides, because it is the
    only one a case-flipped or NFD-spelled project component cannot slip past.
    """
    project = exact_project_root(project_root)
    for external, message in candidates:
        resolved = Path(os.path.abspath(external))
        if resolved == project or project in resolved.parents or _identity_contains(project, external):
            raise VisualDecisionError(message)


def _names_protected_run(path: Path) -> bool:
    """Does any component of ``path`` name the sealed run, however it is spelled?

    ``PROTECTED_RUN_ID in path.parts`` is a case-sensitive string test, and the
    file systems this ships on are not case-sensitive: ``FIRST-CI`` named the
    same sealed directory and answered ``False``.  Components are therefore
    compared in NFC, case-folded - the same fold the file system applies when
    it decides that two names are one file.

    This is the one fold in the module that is deliberately **not** ASCII: the
    question here is not what a specification says but what the *file system*
    does, and that is full Unicode case folding over NFC.  ``str.casefold``
    folds strictly more than any file system (U+212A onto ``k``, for one), so
    every disagreement between the two makes this guard refuse a name the file
    system might still have accepted - the fail-closed direction.
    """
    return any(unicodedata.normalize("NFC", part).casefold() == PROTECTED_RUN_ID for part in path.parts)


def _run_discovery_claim(project: Path, run_id: str) -> Any | None:
    """The run directory's own statement about which run it is - or ``None``.

    ``None`` means **provably absent**: some component of
    ``<project>/.brandkit/work/<run_id>/discovery/handoff.json`` is not there at
    all, which is what a fresh run looks like and is no claim about anything.
    Everything else - a component that exists but is not a directory, a handoff
    that is a symlink or a directory, one that cannot be read or is not JSON -
    raises.

    That distinction is the whole point.  The guard used to read

        except (VisualDecisionError, OSError): handoff = None

    which folded "present but unreadable" into "absent" and made the anchor
    **fail-open**: a ``handoff.json`` replaced by a symlink or by a directory
    disarmed the run-identity check completely, and the sealed tree could then
    be renamed onto a free run name after all.  "I cannot establish the
    identity" is now a refusal, exactly as r15 already required for the path
    identity one function down.

    The rights this costs an attacker are named rather than implied: all three
    remaining ways past this guard - moving ``discovery/`` aside, replacing
    ``handoff.json`` with a symlink, replacing it with a directory - need write
    permission **inside the sealed tree**, and an actor who has that needs no
    deputy in the first place.  An actor who can write only to
    ``.brandkit/work`` is stopped by every one of them.  The CI environment has
    to keep that assumption true (``first-ci`` 0700, owned by the runner).
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptors: list[int] = []
    try:
        try:
            current = os.open(project, flags)
        except OSError as exc:
            raise VisualDecisionError("run discovery handoff cannot be identified") from exc
        descriptors.append(current)
        for part in (".brandkit", "work", run_id, "discovery", "handoff.json"):
            try:
                info = os.lstat(part, dir_fd=current)
            except (FileNotFoundError, NotADirectoryError):
                return None
            except OSError as exc:
                raise VisualDecisionError("run discovery handoff cannot be identified") from exc
            if part == "handoff.json":
                # It is there.  From here on nothing may fall back to "absent";
                # secure_read below refuses a link, a directory, a FIFO and a
                # file that changes under the read, and each of those refusals
                # is now the answer of this call.
                break
            if not stat.S_ISDIR(info.st_mode):
                raise VisualDecisionError("run discovery handoff cannot be identified")
            try:
                current = os.open(part, flags, dir_fd=current)
            except OSError as exc:
                raise VisualDecisionError("run discovery handoff cannot be identified") from exc
            descriptors.append(current)
    finally:
        for descriptor in reversed(descriptors):
            try: os.close(descriptor)
            except OSError: pass
    handoff, _info = secure_read(project / ".brandkit" / "work", f"{run_id}/discovery/handoff.json", "run discovery handoff", exclusive=False)
    return load_json_bytes(handoff, "run discovery handoff")


def visual_workspace(project_root: Path, run_id: str, create: bool = False) -> Path:
    """The run's own workspace, kept out of the sealed run by identity, not by name.

    The guard used to re-derive ``project/.brandkit/work/first-ci`` from its
    *path* at every one of its three sites, and then ask a path question of it.
    That made the whole protection conditional on the name still resolving: the
    moment it did not - because ``work`` had become a symlink one component
    further out - :func:`_identity_contains` answered ``False`` by design ("a
    place that is absent cannot hold it") and all three guards fell silent at
    once.  In the window this function itself admits, the consequence was not a
    trace but the whole thing: the sealed tree became the run's work root.

    Two changes close that, and both are about *when* an identity is taken:

    * The sealed tree's identity is taken **once**, before anything is created,
      from a descriptor that stays open for the rest of the call.  An identity
      that could not be established is not ``False``: only "provably absent"
      (ENOENT / ENOTDIR on the leaf) means there is nothing to protect, while
      an unreadable, looping or otherwise unanswerable ``first-ci`` fails the
      call closed.  Once taken, the anchor survives anything that happens to the
      name afterwards.
    * The walk is descriptor-relative - ``os.mkdir(part, dir_fd=...)`` and
      ``os.open(part, O_NOFOLLOW|O_DIRECTORY, dir_fd=...)`` - exactly as
      :func:`secure_read` and :func:`_safe_output_dir` already do it.  Each
      component is created and then opened *in the directory that was just
      checked*, and the component's own ``fstat`` is compared to the anchor, so
      the thing that is checked and the thing that is used are one object rather
      than two resolutions of one string.  ``O_NOFOLLOW`` makes "no symlink
      component" a property of the open instead of a separate ``lstat`` with a
      window behind it, and no swap of an ancestor can redirect a descriptor
      that is already held.

    A renamed seal *is* recognised, and the earlier claim that it could not be
    was simply wrong.  The sealed tree records its own identity - a
    schema-versioned ``discovery/handoff.json`` naming its ``run_id`` - so the
    guard below reads the run directory's own statement about which run it is
    and refuses a directory that names a different one.  It stands in front of
    the create walk, because the walk creates as it descends.

    What remains is a residual risk with a named rights requirement, not an
    impossibility: an actor who can write to the sealed tree *itself* can move
    ``discovery/`` aside, make this call and move it back.  That actor needs no
    deputy - he can write the tree directly - so nothing is escalated by it.
    An actor who can write only to ``project/.brandkit/work`` is stopped, and
    that is the constellation in which the rename really was an escalation.
    """
    project = exact_project_root(project_root)
    if not RUN_ID.fullmatch(run_id) or run_id == PROTECTED_RUN_ID:
        raise VisualDecisionError("run id is invalid or protected")
    parts = (".brandkit", "work", run_id, "visual-decisions")
    workspace = project.joinpath(*parts)
    # The name guard runs before anything is created, so a run that spells the
    # sealed tree anywhere in its path never gets a directory of its own there.
    if _names_protected_run(workspace):
        raise VisualDecisionError("protected first-ci path is forbidden")
    protected = project / ".brandkit" / "work" / PROTECTED_RUN_ID
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptors: list[int] = []
    try:
        anchor: tuple[int, int] | None
        try:
            marker = os.lstat(protected)
        except (FileNotFoundError, NotADirectoryError):
            # Provably absent - the name is free and there is nothing to anchor
            # to.  This is the *only* reason the call may run unanchored.
            anchor = None
        except OSError as exc:
            raise VisualDecisionError("protected first-ci path cannot be identified") from exc
        else:
            # The reserved name holding something that is not a plain directory
            # is not an absence: protected_run_inventory refuses exactly that
            # shape, and macOS answers O_DIRECTORY|O_NOFOLLOW on a symlink with
            # ENOTDIR rather than ELOOP, so "not a directory" would otherwise
            # read as "absent" and disarm the anchor.
            if not stat.S_ISDIR(marker.st_mode):
                raise VisualDecisionError("protected first-ci path cannot be identified")
            try:
                sealed = os.open(protected, flags); descriptors.append(sealed)
            except OSError as exc:
                raise VisualDecisionError("protected first-ci path cannot be identified") from exc
            anchor = _file_identity(os.fstat(sealed))
        if _identity_holds(anchor, project):
            raise VisualDecisionError("protected first-ci path is forbidden")
        # The run directory says which run it is, and it is asked *before* the
        # create walk.  R15 argued that a renamed seal cannot be recognised
        # because "its identity was never recorded anywhere"; that was wrong.
        # The sealed tree carries its own identity, schema-versioned, inside
        # itself - discovery/handoff.json names its run_id - and this is the
        # very invariant validate_brief already enforces one level down
        # (brief["run_id"] != run_id fails closed), applied to the run directory
        # the workspace is cut out of.  A run that was renamed onto some other
        # run's name is therefore refused instead of being written into.
        #
        # The position is load-bearing, not cosmetic: the walk below is
        # descriptor-relative and creates as it goes, so the same test placed
        # after it has already made a directory inside the sealed tree.  Every
        # guard in this function has to stand in front of the first mkdir.
        #
        # Two honest limits, so this is not read as more than it is:
        #  * It does not make the seal inviolable.  Whoever can write to the
        #    sealed tree *itself* can move discovery/ aside, make the call and
        #    move it back.  This raises the bar; it proves nothing against that
        #    actor - who needs no deputy in the first place.
        #  * Whoever can write only to .brandkit/work (not to first-ci) is
        #    stopped, and that is exactly the constellation in which the rename
        #    was a real escalation: a deputy with more rights (a CI runner)
        #    writing into a tree the caller cannot touch.
        # Absent is no claim; present-but-unreadable is a refusal.  See
        # _run_discovery_claim for why the two may not be folded together.
        document = _run_discovery_claim(project, run_id)
        if document is not None:
            if not isinstance(document, dict) or document.get("run_id") != run_id:
                raise VisualDecisionError("run directory carries a discovery handoff for a different run")
        try:
            current = os.open(project, flags)
        except OSError as exc:
            raise VisualDecisionError("canonical visual-decisions workspace is absent") from exc
        descriptors.append(current)
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise VisualDecisionError("canonical visual-decisions workspace is absent") from exc
            try:
                current = os.open(part, flags, dir_fd=current)
            except FileNotFoundError as exc:
                raise VisualDecisionError("canonical visual-decisions workspace is absent") from exc
            except OSError as exc:
                raise VisualDecisionError("visual-decisions workspace has a linked or non-directory component") from exc
            descriptors.append(current)
            if anchor is not None and _file_identity(os.fstat(current)) == anchor:
                raise VisualDecisionError("protected first-ci path is forbidden")
        if _names_protected_run(workspace) or _identity_holds(anchor, workspace):
            raise VisualDecisionError("protected first-ci path is forbidden")
        # The caller gets a path, and a path is resolved again by whoever uses
        # it.  This is the one place that can still say that the path names the
        # directory this walk actually validated, so it says it.
        try:
            reopened = _file_identity(os.stat(workspace))
        except OSError as exc:
            raise VisualDecisionError("canonical visual-decisions workspace is absent") from exc
        if reopened != _file_identity(os.fstat(current)):
            raise VisualDecisionError("visual-decisions workspace changed while it was being opened")
        return workspace
    finally:
        for descriptor in reversed(descriptors):
            try: os.close(descriptor)
            except OSError: pass


def protected_run_inventory(project_root: Path, run_id: str = PROTECTED_RUN_ID) -> dict[str, Any]:
    """Read-only inventory: SHA-256(canonical JSON of sorted path/type/size/hash records)."""
    project = exact_project_root(project_root)
    if run_id != PROTECTED_RUN_ID:
        raise VisualDecisionError("protected inventory is defined only for first-ci")
    root = project / ".brandkit/work" / run_id
    if root.is_symlink() or not root.is_dir() or root.resolve(strict=True) != root:
        raise VisualDecisionError("protected run root is absent or linked")
    records: list[dict[str, Any]] = []
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in sorted(directories):
            path = current_path / name; info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode): raise VisualDecisionError("protected run contains linked/non-directory member")
            records.append({"path": path.relative_to(root).as_posix(), "type": "directory", "size": 0, "sha256": None})
        for name in sorted(files):
            relative = (current_path / name).relative_to(root).as_posix()
            data, info = secure_read(root, relative, f"protected run {relative}", exclusive=False)
            records.append({"path": relative, "type": "file", "size": info.st_size, "sha256": sha256_bytes(data)})
    records.sort(key=lambda item: (item["path"].encode("utf-8"), item["type"]))
    preimage = {"algorithm": "brandkit-protected-run-inventory.v1", "run_id": run_id, "members": records}
    return {**preimage, "tree_sha256": sha256_bytes(canonical_json(preimage))}


def _relative(relative: str, label: str) -> PurePosixPath:
    if not isinstance(relative, str):
        raise VisualDecisionError(f"{label} is not a normalized relative path")
    posix = PurePosixPath(relative)
    if posix.is_absolute() or not posix.parts or any(part in {"", ".", ".."} for part in posix.parts) or relative != posix.as_posix():
        raise VisualDecisionError(f"{label} is not a normalized relative path")
    return posix


def secure_read(root: Path, relative: str, label: str, *, exclusive: bool = True) -> tuple[bytes, os.stat_result]:
    parts = _relative(relative, label).parts
    try:
        # This used to stand outside the boundary below, so a root that does not
        # exist left this function as a raw FileNotFoundError while every other
        # failure came out as a VisualDecisionError.  No caller reached it
        # before; the guard in visual_workspace is the first that may.
        root = root.resolve(strict=True)
    except OSError as exc:
        raise VisualDecisionError(f"{label} root is unavailable") from exc
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    # O_NONBLOCK is what makes the S_ISREG test below a *test* rather than a
    # promise: O_NOFOLLOW stops a symlink but not a FIFO, and open(O_RDONLY) on
    # a FIFO with no writer blocks for ever - the run replaces its own
    # visual-brief.json with a FIFO and validation hangs instead of failing
    # closed.  With O_NONBLOCK the open returns at once and the stat rejects it.
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptors: list[int] = []
    try:
        current = os.open(root, dir_flags); descriptors.append(current)
        for part in parts[:-1]:
            current = os.open(part, dir_flags, dir_fd=current); descriptors.append(current)
        fd = os.open(parts[-1], file_flags, dir_fd=current); descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or (exclusive and before.st_nlink != 1):
            raise VisualDecisionError(f"{label} must be an exclusive regular file")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk: break
            chunks.append(chunk)
        after = os.fstat(fd)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_nlink)
        data = b"".join(chunks)
        if identity(before) != identity(after) or len(data) != before.st_size:
            raise VisualDecisionError(f"{label} changed while reading")
        return data, before
    except OSError as exc:
        raise VisualDecisionError(f"cannot securely read {label}: {exc}") from exc
    finally:
        for descriptor in reversed(descriptors):
            try: os.close(descriptor)
            except OSError: pass


def confined_regular(root: Path, relative: str, label: str) -> Path:
    secure_read(root, relative, label)
    return root / _relative(relative, label).as_posix()


def load_json(path: Path, root: Path | None = None, relative: str | None = None, label: str = "document") -> Any:
    if root is not None and relative is not None:
        return load_json_bytes(secure_read(root, relative, label)[0], label)
    if path.is_symlink() or not path.is_file():
        raise VisualDecisionError(f"{label} must be a regular file")
    return load_json_bytes(path.read_bytes(), label)


def validate_schema(value: Any, kind: str) -> None:
    if kind not in SCHEMAS:
        raise VisualDecisionError(f"unknown visual-decision schema kind: {kind}")
    _validate_schema_file(value, SCHEMAS[kind], kind)


def _validate_schema_file(value: Any, filename: str, kind: str) -> None:
    # Split out of validate_schema so a schema that is no CLI --kind of its own
    # (the playground data file) is checked by the very same machinery.
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise VisualDecisionError("jsonschema is required from the pinned CI environment") from exc
    schema = load_json(SCHEMA_DIR / filename, label=f"{kind} schema")
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda item: list(item.absolute_path))
    if errors:
        raise VisualDecisionError("\n".join(f"{kind} {'.'.join(map(str, error.absolute_path)) or '<root>'}: {error.message}" for error in errors))


def _channel(value: int) -> float:
    component = value / 255.0
    return component / 12.92 if component <= 0.04045 else ((component + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    if not isinstance(color, str) or not HEX_COLOR.fullmatch(color):
        raise VisualDecisionError(f"invalid sRGB color: {color!r}")
    red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def contrast_ratio(foreground: str, background: str) -> float:
    first, second = relative_luminance(foreground), relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def color_contrast_report(candidate_set_sha256: str, candidate_id: str, roles: dict[str, str], non_color_cues: list[str]) -> dict[str, Any]:
    require_sha256(candidate_set_sha256, "candidate set hash")
    if not IDENTIFIER.fullmatch(candidate_id):
        raise VisualDecisionError("invalid color candidate id")
    if set(roles) != SEMANTIC_ROLES:
        raise VisualDecisionError(f"color roles differ: {sorted(set(roles) ^ SEMANTIC_ROLES)}")
    if len(set(non_color_cues)) < 2 or not {"text", "icon"}.issubset(non_color_cues):
        raise VisualDecisionError("status communication requires text and icon non-color cues")
    tests = []
    for foreground, background, usage, threshold in ROLE_TESTS:
        ratio = contrast_ratio(roles[foreground], roles[background])
        tests.append({
            "foreground_role": foreground,
            "background_role": background,
            "usage": usage,
            "minimum_ratio": threshold,
            "ratio": ratio,
            "ratio_display": f"{ratio:.2f}:1",
            "passed": ratio >= threshold,
        })
    report = {
        "schema_version": "brandkit-color-contrast-report.v1",
        "candidate_set_sha256": candidate_set_sha256,
        "candidate_id": candidate_id,
        "algorithm": "WCAG-2.x-sRGB-relative-luminance",
        "roles": dict(sorted(roles.items())),
        "tests": tests,
        "status_communication": {"color_only": False, "non_color_cues": sorted(non_color_cues)},
        "objective_status": "pass" if all(item["passed"] for item in tests) else "fail",
        "human_review_status": "required",
    }
    validate_schema(report, "color")
    return report


def validate_brief(brief: dict[str, Any], root: Path, *, project_root: Path | None = None, run_id: str | None = None) -> None:
    validate_schema(brief, "brief")
    _verify_bound_file(root, brief["interview_handoff"], "interview handoff")
    if project_root is not None and brief["project_identity_sha256"] != project_identity(project_root):
        raise VisualDecisionError("visual brief project differs")
    if run_id is not None and brief["run_id"] != run_id:
        raise VisualDecisionError("visual brief run differs")


# The acceptance bar names a *file* - scripts/validate_visual_decision_sources.py
# - as the independent second opinion on the candidate sources.  The code bound a
# *module name*: ``import validate_visual_decision_sources``, resolved through
# sys.path, with neither path nor identity nor hash.  A stub earlier on the path
# turned the whole delegation into a no-op and nothing noticed, while the JSON
# document it checks was being verified one line above against an exact SHA-256.
# The delegate is now bound the way every other input to this module is bound:
# by a confined path under ROOT and by the digest of the bytes that are executed.
SOURCE_VALIDATOR_PATH = "scripts/validate_visual_decision_sources.py"
SOURCE_VALIDATOR_SHA256 = "52308d190055b9bb939b22e1a781e466beb87f94e0543cb7300bdf32441acfca"


def _load_source_validator() -> Any:
    """The independent source validator, as a file and a digest rather than a name.

    ``secure_read`` supplies the confinement (no symlink component, no
    non-regular file, no escape from ROOT) and the bytes; the digest is compared
    against the release pin above; and the module is executed *from those very
    bytes*, so the code that runs is the code that was hashed - there is no
    second resolution in between for anything to slip through.

    The pin is deliberately a constant in this file: it makes the trust edge
    explicit and it makes an unannounced change to the delegate fail closed
    here.  Editing the delegate therefore means updating this constant, and a
    test recomputes the digest from the file so the two cannot drift apart
    silently.  Nothing is cached between calls: a cached module would be a
    second reading of the file that no digest covers.
    """
    data, _info = secure_read(ROOT, SOURCE_VALIDATOR_PATH, "independent visual source validator")
    if sha256_bytes(data) != SOURCE_VALIDATOR_SHA256:
        raise VisualDecisionError("independent visual source validator hash differs")
    module = types.ModuleType("brandkit_bound_visual_source_validator")
    module.__file__ = str(ROOT / SOURCE_VALIDATOR_PATH)
    try:
        exec(compile(data, module.__file__, "exec"), module.__dict__)
    except Exception as exc:
        raise VisualDecisionError(f"independent visual source validator failed: {exc}") from exc
    if not callable(getattr(module, "validate_visual_decision_sources", None)):
        raise VisualDecisionError("independent visual source validator does not export its entry point")
    return module


def validate_source_contract(binding: dict[str, Any], sidecar_root: Path) -> dict[str, Any]:
    if binding["path"] != "visual-decision-sources.v1.json":
        raise VisualDecisionError("candidate source contract path differs from the release-bound artifact")
    raw = _verify_bound_file(ROOT, binding, "visual-decision source contract")
    source_validator = _load_source_validator()
    try:
        errors = source_validator.validate_visual_decision_sources(ROOT, sidecar_root)
    except Exception as exc:
        raise VisualDecisionError(f"independent visual source validator failed: {exc}") from exc
    if errors:
        raise VisualDecisionError("independent visual source contract failed: " + "; ".join(errors))
    document = load_json_bytes(raw, "visual-decision source contract")
    selected = [item for item in document["candidates"] if item["decision"] == "selected"]
    if len(selected) != 1 or selected[0]["name"] != document["selected_dependency"]:
        raise VisualDecisionError("visual source selection differs")
    return document


def validate_color_report(report: dict[str, Any]) -> None:
    validate_schema(report, "color")
    expected = color_contrast_report(report["candidate_set_sha256"], report["candidate_id"], report["roles"], report["status_communication"]["non_color_cues"])
    if report != expected:
        raise VisualDecisionError(f"color report calculations differ: {report['candidate_id']}")


def _verify_bound_file(root: Path, binding: dict[str, Any], label: str) -> bytes:
    data, _ = secure_read(root, binding["path"], label)
    actual = sha256_bytes(data)
    if actual != require_sha256(binding["sha256"], f"{label} hash"):
        raise VisualDecisionError(f"{label} hash differs")
    return data


#: Space and printable ASCII, U+0020-U+007E: the characters whose advances decide
#: a fixed pitch on the path that does not rely on ``post`` or PANOSE.
_PRINTABLE_ASCII = range(0x20, 0x7F)


def extract_font_metadata(data: bytes) -> dict[str, Any]:
    try:
        font = TTFont(BytesIO(data), lazy=False, recalcBBoxes=False, recalcTimestamp=False)
        names = font["name"]
        def name(identifier: int, *, required: bool = True) -> str:
            # A record carrying nothing but blanks states nothing.  The fallback
            # below already reads an ABSENT id 16 that way - "an empty id 16 is
            # not a statement" - and a record of three spaces is that same
            # non-statement spelled with bytes.  It used to win: ``minLength: 1``
            # is satisfied, a blank is no control character, and so the sheet
            # printed a family made of spaces and the owner confirmed a name that
            # is none.
            #
            # Dropping those records BEFORE the tie-break settles the platform
            # case in the same expression: a record left empty on one platform
            # must not out-sort a filled one on another.  ``sorted({"",
            # "MacOnly"})[0]`` was the empty string, the expression fell back to
            # id 1, and the typographic family the font does state went unread.
            #
            # ``str.strip()`` is the WIDER, Unicode reading on purpose here, and
            # it is the right one: whatever specification calls a character
            # whitespace, none of it names a family.  The invisible characters
            # that are not whitespace - U+200B, U+2060, U+FEFF, U+00AD - are
            # register point 96 and stay outside this rule; the control class
            # refuses its own members independently of it.
            stated = {item.toUnicode() for item in names.names if item.nameID == identifier}
            values = sorted(value for value in stated if value.strip())
            if not values:
                if required: raise VisualDecisionError(f"font states no name in name table id {identifier}")
                return ""
            # Lexicographic and therefore DETERMINISTIC: a font with two
            # different id-16 records gets the alphabetically first, never the
            # order its table happens to store them in.  Same discipline id 1 has
            # always had; measured since this round rather than merely written.
            return values[0]
        os2 = font["OS/2"]
        cmap = font.getBestCmap() or {}
        widths = {advance for advance, _ in font["hmtx"].metrics.values()}
        # A monospace font may leave post.isFixedPitch at 0 and still give glyphs
        # outside its cell another advance - combining marks 0, fractions or
        # ligatures two cells, a middle dot less than one - so "every glyph one
        # advance" misses it.  Space and printable ASCII is what such a font sets
        # in the cell: one advance over all of it is a fixed pitch too.  This path
        # needs every one of those characters; a font lacking any of them is
        # left to the other three, which stay as they were.
        ascii_advances = {font["hmtx"][cmap[code]][0] for code in _PRINTABLE_ASCII if code in cmap}
        ascii_fixed = all(code in cmap for code in _PRINTABLE_ASCII) and len(ascii_advances) == 1
        fixed = bool(font["post"].isFixedPitch) or len(widths) == 1 or os2.panose.bProportion == 9 or ascii_fixed
        serif_style = os2.panose.bSerifStyle
        classification = "mono" if fixed else ("serif" if 2 <= serif_style <= 10 else "sans")
        axes = []
        if "fvar" in font:
            axes = [{"tag": axis.axisTag, "minimum": axis.minValue, "default": axis.defaultValue, "maximum": axis.maxValue} for axis in font["fvar"].axes]
        codepoints = sorted(cmap)
        # ASCII whitespace, not str.isspace(): a specimen character that Python
        # calls whitespace but a font has to *draw* - U+00A0 above all - would
        # otherwise drop silently out of the coverage requirement, and the
        # candidate would pass with a missing glyph.  SPECIMEN carries only
        # ASCII whitespace today, so this is the same set; it stays the same
        # set when the specimen grows.
        required = sorted({ord(char) for char in SPECIMEN if char not in ASCII_WHITESPACE})
        # The family is the one string of this report the sheet prints, so it has
        # to name the FAMILY.  ``name`` id 1 does not: it is the family name of
        # the font's *default instance*, and a variable font that rests below 400
        # spells a cut there - the sheet then named a weight it does not set,
        # because the specimens run at 400.  Id 16 is the typographic family and
        # a font carries it exactly when id 1 carries such a suffix, so "id 16
        # where the font states one, id 1 otherwise" is the whole rule and needs
        # no list of names.  An empty id 16 is not a statement and falls back too.
        #
        # This is not a second reading: ``_explained_font_classification`` already
        # holds a bound METADATA family against ``(names[16] or names[1])``.  The
        # two places now answer one rule instead of two that can drift apart.
        # ``subfamily``, ``full_name`` and ``postscript_name`` keep naming the
        # instance - nothing about the file is lost, it is only named correctly.
        return {
            "family": name(16, required=False) or name(1), "subfamily": name(2), "full_name": name(4), "version": name(5), "postscript_name": name(6),
            "format": "WOFF2" if data[:4] == b"wOF2" else "OpenType",
            "weight": os2.usWeightClass, "stretch": os2.usWidthClass, "fs_type": os2.fsType,
            "fixed_pitch": fixed, "classification": classification, "axes": axes,
            "cmap_sha256": sha256_bytes(canonical_json(codepoints)),
            "missing_specimen_codepoints": [f"U+{value:04X}" for value in required if value not in cmap],
        }
    except VisualDecisionError:
        raise
    except Exception as exc:
        raise VisualDecisionError(f"font metadata extraction failed: {exc}") from exc


# --- an explained font classification -----------------------------------------
#
# ``extract_font_metadata`` tells serif from sans by PANOSE alone, and PANOSE
# writes "no information" as 0: ``bFamilyType`` 0 and ``bSerifStyle`` 0 both mean
# "Any".  The classification line there reads that 0 as ``sans``, so every font
# built with a zeroed serif style is a sans whatever it looks like.  A font candidate
# may therefore bind a *classification source*, and its declared classification
# then comes from that source - only while all of the following hold, each of
# which fails closed:
#
# * PANOSE really says nothing about the serif style: ``bSerifStyle == 0`` with
#   ``bFamilyType`` 0 ("Any") or 2 ("Latin Text").  The second digit is the serif
#   style only in those two; under 1 ("No Fit"), 3 ("Latin Hand Written"), 4
#   ("Latin Decorative") and 5 ("Latin Symbol") the same digit describes another
#   property, so a 0 there says nothing about serifs either way and those stay
#   refused.  A PANOSE that carries a serif style is the answer, and a source
#   bound beside it is refused rather than consulted; otherwise the source would
#   be a switch that reclassifies any font.
# * The source is of the one supported kind, names a revision of its origin, and
#   its bytes are read from the report root under their SHA-256.  Any other kind
#   is refused, never ignored.  Which revision is the report's business.
# * The statement is read out of those bytes here; nothing in the report is taken
#   on its word.  Only the *main* category counts, never a subdivision of it, and
#   exactly one has to be present: a family filed under two main categories is
#   ambiguous.
# * The family the source describes is the family in the font's own ``name``
#   table and the family the binding names.
# * The source does not overrule what the font's metrics decide - its pitch.  For
#   a proportional font a zeroed serif style leaves only serif against sans open; a
#   fixed-pitch font stays ``mono``, and only a monospace statement explains it.
_CLASSIFICATION_SOURCE_KIND = "google-fonts-metadata"
_GOOGLE_FONTS_ORIGIN = "https://github.com/google/fonts"
_GOOGLE_FONTS_METADATA_PATH = re.compile(r"(?:ofl|apache|ufl)/[a-z0-9_]+/METADATA\.pb")
# Every main category of the Google Fonts catalogue.  DISPLAY and HANDWRITING
# describe a use or a hand rather than a construction and hold serif, sans and
# script faces alike, so they map onto no class and explain nothing.  SERIF holds
# the slab serifs too, as PANOSE's own serif styles do ("Square" is one of them).
_GOOGLE_FONTS_CATEGORY_CLASS: dict[str, str | None] = {
    "SERIF": "serif", "SANS_SERIF": "sans", "MONOSPACE": "mono", "DISPLAY": None, "HANDWRITING": None,
}
_PROTOBUF_TEXT_TOKEN = re.compile(r'[ \t\r\n]+|#[^\n]*|"(?:[^"\\\n]|\\.)*"|[A-Za-z_][A-Za-z0-9_]*|[-+]?[0-9][0-9A-Za-z.+-]*|[:{}]')


def _protobuf_text_fields(data: bytes, label: str) -> dict[str, list[Any]]:
    """The fields of a protobuf text-format message; nested messages are dicts.

    Deliberately a subset - scalar fields ``name: value``, message fields
    ``name { ... }`` with or without a colon, and comments.  Anything else is
    refused rather than skipped, so a construct this reader does not know cannot
    hide a field from it.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VisualDecisionError(f"{label} is not UTF-8") from exc
    tokens: list[str] = []
    position = 0
    while position < len(text):
        match = _PROTOBUF_TEXT_TOKEN.match(text, position)
        if match is None:
            raise VisualDecisionError(f"{label} is not protobuf text format at offset {position}")
        position = match.end()
        if match.group()[0] not in " \t\r\n#":
            tokens.append(match.group())
    stack: list[dict[str, list[Any]]] = [{}]
    index = 0
    while index < len(tokens):
        name = tokens[index]
        index += 1
        if name == "}":
            if len(stack) == 1:
                raise VisualDecisionError(f"{label} closes a message it never opened")
            stack.pop()
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise VisualDecisionError(f"{label} has a value where a field name belongs")
        colon = index < len(tokens) and tokens[index] == ":"
        if colon:
            index += 1
        if index >= len(tokens):
            raise VisualDecisionError(f"{label} is truncated")
        value = tokens[index]
        index += 1
        if value == "{":
            child: dict[str, list[Any]] = {}
            stack[-1].setdefault(name, []).append(child)
            stack.append(child)
        elif colon and value not in {":", "}"}:
            stack[-1].setdefault(name, []).append(value)
        else:
            raise VisualDecisionError(f"{label} has a field without a value: {name}")
    if len(stack) != 1:
        raise VisualDecisionError(f"{label} leaves a message open")
    return stack[0]


def _protobuf_single_string(fields: dict[str, list[Any]], name: str, label: str) -> str:
    values = fields.get(name, [])
    if len(values) != 1 or not isinstance(values[0], str) or not values[0].startswith('"') or "\\" in values[0]:
        raise VisualDecisionError(f"{label} must state exactly one plain top-level {name}")
    return values[0][1:-1]


def _explained_font_classification(candidate: dict[str, Any], asset: bytes, extracted: dict[str, Any], root: Path) -> str:
    """The classification a bound source explains for a font PANOSE says nothing about."""
    label = f"font classification source {candidate['id']}"
    binding = candidate["metadata"]["classification_source"]
    if binding["kind"] != _CLASSIFICATION_SOURCE_KIND:
        raise VisualDecisionError(f"{label} kind is not supported: {binding['kind']}")
    if binding["origin"] != _GOOGLE_FONTS_ORIGIN or not _GOOGLE_FONTS_METADATA_PATH.fullmatch(binding["upstream_path"]):
        raise VisualDecisionError(f"{label} is not a family METADATA.pb of {_GOOGLE_FONTS_ORIGIN}")
    try:
        font = TTFont(BytesIO(asset), lazy=True)
        panose = font["OS/2"].panose
        family_type, serif_style = panose.bFamilyType, panose.bSerifStyle
        names = {identifier: {item.toUnicode() for item in font["name"].names if item.nameID == identifier} for identifier in (1, 16)}
    except Exception as exc:
        raise VisualDecisionError(f"{label}: font tables are unreadable: {exc}") from exc
    if family_type not in (0, 2) or serif_style != 0:
        raise VisualDecisionError(f"{label} is not admissible: PANOSE carries a classification")
    fields = _protobuf_text_fields(_verify_bound_file(root, binding["file"], label), label)
    family = _protobuf_single_string(fields, "name", label)
    category = _protobuf_single_string(fields, "category", label)
    explained = _GOOGLE_FONTS_CATEGORY_CLASS.get(category)
    if explained is None:
        raise VisualDecisionError(f"{label} category {category} maps onto no font class")
    if family != binding["family"] or (names[16] or names[1]) != {family}:
        raise VisualDecisionError(f"{label} describes another family than the font")
    if (explained == "mono") != (extracted["classification"] == "mono"):
        raise VisualDecisionError(f"{label} contradicts the pitch of the font")
    return explained


def _default_instance_family(data: bytes) -> str:
    """``name`` id 1 of a font: the family name of its default instance.

    Read to EXPLAIN a refusal and never to decide what a family is called - the
    one reading of a family name is :func:`extract_font_metadata`.  A font whose
    name table cannot be read at all is no concern of this function; the caller
    has already refused it for that.
    """
    try:
        font = TTFont(BytesIO(data), lazy=True)
        stated = {item.toUnicode() for item in font["name"].names if item.nameID == 1}
        values = sorted(value for value in stated if value.strip())
    except Exception:
        return ""
    return values[0] if values else ""


def _reject_stale_family(candidate: dict[str, Any], asset: bytes, extracted: dict[str, Any]) -> None:
    """Name the one refusal a bundle written before the typographic family earns.

    ``metadata.extracted`` is compared byte for byte and the report schema is
    ``additionalProperties: false``, so a report that pinned the default-instance
    name cannot be migrated in place: the report's digest sits in the candidate
    value, that value's digest in the candidate set, and the set's digest in the
    brief and in both rendered manifests.  Rewriting the one string moves all
    four, which is building the bundle again by another name.  The bundle is
    therefore refused - and refused with its cause, because the generic mismatch
    reads like a tampered report and sends the reader after the wrong thing.
    ``_sheet_view`` stops at the first bad report, so the whole sheet is
    unrenderable until the bundle is rebuilt; saying so is the difference between
    a five-minute rebuild and an afternoon.

    Only a report that spells the old name in BOTH places is one of those: a
    report that carries one spelling in ``family`` and the other in
    ``extracted`` was edited after the fact, and that stays the generic refusal.

    And only a report whose family is the ONLY difference.  The message sends the
    reader to a version of this module and a rebuild; a report that spells the old
    name in both fields *and* carries a forged ``weight``, ``classification`` or
    ``cmap_sha256`` is not a bundle of that version, and telling its reader to
    build again explains away a forgery.  The remaining fields of ``extracted``
    are therefore required to agree already - the refusal is still closed either
    way, because the caller raises the generic mismatch the moment this returns.

    There is therefore exactly one call site, at the ``extracted`` comparison: a
    report of the older version always fails that one first, because ``extracted``
    carries the family too.  Guarding the ``family`` comparison as well would be
    unreachable code - the shapes that reach it are by definition the edited ones
    this refusal does not claim.
    """
    legacy = _default_instance_family(asset)
    if not legacy or legacy == extracted["family"]:
        return
    if ({candidate["family"], candidate["metadata"]["extracted"]["family"]} == {legacy}
            and {**candidate["metadata"]["extracted"], "family": extracted["family"]} == extracted):
        raise VisualDecisionError(
            f"font report names the default instance and not the typographic family: {candidate['id']}; "
            "it was written by an earlier version of this module and the bundle has to be built again")


def validate_font_report(report: dict[str, Any], root: Path) -> None:
    validate_schema(report, "font")
    if report["specimen_sha256"] != SPECIMEN_SHA256:
        raise VisualDecisionError("font report specimen hash differs")
    if {candidate["classification"] for candidate in report["candidates"]} != {"serif", "sans", "mono"}:
        raise VisualDecisionError("font report must cover serif, sans, and mono")
    specimen_bytes = SPECIMEN.encode("utf-8")
    asset_hashes: set[str] = set()
    for candidate in report["candidates"]:
        if candidate["specimen"] != SPECIMEN or candidate["specimen_sha256"] != SPECIMEN_SHA256:
            raise VisualDecisionError("font candidates must use the byte-identical canonical specimen")
        asset = _verify_bound_file(root, candidate["asset"], f"font {candidate['id']}")
        asset_hashes.add(candidate["asset"]["sha256"])
        try:
            validate_woff2(asset, f"font {candidate['id']}")
        except BrandPackageError as exc:
            raise VisualDecisionError(str(exc)) from exc
        actual_metadata = extract_font_metadata(asset)
        classification = actual_metadata["classification"]
        if "classification_source" in candidate["metadata"]:
            classification = _explained_font_classification(candidate, asset, actual_metadata, root)
        if candidate["metadata"]["extracted"] != actual_metadata or candidate["classification"] != classification:
            _reject_stale_family(candidate, asset, actual_metadata)
            raise VisualDecisionError(f"font metadata or classification differs: {candidate['id']}")
        if candidate["family"] != actual_metadata["family"] or actual_metadata["fs_type"] & 0x0002:
            raise VisualDecisionError(f"font family or embedding rights differ: {candidate['id']}")
        # The family is the one string in this report that the sheet prints and
        # that nothing bounds: it comes out of the font's own ``name`` table,
        # decoded by the platform/encoding ids stored in the file.  The equality
        # above makes ``metadata.extracted.family`` the same string, so one check
        # covers both spellings of it.
        _reject_sheet_control(candidate["family"], f"font family {candidate['id']}")
        if actual_metadata["missing_specimen_codepoints"]:
            raise VisualDecisionError(f"font lacks required specimen glyphs: {candidate['id']}")
        rights_bytes = _verify_bound_file(root, candidate["rights"], f"font rights {candidate['id']}")
        rights = load_json_bytes(rights_bytes, f"font rights {candidate['id']}")
        try:
            validate_brand_schema(rights, "rights-evidence.v1.schema.json", f"font rights {candidate['id']}")
        except BrandPackageError as exc:
            raise VisualDecisionError(str(exc)) from exc
        if not isinstance(rights, dict) or rights.get("status") not in {"owned", "licensed", "open-license", "public-domain"} or rights.get("todo") is not None:
            raise VisualDecisionError(f"font rights {candidate['id']} are not closed")
        if rights.get("license_id") != candidate["metadata"]["license_spdx"] or rights.get("asset_id") != candidate["id"]:
            raise VisualDecisionError(f"font license {candidate['id']} differs from evidence")
        pin = candidate["metadata"]["source_pin"]
        source_bytes = _verify_bound_file(root, {"path": pin["path"], "sha256": pin["sha256"]}, f"font source {candidate['id']}")
        if not pin["origin"].startswith("https://"):
            raise VisualDecisionError(f"font source origin {candidate['id']} is not HTTPS")
        if rights.get("source_revision") != pin["revision"] or rights.get("source_sha256") != sha256_bytes(source_bytes):
            raise VisualDecisionError(f"font source pin {candidate['id']} differs from rights evidence")
        license_bytes = _verify_bound_file(root, {"path": candidate["metadata"]["license_evidence"]["path"], "sha256": candidate["metadata"]["license_evidence"]["sha256"]}, f"font license text {candidate['id']}")
        if rights.get("license_evidence_sha256") != sha256_bytes(license_bytes):
            raise VisualDecisionError(f"font license text {candidate['id']} differs from rights evidence")
        if sha256_bytes(specimen_bytes) != candidate["specimen_sha256"]:
            raise VisualDecisionError("font specimen hash differs")
    if len(asset_hashes) != 3:
        raise VisualDecisionError("serif, sans, and mono must bind three distinct font assets")


# XML declares its own encoding, and ``ET.fromstring`` on a *str* silently
# ignores that declaration - it decodes nothing, so the declaration is a
# statement only the other reader acts on.  Measured on this build (CPython
# 3.12): ``ET.fromstring('<?xml version="1.0" encoding="shift_jis"?><svg .../>')``
# parses happily as UTF-8, while every processor that reads the *bytes* - expat
# with a bytes argument, and Blink - obeys the label.  In Shift_JIS a lead byte
# swallows the ASCII byte behind it, so a ``>`` this module sees can be gone for
# the renderer and two elements become one.  Same class as the sheet's
# ``<meta charset>`` (r19) and the stylesheet's ``@charset`` (r21), third format.
#
# Unlike the two sheet members - which have exactly one producer, this module -
# a logo SVG is owner-supplied and comes out of a drawing program that writes
# ``encoding="UTF-8"``.  The rule here is therefore the one XML 1.0 4.3.3
# states: encoding names are matched case-insensitively.  Anything else would be
# a wall rather than a gate, and the two spellings are the same statement.
_XML_DECLARATION = re.compile(rb"\A<\?xml(?![A-Za-z0-9_.:-])(?P<body>[^>]{0,240})\?>", re.IGNORECASE)
_XML_ENCODING = re.compile(rb"""encoding[ \t\r\n]*=[ \t\r\n]*(?:"([^"]*)"|'([^']*)')""")


def _bind_xml_encoding(data: bytes, label: str) -> None:
    """Hold an SVG's bytes to the encoding :func:`_svg_static_checks` decodes with.

    ``bytes.lower()`` is ASCII-only by definition - the one place in this file
    where the Python default already *is* the specification - so it is the right
    fold for an encoding label read out of bytes.
    """
    if data.startswith(_BYTE_ORDER_MARKS):
        raise VisualDecisionError(f"{label} carries a byte order mark")
    if not data[:5].lower().startswith(b"<?xml"):
        # No declaration and no BOM: XML 1.0 4.3.3 makes the entity UTF-8, which
        # is exactly what this module decodes it as.  Nothing left to say.
        return
    match = _XML_DECLARATION.match(data)
    if match is None:
        raise VisualDecisionError(f"{label} does not open with a readable XML declaration")
    encoding = _XML_ENCODING.search(match.group("body"))
    if encoding is not None and (encoding.group(1) if encoding.group(1) is not None else encoding.group(2)).lower() != b"utf-8":
        raise VisualDecisionError(f"{label} declares a character encoding other than utf-8")


def _svg_static_checks(data: bytes, label: str, asset_id: str) -> dict[str, Any]:
    _bind_xml_encoding(data, label)
    try:
        validate_svg(data, label, asset_id)
        root = ET.fromstring(data.decode("utf-8"))
    except (BrandPackageError, UnicodeError, ET.ParseError) as exc:
        raise VisualDecisionError(str(exc)) from exc
    view_box = root.attrib.get("viewBox")
    if not view_box:
        raise VisualDecisionError(f"{label} lacks viewBox")
    # SVG list-of-numbers separation is XML whitespace, not Unicode whitespace:
    # a viewBox whose third separator is U+00A0 counts four numbers to
    # str.split() and three to every renderer - an invalid viewBox that this
    # check called valid, and whose bound the geometry test was measured on.
    values = ascii_split(view_box, XML_WHITESPACE)
    if len(values) != 4:
        raise VisualDecisionError(f"{label} has invalid viewBox")
    try:
        numbers = [float(value) for value in values]
    except ValueError as exc:
        raise VisualDecisionError(f"{label} has non-numeric viewBox") from exc
    if not all(math.isfinite(value) for value in numbers) or numbers[2] <= 0 or numbers[3] <= 0:
        raise VisualDecisionError(f"{label} has invalid viewBox dimensions")
    colors: set[str] = set()
    identifiers: set[str] = set()
    geometry: list[float] = []
    for element in root.iter():
        local = element.tag.split("}")[-1]  # explicit separator: no Unicode semantics
        if local in {"text", "image", "foreignObject", "use", "script", "style"}:
            raise VisualDecisionError(f"{label} contains forbidden {local}")
        element_id = element.attrib.get("id")
        if element_id:
            if element_id in identifiers: raise VisualDecisionError(f"{label} has duplicate SVG ids")
            identifiers.add(element_id)
        for attribute in ("fill", "stroke", "stop-color"):
            value = element.attrib.get(attribute)
            # CSS/SVG paint keywords are ASCII case-insensitive.  A Unicode
            # fold here can answer "this paint is none" for a spelling the
            # renderer paints, which drops a colour out of the very set the
            # monochrome test counts.
            if value and ascii_lower(value) not in {"none", "transparent"}:
                colors.add(ascii_lower(value))
        for attribute, value in element.attrib.items():
            if attribute.split("}")[-1] in {"d", "points", "x", "y", "x1", "x2", "y1", "y2", "cx", "cy", "r", "rx", "ry", "width", "height"}:
                geometry.extend(float(item) for item in re.findall(r"-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)", value))
    bound = max(abs(numbers[0]), abs(numbers[1]), numbers[2], numbers[3]) * 16
    geometry_bounded = all(math.isfinite(value) and abs(value) <= bound for value in geometry)
    if not geometry_bounded: raise VisualDecisionError(f"{label} geometry exceeds its bounded viewBox")
    canonical_svg = ET.tostring(root, encoding="utf-8", short_empty_elements=True)
    return {"view_box": " ".join(values), "color_count": len(colors), "monochrome_static": len(colors) == 1, "geometry_bounded": True, "canonical_svg_sha256": sha256_bytes(canonical_svg)}


def project_identity(project_root: Path) -> str:
    return sha256_bytes(str(exact_project_root(project_root)).encode("utf-8"))


def _derive_key(key: bytes, domain: bytes) -> bytes:
    return hmac.new(key, domain, hashlib.sha256).digest()


# The owner key signs three different kinds of message, and each kind gets its
# own derived subkey - never the raw key, which is now used for nothing but
# derivation.  The domains are named here rather than spelled at the three call
# sites so that "there are exactly three, and they are distinct" is a statement
# a test can make about the module instead of about three string literals.
LEDGER_DOMAIN = b"brandkit-affinity-replay-ledger-v1"
OPERATION_DOMAIN = b"brandkit-affinity-operation-v1"
OWNER_APPROVAL_DOMAIN = b"brandkit-affinity-owner-approval-v1"
# The type tag the owner approval has to carry inside its signed bytes.  It is
# the tag the discovery skill already writes; requiring it is what makes the
# message self-describing rather than "whatever this key happened to sign".
OWNER_APPROVAL_TYPE = "brandkit-discovery-owner-receipt.v1"


def _external_0600(path: Path, label: str) -> bytes:
    absolute = Path(os.path.abspath(path)); parent = exact_project_root(absolute.parent)
    if absolute.parent != parent:
        raise VisualDecisionError(f"{label} parent is not canonical")
    try:
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise VisualDecisionError(f"{label} parent is not an openable canonical directory") from exc
    try:
        # O_NONBLOCK for the same reason as in secure_read: a FIFO in the place
        # of the key or the ledger must fail the S_ISREG test, not hang the run.
        fd = os.open(absolute.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=directory)
    except OSError as exc:
        os.close(directory)
        raise VisualDecisionError(f"{label} must be an exclusive 0600 regular file") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            raise VisualDecisionError(f"{label} must be an exclusive 0600 regular file")
        data = b""
        while True:
            chunk = os.read(fd, 65536)
            if not chunk: break
            data += chunk
        after = os.fstat(fd)
        if (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns): raise VisualDecisionError(f"{label} changed while reading")
        return data
    except OSError as exc:
        raise VisualDecisionError(f"{label} could not be read") from exc
    finally:
        os.close(fd)
        os.close(directory)


def _external_path(value: Any) -> Path:
    """Resolve one external-path field of the affinity descriptor, fail-closed.

    ``str`` and ``os.PathLike[str]`` are both accepted - the surrounding public
    API hands out ``Path`` objects (``project_root``, ``sidecar_root``), so a
    caller passing ``Path`` here is the natural spelling and must not be treated
    as a second, weaker contract.  Everything else (``bytes``, ``int``, ``None``,
    nested containers) is a descriptor error rather than something ``Path()``
    silently coerces.

    ``__fspath__`` is called exactly once per field per run: ``validate_bundle``
    resolves the descriptor once and hands the resulting ``Path`` pair to both
    the lock and the section the lock exists to protect, so a ``PathLike`` whose
    ``__fspath__`` answers differently on a second call cannot make the two
    disagree about which ledger is in play.
    """
    if isinstance(value, (str, os.PathLike)):
        try:
            text = os.fspath(value)
        except TypeError:
            text = None
        if isinstance(text, str) and text:
            return Path(text)
    raise VisualDecisionError("Affinity authentication descriptor is not closed")


def _affinity_descriptor(affinity_auth: Any) -> tuple[Path, Path]:
    """Type the closed affinity descriptor before anything else looks at it."""
    if not isinstance(affinity_auth, dict) or set(affinity_auth) != {"owner_key_path", "replay_ledger_path", "receipts"} or not isinstance(affinity_auth["receipts"], dict):
        raise VisualDecisionError("Affinity authentication descriptor is not closed")
    return _external_path(affinity_auth["owner_key_path"]), _external_path(affinity_auth["replay_ledger_path"])


def _affinity_ledger_lock(project_root: Path, descriptor: tuple[Path, Path] | None) -> int | None:
    """Serialize the whole replay-ledger read-modify-write across processes.

    The lock is an advisory ``flock`` on a dedicated 0600 file that lives beside
    the external ledger, i.e. outside the project, like the key and the ledger
    themselves.  ``flock`` is used instead of an ``O_CREAT|O_EXCL`` sentinel
    because the kernel drops it when the holding file description goes away:
    a run killed with SIGKILL cannot wedge later runs, while an exclusive
    sentinel file would have to be reaped by hand.  Nothing here relaxes the
    ledger contract - the descriptor, scoping, 0600 and outside-the-project
    checks all still run inside the locked section.

    The function is fail-closed: ``None`` means "no ledger is in play at all"
    and is returned only for ``descriptor is None``, i.e. for
    ``affinity_auth is None``.  As soon as a ledger is named, either a held
    lock is returned or a ``VisualDecisionError`` is raised - a run must never
    proceed to the ledger commit unlocked.  The rejections raised here are
    exactly the ones the locked section would raise for the same descriptor, so
    failing early cannot weaken or reword a check.

    The descriptor arrives already resolved, from the single
    ``_affinity_descriptor`` call of the run, so the path this lock is named
    after is by construction the same object the protected section validates
    and commits.
    """
    if descriptor is None:
        return None
    key_path, ledger_path = descriptor
    project = exact_project_root(project_root)
    _outside_project(project, ((key_path, "Affinity keys and replay ledger must remain outside the project"),
                               (ledger_path, "Affinity keys and replay ledger must remain outside the project")))
    ledger = Path(os.path.abspath(ledger_path))
    try:
        parent = exact_project_root(ledger.parent)
    except VisualDecisionError as exc:
        raise VisualDecisionError("Affinity replay ledger parent is not canonical") from exc
    if ledger.parent != parent:
        raise VisualDecisionError("Affinity replay ledger parent is not canonical")
    try:
        directory = os.open(ledger.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise VisualDecisionError("Affinity replay ledger lock is unavailable") from exc
    name = f".affinity-lock.{ledger.name}"
    fd = None
    try:
        for _ in range(64):
            # macOS refuses O_CREAT|O_NOFOLLOW with ENOENT while a concurrent
            # process is creating the same name, so open an existing lock file
            # first and fall back to an exclusive create.
            try:
                fd = os.open(name, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory); break
            except FileNotFoundError:
                pass
            try:
                fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory); break
            except FileExistsError:
                continue
    except OSError as exc:
        raise VisualDecisionError("Affinity replay ledger lock is unavailable") from exc
    finally:
        os.close(directory)
    if fd is None:
        raise VisualDecisionError("Affinity replay ledger lock is unavailable")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise VisualDecisionError("Affinity replay ledger lock is not a regular file")
        # LOCK_EX without LOCK_NB was the only call in this module that does not
        # terminate: a foreign holder of this lock file blocked validate_bundle
        # for ever, which is hanging rather than failing closed (and a hang
        # reports a coverage no test has).  The wait is bounded and the timeout
        # is the same refusal every other lock failure raises.
        limit = time.monotonic() + AFFINITY_LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.monotonic() >= limit:
                    raise VisualDecisionError("Affinity replay ledger lock is unavailable") from None
                time.sleep(0.005)
    except OSError as exc:
        os.close(fd)
        raise VisualDecisionError("Affinity replay ledger lock is unavailable") from exc
    except VisualDecisionError:
        os.close(fd)
        raise
    return fd


def load_replay_ledger(path: Path, owner_key: bytes, project_root: Path, run_id: str) -> tuple[dict[str, Any], bytes]:
    raw = _external_0600(path, "Affinity replay ledger"); ledger = load_json_bytes(raw, "Affinity replay ledger")
    required={"schema_version","project_identity_sha256","run_id","generation","predecessor_sha256","nonces","key_id","hmac_sha256"}
    key=_derive_key(owner_key,LEDGER_DOMAIN); signed={name:value for name,value in ledger.items() if name!="hmac_sha256"}
    if set(ledger)!=required or ledger["schema_version"]!="brandkit-affinity-replay-ledger.v1" or ledger["project_identity_sha256"]!=project_identity(project_root) or ledger["run_id"]!=run_id or ledger["key_id"]!=sha256_bytes(key)[:16] or not is_sha256(ledger["hmac_sha256"]) or not hmac.compare_digest(ledger["hmac_sha256"],hmac.new(key,canonical_json(signed),hashlib.sha256).hexdigest()):
        raise VisualDecisionError("Affinity replay ledger authentication/scope failed")
    if not isinstance(ledger["generation"],int) or ledger["generation"]<0 or len(ledger["nonces"])!=len(set(ledger["nonces"])) or any(not HEX64.fullmatch(item) for item in ledger["nonces"]): raise VisualDecisionError("Affinity replay ledger state differs")
    if ledger["generation"]==0 and (ledger["predecessor_sha256"]!="0"*64 or ledger["nonces"]): raise VisualDecisionError("Affinity replay ledger genesis differs")
    return ledger, raw


def persist_replay_ledger(path: Path, owner_key: bytes, prior: dict[str, Any], prior_raw: bytes, nonces: set[str]) -> None:
    key=_derive_key(owner_key,LEDGER_DOMAIN)
    signed={"schema_version":"brandkit-affinity-replay-ledger.v1","project_identity_sha256":prior["project_identity_sha256"],"run_id":prior["run_id"],"generation":prior["generation"]+1,"predecessor_sha256":sha256_bytes(prior_raw),"nonces":sorted(nonces),"key_id":sha256_bytes(key)[:16]}
    value={**signed,"hmac_sha256":hmac.new(key,canonical_json(signed),hashlib.sha256).hexdigest()}; data=canonical_json(value)
    parent=exact_project_root(Path(os.path.abspath(path)).parent); directory=os.open(parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0)|getattr(os,"O_NOFOLLOW",0)); temp=f".{path.name}.{os.getpid()}.{time.time_ns()}"
    try:
        fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600,dir_fd=directory)
        try: os.write(fd,data); os.fsync(fd)
        finally: os.close(fd)
        current=_external_0600(path,"Affinity replay ledger")
        if current!=prior_raw: raise VisualDecisionError("Affinity replay ledger changed before commit")
        os.replace(temp,path.name,src_dir_fd=directory,dst_dir_fd=directory); os.fsync(directory)
    finally:
        try: os.unlink(temp,dir_fd=directory)
        except FileNotFoundError: pass
        os.close(directory)


def validate_affinity_observation(observation: dict[str, Any], root: Path, *, project_root: Path | None = None, run_id: str | None = None, now: int | None = None) -> None:
    validate_schema(observation, "affinity-observation")
    if observation["probe"]["mode"] != "read-only":
        raise VisualDecisionError("Affinity discovery must remain read-only")
    if project_root is not None and observation["project_identity_sha256"] != project_identity(project_root):
        raise VisualDecisionError("Affinity observation project differs")
    if run_id is not None and observation["run_id"] != run_id:
        raise VisualDecisionError("Affinity observation run differs")
    current = int(time.time()) if now is None else now
    if observation["observed_at"] > current or current > observation["expires_at"] or observation["expires_at"] <= observation["observed_at"]:
        raise VisualDecisionError("Affinity observation is stale or future-dated")
    probe = observation["probe"]
    if probe["inventory_sha256"] != sha256_bytes(canonical_json(probe["providers"])):
        raise VisualDecisionError("Affinity inventory hash differs")
    providers = {item["server_id"]: item for item in probe["providers"]}
    matches = observation["matching_servers"]
    if any(item not in providers for item in matches) or len(matches) != len(set(matches)):
        raise VisualDecisionError("Affinity matching server inventory differs")
    expected_status = "unknown" if not probe["complete"] else ("unavailable" if not matches else ("ambiguous" if len(matches) > 1 else "available"))
    if observation["status"] != expected_status:
        raise VisualDecisionError("Affinity status does not derive from inventory")
    if expected_status != "available":
        if observation["lock"] is not None or observation["mutation_capable"] or not observation["blocker"]:
            raise VisualDecisionError("non-available Affinity observation cannot claim mutation")
        return
    if observation["lock"] is None or not observation["mutation_capable"] or observation["blocker"] is not None:
        raise VisualDecisionError("available Affinity observation requires a mutation-capable immutable lock")
    lock = load_json_bytes(_verify_bound_file(root, observation["lock"], "Affinity MCP lock"), "Affinity MCP lock")
    provider = providers[matches[0]]
    required = {"schema_version", "server_id", "origin", "version", "license_spdx", "source_revision", "source_sha256", "tools"}
    if not isinstance(lock, dict) or set(lock) != required or lock.get("schema_version") != "brandkit-affinity-mcp-lock.v1" or lock.get("server_id") != provider["server_id"] or lock.get("origin") != provider["origin"] or lock.get("version") != provider["version"] or lock.get("tools") != provider["tools"]:
        raise VisualDecisionError("Affinity MCP lock identity differs")
    require_sha256(lock["source_sha256"], "Affinity source hash")
    if not re.fullmatch(r"[0-9a-f]{40}", lock["source_revision"]):
        raise VisualDecisionError("Affinity source revision is not immutable")


def validate_affinity_mutation_receipt(receipt: dict[str, Any], observation: dict[str, Any], root: Path, *, project_root: Path, run_id: str, key: bytes, replay_nonces: set[str], now: int) -> None:
    validate_schema(receipt, "affinity-mutation")
    validate_affinity_observation(observation, root, project_root=project_root, run_id=run_id, now=now)
    if observation["status"] != "available":
        raise VisualDecisionError("Affinity mutation receipt requires an available observation")
    if receipt["observation_sha256"] != sha256_bytes(canonical_json(observation)):
        raise VisualDecisionError("Affinity mutation receipt does not bind the observation")
    if receipt["server_lock_sha256"] != observation["lock"]["sha256"]:
        raise VisualDecisionError("Affinity mutation receipt does not bind the server lock")
    if receipt["project_identity_sha256"] != project_identity(project_root) or receipt["run_id"] != run_id:
        raise VisualDecisionError("Affinity receipt project or run differs")
    expected_staging = f".brandkit/work/{run_id}/visual-decisions/staging/{receipt['document_id']}"
    if receipt["staging_root"] != expected_staging or any(not item["path"].startswith(f"staging/{receipt['document_id']}/") for item in receipt["outputs"]):
        raise VisualDecisionError("Affinity receipt staging/output root differs")
    if receipt["issued_at"] > now or now > receipt["expires_at"] or receipt["expires_at"] <= receipt["issued_at"]:
        raise VisualDecisionError("Affinity mutation receipt is expired or future-dated")
    if receipt["nonce"] in replay_nonces:
        raise VisualDecisionError("Affinity mutation receipt was already consumed")
    signed = {key_name: value for key_name, value in receipt.items() if key_name != "hmac_sha256"}; operation_key=_derive_key(key,OPERATION_DOMAIN)
    expected = hmac.new(operation_key, canonical_json(signed), hashlib.sha256).hexdigest()
    if receipt["key_id"] != sha256_bytes(operation_key)[:16] or not hmac.compare_digest(receipt["hmac_sha256"], expected):
        raise VisualDecisionError("Affinity mutation receipt authentication failed")
    owner_bytes = _verify_bound_file(root, receipt["owner_approval"], "Affinity owner approval")
    owner = load_json_bytes(owner_bytes, "Affinity owner approval")
    if not isinstance(owner, dict):
        raise VisualDecisionError("Affinity owner approval authentication failed")
    # Three signing purposes stand under one owner key, and the third one used
    # to sign with the *raw* key while the other two used derived subkeys - so
    # the domain separation the acceptance bar asks for held for two of three.
    # It now holds for all three, and the approval carries its type tag inside
    # the signed bytes, so a message of one purpose cannot be presented as a
    # message of another the moment this key ever signs anything else.
    owner_key = _derive_key(key, OWNER_APPROVAL_DOMAIN)
    owner_signed = {name: value for name, value in owner.items() if name != "hmac_sha256"}
    if set(owner) != {"schema_version", "key_id", "payload", "hmac_sha256"} or owner.get("schema_version") != OWNER_APPROVAL_TYPE:
        raise VisualDecisionError("Affinity owner approval type or shape differs")
    if owner.get("key_id") != sha256_bytes(owner_key)[:16] or not is_sha256(owner.get("hmac_sha256")) or not hmac.compare_digest(owner["hmac_sha256"], hmac.new(owner_key, canonical_json(owner_signed), hashlib.sha256).hexdigest()):
        raise VisualDecisionError("Affinity owner approval authentication failed")
    owner_payload = owner.get("payload", {})
    expected_owner = {"decision": "graphics-mutate", "project_identity_sha256": project_identity(project_root), "run_id": run_id, "document_id": receipt["document_id"]}
    if not isinstance(owner_payload, dict) or set(owner_payload) != set(expected_owner) or any(owner_payload.get(name) != value for name, value in expected_owner.items()):
        raise VisualDecisionError("Affinity owner approval scope differs")
    provider = next(item for item in observation["probe"]["providers"] if item["server_id"] == observation["matching_servers"][0])
    tool = next((item for item in provider["tools"] if item["name"] == receipt["tool"]["name"]), None)
    if tool != receipt["tool"]:
        raise VisualDecisionError("Affinity tool schema pin differs")
    for binding in [*receipt["inputs"], *receipt["outputs"]]:
        _verify_bound_file(root, binding, "Affinity operation artifact")
    replay_nonces.add(receipt["nonce"])


def validate_logo_report(report: dict[str, Any], root: Path, observation: dict[str, Any], mutation_receipts: dict[str, dict[str, Any]] | None = None, *, project_root: Path | None = None, run_id: str | None = None, key: bytes | None = None, replay_nonces: set[str] | None = None, now: int | None = None) -> None:
    validate_schema(report, "logo")
    validate_affinity_observation(observation, root, project_root=project_root, run_id=run_id, now=now)
    if report["affinity_observation_sha256"] != sha256_bytes(canonical_json(observation)):
        raise VisualDecisionError("logo report does not bind the Affinity observation")
    variants = {candidate["variant"] for candidate in report["candidates"]}
    if variants != {"mark", "lockup-horizontal", "lockup-stacked", "favicon"}:
        raise VisualDecisionError("logo report must cover mark, horizontal/stacked lockups, and favicon")
    for candidate in report["candidates"]:
        if candidate["creation_origin"] == "affinity-mcp" and observation["status"] != "available":
            raise VisualDecisionError("Affinity-authored logo conflicts with unavailable observation")
        if candidate["creation_origin"] == "affinity-mcp":
            receipt = (mutation_receipts or {}).get(candidate["id"])
            if receipt is None or project_root is None or run_id is None or key is None or replay_nonces is None or now is None:
                raise VisualDecisionError("each Affinity-authored logo requires its own complete mutation receipt")
            validate_affinity_mutation_receipt(receipt, observation, root, project_root=project_root, run_id=run_id, key=key, replay_nonces=replay_nonces, now=now)
            if receipt["operation"] == "create-icon-variant":
                raise VisualDecisionError(f"Affinity receipt operation belongs to the icon domain: {candidate['id']}")
            if receipt["outputs"] != [{"candidate_id": candidate["id"], **candidate["asset"]}]:
                raise VisualDecisionError("Affinity receipt output does not bind its logo candidate")
        data = _verify_bound_file(root, candidate["asset"], f"logo {candidate['id']}")
        checks = _svg_static_checks(data, f"logo {candidate['id']}", candidate["asset_id"])
        if candidate["objective_checks"] != checks:
            raise VisualDecisionError(f"logo objective checks differ: {candidate['id']}")
        expected_reviews = {"size-16", "size-24", "size-32", "size-48", "size-64", "size-128", "minimum-width", "monochrome", "clipping", "legibility"}
        if set(candidate["human_reviews"]) != expected_reviews:
            raise VisualDecisionError(f"logo human-review matrix is incomplete: {candidate['id']}")
        if set(candidate["human_reviews"].values()) - {"required", "pass", "fail"}:
            raise VisualDecisionError(f"logo human-review status is invalid: {candidate['id']}")
        if not checks["monochrome_static"] or "fail" in candidate["human_reviews"].values():
            raise VisualDecisionError(f"logo candidate is not review-eligible: {candidate['id']}")
    if report["objective_status"] != "pass":
        raise VisualDecisionError("logo objective status is not derived pass")


_ICON_GEOMETRY = {"d", "points", "x", "y", "x1", "x2", "y1", "y2", "cx", "cy", "r", "rx", "ry", "width", "height"}


def _icon_static_checks(data: bytes, label: str, asset_id: str) -> dict[str, Any]:
    """The logo SVG checks plus what an icon set is judged on: grid and stroke."""
    base = _svg_static_checks(data, label, asset_id)
    root = ET.fromstring(data.decode("utf-8"))
    widths: set[float] = set(); caps: set[str] = set(); joins: set[str] = set(); geometry: list[float] = []
    for element in root.iter():
        for attribute, value in element.attrib.items():
            name = attribute.split("}")[-1]
            if name in {"transform", "gradientTransform"}:
                # A transform moves every coordinate off the grid it was checked on.
                raise VisualDecisionError(f"{label} carries a transform the grid check cannot measure")
            if name == "stroke-width": widths.add(float(value))
            elif name == "stroke-linecap": caps.add(ascii_lower(value))
            elif name == "stroke-linejoin": joins.add(ascii_lower(value))
            elif name in _ICON_GEOMETRY and element is not root:
                geometry.extend(float(item) for item in re.findall(r"-?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)", value))
    return {"view_box": base["view_box"], "color_count": base["color_count"], "monochrome_static": base["monochrome_static"],
            "grid_aligned": all(value == int(value) for value in geometry),
            "stroke_widths": sorted(widths), "stroke_linecaps": sorted(caps), "stroke_linejoins": sorted(joins),
            "canonical_svg_sha256": base["canonical_svg_sha256"]}


def _icon_set_signature(checks: dict[str, Any]) -> tuple[Any, ...]:
    """What every member of one set shares: canvas, stroke and paint."""
    return (checks["view_box"], tuple(checks["stroke_widths"]), tuple(checks["stroke_linecaps"]),
            tuple(checks["stroke_linejoins"]), checks["color_count"])


def _require_icon_build_rules(checks: dict[str, Any], rules: dict[str, Any], label: str) -> None:
    """ADR 0006: a build-rule violation prevents release and is not discussed."""
    grid = rules["grid_units"]
    if checks["view_box"] != f"0 0 {grid} {grid}" or not checks["grid_aligned"]:
        raise VisualDecisionError(f"icon leaves the {grid}-unit grid: {label}")
    if checks["stroke_widths"] != [rules["stroke_width"]]:
        raise VisualDecisionError(f"icon stroke width differs from the build rule: {label}")
    if checks["stroke_linecaps"] != [rules["stroke_linecap"]] or checks["stroke_linejoins"] != [rules["stroke_linejoin"]]:
        raise VisualDecisionError(f"icon stroke linecap/linejoin differs from the build rule: {label}")
    if checks["color_count"] != rules["color_count"]:
        raise VisualDecisionError(f"icon colour count differs from the build rule: {label}")


def validate_icon_report(report: dict[str, Any], root: Path, observation: dict[str, Any], mutation_receipts: dict[str, dict[str, Any]] | None = None, *, project_root: Path | None = None, run_id: str | None = None, key: bytes | None = None, replay_nonces: set[str] | None = None, now: int | None = None) -> None:
    validate_schema(report, "icon")
    validate_affinity_observation(observation, root, project_root=project_root, run_id=run_id, now=now)
    if report["affinity_observation_sha256"] != sha256_bytes(canonical_json(observation)):
        raise VisualDecisionError("icon report does not bind the Affinity observation")
    # ADR 0005/0007: the benchmark and the owner-facing judgement follow the
    # operating mode - blind test and distinctness in a supplement, brand fit
    # and the quality reference in an own style.
    supplement = report["operating_mode"] == "ergaenzung"
    if report["benchmark"]["role"] != ("style-reference" if supplement else "quality-reference"):
        raise VisualDecisionError("icon benchmark role differs from the operating mode")
    # Set consistency is a statement about the set, so it is measured against
    # the set the owner already confirmed - never against the icons under review.
    # An empty set is legitimate: the first icon founds it and meets the rules alone.
    own = {variant["id"] for icon in report["icons"] for variant in icon["variants"]}
    own_files = {variant["asset"]["sha256"] for icon in report["icons"] for variant in icon["variants"]}
    set_signature: set[tuple[Any, ...]] = set()
    for member in report["confirmed_set"]:
        if member["id"] in own or member["asset"]["sha256"] in own_files:
            raise VisualDecisionError(f"icon set consistency measured against itself: {member['id']}")
        data = _verify_bound_file(root, member["asset"], f"confirmed icon {member['id']}")
        set_signature.add(_icon_set_signature(_icon_static_checks(data, f"confirmed icon {member['id']}", member["id"])))
    mode_reviews = {"blind-test", "distinctness"} if supplement else {"brand-fit", "quality-benchmark"}
    variant_reviews = {f"size-{size}" for size in report["target_sizes_px"]} | {"optical-weight", "set-consistency"}
    for icon in report["icons"]:
        values = [variant["value"] for variant in icon["variants"]]
        if len(values) != len(set(values)) or (icon["axis"] is None) != (len(values) == 1):
            raise VisualDecisionError(f"icon axis and variant values differ: {icon['icon_id']}")
        if any(variant["id"] != f"{icon['icon_id']}-{variant['value']}" for variant in icon["variants"]):
            raise VisualDecisionError(f"icon variant file is not named <icon>-<value>: {icon['icon_id']}")
        expected = mode_reviews | ({"variant-pair"} if icon["axis"] is not None else set())
        matrices = [(icon["icon_id"], icon["human_reviews"], expected)] + [(variant["id"], variant["human_reviews"], variant_reviews) for variant in icon["variants"]]
        for label, reviews, wanted in matrices:
            if set(reviews) != wanted:
                raise VisualDecisionError(f"icon human-review matrix is incomplete: {label}")
            if "fail" in reviews.values():
                raise VisualDecisionError(f"icon is not review-eligible: {label}")
        for variant in icon["variants"]:
            if variant["creation_origin"] == "affinity-mcp":
                # One receipt per variant file: create-icon-variant writes exactly one.
                if observation["status"] != "available":
                    raise VisualDecisionError("Affinity-authored icon conflicts with unavailable observation")
                receipt = (mutation_receipts or {}).get(variant["id"])
                if receipt is None or project_root is None or run_id is None or key is None or replay_nonces is None or now is None:
                    raise VisualDecisionError(f"each Affinity-authored icon variant requires its own complete mutation receipt: {variant['id']}")
                validate_affinity_mutation_receipt(receipt, observation, root, project_root=project_root, run_id=run_id, key=key, replay_nonces=replay_nonces, now=now)
                if receipt["operation"] != "create-icon-variant":
                    raise VisualDecisionError(f"Affinity receipt operation is not create-icon-variant: {variant['id']}")
                if receipt["outputs"] != [{"candidate_id": variant["id"], **variant["asset"]}]:
                    raise VisualDecisionError(f"Affinity receipt output does not bind its icon variant: {variant['id']}")
            data = _verify_bound_file(root, variant["asset"], f"icon {variant['id']}")
            checks = _icon_static_checks(data, f"icon {variant['id']}", variant["id"])
            if variant["objective_checks"] != checks:
                raise VisualDecisionError(f"icon objective checks differ: {variant['id']}")
            _require_icon_build_rules(checks, report["build_rules"], variant["id"])
            if set_signature and set_signature != {_icon_set_signature(checks)}:
                raise VisualDecisionError(f"icon breaks set consistency with the confirmed set: {variant['id']}")

def _field_name_tokens(key: str) -> set[str]:
    """Every token a JSON field name spells, under both folds.

    This is a denylist, so the two folds are unioned rather than one replacing
    the other: Python's Unicode ``lower()`` can only ever fold more spellings
    onto a forbidden token, and the ASCII fold catches the one spelling
    (U+212A) that only ASCII case-insensitivity is meant to reach.  Neither
    fold alone may be removed without losing a rejection.
    """
    return set(re.split(r"[-_]", key.lower())) | set(re.split(r"[-_]", ascii_lower(key)))


def _validate_icon_axes(value: dict[str, Any], candidate_id: str) -> None:
    """A variant is an axis at the icon, and each axis value yields one file.

    An axis with a single value is no state, and several files without an axis
    would be several icons under one name - the schema cannot tell either apart.
    """
    icon_ids = [icon["icon_id"] for icon in value["icons"]]
    if len(icon_ids) != len(set(icon_ids)):
        raise VisualDecisionError(f"icon appears more than once in style candidate: {candidate_id}")
    for icon in value["icons"]:
        if (icon["axis"] is None) != (len(icon["variants"]) == 1):
            raise VisualDecisionError(f"icon axis and variant count differ: {candidate_id}/{icon['icon_id']}")


def validate_candidate_set(value: dict[str, Any], root: Path) -> None:
    validate_schema(value, "candidates")
    forbidden_tokens = {"owner", "decision", "selection", "selected", "confirmed", "approved", "normative", "recommendation"}
    def walk(item: Any) -> None:
        if isinstance(item, dict):
            # A denylist, so this is one of the two places that keeps the
            # *wider* Unicode fold beside the ASCII one instead of replacing
            # it: str.lower() can only ever fold more spellings onto a
            # forbidden token, never fewer, and dropping it would be the one
            # direction that loses a rejection.  The ASCII fold is asked as
            # well, so a spelling that only ASCII folding reaches is caught too.
            collision = {key for key in item if _field_name_tokens(key) & forbidden_tokens}
            if collision:
                raise VisualDecisionError(f"candidate set contains normative field: {sorted(collision)}")
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
    for candidate_value in value["candidates"]:
        walk(candidate_value["value"])
    ids = {candidate["id"] for candidate in value["candidates"]}
    if len(ids) != len(value["candidates"]):
        raise VisualDecisionError("candidate ids are not unique")
    for candidate in value["candidates"]:
        if sha256_bytes(canonical_json(candidate["value"])) != candidate["value_sha256"]:
            raise VisualDecisionError(f"candidate value hash differs: {candidate['id']}")
        # The third owner-supplied free-text channel of a bundle, beside the
        # family name and the recommendation rationale.  The schema asks only
        # for a non-empty string, so the same separators and direction switches
        # can enter here; see _reject_sheet_control for why all three answer one
        # rule rather than one rule each.
        _reject_sheet_control(candidate["label"], f"candidate label {candidate['id']}")
        if candidate["kind"] == "icon":
            _validate_icon_axes(candidate["value"], candidate["id"])
    for recommendation in value["recommendations"]:
        if recommendation["candidate_id"] not in ids:
            raise VisualDecisionError("recommendation references an unknown candidate")
        candidate = next(item for item in value["candidates"] if item["id"] == recommendation["candidate_id"])
        if candidate["kind"] != recommendation["kind"]:
            raise VisualDecisionError("recommendation kind differs from its candidate")
        # The rationale is free prose and the sheet prints it verbatim (escaped).
        # Held here rather than in the renderer so a bundle this call accepts is
        # one the renderer can write - and so the refusal names the sentence.
        _reject_sheet_control(recommendation["rationale"], f"recommendation rationale {recommendation['candidate_id']}")


def _report_human_fail(report: dict[str, Any]) -> bool:
    candidates = report.get("candidates", [])
    return any("fail" in item.get("human_reviews", {}).values() for item in candidates)


def candidate_context_sha256(candidate_set: dict[str, Any]) -> str:
    projection = json.loads(canonical_json(candidate_set))
    for candidate in projection["candidates"]:
        candidate["value"]["report"]["sha256"] = "0" * 64
        candidate["value_sha256"] = sha256_bytes(canonical_json(candidate["value"]))
    return sha256_bytes(canonical_json(projection))


def validate_bundle(project_root: Path, run_id: str, now: int, *, sidecar_root: Path | None = None, affinity_auth: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate one bundle while holding the external affinity replay lock.

    The lock spans the complete critical section - reading the ledger,
    validating every receipt against it and committing the new generation -
    so that two concurrent processes cannot consume the same nonces.  It is
    skipped only when ``affinity_auth`` is ``None``, i.e. when there is no
    ledger to protect; for every other descriptor the acquisition either
    succeeds or raises, so the commit below is never reached unlocked.

    The external paths of the descriptor are resolved exactly once, here, and
    the resolved pair is handed to the lock and to the guarded section alike.
    Resolving twice would let an ``os.PathLike`` with a non-constant
    ``__fspath__`` name one file to the lock and a different one to the checks
    the lock protects, which is the same nonces spent twice.
    """
    descriptor = None if affinity_auth is None else _affinity_descriptor(affinity_auth)
    lock = _affinity_ledger_lock(project_root, descriptor)
    try:
        return _validate_bundle_locked(project_root, run_id, now, sidecar_root=sidecar_root, affinity_auth=affinity_auth, descriptor=descriptor)
    finally:
        if lock is not None: os.close(lock)


def verify_bundle(project_root: Path, run_id: str, now: int, *, sidecar_root: Path | None = None, affinity_auth: dict[str, Any] | None = None) -> dict[str, Any]:
    """Answer exactly what :func:`validate_bundle` answers, without spending the receipts.

    This is ``validate_bundle`` with ``commit=False``: the same lock, the same
    locked body, the same result, and the *only* difference is that the ledger
    generation does not advance.  Every check runs either way - the ledger is
    read under the same lock and a nonce already in it is refused exactly as
    before - so this entry point cannot admit a bundle ``validate_bundle``
    would refuse.  It is strictly the weaker capability of the two.

    Why it is public since r1f.  ``render_sheet`` has verified without spending
    since r1e, but it reached the locked body through a private name, and the
    *other* caller that needs the same primitive - the adapter's
    ``prepare-visual-confirmation`` - could only reach the consuming form.  It
    therefore spent the receipts *before* it had looked at the owner's
    selection at all, so a single typo in one candidate id burned a one-shot
    resource and left the run with no reachable correction: exactly the
    false-refusal class this module exists to avoid.  With the verification
    available by name, an operation can establish that it will succeed and
    only then consume, which is the order every operation over a one-shot
    resource has to have.

    This does not create a replay building block.  Consumption still happens in
    exactly one place - ``validate_bundle``, under the lock, advancing the
    generation - and the moment it has happened every later presentation,
    verification included, is refused.  A caller that verifies and then commits
    is doing what a single ``validate_bundle`` call did before, with the
    refusals moved in front of the commit; a caller that only verifies has
    changed nothing at all.
    """
    descriptor = None if affinity_auth is None else _affinity_descriptor(affinity_auth)
    lock = _affinity_ledger_lock(project_root, descriptor)
    try:
        return _validate_bundle_locked(project_root, run_id, now, sidecar_root=sidecar_root, affinity_auth=affinity_auth, descriptor=descriptor, commit=False)
    finally:
        if lock is not None: os.close(lock)


def _validate_bundle_locked(project_root: Path, run_id: str, now: int, *, sidecar_root: Path | None = None, affinity_auth: dict[str, Any] | None = None, descriptor: tuple[Path, Path] | None = None, commit: bool = True) -> dict[str, Any]:
    """Validate one bundle inside the held replay lock.

    ``commit`` decides whether the run *spends* the mutation receipts or merely
    *verifies* them.  Every check above the commit runs either way - the ledger
    is read under the same lock and a nonce already in it is refused exactly as
    before - so ``commit=False`` cannot admit a receipt ``commit=True`` would
    reject; it only declines to advance the generation.

    The distinction exists because the accepted owner flow reaches the same
    receipts twice: once to render the unselected candidate sheet the owner
    looks at, and once to bind the selection the owner then confirms.  Only the
    second of those is an operation - the sheet is a read-only, non-normative
    artifact that decides nothing and mutates nothing - so letting the render
    spend the receipt would mean the owner can either *see* the
    Affinity-authored logos or *confirm* them, never both.  Consumption
    therefore stays with the confirming call, which is the only caller that
    leaves ``commit`` at its default, and single-use is unchanged: a receipt is
    written into the external ledger exactly once, and from then on every later
    presentation - a second render included - is refused.
    """
    project_root = exact_project_root(project_root)
    workspace = visual_workspace(project_root, run_id)
    brief = load_json_bytes(secure_read(workspace, "visual-brief.json", "visual brief")[0], "visual brief")
    candidate_bytes = secure_read(workspace, "candidate-set.json", "candidate set")[0]
    candidates = load_json_bytes(candidate_bytes, "candidate set")
    observation = load_json_bytes(secure_read(workspace, "affinity-observation.json", "Affinity observation")[0], "Affinity observation")
    validate_brief(brief, workspace, project_root=project_root, run_id=run_id)
    validate_candidate_set(candidates, workspace)
    sidecar = sidecar_root or ROOT.parents[1]
    # The sidecar exists to be an *independent* second copy of the narrative.
    # A sidecar root the run itself can write is not independent, so its place
    # is enforced by identity rather than merely defaulted to.
    _outside_project(project_root, ((sidecar, "visual source sidecar must remain outside the project"),))
    source_contract = validate_source_contract(candidates["source_review"], sidecar)
    validate_affinity_observation(observation, workspace, project_root=project_root, run_id=run_id, now=now)
    brief_sha = sha256_bytes(canonical_json(brief))
    candidate_sha = sha256_bytes(candidate_bytes); context_sha = candidate_context_sha256(candidates)
    if candidates["brief_sha256"] != brief_sha:
        raise VisualDecisionError("candidate set does not bind the visual brief")
    selected_source = source_contract["selected_dependency"]
    auth_state: tuple[bytes, Path, dict[str, Any], bytes, set[str], dict[str, dict[str, Any]]] | None = None
    if affinity_auth is not None:
        # The descriptor is resolved once, by validate_bundle, and reaches the lock
        # and this section as the very same pair of paths; re-resolving it here is
        # what would let a mutable __fspath__ split the two apart.
        if descriptor is None:
            raise VisualDecisionError("Affinity authentication descriptor is not closed")
        key_path, ledger_path = descriptor
        _outside_project(project_root, ((key_path, "Affinity keys and replay ledger must remain outside the project"),
                                        (ledger_path, "Affinity keys and replay ledger must remain outside the project")))
        owner_key = _external_0600(key_path, "Affinity owner key")
        if len(owner_key) != 32: raise VisualDecisionError("Affinity owner key must contain exactly 32 bytes")
        ledger, ledger_raw = load_replay_ledger(ledger_path, owner_key, project_root, run_id)
        receipts = {candidate_id: load_json_bytes(_verify_bound_file(workspace, binding, f"Affinity receipt {candidate_id}"), f"Affinity receipt {candidate_id}") for candidate_id, binding in affinity_auth["receipts"].items()}
        auth_state = (owner_key, ledger_path, ledger, ledger_raw, set(ledger["nonces"]), receipts)
    eligible: list[dict[str, Any]] = []
    blockers: list[dict[str, str]] = []
    validated_logo_reports: set[str] = set()
    used_receipts: set[str] = set()
    for candidate in candidates["candidates"]:
        if candidate["source_ids"] != [selected_source]:
            raise VisualDecisionError(f"candidate source coverage differs: {candidate['id']}")
        value = candidate["value"]
        if value["domain"] != candidate["kind"]:
            raise VisualDecisionError(f"candidate domain differs: {candidate['id']}")
        report_bytes = _verify_bound_file(workspace, value["report"], f"report {candidate['id']}")
        report = load_json_bytes(report_bytes, f"report {candidate['id']}")
        if report["candidate_set_sha256"] != context_sha:
            raise VisualDecisionError(f"report candidate-set binding differs: {candidate['id']}")
        passed = report.get("objective_status") == "pass" and not _report_human_fail(report)
        if candidate["kind"] == "color":
            validate_color_report(report)
            if report["candidate_id"] != candidate["id"] or report["roles"] != value["roles"]:
                raise VisualDecisionError("color report candidate/value differs")
        elif candidate["kind"] in {"typography", "code-font"}:
            validate_font_report(report, workspace)
            ids = [item["id"] for item in report["candidates"]]
            classes = {item["id"]: item["classification"] for item in report["candidates"]}
            if candidate["kind"] == "typography":
                valid = value["font_candidate_ids"] == [item for item in ids if classes[item] in {"serif", "sans"}]
            else:
                valid = value["code_font_id"] in classes and classes[value["code_font_id"]] == "mono"
            if not valid or value["specimen_sha256"] != report["specimen_sha256"]:
                raise VisualDecisionError("typography report candidate/value differs")
        elif candidate["kind"] == "icon":
            affinity_ids = {variant["id"] for icon in report.get("icons", []) for variant in icon.get("variants", []) if variant.get("creation_origin") == "affinity-mcp"}
            if affinity_ids:
                if auth_state is None: raise VisualDecisionError("Affinity-authored candidates require external authentication and replay state")
                owner_key, _ledger_path, _ledger, _ledger_raw, nonces, receipts = auth_state
                if not affinity_ids <= set(receipts): raise VisualDecisionError("Affinity receipt inventory differs from authored candidates")
                validate_icon_report(report, workspace, observation, receipts, project_root=project_root, run_id=run_id, key=owner_key, replay_nonces=nonces, now=now)
                used_receipts |= affinity_ids
            else:
                validate_icon_report(report, workspace, observation, project_root=project_root, run_id=run_id, now=now)
            # The owner confirms a style (ADR 0001): the value names the icons,
            # their axes and values; the report holds the files that make them.
            shown = [{"icon_id": icon["icon_id"], "axis": icon["axis"], "variants": [variant["value"] for variant in icon["variants"]]} for icon in report["icons"]]
            origins = {variant["creation_origin"] for icon in report["icons"] for variant in icon["variants"]}
            if value["icons"] != shown or origins != {value["creation_origin"]}:
                raise VisualDecisionError("icon report candidate/value differs")
        else:
            report_hash = value["report"]["sha256"]
            if report_hash not in validated_logo_reports:
                affinity_ids = {item["id"] for item in report["candidates"] if item["creation_origin"] == "affinity-mcp"}
                if affinity_ids:
                    if auth_state is None: raise VisualDecisionError("Affinity-authored candidates require external authentication and replay state")
                    owner_key, _ledger_path, _ledger, _ledger_raw, nonces, receipts = auth_state
                    # Logo and icon receipts share one inventory, so each branch asks
                    # only for its own; the closing check below refuses any receipt
                    # that no branch consumed.
                    if not affinity_ids <= set(receipts): raise VisualDecisionError("Affinity receipt inventory differs from authored candidates")
                    validate_logo_report(report, workspace, observation, receipts, project_root=project_root, run_id=run_id, key=owner_key, replay_nonces=nonces, now=now)
                    used_receipts |= affinity_ids
                else:
                    validate_logo_report(report, workspace, observation, project_root=project_root, run_id=run_id, now=now)
                validated_logo_reports.add(report_hash)
            report_items = [item for item in report["candidates"] if (item["variant"] in {"mark", "favicon"}) == (candidate["kind"] == "logo")]
            value_ids = value["logo_candidate_ids"] if candidate["kind"] == "logo" else value["lockup_candidate_ids"]
            if value_ids != [item["id"] for item in report_items] or any(item["creation_origin"] != value["creation_origin"] for item in report_items):
                raise VisualDecisionError("logo report candidate/value differs")
        expected_status = "candidate" if passed else "blocked"
        if candidate["status"] != expected_status:
            raise VisualDecisionError(f"candidate eligibility status differs: {candidate['id']}")
        item = {"id": candidate["id"], "kind": candidate["kind"], "value": value, "value_sha256": candidate["value_sha256"]}
        (eligible if passed else blockers).append(item if passed else {"id": candidate["id"], "reason": "report-not-review-eligible"})
    eligible_ids = {item["id"] for item in eligible}
    if any(item["candidate_id"] not in eligible_ids for item in candidates["recommendations"]):
        raise VisualDecisionError("recommendation references an ineligible candidate")
    if auth_state is not None:
        owner_key, ledger_path, ledger, ledger_raw, nonces, receipts = auth_state
        if set(receipts) != used_receipts: raise VisualDecisionError("Affinity receipt inventory differs from authored candidates: unused receipts")
        if commit and nonces != set(ledger["nonces"]): persist_replay_ledger(ledger_path, owner_key, ledger, ledger_raw, nonces)
    return {"candidate_set_sha256": candidate_sha, "eligible_candidates": eligible, "blockers": blockers}


def _safe_output_dir(path: Path, name: str = "sheet") -> int:
    # ``name`` is the artifact's one fixed directory: ``sheet`` for the decision
    # sheet, ``playground`` for the playground.  They are siblings on purpose,
    # so that neither can be written into the other.
    if path.name != name: raise VisualDecisionError(f"{name} output name differs")
    # Defense in depth: this is only reachable through visual_workspace today,
    # which is where the sealed run is kept out.  It costs one string test to
    # not depend on that remaining true.
    if _names_protected_run(path): raise VisualDecisionError("protected first-ci path is forbidden")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(path.parent, flags)
    try:
        try: os.mkdir(path.name, 0o700, dir_fd=parent_fd)
        except FileExistsError: pass
        output_fd = os.open(path.name, flags, dir_fd=parent_fd)
        if os.listdir(output_fd):
            os.close(output_fd); raise VisualDecisionError("sheet output must be absent or empty")
        return output_fd
    finally: os.close(parent_fd)


def _write_new(directory_fd: int, name: str, data: bytes) -> None:
    if "/" in name or name in {"", ".", ".."}: raise VisualDecisionError("unsafe output member")
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
    try:
        os.write(fd, data); os.fsync(fd)
    finally: os.close(fd)


# ``frame-ancestors`` used to stand at the end of this policy.  It is removed,
# not forgotten: a policy delivered through <meta http-equiv> may not carry
# ``frame-ancestors``, ``report-uri`` or ``sandbox``, and Blink logs "is ignored
# when delivered via a <meta> element" for every single sheet it renders.  A
# sheet has exactly one delivery form - the meta below, written by
# :func:`_render_sheet` - so the directive was never in force anywhere, and a
# policy that names a protection it does not have is a false statement about
# the artifact.  What the removal costs is nothing that is in force today: the
# sheet has no script (``script-src 'none'``), no form target
# (``form-action 'none'``) and no secret, so being framed buys an attacker no
# capability.  Should a sheet ever be *served* rather than opened from disk, the
# directive belongs in the response header, which is the only place it works.
SHEET_CSP = "default-src 'none'; script-src 'none'; connect-src 'none'; style-src 'self'; img-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'"

# The closed document form of a sheet.
#
# ``html.parser.HTMLParser`` is not an HTML5 parser, and *every* place where it
# and Blink disagree is a hole in this validator: what the Python side does not
# see, the browser still executes.  R16 measured four such divergences at once -
# XML CDATA sections (``<![CDATA[ > <meta http-equiv=refresh ...>]]>`` is one
# invisible ``unknown_decl`` here and a bogus comment ending at the first ``>``
# there), the solidus on a non-void tag, duplicate attributes (HTML5 keeps the
# *first*, ``dict(attrs)`` the last), and a ``<meta>`` policy outside ``<head>``
# (which Blink ignores completely).  That list is open-ended by construction: it
# is the difference between two independent implementations, not a bug with a
# fix.  Patching the four callbacks would leave the fifth.
#
# A sheet is a *generated* artifact, so it does not have to be allowed to be
# arbitrary HTML.  The form below is therefore enumerated **positively** and
# everything outside it fails closed: one lowercase doctype, one ``<html>`` with
# one ``<head>`` and one ``<body>``, tags and attributes spelled exactly one way,
# every attribute value double-quoted and free of ``&``, ``<`` and ``>``,
# balanced nesting with a fixed void-element set, ``<head>`` restricted to
# ``meta``/``title``/``link`` so that no element can implicitly close it, and no
# markup declaration, comment or processing instruction at all.  Inside that form
# there is nothing left for two parsers to disagree about.
#
# :class:`_SheetParser` scans that form itself and then has ``html.parser`` read
# the very same bytes as a second, independent reader; if the two do not produce
# the identical tag sequence, the disagreement is itself a rejection.  Neither
# reader is trusted to be the browser - they are required to agree, inside a
# grammar narrow enough that agreement means all three read the same document.
SHEET_VOID_TAGS = frozenset({"meta", "link", "img"})
SHEET_HEAD_TAGS = frozenset({"meta", "title", "link"})
SHEET_TEXT_ONLY_TAGS = frozenset({"title"})
# The content model of <body>, enumerated positively for the same reason as the
# rest of the form.  Balanced tags alone do not make the two readers agree on
# the TREE: html.parser nests whatever it is given, while the HTML5 tree builder
# rearranges several shapes on its own (WHATWG HTML 13.2.6.4.7 "in body" and
# 13.2.6.4.9-13 "in table" and below).  Each group below closes one of those:
#
# * A start tag for address, article, div, footer, main, p, pre, section, table
#   (no-quirks), ul or h1-h6 CLOSES an open <p> first.  <p><div>x</div></p> is
#   p>div here and p, div, p (the stray </p> opens an empty one) in Blink.  So a
#   <p> - and every element that may stand in one - carries phrasing only.
# * An h1-h6 start tag pops a current h1-h6: <h2><h3>x</h3></h2> is two sibling
#   headings in Blink.  Headings carry phrasing only.
# * An <li> start tag closes an open <li>, walking through <div> and <p> to find
#   it: <li><div><li> is two sibling items.  An <li> stands directly in <ul>.
# * Tables are where R16/R17's class lives.  Text or any non-table element
#   directly in table, thead, tbody or tr is FOSTER PARENTED - moved in front of
#   the table; a <tr> directly in <table> gets an implied <tbody>; a <td> or <th>
#   outside a <tr> gets an implied <tr> inside a table and is dropped outright
#   outside one; a <table> in a cell is a second grid.  Every one of these is a
#   document whose cells Blink counts differently from this scanner.  So the
#   table is closed STRUCTURALLY: <table> carries an optional <thead> and then
#   exactly one explicit <tbody>; <thead> and <tbody> carry <tr> only; <tr>
#   carries <th> and <td> only; cells carry phrasing only; and no text at all -
#   not even ASCII whitespace, which HTML5 would keep - stands directly in
#   table, thead, tbody or tr.  Cells carry no attribute (no colspan/rowspan):
#   a span changes which column a number is read under.
# * <pre> carries text only, and HTML5 drops a line break directly after the
#   <pre> start tag, so a <pre> that opens with one is refused (see _scan).
SHEET_PHRASING_TAGS = frozenset({"strong", "code", "span", "img"})
SHEET_CHILDREN: dict[str, frozenset[str]] = {
    **{tag: SHEET_PHRASING_TAGS for tag in ("p", "h1", "h2", "h3", "strong", "code", "span", "th", "td")},
    "pre": frozenset(),
    "ul": frozenset({"li"}),
    "li": SHEET_PHRASING_TAGS | {"ul"},
    "table": frozenset({"thead", "tbody"}),
    "thead": frozenset({"tr"}),
    "tbody": frozenset({"tr"}),
    "tr": frozenset({"th", "td"}),
}
SHEET_PARENTS: dict[str, frozenset[str]] = {
    "li": frozenset({"ul"}),
    "thead": frozenset({"table"}),
    "tbody": frozenset({"table"}),
    "tr": frozenset({"thead", "tbody"}),
    "th": frozenset({"tr"}),
    "td": frozenset({"tr"}),
}
SHEET_NO_TEXT_TAGS = frozenset({"table", "thead", "tbody", "tr"})
_SHEET_DOCTYPE = re.compile(r"<!doctype html>", re.IGNORECASE)
# ``\s`` on ``str`` is Unicode whitespace, and HTML5 tag syntax is ASCII
# whitespace: with ``\s`` these two grammars read ``</p\u00a0>`` as an end tag
# for ``p`` while Blink reads the tag name ``p\u00a0``, finds no such open
# element and drops the end tag on the floor - one document, two trees.  The
# class is spelled out rather than left to ``re.ASCII``, which would also admit
# U+000B.
_SHEET_MARKUP = re.compile(rf'<(/?)([a-z][a-z0-9]*)((?:{_ASCII_SPACE}+[a-z][a-z0-9-]*(?:="[^"<>&]*")?)*){_ASCII_SPACE}*(/?)>')
_SHEET_ATTRIBUTE = re.compile(rf'{_ASCII_SPACE}+([a-z][a-z0-9-]*)(?:="([^"<>&]*)")?')
# Everything a browser folds to U+FFFD or drops outright, and which therefore
# cannot mean the same thing to two readers.  Tab, LF and CR stay.
#
# The C0 range was the whole rule until r25 measured the rest of the same class
# one level up: code points that are not C0 controls, so they passed this
# pattern, and that still make one string read as two different things to two
# readers.  The sheet carried all of them raw while the filled review, which
# writes the same owner strings with ``ensure_ascii=True``, escaped them.
#
#  * U+0085, U+2028 and U+2029 are line terminators to some readers and
#    ordinary content to others.  A family name carrying one of them is a single
#    record on the page and two lines in a terminal, a diff or a log.
#  * U+000C is the fourth character of that same group, and the one this class
#    kept the longest.  It is ASCII whitespace to HTML5 and a whitespace token to
#    css-syntax-3 - both tag grammars and the CSS tokenizer go on carrying it, and
#    ``ASCII_WHITESPACE`` is unchanged - but it is a line boundary to
#    ``str.splitlines()``, so a family name carrying one made the sheet read as
#    three lines more for a terminal, a diff or a log than for a reader counting
#    U+000A.  That is the same defect as the three above, by the same mechanism.
#    Measured as a set, the class now covers every ``str.splitlines()`` boundary
#    except U+000A and U+000D, which the document form needs, and the whole of
#    Unicode's ``Bidi_Control``.  The reach is wider than the owner channels: a
#    stylesheet may no longer carry U+000C either.  This module writes none, and
#    what css-syntax-3 says about it is measured at the scanner primitive, which
#    is where the tokenizer is.
#  * The bidi formatting characters reorder the text AROUND them: U+061C,
#    U+200E and U+200F, the embeddings and overrides U+202A-U+202E, and the
#    isolates U+2066-U+2069.  An override inside a font family name shows the
#    owner a name that reads differently from the bytes he is about to confirm,
#    and it drags the neighbouring text into a visual order the markup does not
#    have.  ``html.escape`` cannot help with either group - it carries the
#    markup characters and nothing else.
#
# There is no honest use for one of these inside a generated comparison sheet:
# the document declares one direction, the sample text is a module constant, and
# a name whose script needs bidi gets it from the script, not from a control.
# The two groups are held on the same object as the C0 range so the HTML, every
# CSS member and every owner-supplied string enter through one rule instead of
# three that can drift apart.
_SHEET_CONTROL = re.compile("[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f\\x7f\\x85"
                            "\\u061c\\u200e\\u200f\\u202a-\\u202e"
                            "\\u2028\\u2029\\u2066-\\u2069]")


def _reject_sheet_control(value: str, label: str) -> None:
    """Hold one owner-supplied string to what the sheet's document form allows.

    ``_render_sheet`` interpolates twelve values into the page.  Ten of them are
    enums, id patterns, hex colours or a module constant - the schema decides
    them, and the renderer cannot write anything the document form refuses.  Two
    are unbounded free text: a font ``family``, which is whatever the OpenType
    ``name`` table of an owner-supplied file says, and a recommendation
    ``rationale``, which is prose written by an agent or a human.  ``html.escape``
    carries the markup characters; it carries neither a C0 character nor one of
    the separators and bidi controls the pattern names.

    A candidate ``label`` is the third string of that kind.  It does not reach
    the sheet at all - the sheet titles a card by candidate id - but it is
    owner-supplied free text under the same schema, and the playground shows it.
    It is held here rather than only where it is shown, so the three free-text
    channels of one bundle answer one rule and a later reader cannot find two of
    them closed and the third open.

    Left alone, a bundle that ``validate_bundle`` had just accepted made
    ``_render_sheet`` fail inside its own ``validate_sheet_manifest`` with *sheet
    document form: forbidden control character* - a message that names the HTML
    while the cause sits in a font file or in a sentence.  That is the r17 class
    over an owner-supplied channel: fail-closed, so no hole, but an unrenderable
    bundle and a refusal at the wrong artifact.

    The statement is therefore made where the string enters, and the refusal
    carries the name of the thing that is wrong.  The three call sites stand
    earlier in this file than the pattern on purpose: the rule belongs to the
    sheet, the obligation belongs to whatever hands the sheet a string.
    """
    if _SHEET_CONTROL.search(value):
        raise VisualDecisionError(f"{label} contains a forbidden control character")


class _SheetHTMLView(HTMLParser):
    """The second reading of a sheet, by ``html.parser``, used only for agreement.

    Every callback that can receive markup ``html.parser`` resolves differently
    from HTML5 is overridden and fails closed - a markup declaration other than
    the leading doctype, an XML CDATA section (``unknown_decl``), a processing
    instruction, a comment.  What is left is the tag stream, which the caller
    compares element for element with its own scan.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[tuple[str, str, tuple[tuple[str, str | None], ...]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.events.append(("start", tag, tuple(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.events.append(("start", tag, tuple(attrs)))

    def handle_endtag(self, tag: str) -> None:
        self.events.append(("end", tag, ()))

    def handle_decl(self, decl: str) -> None:
        if self.events or ascii_lower(ascii_strip(decl)) != "doctype html":
            raise VisualDecisionError("sheet contains a markup declaration outside the leading doctype")

    def unknown_decl(self, data: str) -> None:
        # This is where an XML CDATA section lands, and where it used to vanish.
        raise VisualDecisionError("sheet contains a markup declaration outside the leading doctype")

    def handle_pi(self, data: str) -> None:
        raise VisualDecisionError("sheet contains a processing instruction")

    def handle_comment(self, data: str) -> None:
        raise VisualDecisionError("sheet contains an HTML comment")


class _SheetParser:
    # table, thead, tbody, tr, th and td since the render-sheet fix, owner
    # decision 2026-09-11, closed structurally by SHEET_CHILDREN/SHEET_PARENTS.
    allowed_tags = {"html", "head", "meta", "title", "link", "body", "main", "h1", "h2", "h3", "p", "strong", "section", "article", "div", "span", "code", "pre", "img", "ul", "li", "footer",
                    "table", "thead", "tbody", "tr", "th", "td"}
    allowed_http_equiv = {"Content-Security-Policy"}
    # ``rel`` was collected but never *limited*, and the manifest schema knows no
    # file extension, so <link rel="stylesheet" href="assets/second.css"> was a
    # perfectly regular member whose CSS nothing ever looked at.  The relation is
    # therefore an allowlist of exactly one value, spelled exactly, and every
    # link has to name a target - the same fail-closed shape the meta pragma got.
    allowed_link_rel = {"stylesheet"}
    # ``charset`` sat in the attribute allowlist below and its *value* was never
    # looked at, while validate_sheet_manifest decodes index.html as UTF-8
    # unconditionally.  <meta charset="shift_jis"> was accepted, and Blink obeys
    # it: document.characterSet was Shift_JIS on a sheet this module had
    # certified.  Validator and renderer then read the same bytes as two
    # different character streams, and the artifact whose only purpose is human
    # inspection showed a text nobody had validated.  The declaration is
    # therefore an allowlist of exactly the encoding the validator assumes,
    # spelled exactly, exactly once, and in the one position where the HTML5
    # encoding sniffer is guaranteed to find it.
    allowed_charset = "utf-8"
    # ``pre`` was missing here while _render_sheet writes <pre class="specimen">
    # for every mono font candidate, so the renderer's own sheet was refused by
    # its own validator the moment a code font was in the bundle.  Nothing
    # noticed because no test ever put a rendered sheet through
    # validate_sheet_manifest; one does now.
    # ``span`` joined this map for the numeral rows: a row shows the digits in the
    # candidate face and names the feature beside them in the document face, so the
    # name needs a class of its own - without one it runs in the candidate's font at
    # the candidate's normalised size, and the rows stop being comparable, which is
    # the whole point of showing them.  This is the same shape the ``pre`` addition
    # above had, and ``class`` carries no capability: no URL, no script, no pragma,
    # and its value is generated rather than owner text.
    allowed_attrs = {"html": {"lang"}, "meta": {"charset", "name", "content", "http-equiv"}, "link": {"rel", "href"}, "section": {"id"}, "article": {"class"}, "div": {"class"}, "p": {"class"}, "pre": {"class"}, "span": {"class"}, "img": {"src", "alt"}}
    body_tags = allowed_tags - {"html", "head", "body"} - set(SHEET_HEAD_TAGS)

    def __init__(self) -> None:
        self.csp: str | None = None
        self.charset: str | None = None
        # Character offset of the charset declaration, so the caller can hold it
        # to the 1024-byte window HTML5 gives the encoding sniffer.
        self.charset_offset: int | None = None
        self.references: set[str] = set()
        self.sections: set[str] = set()
        self.stylesheets: set[str] = set()
        self._chunks: list[str] = []

    def feed(self, data: str) -> None:
        self._chunks.append(data)

    def close(self) -> None:
        page = "".join(self._chunks)
        events = self._scan(page)
        view = _SheetHTMLView()
        view.feed(page)
        view.close()
        if view.events != events:
            # Two readers, one document, one answer.  Any divergence at all -
            # a construct html.parser resolves differently, a construct the scan
            # above resolves differently - is a document this apparatus cannot
            # make a statement about, so it makes none.
            raise VisualDecisionError("sheet HTML is read differently by the two independent parsers")

    # -- the positively enumerated form ------------------------------------

    def _attributes(self, tag: str, blob: str) -> list[tuple[str, str | None]]:
        attrs: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        position = 0
        for match in _SHEET_ATTRIBUTE.finditer(blob):
            if match.start() != position:
                raise VisualDecisionError(f"sheet document form: unreadable attribute on {tag}")
            position = match.end()
            name = match.group(1)
            if name in seen:
                # HTML5 keeps the *first* duplicate, ``dict(attrs)`` the last,
                # so ``<img src=REMOTE src=LOCAL>`` was checked as the local one
                # and loaded as the remote one.  Neither reading is adopted:
                # a duplicate is refused.
                raise VisualDecisionError(f"sheet contains a duplicate HTML attribute: {tag} {name}")
            seen.add(name)
            attrs.append((name, match.group(2)))
        if position != len(blob):
            raise VisualDecisionError(f"sheet document form: unreadable attribute on {tag}")
        return attrs

    def _text(self, text: str, stack: list[str]) -> None:
        # HTML5 whitespace, not str.strip().  Every one of the other 24 code
        # points Python calls whitespace is a *character token* to Blink, and a
        # character token in "in head" closes <head> implicitly - after which
        # the policy meta stands in <body> and is ignored outright.  One
        # U+00A0 after the doctype, inside <html> or inside <head> was enough,
        # and the validator said ACCEPTED to all three.
        #
        # Asked first, before the blank test: in table, thead, tbody and tr
        # every character token is a question of foster parenting, so the form
        # carries none there at all.
        if text and stack and stack[-1] in SHEET_NO_TEXT_TAGS:
            raise VisualDecisionError(f"sheet document form: text directly in <{stack[-1]}>")
        if ascii_blank(text):
            return
        if not stack or (stack[-1] not in SHEET_TEXT_ONLY_TAGS and "body" not in stack):
            raise VisualDecisionError("sheet document form: text outside <body>")

    def _scan(self, page: str) -> list[tuple[str, str, tuple[tuple[str, str | None], ...]]]:
        if _SHEET_CONTROL.search(page):
            raise VisualDecisionError("sheet document form: forbidden control character")
        doctype = _SHEET_DOCTYPE.match(page)
        if doctype is None:
            raise VisualDecisionError("sheet document form: the document does not open with the HTML5 doctype")
        events: list[tuple[str, str, tuple[tuple[str, str | None], ...]]] = []
        stack: list[str] = []
        head_state = body_state = 0  # 0 unseen, 1 open, 2 closed
        head_children = 0
        policies = 0
        # The children each open <table> has had so far, innermost last.
        tables: list[list[str]] = []
        index, length = doctype.end(), len(page)
        while index < length:
            opening = page.find("<", index)
            self._text(page[index:opening if opening >= 0 else length], stack)
            if opening < 0:
                break
            match = _SHEET_MARKUP.match(page, opening)
            if match is None:
                # A markup declaration, a comment, a processing instruction, a
                # bogus comment, an unquoted or uppercase attribute, a stray
                # "<" - every one of them is a place where two parsers may part.
                raise VisualDecisionError(f"sheet document form: markup that is not a plain tag: {page[opening:opening + 24]!r}")
            closing, tag, blob, solidus = match.group(1), match.group(2), match.group(3), match.group(4)
            index = match.end()
            if closing:
                if ascii_strip(blob) or solidus:
                    raise VisualDecisionError(f"sheet document form: an end tag carries markup: {tag}")
                if tag in SHEET_VOID_TAGS:
                    raise VisualDecisionError(f"sheet document form: void element carries an end tag: {tag}")
                if not stack or stack[-1] != tag:
                    raise VisualDecisionError(f"sheet document form: unbalanced end tag: {tag}")
                stack.pop()
                events.append(("end", tag, ()))
                if tag == "table" and "tbody" not in tables.pop():
                    raise VisualDecisionError("sheet document form: <table> has no explicit <tbody>")
                if tag == "head": head_state = 2
                if tag == "body": body_state = 2
                continue
            if tag not in self.allowed_tags:
                raise VisualDecisionError(f"sheet contains forbidden HTML tag: {tag}")
            if solidus and tag not in SHEET_VOID_TAGS:
                # HTML5 discards the solidus on an ordinary start tag, so <div/>
                # *opens* a div; treating it as a self-closing element is how a
                # whole subtree became invisible to the validator.
                raise VisualDecisionError(f"sheet document form: solidus on a non-void tag: {tag}")
            attrs = self._attributes(tag, blob)
            # ``stack[-1:] == ["head"]`` and not "head in stack": the policy has
            # to be a direct child of an open <head> that no <body> has followed.
            self._element(tag, attrs, head_open=head_state == 1 and body_state == 0 and stack[-1:] == ["head"])
            if not stack:
                if tag != "html" or events:
                    raise VisualDecisionError("sheet document form: the root element must be a single <html>")
            elif stack[-1] == "html":
                if tag == "head":
                    if head_state or body_state:
                        raise VisualDecisionError("sheet document form: <head> is not the first child of <html>")
                    head_state = 1
                elif tag == "body":
                    if head_state != 2 or body_state:
                        raise VisualDecisionError("sheet document form: <body> does not follow a closed <head>")
                    body_state = 1
                else:
                    raise VisualDecisionError(f"sheet document form: only <head> and <body> may sit in <html>: {tag}")
            elif stack[-1] == "head":
                # The head content model is closed to exactly the three elements
                # that keep Blink in its "in head" insertion mode.  Anything else
                # would close <head> implicitly there and not here, which is
                # precisely how a policy meta ends up in the body.
                if tag not in SHEET_HEAD_TAGS:
                    raise VisualDecisionError(f"sheet document form: element not allowed in <head>: {tag}")
                head_children += 1
                # HTML5 only guarantees that the encoding declaration is honoured
                # while it stands in the first 1024 bytes; the caller checks that
                # window in bytes, and the form fixes the *position* so the
                # window cannot be pushed by a long attribute value further out.
                if head_children == 1 and "charset" not in dict(attrs):
                    raise VisualDecisionError("sheet document form: <head> does not open with its encoding declaration")
                if "charset" in dict(attrs):
                    self.charset_offset = opening
            elif stack[-1] in SHEET_TEXT_ONLY_TAGS:
                raise VisualDecisionError(f"sheet document form: <{stack[-1]}> carries markup")
            elif "body" not in stack:
                raise VisualDecisionError(f"sheet document form: element outside <head> and <body>: {tag}")
            elif tag not in self.body_tags:
                raise VisualDecisionError(f"sheet document form: element not allowed in <body>: {tag}")
            else:
                self._content_model(tag, stack, tables)
            events.append(("start", tag, tuple(attrs)))
            if tag not in SHEET_VOID_TAGS:
                stack.append(tag)
            if tag == "pre" and page.startswith(("\n", "\r"), index):
                raise VisualDecisionError("sheet document form: <pre> opens with a line break the browser drops")
            if tag == "meta" and any(name == "http-equiv" for name, _value in attrs):
                policies += 1
                if policies > 1:
                    # Blink composes several policies as an intersection, so a
                    # second one cannot loosen the first - but the artifact is
                    # then no longer the one closed policy this module promises,
                    # and last-writer-wins was how the promise was read.
                    raise VisualDecisionError("sheet declares more than one content policy")
        if stack:
            raise VisualDecisionError(f"sheet document form: unclosed element: {stack[-1]}")
        if head_state != 2 or body_state != 2:
            raise VisualDecisionError("sheet document form: the document has no closed <head> and <body>")
        return events

    def _content_model(self, tag: str, stack: list[str], tables: list[list[str]]) -> None:
        """One body element against the enumerated content model (see SHEET_CHILDREN)."""
        parent = stack[-1]
        children = SHEET_CHILDREN.get(parent)
        if children is not None and tag not in children:
            raise VisualDecisionError(f"sheet document form: <{tag}> may not stand in <{parent}>")
        parents = SHEET_PARENTS.get(tag)
        if parents is not None and parent not in parents:
            raise VisualDecisionError(f"sheet document form: <{tag}> must stand directly in <{'> or <'.join(sorted(parents))}>")
        if parent == "table":
            seen = tables[-1]
            # An optional <thead>, then exactly one <tbody>, then nothing.
            if "tbody" in seen or (tag == "thead" and seen):
                raise VisualDecisionError(f"sheet document form: <{tag}> is out of order in <table>")
            seen.append(tag)
        if tag == "table":
            tables.append([])

    # -- what the elements of that form are allowed to say ------------------

    def _element(self, tag: str, attrs: list[tuple[str, str | None]], *, head_open: bool) -> None:
        values = dict(attrs)
        if set(values) - self.allowed_attrs.get(tag, set()) or any(ascii_lower(name).startswith("on") or name == "style" for name in values):
            raise VisualDecisionError(f"sheet contains forbidden HTML attributes: {tag}")
        if tag == "meta" and "charset" in values:
            # Exactly the encoding validate_sheet_manifest decodes with, spelled
            # exactly, and only one of them: HTML5 honours the FIRST encoding
            # declaration, so a second one is a statement this apparatus would
            # read and the browser would not.
            if values["charset"] != self.allowed_charset:
                raise VisualDecisionError(f"sheet declares a character encoding other than {self.allowed_charset}: {values['charset']}")
            if self.charset is not None:
                raise VisualDecisionError("sheet declares more than one character encoding")
            self.charset = values["charset"]
        if tag == "meta" and "http-equiv" in values:
            # http-equiv was compared against the CSP name but never *limited*
            # to it, and "content" was looked at for nothing else.  A
            # <meta http-equiv="refresh"> then navigated a validated, "offline"
            # sheet to a foreign origin, and SHEET_CSP cannot stop that: it
            # carries no navigate-to directive and default-src does not govern
            # top-level navigation.  The declaration is therefore an allowlist
            # of exactly one pragma, and every other one fails closed.
            if values["http-equiv"] not in self.allowed_http_equiv:
                raise VisualDecisionError(f"sheet contains a forbidden meta pragma: {values['http-equiv']}")
            # Blink honours a meta-delivered policy **only inside <head>**: two
            # sheets byte-identical but for the position of this one element
            # differ by whether any policy is in force at all, and the body
            # variant ran foreign script with no violation report at all.  The
            # head content model above already keeps <meta> out of the body;
            # this is the same statement made directly, so that neither of the
            # two rules alone is load-bearing.
            if not head_open:
                raise VisualDecisionError("sheet declares its content policy outside <head>")
            self.csp = values.get("content")
        if tag == "link":
            if values.get("rel") not in self.allowed_link_rel:
                raise VisualDecisionError(f"sheet contains a forbidden link relation: {values.get('rel')}")
            if not values.get("href"):
                raise VisualDecisionError("sheet contains a link without a target")
            self.stylesheets.add(values["href"])
        if tag == "section" and values.get("id"): self.sections.add(values["id"])
        for name in ("src", "href"):
            if name in values:
                target = values[name] or ""
                # A URL scheme is ASCII case-insensitive (RFC 3986 3.1) and a
                # browser strips ASCII whitespace out of a URL - leading,
                # trailing, and tab/LF/CR/FF anywhere inside it - before it ever
                # looks at the scheme.  This comparison did neither, so
                # <img src="HTTPS://evil.invalid/x"> and <img src=" javascript:x">
                # walked past it and were caught only by the closed-manifest
                # equality further down.  The sheet was still refused, but by
                # ONE of two gates; "two independent gates" is the statement
                # this module makes, so the fold and the strip belong here.
                # Both only ever add rejections: an honest reference is an
                # already normalized relative path with no whitespace and no
                # scheme, and reads the same through either spelling.
                # The probe is compared instead of the raw target rather than
                # beside it: neither fold can remove a "/" or a ".", so every
                # spelling the raw text would have caught the probe catches too,
                # and a second, weaker copy of the same three tests would be
                # dead code that no mutation could reach.
                probe = ascii_lower("".join(ascii_split(target)))
                if (probe.startswith(("http:", "https:", "data:", "//", "javascript:"))
                        or PurePosixPath(probe).is_absolute() or ".." in PurePosixPath(probe).parts):
                    raise VisualDecisionError("sheet contains a remote or unsafe URL")
                self.references.add(target)


CSS_WHITESPACE = tuple(ASCII_WHITESPACE)
CSS_NEWLINE = ("\n", "\r", "\f")
CSS_HEX = "0123456789abcdefABCDEF"


def _css_ident_char(char: str) -> bool:
    """One ident code point per css-syntax-3 4.3: letters, digits, ``_``, ``-``, non-ASCII.

    The letter and digit ranges are spelled out rather than left to
    ``str.isalnum()``: the specification says *ASCII* letter and *ASCII* digit,
    and while the two happen to agree today (every non-ASCII code point is an
    ident code point anyway), an identity that holds by accident is not the
    rule this scanner is measured against.
    """
    return ("a" <= char <= "z") or ("A" <= char <= "Z") or ("0" <= char <= "9") or char in ("_", "-") or ord(char) >= 0x80


def _css_ident_start_char(char: str) -> bool:
    """One ident-start code point per css-syntax-3 4.3: letter, non-ASCII, ``_`` - never a digit."""
    return ("a" <= char <= "z") or ("A" <= char <= "Z") or char == "_" or ord(char) >= 0x80


def _css_newline_run(css: str, index: int) -> int:
    """Length in raw characters of the newline at ``index``, ``0`` if there is none.

    css-syntax-3 3.3 replaces a CRLF pair with a single U+000A before the
    tokenizer ever runs, so the pair is *one* newline: one line continuation,
    or the single whitespace that 4.3.7 lets a hex escape swallow.  Treating it
    as two characters is what let ``\\`` + CRLF and ``\\41`` + CRLF end a string
    token here while the browser read straight on.
    """
    if css.startswith("\r\n", index): return 2
    return 1 if index < len(css) and css[index] in CSS_NEWLINE else 0


def _css_valid_escape(css: str, index: int) -> bool:
    """css-syntax-3 4.3.8: do the code points at ``index`` start a valid escape?

    A backslash followed by a newline does not; a backslash followed by
    anything else - end of input included, which 4.3.7 folds to U+FFFD - does.
    """
    return index < len(css) and css[index] == "\\" and _css_newline_run(css, index + 1) == 0


# What one call to :func:`_consume_escape` found at the backslash.  The *length*
# of an escape is decided in exactly one place for the whole module; these kinds
# only let each caller apply its own policy to the same finding, so a change to
# the length rule can no longer reach one branch and miss another.
_ESCAPE_NONE = "none"                  # no escape at all: "\\" + newline outside a string
_ESCAPE_EOF = "eof"                    # "\\" at end of input (4.3.7 parse error)
_ESCAPE_CONTINUATION = "continuation"  # "\\" + newline inside a string (4.3.5), decodes to nothing
_ESCAPE_REPLACED = "replaced"          # NULL, surrogate or out-of-range hex, folded to U+FFFD
_ESCAPE_HEX = "hex"                    # 1-6 hex digits plus the one whitespace they swallow
_ESCAPE_LITERAL = "literal"            # one escaped non-newline code point


def _consume_escape(css: str, index: int, *, in_string: bool) -> tuple[int, str | None, str]:
    """Consume the one escape sequence that starts at the backslash at ``index``.

    This is the single implementation of css-syntax-3 4.3.7 ("consume an
    escaped code point") for the whole scanner: the ident run, the url token,
    the string token *and* the decode stage all call it, so an escape is
    exactly as long in each of them.  Approximating that length differently per
    branch - two characters here, the real rule there - is what repeatedly
    desynchronised this scanner from the browser: the scanner stopped inside an
    escape, saw the rest as ordinary text, opened a comment on it and deleted
    declarations the browser still executed.  The decode stage used to be a
    second implementation as a regular expression; it is not any more, which is
    why weakening the CRLF rule now shows up in every branch at once.

    Returns ``(end, value, kind)`` where ``end`` is the index just past the
    consumed characters, ``value`` is the decoded code point and ``kind`` is one
    of the ``_ESCAPE_*`` constants above.  ``value`` is ``""`` for a sequence
    that decodes to nothing (a string's line continuation) and ``None`` when the
    backslash starts no escape at all; ``end`` is then ``index``, i.e. nothing
    was consumed and the caller decides what the stray backslash means in its
    own state.

    The rule itself: a backslash, then either 1-6 hex digits plus *one*
    optional following whitespace character that belongs to the escape (a CRLF
    pair being that one character, per 3.3), or one single non-newline code
    point.  A backslash before a newline is a line continuation inside a string
    token (4.3.5) and no escape anywhere else (4.3.8).  Out-of-range, NULL and
    surrogate escapes fold to U+FFFD exactly as the tokenizer folds them, which
    can never turn something else into ``url``; the ``_ESCAPE_REPLACED`` kind
    lets the decode stage refuse them without the tokenizer having to.
    """
    length = len(css)
    cursor = index + 1
    if cursor >= length:
        # A trailing backslash: 4.3.5 drops it from a string, 4.3.7 folds it to
        # U+FFFD elsewhere.  Either way it consumes only itself and spells no
        # ident; the decode stage refuses the ``_ESCAPE_EOF`` kind afterwards.
        return cursor, ("" if in_string else "\ufffd"), _ESCAPE_EOF
    newline = _css_newline_run(css, cursor)
    if newline:
        return (cursor + newline, "", _ESCAPE_CONTINUATION) if in_string else (index, None, _ESCAPE_NONE)
    if css[cursor] in CSS_HEX:
        digits = ""
        while cursor < length and len(digits) < 6 and css[cursor] in CSS_HEX:
            digits += css[cursor]; cursor += 1
        trailing = _css_newline_run(css, cursor)
        if trailing: cursor += trailing
        elif cursor < length and css[cursor] in CSS_WHITESPACE: cursor += 1
        codepoint = int(digits, 16)
        if codepoint == 0 or codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            return cursor, "\ufffd", _ESCAPE_REPLACED
        return cursor, chr(codepoint), _ESCAPE_HEX
    return cursor + 1, css[cursor], _ESCAPE_LITERAL


def _css_ident_run(css: str, index: int) -> tuple[int, str]:
    """Consume one ident sequence (css-syntax-3 4.3.11) and return its end and value.

    Only the decoded value matters to the caller, which uses it to decide
    whether the run spells ``url`` in front of a ``(``.  Escapes are decoded by
    the shared :func:`_consume_escape`, the same routine the string and url
    branches use, so ``u\\72l(`` names the same ident as ``url(`` and opens the
    same url token.
    """
    length = len(css)
    name: list[str] = []
    while index < length:
        char = css[index]
        if char == "\\":
            end, value, _kind = _consume_escape(css, index, in_string=False)
            if value is None or end <= index: break
            name.append(value); index = end
            continue
        if not _css_ident_char(char):
            break
        name.append(char); index += 1
    return index, "".join(name)


def _css_starts_ident_sequence(css: str, index: int) -> bool:
    """css-syntax-3 4.3.9: would an ident sequence start at ``index``?

    Note that a digit does not start one - ``5url`` is a dimension, not an
    ident - which is why the caller may not simply look for ident code points.
    """
    if index >= len(css): return False
    char = css[index]
    if char == "-":
        following = index + 1
        if following < len(css) and (css[following] == "-" or _css_ident_start_char(css[following])): return True
        return _css_valid_escape(css, following)
    return _css_ident_start_char(char) or _css_valid_escape(css, index)


def _copy_css_url_token(css: str, start: int, after_paren: int, out: list[str]) -> int:
    """Copy one unquoted ``url(...)`` token verbatim (css-syntax-3 4.3.6).

    Inside an unquoted url token the tokenizer is neither looking for a comment
    nor for a string: ``/*`` there is ordinary URL text, so a scanner that
    opens a comment on it deletes declarations the browser still parses.  The
    token is therefore copied through unchanged up to the ``)`` that closes it,
    or to end of input.  Whitespace inside the token ends the URL itself, but
    what follows is either that same ``)`` or - per 4.3.14 - the remnants of a
    bad url token, which run to the very same ``)`` without any comment or
    string handling either; copying straight on to the ``)`` is exactly what
    the tokenizer does in both cases.  An escape is consumed by the shared
    :func:`_consume_escape`, so ``\\29`` cannot be mistaken for the closing
    ``)`` and a backslash before a newline is the parse error of 4.3.6 rather
    than an escape - the very same reading the other two branches use.

    ``url(`` followed (after optional whitespace) by a quote is an ordinary
    function token plus a string token, so that case is handed back to the main
    loop instead; the caller detects it and never enters this state.
    """
    length = len(css)
    out.append(css[start:after_paren])
    index = after_paren
    while index < length:
        char = css[index]
        if char == "\\":
            end, value, _kind = _consume_escape(css, index, in_string=False)
            if value is not None and end > index:
                out.append(css[index:end]); index = end; continue
        out.append(char); index += 1
        if char == ")": break
    return index


def _strip_css_comments(css: str) -> str:
    """Drop CSS comments the way a real tokenizer sees them - never inside a string or a url.

    css-syntax-3 consumes a comment only where a token may start.  Two of its
    states are not such a place, and both have to be modelled or the scan
    deletes text the browser executes:

    * while "consume a string token" (4.3.5) runs, ``/*`` is ordinary string
      content.  A plain ``/\\*.*?\\*/`` regex does not know that, so hiding
      ``/*`` and ``*/`` in two ``content`` strings would delete every
      declaration between them from the scanner's view.
    * while "consume a url token" (4.3.6) runs, ``/*`` is ordinary URL content.
      ``url(assets/a.svg/*)`` would otherwise open a comment that swallows the
      remainder of the sheet - up to a later ``*/`` or, with none, to end of
      input - while the browser parses every following rule normally.

    A string token ends at its matching quote, at an unescaped newline (which
    the token does not consume) or at end of input.  Its backslashes are read
    by the shared :func:`_consume_escape`: a hex escape carries one following
    whitespace character with it and a backslash before a newline continues the
    string, so neither ends the token here while the browser reads on.

    Entry into the url state has to be exact in *both* directions, not merely
    conservative.  It is entered only for an ident run that decodes to ``url``
    (ASCII case-insensitive) immediately followed by ``(`` and then, after
    optional whitespace, by something other than a quote - and only where the
    tokenizer starts an ident-like token, so the ident behind a ``#`` or ``@``
    (a hash or at-keyword token per 4.3.1) does not open one.  Entering the
    state where the tokenizer does not is *not* harmless: the state ends at a
    ``)`` the scanner would otherwise have read as string content, and a
    comment can then open behind that ``)`` which the tokenizer never opens -
    turning a rejection into an acceptance.  String and url contents are copied
    through verbatim, so the escape stage and the offline token scan still see
    everything they hold.
    """
    out: list[str] = []
    index = 0
    length = len(css)
    while index < length:
        char = css[index]
        if char == "/" and css.startswith("/*", index):
            end = css.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        if char in ("\"", "'"):
            out.append(char); index += 1
            while index < length:
                current = css[index]
                if current == "\\":
                    # Every branch that consumes an escape insists on progress.
                    # The string branch used to trust that _consume_escape can
                    # never stand still with in_string=True; when that
                    # invariant broke, this loop span for ever and the suite
                    # *hung* instead of turning red.  A guard that costs one
                    # comparison turns a broken invariant into an ordinary
                    # wrong answer, which a test can see.
                    end, _value, _kind = _consume_escape(css, index, in_string=True)
                    if end > index:
                        out.append(css[index:end]); index = end; continue
                if current in CSS_NEWLINE: break
                out.append(current); index += 1
                if current == char: break
            continue
        if char in ("#", "@"):
            # 4.3.1: what follows is the ident sequence of a hash or at-keyword
            # token, never of an ident-like token, so a "url" spelled there
            # opens no url token - for the browser and therefore here neither.
            # "#" takes any ident code point (digits included), "@" needs a
            # sequence that really starts an ident.
            following = index + 1
            starts = (_css_starts_ident_sequence(css, following) if char == "@"
                      else following < length and (_css_ident_char(css[following]) or _css_valid_escape(css, following)))
            if starts:
                end, _name = _css_ident_run(css, following)
                if end > following:
                    out.append(css[index:end]); index = end
                    continue
            out.append(char); index += 1
            continue
        if char == "\\" or _css_ident_char(char):
            end, name = _css_ident_run(css, index)
            if end > index:
                cursor = end + 1
                # css-syntax-3 matches ident keywords ASCII case-insensitively.
                # A Unicode fold would enter the url-token state on an ident
                # the tokenizer never reads as ``url`` - and the docstring above
                # says why entering that state early turns a rejection into an
                # acceptance.
                if ascii_lower(name) == "url" and end < length and css[end] == "(":
                    while cursor < length and css[cursor] in CSS_WHITESPACE:
                        cursor += 1
                    if cursor >= length or css[cursor] not in ("\"", "'"):
                        index = _copy_css_url_token(css, index, cursor, out)
                        continue
                    out.append(css[index:cursor]); index = cursor
                    continue
                out.append(css[index:end]); index = end
                continue
        out.append(char); index += 1
    return "".join(out)


def _decode_css_escapes(text: str) -> str:
    """Decode every CSS escape in ``text`` through the one shared escape routine.

    This stage used to be a second, independent implementation of 4.3.7 written
    as a regular expression, and the divergence between the two was the direct
    cause of the last three holes in this scanner: the CRLF pair counted as one
    character in :func:`_consume_escape` and as two in the regex, so a payload
    could be one token to the scan and another to the browser.  There is now
    one implementation, and a change to the length rule - the CRLF run above
    all - moves both stages together or neither.

    The stage keeps its own *policy*, which is stricter than the tokenizer's on
    purpose and unchanged from the regular expression it replaces:

    * a backslash before a newline is read as the line continuation of 4.3.5
      everywhere, i.e. with ``in_string=True``, so the decoded text never
      carries a stray backslash out of that case;
    * a NULL, surrogate or out-of-range hex escape is refused instead of folded
      to U+FFFD - the tokenizer folds it, but nothing this sheet may legally
      contain needs it, so it fails closed;
    * a backslash at end of input is left standing, which is what the residual
      backslash check downstream then refuses.
    """
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text[index] != "\\":
            out.append(text[index]); index += 1
            continue
        end, value, kind = _consume_escape(text, index, in_string=True)
        if kind == _ESCAPE_REPLACED:
            raise VisualDecisionError("sheet CSS contains an invalid escape")
        if kind == _ESCAPE_EOF or end <= index:
            out.append("\\"); index = max(end, index + 1)
            continue
        out.append(value or ""); index = end
    return "".join(out)


def _decoded_canonical_css(value: str) -> str:
    """Decode every escape in an already comment-free sheet, to a fixed point.

    The result is used for *substring* policy - the forbidden scheme and at-rule
    tokens - and as a second, additive pass of the reference scan.  It is never
    the text whose token boundaries decide anything: see
    :func:`_css_resource_references`.
    """
    previous = None
    for _ in range(3):
        if value == previous: break
        previous = value; value = _decode_css_escapes(value)
    if "\\" in value: raise VisualDecisionError("sheet CSS contains an undecodable escape")
    return value


def _security_canonical_css(css: str) -> str:
    """Remove comments and decode CSS escapes before applying the offline policy.

    The order is load-bearing and mirrors the tokenizer: comments are removed
    from the raw text first, escapes are decoded afterwards.  Decoding first
    would let ``\\22`` synthesize a quote that no browser treats as a string
    delimiter and ``\\2f\\2a`` synthesize a comment no browser ever opens, i.e.
    the scanner would start hiding content the browser still executes.  For the
    same reason no comment is stripped after decoding: a ``/*`` that only
    appears once an escape is decoded is not a comment to a browser either, so
    it stays visible to the offline token scan.

    What this text is *for* has narrowed.  It answers substring questions - is
    there an ``@import``, an ``http:``, a ``data:`` - where decoding first is the
    browser-faithful reading, and it feeds a second, additive run of the
    reference scan.  The reference scan that decides token boundaries reads the
    raw, comment-free text instead (:func:`_scan_sheet_css`), because a decoded
    character that acts as a delimiter is exactly the hole this ordering was
    written to prevent one stage earlier.
    """
    return _decoded_canonical_css(_strip_css_comments(css))


# Functions whose argument list names a resource the browser will fetch.  A
# quoted <string> in one of these is a URL, exactly as an unquoted url() token
# is: CSS Images 4 lets image-set() take a bare <string>, and Blink resolves it
# in background-image, mask-image, list-style-image, border-image-source,
# cursor and content, as well as nested inside -webkit-cross-fade().  The
# offline scan used to know a single spelling, "url(", so every one of these
# fetched a file that was not in the closed manifest while the manifest check
# reported that the references matched.
CSS_URL_FUNCTIONS = {
    "url", "src",
    "image", "image-set", "-webkit-image-set",
    "cross-fade", "-webkit-cross-fade",
}
# Properties whose value is a URL even when it is written as a bare string, so
# a @font-face descriptor cannot smuggle one past the function rule above.
CSS_URL_PROPERTIES = {"src"}
# At-rules whose prelude names a resource, with or without url().  @import is
# refused outright by the forbidden-token scan; it is listed here so that the
# reference set stays honest if that scan is ever narrowed.
CSS_URL_AT_RULES = {"import"}
# Every other function the sheet may use.  This is a closed list on purpose:
# the promise is that the asset references match the closed manifest, and a
# function nobody has looked at may be one more way to name a resource.  An
# unknown function is therefore refused rather than ignored - the fail-closed
# answer for a reference form this scanner cannot prove it understands.
CSS_ALLOWED_FUNCTIONS = CSS_URL_FUNCTIONS | {
    "attr", "calc", "clamp", "color", "color-mix", "counter", "counters", "env",
    "fit-content", "format", "hsl", "hsla", "hwb", "lab", "lch", "local", "max",
    "min", "minmax", "oklab", "oklch", "repeat", "rgb", "rgba", "tech", "var",
    "linear-gradient", "radial-gradient", "conic-gradient",
    "repeating-linear-gradient", "repeating-radial-gradient", "repeating-conic-gradient",
}


def _css_url_token_end(text: str, cursor: int) -> int:
    """Index of the ``)`` that closes the unquoted url token running from ``cursor``.

    ``str.find(")")`` was the whole of this rule, and it read a *decoded* text:
    a ``\\29`` had already become a real ``)`` by then, so the scanner ended the
    url token in the middle of a URL that Blink reads to its end.  The reference
    it reported was a proper prefix of the one the browser resolves - and a
    prefix that happens to be a manifest member passes the equality check while
    the browser fetches something else entirely.

    An escape is therefore consumed here by the shared :func:`_consume_escape`,
    exactly as :func:`_copy_css_url_token` consumes it, so the token ends where
    the tokenizer ends it: at an unescaped ``)`` or at end of input (4.3.6, and
    4.3.14 for the remnants of a bad url token).  Returns ``len(text)`` when
    there is no closing ``)``.
    """
    length = len(text)
    while cursor < length:
        char = text[cursor]
        if char == "\\":
            end, value, _kind = _consume_escape(text, cursor, in_string=False)
            if value is not None and end > cursor:
                cursor = end
                continue
        if char == ")":
            return cursor
        cursor += 1
    return length


def _css_string_value(text: str, index: int) -> tuple[int, str]:
    """Consume the string token that starts at the quote at ``index``.

    Runs to the matching quote, to an unescaped newline (which the token does
    not consume, 4.3.5) or to end of input, and answers with the index just past
    the token and the string's decoded content.

    The token boundary is decided on the **raw** text: a backslash is handed to
    the shared :func:`_consume_escape`, so a ``\\22`` inside the token is part of
    the token's *content* and can never act as the delimiter that ends it.  Only
    once the boundary stands is the content decoded, by the one decode stage
    :func:`_decode_css_escapes`.  Reading it the other way round - decode the
    whole sheet, then look for quotes - is what let a single ``\\22`` flip the
    quote parity of everything behind it and hide every following reference from
    this scanner, from the function allowlist and from the ``url(`` regex alike.
    """
    quote = text[index]
    index += 1
    start = index
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\":
            end, _value, _kind = _consume_escape(text, index, in_string=True)
            if end > index:
                index = end
                continue
        if char == quote:
            return index + 1, _decode_css_escapes(text[start:index])
        if _css_newline_run(text, index):
            return index, _decode_css_escapes(text[start:index])
        index += 1
    return length, _decode_css_escapes(text[start:length])


def _css_resource_references(text: str) -> tuple[set[str], set[str]]:
    """Every resource the canonicalised sheet CSS names, and every function it uses.

    The scan walks the same token states as :func:`_strip_css_comments` and
    collects a reference wherever the browser would resolve one:

    * an unquoted ``url( ... )`` token;
    * a ``<string>`` argument of a function in :data:`CSS_URL_FUNCTIONS`, which
      is how ``image-set("x.png" 1x)`` and ``-webkit-cross-fade`` name an image
      without ever writing ``url``;
    * a ``<string>`` value of a property in :data:`CSS_URL_PROPERTIES`, i.e. the
      ``src:`` descriptor of ``@font-face``;
    * a ``<string>`` in the prelude of an at-rule in :data:`CSS_URL_AT_RULES`,
      i.e. ``@import "x.css"`` written without ``url()``.

    The innermost enclosing function decides, so ``format("woff2")`` beside a
    ``url()`` in the same ``src:`` descriptor is not a reference while
    ``image-set(url("a.svg"))`` yields one.  Function names are returned
    alongside so the caller can refuse a function this list has never seen.

    **The text handed in is the raw, comment-free sheet, escapes and all.**  The
    structure of a token is decided on those raw characters and only the *value*
    of a finished token is decoded, by :func:`_decode_css_escapes`.  The scan
    used to run on the fully decoded text instead, and that order was a hole of
    its own twice over: a ``\\29`` had become a ``)`` that ended a url token the
    browser reads past, and a ``\\22`` had become a ``"`` that shifted the quote
    parity of the rest of the sheet and hid every reference behind it.  A
    decoded code point must never act as a syntax character - it is content of
    the token it stands in, and nothing else.  Running the scan a second time
    over the decoded text stays useful as a purely additive over-approximation
    (see :func:`_scan_sheet_css`), never as the reading that decides a boundary.
    """
    references: set[str] = set()
    functions: set[str] = set()
    stack: list[str] = []
    prop: str | None = None
    at_rule: str | None = None
    # The ident run that ended most recently, as ``(end, decoded name)``.  The
    # property in front of a ":" used to be read by walking *backwards* over
    # ident code points, which is a third reading of the same text and stops at
    # a backslash: "s\72c:" then spelled the property "rc" and the src
    # descriptor slipped past the string rule.  An ident run is already
    # consumed - escapes and all - by _css_ident_run, so the property is that
    # run when it stands immediately in front of the colon, and nothing else.
    pending: tuple[int, str] | None = None
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in ("\"", "'"):
            end, value = _css_string_value(text, index)
            inner = stack[-1] if stack else None
            if inner in CSS_URL_FUNCTIONS or (not stack and (prop in CSS_URL_PROPERTIES or at_rule in CSS_URL_AT_RULES)):
                references.add(value)
            index = end
            continue
        if char in ("#", "@"):
            following = index + 1
            starts = (_css_starts_ident_sequence(text, following) if char == "@"
                      else following < length and (_css_ident_char(text[following]) or _css_valid_escape(text, following)))
            if starts:
                end, name = _css_ident_run(text, following)
                if end > following:
                    if char == "@": at_rule = ascii_lower(name)
                    index = end
                    continue
            index += 1
            continue
        if char == "\\" or _css_ident_char(char):
            end, name = _css_ident_run(text, index)
            if end > index:
                if end < length and text[end] == "(":
                    cursor = end + 1
                    while cursor < length and text[cursor] in CSS_WHITESPACE:
                        cursor += 1
                    if ascii_lower(name) == "url" and (cursor >= length or text[cursor] not in ("\"", "'")):
                        stop = _css_url_token_end(text, cursor)
                        references.add(_decode_css_escapes(text[cursor:stop].strip("".join(CSS_WHITESPACE))))
                        index = stop + 1 if stop < length else length
                        continue
                    if _css_starts_ident_sequence(text, index):
                        functions.add(ascii_lower(name))
                        stack.append(ascii_lower(name))
                    else:
                        stack.append("")
                    index = end + 1
                    continue
                pending = (end, ascii_lower(name))
                index = end
                continue
        if char == "(":
            stack.append(""); index += 1
            continue
        if char == ")":
            if stack: stack.pop()
            index += 1
            continue
        if char == ":":
            if not stack:
                cursor = index - 1
                while cursor >= 0 and text[cursor] in CSS_WHITESPACE:
                    cursor -= 1
                prop = pending[1] if pending is not None and pending[0] == cursor + 1 else None
            index += 1
            continue
        if char in (";", "{", "}", ","):
            if char in (";", "{", "}"):
                # A block or declaration boundary ends any function left open by
                # malformed input, so an unclosed "(" earlier in the sheet
                # cannot make a later "src:" descriptor look like it stands
                # inside a function and slip past the string rule.
                stack.clear(); prop = None
                if char in ("{", ";"): at_rule = None
            index += 1
            continue
        index += 1
    return references, functions


# The closed grammar of a sheet member.  The manifest schema only forbids "..",
# an absolute path and exotic characters; it knows no extension and no place, so
# any name at all was a legal member and only two of them were ever looked at.
# A member is therefore one of exactly three shapes, and the shape decides how
# the member is read.
SHEET_ROOT_MEMBERS = {"index.html": "html", "style.css": "css"}
SHEET_ASSET_MEMBER = re.compile(r"assets/[A-Za-z0-9][A-Za-z0-9._-]*\.(css|svg|woff2)")


def _sheet_member_kind(path: str) -> str:
    """``"html"``, ``"css"`` or ``"asset"`` for a manifest member - or fail closed.

    The offline apparatus is only worth what its *scope* is worth.  Everything
    below ran over ``contents["style.css"]`` and nothing else, so a second
    stylesheet was hashed and then never read again: comment handling, escape
    policy, the forbidden-token scan, the function allowlist and the manifest
    equality all simply did not apply to it.  Naming the members instead of
    comparing them is what makes "every CSS in the bundle is scanned" a
    statement about the bundle rather than about one file name.
    """
    kind = SHEET_ROOT_MEMBERS.get(path)
    if kind is not None:
        return kind
    match = SHEET_ASSET_MEMBER.fullmatch(path)
    if match is None:
        raise VisualDecisionError(f"sheet manifest member is not an allowed sheet file: {path}")
    return "css" if match.group(1) == "css" else "asset"


def _sheet_css_reference(base: PurePosixPath, reference: str) -> str:
    """One CSS reference as the manifest path it names, else its own text verbatim.

    A stylesheet resolves its references against **its own** location, so a
    reference out of ``assets/second.css`` is not the manifest path its text
    spells.  Only a plain, already normalized relative reference is joined onto
    that location; anything else - a scheme, an authority (``//host/x``), an
    absolute path, an empty token, a ``.`` or a ``..`` - is handed on unchanged.

    Unchanged is the fail-closed answer, not a shrug.  Every member of the
    manifest is a normalized path by :func:`_sheet_member_kind`, so a reference
    that is *not* one can never equal a member and the closed-manifest equality
    below rejects it with the one message that has always named this failure.
    Nothing here normalizes a reference into agreement: ``./assets/a.svg`` and
    ``assets/x/../a.svg`` are refused exactly as they were before, because the
    promise is that the reference matches the manifest, not that some reading of
    it can be made to.
    """
    posix = PurePosixPath(reference)
    if (not reference or reference != posix.as_posix() or posix.is_absolute()
            or any(part in {"", ".", ".."} for part in posix.parts)):
        return reference
    return posix.as_posix() if base.as_posix() == "." else (base / posix).as_posix()


def _scan_sheet_css(path: str, css: str) -> set[str]:
    """The offline policy for one stylesheet, whichever member it is.

    Every rule that used to apply to ``style.css`` alone applies here, and the
    caller runs this over every CSS member of the manifest.

    Two reference scans are run and their results are *unioned*.  The first
    reads the raw, comment-free text and is the browser-faithful one: it decides
    token boundaries on the same characters Blink tokenizes.  The second reads
    the decoded text; it is not a second opinion but an over-approximation kept
    for the same reason the ``url(`` regex below is kept - it can only ever add
    references and function names, so no payload this module rejected before can
    be accepted because the scan became more exact.  The ``url(`` text search is
    the third member of that union.
    """
    base = PurePosixPath(path).parent
    stripped = _strip_css_comments(css)
    decoded = _decoded_canonical_css(stripped)
    # The second of the two denylists that keep both folds: the Unicode fold
    # can only ever match a forbidden token in more spellings, the ASCII fold is
    # the one the tokenizer actually uses.  Neither may be dropped.
    folded = (decoded.lower(), ascii_lower(decoded))
    if any(token in text for token in ("@import", "http:", "https:", "data:", "javascript:", "expression(") for text in folded):
        raise VisualDecisionError("sheet CSS contains network or active content")
    raw_urls, raw_functions = _css_resource_references(stripped)
    decoded_urls, decoded_functions = _css_resource_references(decoded)
    unknown = sorted((raw_functions | decoded_functions) - CSS_ALLOWED_FUNCTIONS)
    if unknown:
        raise VisualDecisionError(f"sheet CSS uses an unsupported function: {unknown}")
    # The crude ``url(`` net, in both whitespace readings.  It is purely
    # additive - every name it finds has to equal a manifest member or the
    # equality below refuses the sheet - so asking it twice can only add.  The
    # ASCII reading is the browser's; the Unicode one is kept beside it because
    # removing a member of an additive union is the one direction that loses a
    # rejection.
    net = set(re.findall(r'url\(\s*["\']?([^"\')\s]+)', decoded, flags=re.IGNORECASE))
    net |= set(re.findall(rf'url\({_ASCII_SPACE}*["\']?([^"\'){_ASCII_SPACE_CHARS}]+)', decoded, flags=re.IGNORECASE))
    references = raw_urls | decoded_urls | net
    return {_sheet_css_reference(base, reference) for reference in references}


# css-syntax-3 3.2 "determine the fallback encoding": when the response carries
# no charset - over ``file://`` always, and for ``text/css`` without parameters
# too - a stylesheet is decoded by the byte sequence ``@charset "`` ... ``";`` at
# its very start.  That rule is read from BYTES, before any decoding, and r19
# bound only ``index.html``: the CSS members were decoded as UTF-8 on the same
# line with nothing holding them to it.
#
# Measured (Chrome 152, both over ``http://`` and over ``file://``):
#
#     @charset "iso-2022-jp";<ESC>$B/*<ESC>(B{}body{background-image:url(x.svg)}
#
# is, to this scanner, one comment running to end of file - ``_scan_sheet_css``
# returned the empty set - and, to Blink, a dead rule plus a live ``body`` rule
# whose ``url()`` it fetched.  ``SHEET_CSP`` is no backstop: ``img-src 'self'``
# allows exactly that same-origin resource, and over ``file://`` a
# meta-delivered policy governs nothing of the sort.  Removing the ``@charset``
# line and changing nothing else makes Blink see the same comment this scanner
# sees: the declaration alone is the pivot.
#
# Two statements bind the decode, and each stands on its own.
#
#  * The only encoding rule a CSS member may carry is one that agrees with the
#    decode, written in the byte sequence css-syntax-3 tells a browser to look
#    for, at offset zero - and the member may not carry the text anywhere else
#    either.  The "anywhere else" half is not decoration: today an engine
#    honours the rule only at offset zero, and pinning that to the *only* place
#    the text may occur means no future placement quirk of any fallback
#    algorithm can put a second reading back.  A generated artifact with exactly
#    one producer pays nothing for it; this module's own stylesheet carries no
#    ``@charset`` at all.
#  * ``_SHEET_CONTROL``, which the document form has always applied to the HTML,
#    applies to a CSS member too.  Every C0 character in it is something a
#    browser folds to U+FFFD, drops outright, or - as here - reads as the switch
#    of a stateful encoding.  ESC is the transport the payload above needs, and
#    it is now refused whatever any declaration says.
#
# The two halves of "the rule" are not the same kind of statement, and r22
# measured the difference in Blink (152, both delivery paths):
#
#   * The SPELLING is a byte sequence.  css-syntax-3 3.2 looks for the bytes
#     ``@charset "`` - lowercase, one space, one quote - at offset zero, and for
#     ``";`` behind the label.  ``@CHARSET "utf-8";``, ``@charset  "utf-8";``,
#     ``@charset 'utf-8';``, a missing semicolon, or the rule one newline down
#     are all INERT in Blink: no encoding is taken from them, cssRules stays
#     empty for that rule.  A form this module can say nothing about does not
#     belong in a generated artifact, so every one of them stays refused.
#   * The LABEL is not a byte sequence.  css-syntax-3 hands the bytes between
#     the quotes to the Encoding Standard's "get an encoding", which strips
#     leading and trailing ASCII whitespace, ASCII-lowercases, and looks the
#     result up in a label table.  ``UTF-8``, ``utf8`` and the four remaining
#     utf-8 labels are therefore the *same statement* as ``utf-8`` - Blink reads
#     all six identically, measured - and r21's fold refused them.  That was a
#     false rejection of legitimate CSS, not a hardening, and it is taken back
#     here: this decodes the label the way the standard decodes it, and refuses
#     anything that does not come out utf-8.
#
# The padding a label may carry is where the two halves meet, and it is measured
# rather than assumed.  "Get an encoding" strips five ASCII whitespace bytes,
# but a CSS string may not CONTAIN three of them: css-syntax-3 preprocesses CR
# and FF into a newline, and a newline inside a string ends it as a bad-string
# token.  Chrome 152, both delivery paths, twelve padded labels:
#
#     @charset " utf-8 ";  @charset "\tutf-8\t";   rules apply, sheet decoded utf-8
#     @charset "\nutf-8";  @charset "utf-8\r";     cssRules EMPTY - the whole
#     @charset "\futf-8";  @charset "utf-8\n";     stylesheet is dead
#
# So space and tab are padding, and the other three are a form about which this
# module can say nothing - refused, exactly as the wrong spellings are.
_CSS_CHARSET_TEXT = re.compile(rb"@charset", re.IGNORECASE)
#: css-syntax-3 3.2, byte for byte, anchored at offset zero.
_CSS_CHARSET_RULE = re.compile(rb'\A@charset "([^"]{0,1024})";')
#: Every label the Encoding Standard maps to utf-8, already folded.
_UTF8_LABELS = frozenset({b"unicode-1-1-utf-8", b"unicode11utf8", b"unicode20utf8",
                          b"utf-8", b"utf8", b"x-unicode20utf8"})
#: The whitespace a CSS string can carry around a label and a browser strips off
#: it: the two of the Encoding Standard's five that are not a newline.
_CSS_LABEL_PADDING = b" \t"


def _bind_css_encoding(path: str, data: bytes) -> None:
    """Hold one CSS member to the encoding :func:`validate_sheet_manifest` decodes with."""
    if _CSS_CHARSET_TEXT.search(data) is None:
        # No rule, no BOM (checked by the caller), and the environment encoding
        # is index.html's, which is bound to utf-8 - so every step of the
        # fallback chain ends where this module decodes.
        return
    rule = _CSS_CHARSET_RULE.match(data)
    # An offset test would be dead code beside the first clause: the pattern is
    # anchored, so a member that carries the rule has it at offset zero by
    # construction, and one that does not fails that clause whatever the offset.
    # ``bytes.lower()`` is ASCII-only by definition, which is exactly the fold
    # "get an encoding" asks for - the same argument the XML side already makes.
    if (rule is None
            or rule.group(1).strip(_CSS_LABEL_PADDING).lower() not in _UTF8_LABELS
            or _CSS_CHARSET_TEXT.search(data, 1) is not None):
        raise VisualDecisionError(f"sheet CSS declares an encoding other than utf-8: {path}")


def validate_sheet_manifest(manifest: dict[str, Any], root: Path) -> None:
    validate_schema(manifest, "sheet")
    paths = [item["path"] for item in manifest["files"]]
    if len(paths) != len(set(paths)) or not {"index.html", "style.css"}.issubset(paths):
        raise VisualDecisionError("sheet manifest file inventory is incomplete or duplicated")
    kinds = {path: _sheet_member_kind(path) for path in paths}
    contents = {binding["path"]: _verify_bound_file(root, binding, f"sheet file {binding['path']}") for binding in manifest["files"]}
    css_members = sorted(path for path, kind in kinds.items() if kind == "css")
    # The encoding statements are made about BYTES, before the decode that would
    # otherwise be their only witness.
    for path in ("index.html", *css_members):
        if contents[path].startswith(_BYTE_ORDER_MARKS):
            raise VisualDecisionError(f"sheet member carries a byte order mark: {path}")
    for path in css_members:
        _bind_css_encoding(path, contents[path])
    try:
        page = contents["index.html"].decode("utf-8")
        stylesheets = {path: contents[path].decode("utf-8") for path in css_members}
    except UnicodeError as exc: raise VisualDecisionError("sheet HTML/CSS is not UTF-8") from exc
    for path in css_members:
        # The second, independent statement: the control characters the document
        # form has always refused in the HTML are refused in a stylesheet too.
        if _SHEET_CONTROL.search(stylesheets[path]):
            raise VisualDecisionError(f"sheet CSS contains a forbidden control character: {path}")
    # The HTML half of the same assumption, bound in r19: <meta charset="shift_jis">
    # was accepted and obeyed by Blink, so validator and renderer read one file
    # as two character streams.  Three statements bind them, and each is needed:
    #  * a byte order mark overrides every declaration in HTML5 and is a stray
    #    U+FEFF in CSS - refused above, on the bytes, for every member;
    #  * the declaration has to say the encoding that was decoded with (the
    #    parser holds it to exactly one spelling, exactly once);
    #  * and it has to stand inside the 1024-byte window in which HTML5
    #    guarantees the sniffer looks - the document form puts it first in
    #    <head>, and this measures the window in bytes, because <html lang="...">
    #    in front of it is an attribute value of unbounded length.
    parser = _SheetParser(); parser.feed(page); parser.close()
    if parser.charset != _SheetParser.allowed_charset or parser.charset_offset is None:
        raise VisualDecisionError("sheet does not declare the encoding it is validated as")
    if len(page[:parser.charset_offset].encode("utf-8")) >= 1024:
        raise VisualDecisionError("sheet declares its encoding outside the first 1024 bytes")
    if parser.csp != SHEET_CSP:
        raise VisualDecisionError("sheet CSP differs from the closed offline policy")
    required_sections = {"candidates-color", "candidates-typography", "candidates-code-font", "candidates-logo", "candidates-lockup", "recommendations", "owner-review"}
    if parser.sections != required_sections:
        raise VisualDecisionError("sheet semantic sections differ")
    # The two directions of "every stylesheet the browser loads is a stylesheet
    # this apparatus has read": no <link rel="stylesheet"> may name a member
    # that is not scanned as CSS, and no CSS member may sit in the bundle
    # without the page loading it.
    if parser.stylesheets != set(stylesheets):
        raise VisualDecisionError("sheet stylesheet links differ from the CSS members of the closed manifest")
    css_urls: set[str] = set()
    for path in sorted(stylesheets):
        css_urls |= _scan_sheet_css(path, stylesheets[path])
    referenced = (parser.references | css_urls) - {"style.css"}
    if referenced != {path for path in contents if path.startswith("assets/")}:
        raise VisualDecisionError("sheet asset references differ from the closed manifest")


# --- what the decision sheet shows ---------------------------------------------
#
# The first owner run needed a hand-built sheet: this renderer accepted exactly
# one candidate per font and logo domain and all five domains at once, while
# references/decision-sheet.md already treated logos as optional.  The owner
# feedback on that sheet - applied colour examples, set typography, real code
# blocks, more than three colour and type candidates, "a total impression of
# the CI rather than everything" - is answered here for every brand.
#
# Generic by construction.  The only vocabulary below is the five decision
# domains, the nineteen semantic colour roles, the font classifications and
# the GSUB feature tags.  Sample text comes from a table keyed by the brief's
# locale; it names no brand, colour or family, and no font is recognised by its
# name - ligatures are read out of the font's own GSUB table.
SHEET_DOMAINS = ("color", "typography", "code-font", "logo", "lockup")
DOMAIN_LABELS = {"color": "Farbe", "typography": "Typografie", "code-font": "Code-Font", "logo": "Logo", "lockup": "Lockup"}
#: The GSUB features that make a code font draw ligatures by default.
LIGATURE_FEATURES = frozenset({"liga", "calt"})
#: The GSUB features that give a font a second set of digits, read out of the
#: font with exactly the strictness the ligatures get: a feature counts only if a
#: script's language system references it and it has at least one lookup.  A
#: digit style the file does not carry is no choice the owner has, and a family
#: name is never evidence for one.
NUMERAL_FEATURES = frozenset({"lnum", "onum", "pnum", "tnum", "zero"})
#: What each of them is called where it is shown.  These name typographic facts,
#: not fonts; nothing here knows a family.
NUMERAL_LABELS = {"lnum": "Versalziffern", "onum": "Mediävalziffern", "pnum": "proportional",
                  "tnum": "tabellarisch", "zero": "geschlitzte Null"}
#: The one digit run every numeral row shows, so two rows differ in nothing but
#: the feature under test.
NUMERAL_SAMPLE = "0123456789"
#: The x-height every specimen is normalised to, as a fraction of the em.  Half
#: the em is a round figure that belongs to no font, sits inside the band real
#: text faces occupy, and leaves a font whose x-height IS half its em at the
#: nominal size of its role.  It is a reference point and never a recommendation.
#: At one declared size a sheet compares x-heights instead of fonts: measured
#: across a real catalogue the ratio runs from 0.413 to 0.550, a third more
#: letter for the same size, and a narrow-running face cannot be told apart from
#: a small-running one.  In practice the size would be adjusted, so the sheet
#: adjusts it and says so.
X_HEIGHT_TARGET = Fraction(1, 2)
#: The grid a normalised size is spelled on, in rem, and therefore the entire
#: tolerance of this normalisation: a size rounded to this grid misses its target
#: x-height by at most half a step times the font's own ratio, which at a 16 px
#: root is under a thousandth of a pixel.  It is a named constant because the
#: tolerance of the test has to be DERIVED from the rounding rather than guessed.
SIZE_QUANTUM = Fraction(1, 10000)
#: The floor under a stated x-height, as a fraction of the em, INCLUSIVE.  The
#: metric used to be believed downwards without any bound at all: ``sxHeight`` 1
#: of 1000 passed every check and produced sizes of 575 rem, a sheet 9200 px
#: wide that still validated.  The bound is measured, not guessed - across a real
#: catalogue of text, display and handwriting faces the ratio runs from 0.378 to
#: 0.732, so a quarter of the em sits a factor 1.5 below the smallest real face
#: and refuses that 1/1000 by a factor of 250.  A face exactly ON the quarter is
#: kept, because the bound is a plausibility floor and not a taste.
X_HEIGHT_FLOOR = Fraction(1, 4)
#: The second, independent net: the stated metric is held against the one thing
#: that can be MEASURED instead of claimed, the outline of the very glyph the
#: metric is named after.  Believing the metric and never looking at the glyph is
#: what let the 1/1000 through; this refuses a statement its own drawing
#: contradicts by an order of magnitude, in BOTH directions - a metric far above
#: its outline is the case the floor above cannot see.  The factor is wide on
#: purpose: over the same real catalogue the two disagree by at most 15 % (an
#: overshoot or a flourish is a design feature, not an error), while the
#: reproduced 1/1000 sits at a factor of 700.  It is a net for a wrong number,
#: not a ruler for a good one.
X_HEIGHT_INK_FACTOR = 4
#: The root font size the artifacts are SPECIFIED at, in CSS pixels.  Every
#: length here is spelled in rem, and a first baseline is a whole-pixel quantity
#: in the engine, so the compensation has to be derived at a stated root.  16 is
#: the initial value of ``font-size`` and the root both artifacts are written
#: for; at another root the sizes move with it and the residual is bounded by
#: the rounding named at BASELINE_METRIC rather than by anything this module
#: could compute.
BASELINE_ROOT_PX = 16
#: WHICH vertical metric the engine splits a line box with - measured, not
#: assumed, because the answer is platform- and flag-dependent.  Six probe fonts
#: whose ``hhea``, ``OS/2`` typo and ``usWin`` values deliberately disagree were
#: laid out and their DRAWN baseline read back: the engine followed ``hhea`` in
#: all six, including the two that set USE_TYPO_METRICS (fsSelection bit 7) at
#: OS/2 version 4.  The line gap moves ``line-height: normal`` and does NOT
#: enter the ascent/descent split - it is non-zero for 275 of 855 real faces, so
#: assuming either way would have mis-compensated all of them.
BASELINE_METRIC = "hhea"
#: The UNIT the engine rounds the vertical metric in - measured, not assumed, and
#: the reason this compensation can be exact where the previous one could not.
#: Probe faces whose ``hhea`` values are chosen by hand were laid out at absolute
#: sizes and absolute line boxes and their DRAWN baseline read back two ways at
#: once: rasterised ink against a fiducial of known geometry in the same image,
#: and the layout engine's own baseline.  The two agree to 0.009 px, and the drawn
#: baseline is the SAME at device pixel ratio 1 and 2 to within that - so the
#: engine rounds in whole CSS pixels and NOT on the device grid.  Over 216 probe
#: blocks at both ratios the rule below reproduced every block that keeps its
#: ascent and descent off an exact half pixel (390 of 390) and only 34 of 42 that
#: land on one; that tie is refused rather than guessed (see
#: :func:`_drawn_first_baseline`).
BASELINE_ROUNDING = "css-px"
#: role -> (nominal size in rem, line box in rem).  The nominal size is what the
#: role was set at before normalisation, and what a font at X_HEIGHT_TARGET is
#: still set at.  The line box is an ABSOLUTE length on purpose, computed from
#: the nominal size and not from the normalised one: it is then the same distance
#: for every candidate, so rows stay pitch-for-pitch comparable however far one
#: candidate's size had to move.
ROLE_METRICS = {
    "specimen": (Fraction(115, 100), Fraction(1725, 1000)),
    "heading": (Fraction(160, 100), Fraction(192, 100)),
    "subheading": (Fraction(120, 100), Fraction(180, 100)),
    "body": (Fraction(105, 100), Fraction(168, 100)),
    "table-head": (Fraction(100, 100), Fraction(150, 100)),
    "table-cell": (Fraction(100, 100), Fraction(150, 100)),
    "code": (Fraction(95, 100), Fraction(1425, 1000)),
    "numerals": (Fraction(115, 100), Fraction(1725, 1000)),
}
#: The role a face plays in a candidate, and therefore the one size that is
#: DISCLOSED for it: body text is set in the serif, headings in the sans, code in
#: the mono.  A face still carries every role it can reach, because the scale
#: factor is a property of the file rather than of the block.
FACE_ROLE = {"serif": "body", "sans": "heading", "mono": "code"}
#: role -> selector, once per artifact.  Two maps because the twins name their
#: blocks differently; the SIZES come from the one table above, so they cannot
#: drift apart on the thing the owner compares.
SHEET_ROLE_SELECTORS = {"specimen": ".specimen", "heading": ".t-heading", "subheading": ".t-subheading",
                        "body": ".t-body", "table-head": "th", "table-cell": "td",
                        "code": ".code,.code-liga,.code-noliga", "numerals": ".numerals"}
PLAYGROUND_ROLE_SELECTORS = {"heading": ".pg-heading", "subheading": ".pg-subheading", "body": ".pg-body",
                             "table-head": "th", "table-cell": "td", "code": ".pg-code",
                             "numerals": ".pg-numerals"}
#: role -> the classifications whose blocks that role is really SET in, once per
#: artifact, read off the markup these renderers write: the typeset block puts
#: headings in the sans and body, list, table and quote in the serif; the code
#: block is the mono; the digit rows are the serif of a typography card and the
#: mono of a code card; a specimen is shown for every face.  It is a map and not a
#: derivation because the markup is the only authority on it.
#:
#: The baseline target is the maximum over the faces of THESE classifications and
#: not over every face of the bundle.  Taken over all of them, a serif that never
#: sets ``.t-heading`` still decided where every heading sits, and pushed the
#: whole role a whole pixel lower than the faces that draw it need - measured on a
#: real bundle: heading, subheading and code each carried one pixel of overhang
#: that nothing on the page could show.
SHEET_ROLE_CLASSES = {"specimen": ("mono", "sans", "serif"), "heading": ("sans",),
                      "subheading": ("sans",), "body": ("serif",), "table-head": ("serif",),
                      "table-cell": ("serif",), "code": ("mono",), "numerals": ("mono", "serif")}
PLAYGROUND_ROLE_CLASSES = {"heading": ("sans",), "subheading": ("sans",), "body": ("serif",),
                           "table-head": ("serif",), "table-cell": ("serif",), "code": ("mono",),
                           "numerals": ("mono", "serif")}
#: weight -> the selectors set in it, once per artifact.  The sheet asks a face
#: for three weights, the playground for the same three in its own block names;
#: the CLAMP is one function, so a candidate cannot be held to one rule here and
#: another there.  These stand beside the role maps rather than beside the clamp
#: because a role and the weight it is drawn in are now one statement: the size a
#: role is set at depends on the weight it is drawn in (see ROLE_WEIGHTS).
SHEET_WEIGHT_SELECTORS = {700: ".t-heading,th,.code-label", 600: ".t-subheading"}
PLAYGROUND_WEIGHT_SELECTORS = {700: ".pg-heading,.pg-action,.pg-kpi-value,.pg-note,th", 600: ".pg-subheading"}
#: The weight a face keeps wherever no weight map names the block.
BASE_WEIGHT = 400


def _role_weights(role_selectors: dict[str, str], weight_selectors: dict[int, str]) -> dict[str, int]:
    """role -> the ONE weight that role is drawn in, derived from the two maps.

    Not maintained a third time by hand: a block a weight map names is set in
    that weight, every other block keeps BASE_WEIGHT.  A role whose own blocks
    disagree about the weight is refused HERE, when the module loads, because
    such a role has no single drawn x-height to normalise onto - it would be one
    number for one of its blocks and a different one for the other.  That is
    exactly what the role ``table`` was: ``th`` is asked for 700 and ``td`` is
    not, at one size, so the two drew letters of different height under one
    promise.  It is now two roles, and this guard is why it cannot silently
    become one again.
    """
    named = {part: weight for weight, selector in weight_selectors.items() for part in selector.split(",")}
    weights: dict[str, int] = {}
    for role, selector in role_selectors.items():
        found = {named.get(part, BASE_WEIGHT) for part in selector.split(",")}
        if len(found) != 1:
            raise VisualDecisionError(f"role is drawn in more than one weight: {role} {sorted(found)}")
        weights[role] = found.pop()
    return weights


SHEET_ROLE_WEIGHTS = _role_weights(SHEET_ROLE_SELECTORS, SHEET_WEIGHT_SELECTORS)
PLAYGROUND_ROLE_WEIGHTS = _role_weights(PLAYGROUND_ROLE_SELECTORS, PLAYGROUND_WEIGHT_SELECTORS)
#: Every weight either artifact asks a face for, and therefore every instance a
#: face has to state its x-height for - no more and no fewer.
ASKED_WEIGHTS = tuple(sorted({BASE_WEIGHT, *SHEET_WEIGHT_SELECTORS, *PLAYGROUND_WEIGHT_SELECTORS}))
GENERIC_FAMILIES = {"serif": "serif", "sans": "sans-serif", "mono": "monospace"}
#: The neutral reference palette.  Every typography and code view stands on it,
#: so no colour candidate is shown more often than another; it is a grey scale
#: that belongs to no candidate and passes every ROLE_TESTS pairing (pinned).
REFERENCE_PALETTE = {
    "canvas": "#FFFFFF", "surface": "#F3F3F3", "surface-strong": "#E2E2E2", "text": "#1C1C1C",
    "text-muted": "#545454", "border": "#767676", "primary": "#2B2B2B", "on-primary": "#FFFFFF",
    "secondary": "#5E5E5E", "on-secondary": "#FFFFFF", "accent": "#404040", "on-accent": "#FFFFFF",
    "success": "#3A3A3A", "on-success": "#FFFFFF", "warning": "#E2E2E2", "on-warning": "#1C1C1C",
    "danger": "#1C1C1C", "on-danger": "#FFFFFF", "focus": "#1C1C1C",
}


def _code_sample() -> str:
    """One code block that exercises what a code font has to get right."""
    return "\n".join((
        "# Prüfbericht → Zusammenfassung",
        "def zusammenfassen(werte: list[float], grenze: float = 0.75) -> dict:",
        '    """Mittelwert und Anzahl; O0 und 1lI bleiben unterscheidbar."""',
        "    gültig = [w for w in werte if w >= 0 and w != 1e9]",
        '    return {"n": len(gültig), "mittel": sum(gültig) / max(len(gültig), 1)}',
        "",
        "// Operatoren und Pfeile: -> => <= >= != === !== && || :: ... |> <- ← →",
        'const status = anzahl > 0 ? "bestanden" : "offen"; /* 0xFF, 3.14, 1_000 */',
        "",
        f"sha256  {SPECIMEN_SHA256}",
        "pfad    /srv/daten/berichte/2026-09/quartal-3.json",
        "log     2026-09-11T08:15:42Z INFO  export fertig in 1.284 ms (lauf=7f3a9c)",
        'json    {"id": 42, "aktiv": true, "werte": [0.5, 1.0, -2], "notiz": null}',
    ))


#: Sample content per locale.  The visual brief has no field for example text -
#: its schema is closed - so nothing is taken from a run and nothing is invented
#: per brand: the sheet uses this generic, brand-free standard for the brief's
#: locale and refuses a locale it has no text for (see _sample_content).
SAMPLE_CONTENT: dict[str, dict[str, Any]] = {
    "de-DE": {
        "heading": "Jahresbericht in Zahlen",
        "subheading": "Entwicklung im dritten Quartal",
        "paragraphs": [
            "Der Umsatz stieg gegenüber dem Vorjahr deutlich. Größere Aufträge aus drei Regionen trugen den Zuwachs, während die Kosten stabil blieben.",
            "Für das kommende Halbjahr sind zusätzliche Investitionen in Qualität und Service geplant. Die Einzelheiten folgen im nächsten Bericht.",
        ],
        "list": ["Umsatz: plus 12,4 Prozent", "Neue Verträge: 318", "Lieferzeit: 2,1 Tage im Mittel"],
        "table_head": ["Region", "Q2", "Q3", "Änderung"],
        "table_rows": [["Nord", "1.204", "1.366", "+13,5 %"], ["Süd", "987", "1.052", "+6,6 %"], ["West", "2.310", "2.598", "+12,5 %"]],
        "quote": "„Klare Zahlen schaffen Vertrauen – im Team wie bei der Kundschaft.“",
        "action_primary": "Bericht öffnen",
        "action_secondary": "Später ansehen",
        "action_focus": "Fokus sichtbar",
        "notice_label": "Hinweis:",
        "notice": "Die Zahlen für Oktober sind vorläufig.",
        "error_label": "Fehler:",
        "error": "Die Datei konnte nicht geladen werden.",
        "kpi_value": "98,6 %",
        "kpi_label": "Termintreue im Quartal",
        "code": _code_sample(),
    },
}


def _sample_content(brief: dict[str, Any]) -> dict[str, Any]:
    content = SAMPLE_CONTENT.get(brief.get("locale"))
    if content is None:
        raise VisualDecisionError(f"no generic sample content for locale: {brief.get('locale')!r}")
    return content


def _typeset_text(content: dict[str, Any]) -> str:
    """Every character the set-typography sample puts in front of a text font."""
    cells = [cell for row in content["table_rows"] for cell in row]
    return "".join((content["heading"], content["subheading"], *content["paragraphs"], *content["list"], *content["table_head"], *cells, content["quote"]))


def _ink_x_height(font: TTFont, cmap: dict[int, str], location: dict[str, float] | None) -> int | None:
    """Top of the OUTLINE of ``x`` at one instance of the font, or None.

    The metric states an x-height; this measures the letter the metric is named
    after, at the instance a browser really draws.  ``location`` is a user-space
    axis position and ``None`` is the default instance; an axis the caller does
    not name keeps its own default, which is exactly what a page that switches
    optical sizing off gets.  Returns None where there is nothing to measure -
    no ``x`` in the cmap, no such glyph, or a glyph without a contour - so every
    caller has to say what it does with that rather than read a silent zero.
    """
    name = (cmap or {}).get(ord("x"))
    if name is None:
        return None
    glyphs = font.getGlyphSet() if location is None else font.getGlyphSet(location=location, normalized=False)
    if name not in glyphs:
        return None
    pen = BoundsPen(glyphs)
    glyphs[name].draw(pen)
    return int(round(pen.bounds[3])) if pen.bounds else None


def _font_facts(asset: bytes, label: str) -> dict[str, Any]:
    """What the sheet states about one font beyond its specimen.

    ``codepoints`` is the font's own cmap, so the sheet can name the characters
    of its sample text that a browser would draw from a fallback font instead.
    ``ligatures`` are the LIGATURE_FEATURES the font's GSUB table really
    applies: a feature counts only if a script's language system references it
    and it has at least one lookup, because a feature record nothing references
    and a feature without lookups both draw nothing.  The font's name plays no
    part in either answer.

    ``numerals`` are the NUMERAL_FEATURES read from that same set, under that
    same rule.  The distinction the sheet has to keep is between "this font
    offers no second set of digits" and "nothing was looked at": a font really
    can carry none - one of fourteen in a measured catalogue - so the empty tuple
    is a statement and is shown as one.

    ``x_height``, ``units_per_em`` and ``x_height_source`` are what the specimens
    are normalised on.  ``OS/2`` states the x-height from version 2 onwards; a
    file that predates it or leaves the field at zero has it MEASURED off the
    glyph the metric is named after.  Metric first and measurement only as a
    substitute is necessity rather than caution: across a real catalogue the two
    disagree for six faces of fourteen, and the statement in the file is the one
    its designer made.  Lacking both the font is refused here rather than set at
    a size nothing justifies - the same shape the weight guard has, for the same
    reason.
    """
    try:
        font = TTFont(BytesIO(asset), lazy=False, recalcBBoxes=False, recalcTimestamp=False)
        cmap = font.getBestCmap() or {}
        codepoints = frozenset(cmap.keys())
        features: set[str] = set()
        numerals: set[str] = set()
        if "GSUB" in font:
            table = font["GSUB"].table
            records = table.FeatureList.FeatureRecord if table.FeatureList is not None else []
            referenced: set[int] = set()
            for script in (table.ScriptList.ScriptRecord if table.ScriptList is not None else []):
                systems = [script.Script.DefaultLangSys] if script.Script.DefaultLangSys is not None else []
                systems += [record.LangSys for record in script.Script.LangSysRecord]
                for system in systems:
                    referenced.update(system.FeatureIndex)
                    if system.ReqFeatureIndex != 0xFFFF:
                        referenced.add(system.ReqFeatureIndex)
            applied = {record.FeatureTag for index, record in enumerate(records)
                       if index in referenced and record.Feature.LookupCount > 0}
            features = applied & LIGATURE_FEATURES
            numerals = applied & NUMERAL_FEATURES
        os2 = font["OS/2"]
        units_per_em = int(font["head"].unitsPerEm)
        # The metric a browser really splits the line box with, and therefore
        # the one the common first baseline is derived from.  WHICH table that
        # is was measured rather than assumed (see BASELINE_METRIC): six probe
        # fonts whose hhea, OS/2 typo and usWin values deliberately disagree,
        # two of them with the USE_TYPO_METRICS bit set at OS/2 version 4.  The
        # engine followed ``hhea`` in every one of the six and ignored the line
        # gap.  A file that states no usable ascent is refused here rather than
        # laid out on a guess - the same shape the weight guard has.
        vertical = font["hhea"]
        ascent, descent = int(vertical.ascent), int(vertical.descent)
        if ascent <= 0 or ascent - descent <= 0:
            raise VisualDecisionError(f"{label} states no usable vertical metrics: {ascent}/{descent}")
        stated = int(getattr(os2, "sxHeight", 0) or 0)
        ink = _ink_x_height(font, cmap, None)
        # A stated metric that breaks the floor while the font's own ``x`` holds
        # it is a wrong STATEMENT about a letter that draws perfectly well, so
        # the outline becomes the source - the very fallback an absent metric
        # already gets, applied to a metric that is present and implausible.
        # It is not a way around the floor: the fallback is taken only where the
        # two do NOT contradict each other beyond X_HEIGHT_INK_FACTOR, so the
        # reproduced 1/1000 against an outline of 700 fails that test and stays
        # refused.  Measured over 848 real faces from 439 files: exactly one
        # changes verdict, and every face still refused lacks a Latin ``x``.
        under_floor = 0 < stated * X_HEIGHT_FLOOR.denominator < units_per_em * X_HEIGHT_FLOOR.numerator
        ink_holds = ink is not None and ink * X_HEIGHT_FLOOR.denominator >= units_per_em * X_HEIGHT_FLOOR.numerator
        consistent = ink is not None and stated > 0 and stated < ink * X_HEIGHT_INK_FACTOR and ink < stated * X_HEIGHT_INK_FACTOR
        from_outline = stated <= 0 or (under_floor and ink_holds and consistent)
        x_height_source = "glyph-x" if from_outline else "os2-sxheight"
        x_height = (ink or 0) if from_outline else stated
        if not 0 < x_height <= units_per_em:
            raise VisualDecisionError(f"{label} states no usable x-height: {x_height}/{units_per_em}")
        # Downwards the metric used to be believed without any bound: 1 of 1000
        # passed here and set the specimens at 575 rem.  Two nets now, and they
        # catch different things.  The floor is a plausibility bound on the ratio
        # itself (X_HEIGHT_FLOOR).  The cross-check holds the STATEMENT against
        # the one thing that can be measured rather than claimed - the outline of
        # the glyph the metric is named after - and it is the only one of the two
        # that also sees a metric far ABOVE its own drawing.
        if x_height * X_HEIGHT_FLOOR.denominator < units_per_em * X_HEIGHT_FLOOR.numerator:
            raise VisualDecisionError(f"{label} states an x-height under {X_HEIGHT_FLOOR} of its em: {x_height}/{units_per_em}")
        if ink is not None and not (x_height < ink * X_HEIGHT_INK_FACTOR and ink < x_height * X_HEIGHT_INK_FACTOR):
            raise VisualDecisionError(f"{label} states an x-height its own glyph x contradicts: {x_height}/{units_per_em} against {ink}")
        # The weight range the face really has.  A variable font states it on
        # its ``wght`` axis; a static font has exactly the one weight its OS/2
        # table declares.  Nothing else is a source: a family name, a file name
        # or a subfamily string is a claim, not a range.  Without one of the two
        # the font is refused here rather than rendered, because the alternative
        # is a sheet that asks for a weight the file cannot draw and lets the
        # browser fake it - which is the very thing the reader is asked to judge.
        axis = next((item for item in font["fvar"].axes if item.axisTag == "wght"), None) if "fvar" in font else None
        # Whether the range is an AXIS or a single declared weight is a fact of
        # the file and a different statement to the owner: a variable face can
        # be asked for more, a static one cannot, and "400 to 400" alone does
        # not say which of the two it is.  Read here, beside the range it
        # belongs to, so the sheet and its twin cannot answer it differently.
        weight_axis = axis is not None
        weights = (int(axis.minValue), int(axis.maxValue)) if axis is not None else (int(font["OS/2"].usWeightClass),) * 2
        if not 1 <= weights[0] <= weights[1] <= 1000:
            raise VisualDecisionError(f"{label} states no usable weight range: {weights[0]}-{weights[1]}")
        # The x-height of every instance the artifacts really DRAW.  Both
        # stylesheets switch optical sizing off, so ``opsz`` rests at its default
        # - the instance whose metric was read above - and ``wght`` is the only
        # axis still moving.  It moves the drawn letter without always moving the
        # metric: over a real catalogue the metric follows the axis for two faces
        # of twelve while the OUTLINE of ``x`` follows it for eight, and where
        # both move they move by the same amount, to the unit.  So the metric
        # stays the SOURCE of the x-height and the outline is read for the one
        # thing the metric is silent about, the movement between instances.  A
        # face without a measurable ``x`` or without the axis simply does not
        # move, which is the truth for a static font and the safe answer for the
        # other: no movement is invented from a letter that cannot be measured.
        x_heights: dict[int, int] = {}
        for asked in ASKED_WEIGHTS:
            moved = _ink_x_height(font, cmap, {"wght": min(max(asked, weights[0]), weights[1])}) if axis is not None else None
            value = x_height + (0 if moved is None or ink is None else moved - ink)
            if not 0 < value <= units_per_em:
                raise VisualDecisionError(f"{label} states no usable x-height at weight {asked}: {value}/{units_per_em}")
            # Both nets again, per DRAWN instance.  They used to hold the default
            # instance only, while the sizes this module sets come from these
            # values: a face of 300/1000 at the default and 40/1000 at 700 was
            # accepted and drew a 320 px heading, and the twin refused the very
            # same file.  The twin was the stricter of the two; it no longer is.
            if value * X_HEIGHT_FLOOR.denominator < units_per_em * X_HEIGHT_FLOOR.numerator:
                raise VisualDecisionError(f"{label} states an x-height under {X_HEIGHT_FLOOR} of its em at weight {asked}: {value}/{units_per_em}")
            if moved is not None and not (value < moved * X_HEIGHT_INK_FACTOR and moved < value * X_HEIGHT_INK_FACTOR):
                raise VisualDecisionError(f"{label} states an x-height its own glyph x contradicts at weight {asked}: {value}/{units_per_em} against {moved}")
            x_heights[asked] = value
    except VisualDecisionError:
        raise
    except Exception as exc:
        raise VisualDecisionError(f"{label} tables cannot be read: {exc}") from exc
    return {"codepoints": codepoints, "ligatures": tuple(sorted(features)), "numerals": tuple(sorted(numerals)),
            "weights": weights, "weight_axis": weight_axis, "x_height": x_height, "x_heights": x_heights,
            "units_per_em": units_per_em, "x_height_source": x_height_source,
            "ascent": ascent, "descent": descent}


def _missing_codepoints(facts: dict[str, Any], text: str) -> list[str]:
    return [f"U+{value:04X}" for value in sorted({ord(char) for char in text if char not in ASCII_WHITESPACE}) if value not in facts["codepoints"]]


def _sheet_view(artifact_root: Path, brief: dict[str, Any], candidate_set: dict[str, Any], affinity_observation: dict[str, Any], *, logo_authentication: dict[str, Any] | None = None) -> dict[str, Any]:
    """Every candidate of the set with its own bound report, re-validated, grouped by domain.

    The renderer used to be handed one colour-report list, one font report and
    one logo report, which is why it could route exactly one font and one logo
    candidate.  It now reads the report each candidate binds - by path and
    SHA-256, from the artifact root - so any number of candidates per domain can
    be shown, two candidates may share a report or not, and no report reaches
    the page that its own candidate does not name.  Each distinct report is
    validated once, in the form ``validate_bundle`` uses; a candidate is then
    held to its report exactly as far as the page reads it.

    The coupling "one font report shared by typography and code font" is
    therefore released rather than kept: it was a statement about how one
    fixture was built, not a rule of the bundle, and ``validate_bundle`` never
    stated it.  Released fail-closed - every report is validated whichever
    candidates share it, and a candidate whose value names fonts or logo
    variants its report does not carry is refused.
    """
    validate_brief(brief, artifact_root)
    validate_candidate_set(candidate_set, artifact_root)
    context = candidate_context_sha256(candidate_set)
    reports: dict[tuple[str, str], dict[str, Any]] = {}
    # One replay set for the whole call: no two authored candidates of the
    # bundle may share a nonce, whichever report carries them.  The external
    # ledger is not read here - see _render_sheet.
    nonces: set[str] = set()
    domains: dict[str, list[dict[str, Any]]] = {kind: [] for kind in SHEET_DOMAINS}
    for candidate in candidate_set["candidates"]:
        kind, value = candidate["kind"], candidate["value"]
        if value["domain"] != kind:
            raise VisualDecisionError(f"candidate domain differs: {candidate['id']}")
        family = "font" if kind in {"typography", "code-font"} else "logo" if kind in {"logo", "lockup"} else "color"
        key = (family, value["report"]["sha256"])
        report = reports.get(key)
        if report is None:
            report = load_json_bytes(_verify_bound_file(artifact_root, value["report"], f"report {candidate['id']}"), f"report {candidate['id']}")
            if family == "color":
                validate_color_report(report)
            elif family == "font":
                validate_font_report(report, artifact_root)
            elif logo_authentication is None:
                validate_logo_report(report, artifact_root, affinity_observation)
            else:
                validate_logo_report(report, artifact_root, affinity_observation, logo_authentication["receipts"],
                                     project_root=logo_authentication["project_root"], run_id=logo_authentication["run_id"],
                                     key=logo_authentication["key"], replay_nonces=nonces, now=logo_authentication["now"])
            if report["candidate_set_sha256"] != context:
                raise VisualDecisionError(f"{family} report does not bind the candidate set")
            reports[key] = report
        eligible = report["objective_status"] == "pass" and not _report_human_fail(report)
        if candidate["status"] != ("candidate" if eligible else "blocked"):
            raise VisualDecisionError(f"candidate eligibility status differs: {candidate['id']}")
        entry: dict[str, Any] = {"candidate": candidate, "eligible": eligible, "report": report}
        if family == "color":
            if report["candidate_id"] != candidate["id"] or report["roles"] != value["roles"]:
                raise VisualDecisionError("color report candidate/value differs")
        elif family == "font":
            by_id = {item["id"]: item for item in report["candidates"]}
            wanted = value["font_candidate_ids"] if kind == "typography" else [value["code_font_id"]]
            fonts = [by_id.get(identifier) for identifier in wanted]
            classes = sorted(item["classification"] for item in fonts if item is not None)
            if None in fonts or classes != (["sans", "serif"] if kind == "typography" else ["mono"]) or value["specimen_sha256"] != report["specimen_sha256"]:
                raise VisualDecisionError("typography report candidate/value differs")
            entry["fonts"] = fonts
        else:
            items = [item for item in report["candidates"] if (item["variant"] in {"mark", "favicon"}) == (kind == "logo")]
            identifiers = value["logo_candidate_ids"] if kind == "logo" else value["lockup_candidate_ids"]
            if identifiers != [item["id"] for item in items] or any(item["creation_origin"] != value["creation_origin"] for item in items):
                raise VisualDecisionError("logo report candidate/value differs")
            entry["items"] = items
        domains[kind].append(entry)
    # The sheet names every open domain.  One it has no section for would drop
    # out of the owner's view unnoticed, so it is refused instead.
    unshown = [kind for kind in brief["open_decisions"] if kind not in SHEET_DOMAINS]
    if unshown:
        raise VisualDecisionError(f"sheet has no section for open domain: {unshown}")
    return {"domains": domains, "open": [kind for kind in SHEET_DOMAINS if kind in brief["open_decisions"]]}


def _sheet_assets(view: dict[str, Any], artifact_root: Path) -> dict[str, Any]:
    """The font and logo files the eligible candidates show, keyed by content.

    Assets are named by their SHA-256 rather than by a report-local candidate
    id: two reports may reuse one local id for different drawings, and a
    content name can neither collide nor be spelled twice for one file.  A font
    face is one (asset, classification) pair.
    """
    faces: dict[tuple[str, str], int] = {}
    fonts: dict[str, bytes] = {}
    facts: dict[str, dict[str, Any]] = {}
    logos: dict[str, bytes] = {}
    for kind in ("typography", "code-font"):
        for entry in view["domains"][kind]:
            if not entry["eligible"]:
                continue
            for item in entry["fonts"]:
                digest = item["asset"]["sha256"]
                if digest not in fonts:
                    fonts[digest] = _verify_bound_file(artifact_root, item["asset"], f"font {item['id']}")
                    facts[digest] = _font_facts(fonts[digest], f"font {item['id']}")
                faces.setdefault((digest, item["classification"]), len(faces))
    for kind in ("logo", "lockup"):
        for entry in view["domains"][kind]:
            for item in entry["items"] if entry["eligible"] else []:
                digest = item["asset"]["sha256"]
                if digest not in logos:
                    logos[digest] = _verify_bound_file(artifact_root, item["asset"], f"logo {item['id']}")
    return {"faces": faces, "fonts": fonts, "facts": facts, "logos": logos}


def _write_assets(output_fd: int, assets: dict[str, Any]) -> dict[str, str]:
    try:
        os.mkdir("assets", 0o700, dir_fd=output_fd)
        assets_fd = os.open("assets", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=output_fd)
    except Exception:
        os.close(output_fd); raise
    copied: dict[str, str] = {}
    try:
        for suffix, members in (("woff2", assets["fonts"]), ("svg", assets["logos"])):
            for digest, data in members.items():
                name = f"{digest}.{suffix}"; _write_new(assets_fd, name, data)
                copied[f"assets/{name}"] = sha256_bytes(data)
    except Exception:
        os.close(output_fd); raise
    finally:
        os.close(assets_fd)
    return copied


def _x_height_ratio(facts: dict[str, Any], weight: int = BASE_WEIGHT) -> Fraction:
    """The x-height of a face as an exact fraction of its em, at one drawn weight.

    The weight belongs in the signature because the answer depends on it: a
    variable face draws a different ``x`` at 700 than at 400, and the size that
    puts a role on target is the size for the instance THAT role is drawn in.
    """
    return Fraction(int(facts["x_heights"][weight]), int(facts["units_per_em"]))


def _normalised_size(nominal: Fraction, ratio: Fraction) -> Fraction:
    """``nominal`` scaled so a face of ``ratio`` reaches X_HEIGHT_TARGET, on the grid.

    Exact rational arithmetic and then ONE rounding, onto SIZE_QUANTUM, half away
    from zero.  Two candidates therefore differ in effective x-height by at most
    one rounding step, which is what turns "the same x-height" into a measurable
    claim instead of an intention.
    """
    steps = (nominal * X_HEIGHT_TARGET / ratio) / SIZE_QUANTUM
    return ((steps.numerator * 2 + steps.denominator) // (steps.denominator * 2)) * SIZE_QUANTUM


def _quantise(value: Fraction) -> Fraction:
    """One length onto the SIZE_QUANTUM grid, half away from zero.

    The same single rounding ``_normalised_size`` does, factored out because the
    baseline compensation needs it too and must not invent a second grid.
    """
    steps = value / SIZE_QUANTUM
    return ((steps.numerator * 2 + steps.denominator) // (steps.denominator * 2)) * SIZE_QUANTUM


def _first_baseline(facts: dict[str, Any], size: Fraction, line: Fraction) -> Fraction:
    """Where the engine puts the FIRST baseline inside one line box, in rem.

    Half the leading plus the ascent, with ascent and descent read from the
    table BASELINE_METRIC names.  This is a model of the engine and it is exact
    only up to the engine's own rounding: measured over 60 sizes chosen to land
    on the half-way points, no rounding rule - half-up, half-even or half-down -
    reproduces the drawn baseline for all of them (40, 46 and 50 of 60).  The
    compensation therefore removes the FONT-dependent part of the offset, which
    is whole pixels, and leaves that rounding, which is under one.
    """
    content = Fraction(int(facts["ascent"]) - int(facts["descent"]), int(facts["units_per_em"])) * size
    return (line - content) / 2 + Fraction(int(facts["ascent"]), int(facts["units_per_em"])) * size


def _round_half_up(value: Fraction) -> Fraction:
    """One length to a whole number, halves away from zero."""
    return Fraction((value.numerator * 2 + value.denominator) // (value.denominator * 2))


def _drawn_first_baseline(facts: dict[str, Any], size: Fraction, line: Fraction, label: str) -> Fraction:
    """Where the engine really DRAWS the first baseline, in whole CSS pixels.

    :func:`_first_baseline` is the exact arithmetic; this is the arithmetic the
    ENGINE does, which is the one the reader sees.  Measured (see
    BASELINE_ROUNDING): ascent and descent are each rounded to a whole CSS pixel,
    the leading is split with those rounded numbers, and the baseline that comes
    out is floored to a whole CSS pixel.  Because the result is a whole number of
    pixels, a compensation derived from it is a whole number of pixels too - and a
    whole-pixel shift is the one shift that cannot pull apart a role whose faces
    already share a baseline, which a fractional one demonstrably did.

    The one case the engine does not answer reproducibly is an ascent or descent
    landing EXACTLY on a half pixel: over the probe blocks that do, no rounding
    rule reproduces them all.  That is refused by name here rather than guessed,
    because a guess would silently misplace the role by a whole pixel.
    """
    upem = int(facts["units_per_em"])
    ascent = Fraction(int(facts["ascent"]), upem) * size * BASELINE_ROOT_PX
    descent = Fraction(-int(facts["descent"]), upem) * size * BASELINE_ROOT_PX
    for name, value in (("ascent", ascent), ("descent", descent)):
        if (value * 2).denominator == 1 and (value * 2).numerator % 2:
            raise VisualDecisionError(
                f"{label} draws its {name} exactly on a half pixel, where this engine's "
                f"rounding is not reproducible: {value} px")
    rounded = _round_half_up(ascent)
    content = rounded + _round_half_up(descent)
    placed = (line * BASELINE_ROOT_PX - content) / 2 + rounded
    return Fraction(placed.numerator // placed.denominator)


def _baseline_targets(faces: list[tuple[dict[str, Any], str]], selectors: dict[str, str],
                      role_weights: dict[str, int], role_classes: dict[str, tuple[str, ...]],
                      label: str) -> dict[str, Fraction]:
    """The one DRAWN first baseline per role, over the faces that really set it.

    The LOWEST common line: the largest drawn baseline any face that SETS this
    role has, so every compensation is a downward shift for those faces and no
    ascender is pushed out of its box.  It is derived from the set of files on the
    sheet rather than set as a constant, because a constant that suited one bundle
    would clip another.  Both artifacts show the same faces, so both arrive at the
    same target.

    Only the faces of ``role_classes`` count.  A face that never sets the role has
    no baseline to share, and letting it decide the target moved every block of
    the role further down than anything on the page required.
    """
    # A bundle may show no font at all - a colour-only brief, or every
    # typography candidate blocked.  There is then nothing to align, and the
    # caller writes no face rule either, so an empty map is the honest answer.
    # It used to be a ValueError out of ``max()`` over an empty sequence.
    if not faces:
        return {}
    targets: dict[str, Fraction] = {}
    for role in selectors:
        nominal, line = ROLE_METRICS[role]
        # A bundle can carry no face of the drawing classification at all - a
        # brief without a code font has no mono.  Nothing then draws the role, so
        # no target can be observed; the widest set is the honest fallback and the
        # rule it writes is never seen.
        drawing = [facts for facts, classification in faces if classification in role_classes[role]]
        offsets = []
        for facts in (drawing or [facts for facts, _classification in faces]):
            ratio = Fraction(int(facts["x_heights"][role_weights[role]]), int(facts["units_per_em"]))
            offsets.append(_drawn_first_baseline(facts, _normalised_size(nominal, ratio), line,
                                                 f"{label} {role}"))
        targets[role] = max(offsets)
    return targets


def _rem(value: Fraction) -> str:
    """One length on the SIZE_QUANTUM grid, in the one spelling this module uses."""
    steps = value / SIZE_QUANTUM
    if steps.denominator != 1 or steps.numerator < 0:
        raise VisualDecisionError(f"length is not on the rem grid this module spells: {value}")
    whole, part = divmod(steps.numerator, SIZE_QUANTUM.denominator)
    return f"{whole}" if not part else f"{whole}.{part:04d}".rstrip("0")


def _rem_signed(value: Fraction) -> str:
    """A length on the SIZE_QUANTUM grid that is allowed to be negative.

    The compensation is the one length here that can point upwards: a role's
    target is the lowest baseline of the faces that DRAW it, and a face that never
    draws the role may sit below that target.  Sizes and line boxes keep the
    strict :func:`_rem`, which refuses a negative outright.
    """
    return f"-{_rem(-value)}" if value < 0 else _rem(value)


def _face_metric_rules(prefix: str, index: int, facts: dict[str, Any],
                       selectors: dict[str, str], role_weights: dict[str, int],
                       targets: dict[str, Fraction]) -> str:
    """Every role this face can set, each at the size that puts its x-height on target.

    The SIZE is per candidate AND per role, because the x-height is a property of
    the file and of the instance the role is drawn in.  Reading one ratio for all
    roles is what left the drawn letters unequal while the declared promise held
    exactly: the sheet sets a heading at 700, and eight variable faces of twelve
    draw a taller ``x`` there than at 400.  The LINE BOX is not per candidate, so
    it is spelled as the same absolute length for all of them.  A reader
    comparing two cards then compares letters of equal drawn height on rows of
    equal pitch - the comparison the owner asked for, and the one a single
    declared size quietly refuses to make.
    """
    heights, units_per_em = facts["x_heights"], int(facts["units_per_em"])
    rules = []
    for role, selector in selectors.items():
        size, line = ROLE_METRICS[role]
        ratio = Fraction(int(heights[role_weights[role]]), int(units_per_em))
        normalised = _normalised_size(size, ratio)
        # The shift that puts this face's DRAWN first baseline on the role's
        # common one.  Both sides are whole CSS pixels, so the shift is a whole
        # number of pixels and lands on the rem grid exactly - it takes the
        # engine's rounding WITH it instead of leaving it as a residual, which is
        # what kept two candidates up to one and a half pixels apart while the
        # declared compensation looked exact.  It is negative only for a face that
        # does not draw the role (see :func:`_rem_signed`).
        # ``position:relative`` moves the DRAWING and not the flow, so the block
        # still advances by its line boxes and row n of every candidate stays on
        # row n of every other; a padding would have moved the baseline and the
        # advance together.
        drawn = _drawn_first_baseline(facts, normalised, line, f"{prefix}-{index} {role}")
        shift = (targets[role] - drawn) / BASELINE_ROOT_PX
        target = ",".join(f".{prefix}-{index} {part}" for part in selector.split(","))
        rules.append(f"{target}{{font-size:{_rem(normalised)}rem;line-height:{_rem(line)}rem;"
                     f"position:relative;top:{_rem_signed(shift)}rem}}")
    return " ".join(rules)


def _weight_descriptor(weights: tuple[int, int]) -> str:
    """The ``font-weight`` descriptor of one ``@font-face``: a range, or the one weight."""
    low, high = weights
    return str(low) if low == high else f"{low} {high}"


def _weights_set(weights: tuple[int, int], selectors: dict[int, str]) -> tuple[int, ...]:
    """The weights an artifact really SETS on a face, clamped into what it has.

    The same clamp ``_face_weight_rules`` writes into the stylesheet, read back
    as a set of numbers rather than as CSS - so the disclosure cannot say one
    thing while the rules do another.  A static face collapses to a single
    value here, and that IS the answer the owner is after: a file that states
    one weight draws headings in the same weight as body text.
    """
    low, high = weights
    return tuple(sorted({min(max(value, low), high) for value in (BASE_WEIGHT, *selectors)}))


def _weight_note(facts: dict[str, Any], selectors: dict[int, str]) -> str:
    """What the FILE offers and what the artifact sets, as one sentence.

    Both halves come out of the font: the range from its ``wght`` axis or its
    single declared weight, the set from clamping the asked weights into that
    range.  Nothing here is read from a family name.
    """
    low, high = facts["weights"]
    used = _weights_set(facts["weights"], selectors)
    offered = (f"variable Achse wght {low} bis {high}" if facts["weight_axis"]
               else f"starr {low}" if low == high else f"statisch {low} bis {high}")
    drawn = ", ".join(str(value) for value in used)
    reserve = "" if len(used) > 1 else " - kein Zulegen in Überschriften möglich"
    return f"Gewichte: {offered}; gesetzt werden {drawn}{reserve}."


def _face_weight_rules(prefix: str, index: int, weights: tuple[int, int], selectors: dict[int, str]) -> str:
    """The weights this face is asked for, each clamped into what the font has.

    Without a ``font-weight`` descriptor a browser matches every requested
    weight to the single face it has and *synthesises* the difference: it smears
    the outline to fake a bold the file does not contain, and the reader then
    judges a weight nobody drew.  Measured on a real run: fourteen faces, no
    descriptor, headings at 600 and 700 - so every heading on that sheet was a
    fake.  The descriptor states the truth about the file; these rules keep the
    page from asking outside it.

    The sheet asks for three weights inside a candidate face - 700 for a
    heading, a table head and a code label, 600 for a subheading, 400 for
    everything else - and each is clamped rather than dropped, so a font that
    starts at 500 sets its body text in 500 and one that ends at 400 sets its
    heading in 400.  The clamp is per candidate because the range is a property
    of the file, not of the sheet; the *sizes* stay the same for all candidates,
    which is what makes them comparable.
    """
    low, high = weights
    def clamp(value: int) -> int:
        return min(max(value, low), high)
    rules = [f".{prefix}-{index}{{font-weight:{clamp(400)}}}"]
    for weight, selector in sorted(selectors.items(), reverse=True):
        target = ",".join(f".{prefix}-{index} {part}" for part in selector.split(","))
        rules.append(f"{target}{{font-weight:{clamp(weight)}}}")
    return " ".join(rules)


def _sheet_css(font_faces: str, swatch_rules: str) -> str:
    """The style sheet this module writes, as one expression.

    Extracted so the offline scanner can be measured against the very CSS
    the renderer emits.  Nothing validated the two against each other: the
    scanner now refuses a CSS function it has never seen, and the only
    honest way to keep that gate from becoming a wall is a test that feeds
    it this exact text - format(), var(), repeat() and minmax() included.
    ``swatch_rules`` carries the generated palette and swatch rules.

    The table wrapper states ``overflow-y: hidden`` because that is what it
    really GETS: measured in the target engine, ``overflow-y: clip`` beside
    ``overflow-x: auto`` computes to ``hidden``, and ``overflow-clip-margin``
    applies only to ``clip`` - written beside ``hidden`` it is inert.  The
    counter-check was two-sided: with an artificial downward shift the rule WITH
    the margin let exactly as much ink escape as the same rule WITHOUT it, while
    ``overflow: clip`` on both axes kept it.  Clipping both axes is deliberately
    NOT done: ``overflow-x: clip`` would take a wide table its horizontal
    scrolling, and unreachable content is worse than a mitigation that has
    nothing to mitigate - up to an 8 px shift no ink is lost either way, and the
    shift this module writes is a small number of whole pixels.  The purpose the
    declaration serves is the one it can serve: no vertical scrollbar.
    """
    reference = ";".join(f"--{role}:{value}" for role, value in REFERENCE_PALETTE.items())
    # One rule per numeral feature, generated from the very set the detection
    # reads, so a feature can never be shown without a rule that switches it on
    # or carry a rule the detection does not know.
    numerals = " ".join(f'.num-{tag}{{font-feature-settings:"{tag}" 1}}' for tag in sorted(NUMERAL_FEATURES))
    return f"""{font_faces}
{swatch_rules}
:root{{--ink:#171717;--paper:#fff;--line:#d4d4d4;--muted:#555;color-scheme:light}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 sans-serif}} main{{max-width:90rem;margin:auto;padding:1rem;overflow-wrap:anywhere;hyphens:auto}} h1,h2,h3{{line-height:1.15}} .notice{{border:.2rem solid #111;padding:1rem}} .reference{{border-left:.3rem solid var(--line);padding:.25rem .75rem;color:var(--muted)}} .state{{font-weight:700}}
.grid{{display:grid;grid-template-columns:1fr;gap:1rem}} .card{{border:1px solid var(--line);padding:1rem;min-width:0;overflow-wrap:anywhere}} .swatches{{margin:.75rem 0}} .swatch{{display:flex;justify-content:space-between;gap:1rem;border-left-width:3rem;border-left-style:solid;padding:.4rem}}
.table-wrap{{overflow-x:auto;overflow-y:hidden;max-width:100%;margin:.75rem 0}} table{{border-collapse:collapse;min-width:100%}} th,td{{border:1px solid var(--line);padding:.3rem .5rem;text-align:left;vertical-align:top;overflow-wrap:normal;hyphens:manual}} td{{font-variant-numeric:tabular-nums;text-align:right}}
img{{display:block;max-width:100%;height:5rem;background:#eee}} .specimen{{white-space:pre-wrap;font-size:1.15rem;letter-spacing:.01em}} pre{{overflow-x:auto;max-width:100%}} code{{font-size:.875rem}}
.pal-ref{{{reference}}} .ci,.set{{background:var(--canvas);color:var(--text);border:1px solid var(--border);padding:1rem;margin:.75rem 0}} .ci th,.set th{{background:var(--surface-strong)}} .ci td,.set td{{background:var(--surface)}} .ci th,.ci td,.set th,.set td{{border-color:var(--border)}}
.ci-heading,.t-heading{{font-size:1.6rem;font-weight:700;line-height:1.2;margin:0 0 .5rem}} .t-subheading{{font-size:1.2rem;font-weight:600;margin:0 0 .75rem}} .t-body,.ci-body{{font-size:1.05rem;line-height:1.6}} .t-quote{{border-left:.25rem solid var(--border);padding-left:.75rem;font-style:italic}}
.ci-actions{{display:flex;flex-wrap:wrap;gap:.75rem;margin:.75rem 0}} .ci-action{{margin:0;padding:.5rem 1rem;border-radius:.375rem;font-weight:700}} .ci-primary{{background:var(--primary);color:var(--on-primary)}} .ci-secondary{{background:var(--secondary);color:var(--on-secondary)}} .ci-focus{{background:var(--surface);color:var(--text);outline:3px solid var(--focus);outline-offset:3px}}
.ci-kpi{{background:var(--accent);color:var(--on-accent);padding:.75rem 1rem;margin:.75rem 0}} .ci-kpi-value{{font-size:2.2rem;font-weight:700;margin:0}} .ci-kpi-label{{margin:0}} .ci-state{{padding:.5rem .75rem;margin:.5rem 0}} .ci-warning{{background:var(--warning);color:var(--on-warning)}} .ci-danger{{background:var(--danger);color:var(--on-danger)}}
.face-ref-sans{{font-family:sans-serif}} .face-ref-serif,.face-ref-serif pre{{font-family:serif}} .face-ref-mono,.face-ref-mono pre{{font-family:monospace}}
.code,.code-liga,.code-noliga{{white-space:pre-wrap;overflow-wrap:break-word;font-size:.95rem;line-height:1.5;background:#f6f6f6;border:1px solid var(--line);padding:.75rem;margin:.25rem 0 .75rem;tab-size:4}} .code-liga{{font-variant-ligatures:normal;font-feature-settings:"liga" 1,"calt" 1}} .code-noliga{{font-variant-ligatures:none;font-feature-settings:"liga" 0,"calt" 0}} .code-label{{margin:.5rem 0 0;font-weight:700}}
{numerals}
.numerals-set{{margin:.5rem 0 .75rem}} .numerals{{margin:.15rem 0;white-space:pre-wrap;font-variant-numeric:normal;letter-spacing:.01em}} .numerals-name{{font-family:sans-serif;font-size:.875rem;font-weight:400;font-variant-numeric:normal;font-feature-settings:normal;letter-spacing:normal;margin-left:1rem;color:var(--muted)}}
.logo{{margin:.75rem 0}} .logo-sizes{{display:flex;flex-wrap:wrap;gap:1rem;align-items:flex-end;padding:.5rem;background:#fff;border:1px solid var(--line)}} .logo-dark{{background:#1a1a1a}} .px-16 img{{height:16px;background:transparent}} .px-24 img{{height:24px;background:transparent}} .px-32 img{{height:32px;background:transparent}} .px-48 img{{height:48px;background:transparent}}
@media(min-width:768px){{main{{padding:2rem}}.grid{{grid-template-columns:repeat(2,minmax(0,1fr))}}}} @media(min-width:1440px){{.grid{{grid-template-columns:repeat(3,minmax(0,1fr))}}}} @media(max-width:320px){{main{{padding:.5rem}}.card{{padding:.75rem}}}}
"""


def _sheet_table(head: list[str] | tuple[str, ...], rows: list[list[str]] | list[tuple[str, ...]]) -> str:
    """A table in the closed form: explicit tbody, a row header per row, text-only cells."""
    escape = html.escape
    head_cells = "".join(f"<th>{escape(cell)}</th>" for cell in head)
    body = "".join("<tr>" + f"<th>{escape(row[0])}</th>" + "".join(f"<td>{escape(cell)}</td>" for cell in row[1:]) + "</tr>" for row in rows)
    return f'<div class="table-wrap"><table><thead><tr>{head_cells}</tr></thead><tbody>{body}</tbody></table></div>'


def _sheet_ci_view(content: dict[str, Any], palette: str) -> str:
    """One colour candidate applied: heading, text, actions, focus, KPI, notice, error and numbers."""
    escape = html.escape
    return (f'<div class="ci {palette}">'
            f'<p class="ci-heading face-ref-sans">{escape(content["heading"])}</p>'
            f'<p class="ci-body face-ref-serif">{escape(content["paragraphs"][0])}</p>'
            '<div class="ci-actions face-ref-sans">'
            f'<p class="ci-action ci-primary">{escape(content["action_primary"])}</p>'
            f'<p class="ci-action ci-secondary">{escape(content["action_secondary"])}</p>'
            f'<p class="ci-action ci-focus">{escape(content["action_focus"])}</p></div>'
            f'<div class="ci-kpi face-ref-sans"><p class="ci-kpi-value">{escape(content["kpi_value"])}</p><p class="ci-kpi-label">{escape(content["kpi_label"])}</p></div>'
            f'<p class="ci-state ci-warning face-ref-serif"><strong>{escape(content["notice_label"])}</strong> {escape(content["notice"])}</p>'
            f'<p class="ci-state ci-danger face-ref-serif"><strong>{escape(content["error_label"])}</strong> {escape(content["error"])}</p>'
            f'<div class="face-ref-serif">{_sheet_table(content["table_head"], content["table_rows"])}</div></div>')


def _sheet_typeset(content: dict[str, Any], sans: str, serif: str) -> str:
    """Set typography in context: sans headings, serif body, on the neutral reference palette."""
    escape = html.escape
    return (f'<div class="set pal-ref"><div class="{sans}"><p class="t-heading">{escape(content["heading"])}</p>'
            f'<p class="t-subheading">{escape(content["subheading"])}</p></div><div class="{serif}">'
            + "".join(f'<p class="t-body">{escape(text)}</p>' for text in content["paragraphs"])
            + "<ul>" + "".join(f"<li>{escape(item)}</li>" for item in content["list"]) + "</ul>"
            + _sheet_table(content["table_head"], content["table_rows"])
            + f'<p class="t-quote">{escape(content["quote"])}</p></div></div>')


def _numeral_rows(label: str, facts: dict[str, Any], face_class: str) -> str:
    """The digit sets this font really offers, one named row each - or that it offers none.

    "No information" must not be readable as "no variants", so the empty case is
    a sentence of its own rather than an absent block: a font really can carry
    none of the five, and on a measured catalogue one of fourteen does.
    """
    escape = html.escape
    if not facts["numerals"]:
        return (f'<p>{escape(label)}: keine der Ziffernfeatures {escape(", ".join(sorted(NUMERAL_FEATURES)))} '
                "im Font. Das ist eine Aussage der Fonttabellen, kein fehlender Befund - die Schrift setzt "
                "ihren einen Ziffernsatz.</p>")
    rows = "".join(f'<p class="numerals num-{escape(tag)}">{escape(NUMERAL_SAMPLE)}'
                   f'<span class="numerals-name">{escape(NUMERAL_LABELS[tag])} ({escape(tag)})</span></p>'
                   for tag in facts["numerals"])
    # The digit run gets the same coverage statement the set text and the code
    # block get.  A font can carry tnum and no digits at all: the browser then
    # draws ten digits out of a fallback font under a heading that says these are
    # the font's own digit variants, and every row shows the same substitute.
    # The feature is real - it is read out of GSUB - but the letters are not the
    # candidate's, and that is the one thing this block exists to show.
    return (f'<p>{escape(label)}: Ziffernvarianten aus den Fonttabellen, '
            f'{escape(", ".join(facts["numerals"]))}. Jede Zeile zeigt dieselben zehn Ziffern in derselben '
            f'Größe.</p>' + _coverage_note(label, facts, NUMERAL_SAMPLE)
            + f'<div class="numerals-set {escape(face_class)}">{rows}</div>')


def _metric_note(label: str, facts: dict[str, Any], role: str) -> str:
    """What this face was normalised on, and the size that came out of it.

    The size is disclosed so nobody reads it as a recommendation: it is a
    consequence of the x-height, and in a real project the size would be set
    against the design rather than against this sheet.

    The WEIGHT is named with it because the x-height is stated for the instance
    this role is drawn in, and the range is named because the playground named it
    and the sheet did not - one owner reading both artifacts side by side should
    not have to conclude that the range is a property of the playground.
    """
    weight = SHEET_ROLE_WEIGHTS[role]
    ratio = _x_height_ratio(facts, weight)
    stated = facts["x_heights"][weight]
    low, high = facts["weights"]
    source = "OS/2.sxHeight" if facts["x_height_source"] == "os2-sxheight" else "gemessen an der Glyphe x"
    return (f'<p>{html.escape(label)}: x-Höhe {stated} von {facts["units_per_em"]} Einheiten '
            f'({float(ratio):.3f} em, {source}), gelesen an der gezeichneten Instanz bei Gewicht {weight}. '
            f'{_weight_note(facts, SHEET_WEIGHT_SELECTORS)} Gewichtsbereich {low} bis {high}. Auf gleiche x-Höhe normalisiert, Grad '
            f'{_rem(_normalised_size(ROLE_METRICS[role][0], ratio))} rem bei gleichem absolutem Zeilenabstand. '
            "Der Grad folgt aus der Normalisierung und ist keine Empfehlung.</p>")


def _font_overview(entries: list[dict[str, Any]], facts: dict[str, dict[str, Any]]) -> str:
    """One row per shown font: what it was normalised to, and whether it sets a number column.

    ``tnum`` is in this table rather than only in the card, because it is the one
    numeral feature that decides a question the owner actually has - whether the
    font can hold a column of figures still.
    """
    rows = []
    for entry in entries:
        for item in entry["fonts"]:
            fact = facts[item["asset"]["sha256"]]
            role = FACE_ROLE[item["classification"]]
            weight = SHEET_ROLE_WEIGHTS[role]
            ratio = _x_height_ratio(fact, weight)
            low, high = fact["weights"]
            rows.append((entry["candidate"]["id"], item["classification"], item["family"],
                         f'{fact["x_heights"][weight]}/{fact["units_per_em"]}', f"{float(ratio):.3f}",
                         "OS/2" if fact["x_height_source"] == "os2-sxheight" else "Glyphe x",
                         f'{_rem(_normalised_size(ROLE_METRICS[role][0], ratio))} rem',
                         f'{low}–{high}' if fact["weight_axis"] else (f'{low}' if low == high else f'{low}–{high} statisch'),
                         ", ".join(str(value) for value in _weights_set(fact["weights"], SHEET_WEIGHT_SELECTORS)),
                         "ja" if "tnum" in fact["numerals"] else "nein"))
    return _sheet_table(("Kandidat", "Klasse", "Familie", "x-Höhe", "x/em", "Quelle", "Grad",
                         "Gewichte im Font", "gesetzt", "tabellarische Ziffern"), rows)


def _coverage_note(label: str, facts: dict[str, Any], text: str) -> str:
    missing = _missing_codepoints(facts, text)
    if not missing:
        return f"<p>{html.escape(label)}: jedes Zeichen des Beispieltexts steht im Font.</p>"
    return f"<p>{html.escape(label)}: nicht im Font, Darstellung aus einer Ersatzschrift: {html.escape(', '.join(missing))}.</p>"


def _domain_state(kind: str, entries: list[dict[str, Any]], open_domains: list[str], affinity_observation: dict[str, Any]) -> str:
    """How a domain stands: open with candidates, open and blocked, open and empty, or not open."""
    eligible = sum(1 for entry in entries if entry["eligible"]); blocked = len(entries) - eligible
    if kind not in open_domains:
        text = "Im Visual Brief nicht offen." + (" Das Bundle enthält trotzdem Kandidaten, sie stehen unten." if entries else "")
    elif eligible:
        text = f"Offen. Prüfbare Kandidaten: {eligible}." + (f" Blockiert: {blocked}." if blocked else "")
    elif blocked:
        text = f"Offen, Blocker: kein prüfbarer Kandidat. Blockiert: {blocked}."
    elif kind in {"logo", "lockup"} and affinity_observation["status"] != "available":
        text = (f"Offen, Blocker: Affinity MCP ist {affinity_observation['status']}. Ohne verfügbare Affinity-Capability "
                "entstehen keine Logo- oder Lockup-Kandidaten, und dieser Bogen zeigt keine Ersatzgrafik.")
    else:
        text = "Offen: das Bundle enthält keine Kandidaten für diese Domäne."
    return f'<p class="state">{html.escape(text)}</p>'


def _render_sheet(
    output: Path,
    artifact_root: Path,
    brief: dict[str, Any],
    candidate_set: dict[str, Any],
    affinity_observation: dict[str, Any],
    candidate_file_sha256: str | None = None,
    *,
    logo_authentication: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one candidate sheet for reports this function re-validates itself.

    Every report is read through the candidate that binds it (see _sheet_view),
    so the sheet shows any number of candidates per domain and any subset of
    the five domains; a domain without candidates is stated as open, not open or
    as a named blocker, never filled with a substitute graphic.

    ``logo_authentication`` carries what :func:`validate_logo_report` needs for
    Affinity-authored candidates - the mutation receipts, the project root, the
    run id, the owner key and the clock - and is ``None`` for every bundle
    without them, which is the shape every caller had before.  It is *not*
    optional in the sense of "may be omitted": :func:`render_sheet` supplies it
    exactly when the bundle carries a descriptor, and without it an
    Affinity-authored report is refused here rather than rendered, which is the
    behaviour this parameter preserves for the unauthenticated case.

    Why it had to exist: this re-validation is the reason an Affinity-authored
    bundle could not be rendered at all.  ``validate_bundle`` accepted the
    report with its receipts one call earlier, and this line then re-validated
    the *same* report *without* them and refused it with "each Affinity-authored
    logo requires its own complete mutation receipt" - unconditionally, on the
    very first attempt, with unspent receipts.  ``render_sheet`` is the only
    producer of the unselected candidate sheet, so the accepted owner flow had
    no first step for exactly the case ``affinity_auth`` exists for.  The replay
    set handed to the logo validation is a fresh, call-local one: the external
    ledger has already had its say under its lock in ``validate_bundle``, and
    nothing here reads or writes it.
    """
    view = _sheet_view(artifact_root, brief, candidate_set, affinity_observation, logo_authentication=logo_authentication)
    content = _sample_content(brief)
    candidate_set_sha256 = candidate_file_sha256 or sha256_bytes(canonical_json(candidate_set))
    assets = _sheet_assets(view, artifact_root)
    domains, faces, facts, escape = view["domains"], assets["faces"], assets["facts"], html.escape
    eligible = {kind: [entry for entry in domains[kind] if entry["eligible"]] for kind in SHEET_DOMAINS}

    swatch_rules: list[str] = []
    color_cards: list[str] = []
    for index, entry in enumerate(eligible["color"]):
        report = entry["report"]
        swatch_rules.append(f".pal-{index}{{" + ";".join(f"--{role}:{value}" for role, value in report["roles"].items()) + "}")
        swatches = []
        for role_index, (role, value) in enumerate(report["roles"].items()):
            swatch_rules.append(f".swatch-{index}-{role_index}{{border-left-color:{value}}}")
            swatches.append(f'<div class="swatch swatch-{index}-{role_index}"><span>{escape(role)}</span><code>{escape(value)}</code></div>')
        tests = _sheet_table(("Vordergrund", "Hintergrund", "Verwendung", "Minimum", "Ratio", "Status"),
                             [(test["foreground_role"], test["background_role"], test["usage"], f"{test['minimum_ratio']:g}:1",
                               test["ratio_display"], "bestanden" if test["passed"] else "nicht bestanden") for test in report["tests"]])
        color_cards.append(f'<article class="card"><h3>Kandidat {escape(entry["candidate"]["id"])}</h3>'
                           f'<p>Objektiver WCAG-Status: <strong>{escape(report["objective_status"])}</strong></p><p>Visuelle Prüfung: erforderlich</p>'
                           + _sheet_ci_view(content, f"pal-{index}") + f'<div class="swatches">{"".join(swatches)}</div>{tests}</article>')

    def face(item: dict[str, Any]) -> str:
        return f"face-{faces[(item['asset']['sha256'], item['classification'])]}"

    type_cards: list[str] = []
    typeset_text = _typeset_text(content)
    for entry in eligible["typography"]:
        by_class = {item["classification"]: item for item in entry["fonts"]}
        specimens = "".join(f'<div class="{face(item)}"><p>{escape(item["classification"])} · {escape(item["family"])}</p><p class="specimen">{escape(SPECIMEN)}</p></div>'
                            for item in entry["fonts"])
        type_cards.append(f'<article class="card"><h3>Kandidat {escape(entry["candidate"]["id"])}</h3>{specimens}'
                          f'<p>Gesetzter Text: Überschriften in {escape(by_class["sans"]["family"])}, Fließtext in {escape(by_class["serif"]["family"])}.</p>'
                          + _sheet_typeset(content, face(by_class["sans"]), face(by_class["serif"]))
                          + "".join(_coverage_note(f"{item['classification']} · {item['family']}", facts[item["asset"]["sha256"]], typeset_text) for item in (by_class["sans"], by_class["serif"]))
                          + "".join(_metric_note(f"{item['classification']} · {item['family']}", facts[item["asset"]["sha256"]], FACE_ROLE[item["classification"]])
                                    for item in (by_class["sans"], by_class["serif"]))
                          + _numeral_rows(f"Ziffern im Fließtext · {by_class['serif']['family']}",
                                          facts[by_class["serif"]["asset"]["sha256"]], face(by_class["serif"]))
                          + "<p>Lesbarkeit, Laufweite, Fallback und 200%-Reflow: menschliche Prüfung erforderlich.</p></article>")

    code_cards: list[str] = []
    for entry in eligible["code-font"]:
        item = entry["fonts"][0]; font_facts = facts[item["asset"]["sha256"]]; code = escape(content["code"])
        if font_facts["ligatures"]:
            note = f'<p>Ligaturen: GSUB-Features {escape(", ".join(font_facts["ligatures"]))} im Font. Der erste Block zeigt sie aktiv, der zweite abgeschaltet.</p>'
            blocks = (f'<div class="{face(item)}"><p class="code-label">Ligaturen aktiv</p><pre class="code-liga">{code}</pre>'
                      f'<p class="code-label">Ligaturen abgeschaltet</p><pre class="code-noliga">{code}</pre></div>')
        else:
            note = "<p>Ligaturen: der Font hat keine liga- oder calt-Features.</p>"
            blocks = f'<div class="{face(item)}"><pre class="code">{code}</pre></div>'
        code_cards.append(f'<article class="card"><h3>Kandidat {escape(entry["candidate"]["id"])}</h3>'
                          f'<div class="{face(item)}"><p>mono · {escape(item["family"])}</p><pre class="specimen">{escape(SPECIMEN)}</pre></div>'
                          + note + blocks + _coverage_note(f"mono · {item['family']}", font_facts, content["code"])
                          + _metric_note(f"mono · {item['family']}", font_facts, FACE_ROLE["mono"])
                          + _numeral_rows(f"Ziffern im Code · {item['family']}", font_facts, face(item))
                          + "<p>Code-Lesbarkeit und Zeichenunterscheidung: menschliche Prüfung erforderlich.</p></article>")

    def logo_cards(kind: str) -> list[str]:
        cards = []
        for entry in eligible[kind]:
            figures = []
            for item in entry["items"]:
                source = f'assets/{item["asset"]["sha256"]}.svg'; variant = escape(item["variant"])
                sizes = "".join(f'<div class="px-{size}"><img src="{source}" alt="{variant} {size} px"></div>' for size in (16, 24, 32, 48))
                figures.append(f'<div class="logo"><p>{variant} · {escape(item["id"])}</p><img src="{source}" alt="{variant} candidate">'
                               f'<div class="logo-sizes">{sizes}</div><div class="logo-sizes logo-dark">{sizes}</div></div>')
            cards.append(f'<article class="card"><h3>Kandidat {escape(entry["candidate"]["id"])}</h3>{"".join(figures)}'
                         "<p>16/24/32 px, Monochrom, Clipping und Lesbarkeit: menschliche Prüfung erforderlich.</p></article>")
        return cards

    references = {
        "color": "Jede Farbansicht setzt dieselben generischen Systemschriften: Überschriften serifenlos, Fließtext mit Serifen. Sie gehören zu keinem Typografiekandidaten.",
        "typography": "Jeder gesetzte Text hat denselben Inhalt und steht auf derselben neutralen Graupalette dieses Bogens. Sie gehört zu keinem Farbkandidaten. Die Specimens laufen nicht mit gleichem Grad, sondern mit gleicher x-Höhe: der Grad je Kandidat folgt daraus und ist ausgewiesen, der Zeilenabstand ist absolut gleich.",
        "code-font": "Jeder Codeblock hat denselben Inhalt und steht auf neutralem Grund, der zu keinem Farbkandidaten gehört. Auch hier ist auf gleiche x-Höhe normalisiert und der Grad je Kandidat ausgewiesen.",
    }
    titles = {"color": "Farbkandidaten", "typography": "Typografiekandidaten", "code-font": "Code-Font-Kandidaten", "logo": "Logo-Kandidaten", "lockup": "Lockup-Kandidaten"}
    cards = {"color": color_cards, "typography": type_cards, "code-font": code_cards, "logo": logo_cards("logo"), "lockup": logo_cards("lockup")}
    sections = []
    for kind in SHEET_DOMAINS:
        blocked = [entry for entry in domains[kind] if not entry["eligible"]]
        reference = (f'<p class="reference"><strong>Referenzbelegung, keine Empfehlung:</strong> {escape(references[kind])} '
                     "Freie Kombinationen zeigt der nicht normative Playground.</p>") if kind in references and cards[kind] else ""
        survey = _font_overview(eligible[kind], facts) if kind in {"typography", "code-font"} and cards[kind] else ""
        sections.append(f'<section id="candidates-{kind}"><h2>{titles[kind]}</h2>' + _domain_state(kind, domains[kind], view["open"], affinity_observation)
                        + reference + survey + (f'<div class="grid">{"".join(cards[kind])}</div>' if cards[kind] else "")
                        + ("<ul>" + "".join(f"<li>Kandidat {escape(entry['candidate']['id'])}: blockiert, der Prüfbericht ist nicht review-fähig.</li>" for entry in blocked) + "</ul>" if blocked else "")
                        + "</section>")
    overview = _sheet_table(("Domäne", "Visual Brief", "Prüfbare Kandidaten", "Blockiert"),
                            [(DOMAIN_LABELS[kind], "offen" if kind in view["open"] else "nicht offen", str(len(eligible[kind])), str(len(domains[kind]) - len(eligible[kind])))
                             for kind in SHEET_DOMAINS])
    recommendations = "".join(
        f'<li><strong>Unverbindliche Empfehlung:</strong> {escape(item["candidate_id"])} — {escape(item["rationale"])}</li>'
        for item in candidate_set["recommendations"]
    ) or "<li>Keine Empfehlung abgegeben.</li>"
    sheet_targets = _baseline_targets([(facts[digest], classification) for digest, classification in faces],
                                      SHEET_ROLE_SELECTORS, SHEET_ROLE_WEIGHTS, SHEET_ROLE_CLASSES, "sheet")
    font_faces = "\n".join(
        f'@font-face{{font-family:"VisualCandidate{index}";src:url("assets/{digest}.woff2") format("woff2");font-display:block;font-weight:{_weight_descriptor(facts[digest]["weights"])}}} '
        f'.face-{index},.face-{index} pre{{font-family:"VisualCandidate{index}",{GENERIC_FAMILIES[classification]};font-optical-sizing:none}} '
        + _face_weight_rules("face", index, facts[digest]["weights"], SHEET_WEIGHT_SELECTORS) + " "
        + _face_metric_rules("face", index, facts[digest], SHEET_ROLE_SELECTORS, SHEET_ROLE_WEIGHTS, sheet_targets)
        for (digest, classification), index in faces.items()
    )
    css = _sheet_css(font_faces, " ".join(swatch_rules))
    document = f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="{SHEET_CSP}"><title>BrandKit Visual Decisions</title><link rel="stylesheet" href="style.css"></head>
<body><main><h1>Visueller Entscheidungsbogen</h1><p class="notice"><strong>Nicht normativ.</strong> Kandidaten und Empfehlungen sind getrennte Prüfartefakte. Dieser Bogen trifft keine Auswahl.</p><div class="overview">{overview}</div>{"".join(sections)}<section id="recommendations"><h2>Unverbindliche Empfehlungen</h2><ul>{recommendations}</ul></section><section id="owner-review"><h2>Owner-Review</h2><p>Keine Auswahlmöglichkeit in diesem Artefakt. Die Bestätigung erfolgt im bestehenden Interviewdialog.</p><ul><li>Prüfe den Bogen bei 320, 768 und 1440 CSS-Pixeln sowie bei 200 % Zoom.</li><li>Nenne danach je Domäne nur Candidate-IDs oder open beziehungsweise not-applicable.</li><li>Die Specimens sind je Kandidat auf gleiche x-Höhe normalisiert. Der je Kandidat ausgewiesene Grad ist die Folge davon und keine Empfehlung.</li><li>Ohne gepinnten Renderer bleiben Font-Load und Fallback, Pixel-Clipping, Kleinstdetails und Screenshot-Reflow not-run.</li></ul></section><footer>Affinity MCP: {escape(affinity_observation['status'])}.</footer></main></body></html>
"""
    output_fd = _safe_output_dir(output)
    copied = _write_assets(output_fd, assets)
    css_bytes, document_bytes = css.encode("utf-8"), document.encode("utf-8")
    try:
        _write_new(output_fd, "style.css", css_bytes); _write_new(output_fd, "index.html", document_bytes)
    except Exception:
        os.close(output_fd); raise
    files = {"index.html": sha256_bytes(document_bytes), "style.css": sha256_bytes(css_bytes), **copied}
    manifest = {
        "schema_version": "brandkit-visual-sheet-manifest.v1",
        "brief_sha256": sha256_bytes(canonical_json(brief)),
        "candidate_set_sha256": candidate_set_sha256,
        "affinity_observation_sha256": sha256_bytes(canonical_json(affinity_observation)),
        "files": [{"path": path, "sha256": digest} for path, digest in sorted(files.items())],
        "viewports": list(VIEWPORTS),
        "offline": True,
        "interactive_controls": False,
        "preselection": False,
        "normative": False,
        "human_review_status": "required",
    }
    validate_schema(manifest, "sheet")
    _write_new(output_fd, "manifest.json", canonical_json(manifest)); os.close(output_fd)
    validate_sheet_manifest(manifest, output)
    return manifest


def _verified_render_inputs(project_root: Path, run_id: str, now: int, *, sidecar_root: Path | None, affinity_auth: dict[str, Any] | None) -> tuple[Path, dict[str, Any], dict[str, Any], bytes, dict[str, Any], dict[str, Any] | None]:
    """The bundle a renderer may show: verified under the ledger lock, never spent.

    Shared by the sheet and the playground, so the two artifacts are rendered
    from the same validated bundle by the same steps; see render_sheet for why
    each step is there.
    """
    workspace = visual_workspace(project_root, run_id)
    descriptor = None if affinity_auth is None else _affinity_descriptor(affinity_auth)
    if descriptor is not None:
        affinity_auth = {"owner_key_path": descriptor[0], "replay_ledger_path": descriptor[1], "receipts": affinity_auth["receipts"]}
    lock = _affinity_ledger_lock(project_root, descriptor)
    try:
        _validate_bundle_locked(project_root, run_id, now, sidecar_root=sidecar_root, affinity_auth=affinity_auth, descriptor=descriptor, commit=False)
    finally:
        if lock is not None: os.close(lock)
    logo_authentication = None
    if descriptor is not None:
        logo_authentication = {
            "receipts": {candidate_id: load_json_bytes(_verify_bound_file(workspace, binding, f"Affinity receipt {candidate_id}"), f"Affinity receipt {candidate_id}")
                         for candidate_id, binding in affinity_auth["receipts"].items()},
            "project_root": exact_project_root(project_root), "run_id": run_id,
            "key": _external_0600(descriptor[0], "Affinity owner key"), "now": now,
        }
    brief = load_json_bytes(secure_read(workspace, "visual-brief.json", "visual brief")[0], "visual brief")
    candidate_bytes = secure_read(workspace, "candidate-set.json", "candidate set")[0]; candidates = load_json_bytes(candidate_bytes, "candidate set")
    observation = load_json_bytes(secure_read(workspace, "affinity-observation.json", "Affinity observation")[0], "Affinity observation")
    return workspace, brief, candidates, candidate_bytes, observation, logo_authentication


def render_sheet(project_root: Path, run_id: str, now: int, *, sidecar_root: Path | None = None, affinity_auth: dict[str, Any] | None = None) -> dict[str, Any]:
    """Render the unselected candidate sheet for a bundle this module accepts.

    The two keyword arguments are the trust inputs of :func:`validate_bundle`
    and are forwarded to it unchanged.  They are *added*, keyword-only and
    defaulting to ``None``, so every existing caller - the CLI subcommand and
    the r25 tests included - keeps the exact behaviour it had: ``None`` is what
    the two parameters were pinned to before, so the previous call site is the
    new default rather than a special case.  Nothing here relaxes a promise:
    the descriptor is validated by ``validate_bundle`` under its external lock,
    and a sheet is still only rendered for a bundle that validator accepted.

    Why it was wrong to pin them: the sheet is the *first* step of the owner
    flow, so a bundle whose sources live in a relocated sidecar had no reachable
    first step at all - not merely an unrenderable one.  ``sidecar_root`` closes
    that, and it is measured both ways (a sidecar that carries the narrative
    renders, one that does not is refused here rather than defaulted past).

    ``affinity_auth`` is forwarded for the same reason, and since r1e it gets
    an Affinity-authored bundle all the way to a rendered sheet rather than one
    layer further than before.  Two things had to be true for that, and both
    are visible here:

    * ``_render_sheet`` re-validates the logo report, and it used to do so
      *without* receipts, which refused every Affinity-authored candidate
      unconditionally - on the first attempt, with unspent receipts.  It is now
      handed the very receipts ``validate_bundle`` just accepted, in the form
      ``validate_bundle`` itself uses.
    * The receipts are single-use and the accepted flow reaches them twice:
      here, to render the sheet the owner looks at, and again in the operation
      that binds the selection the owner confirms.  Rendering a read-only,
      non-normative artifact is not an operation, so this call **verifies**
      the receipts under the ledger lock without spending them
      (``commit=False``) and leaves the single consumption to the confirming
      call.  A receipt that is already in the ledger is still refused here, and
      a receipt is still written into the ledger exactly once - so the promise
      is unchanged and the flow is, for the first time, executable end to end.

    The descriptor is resolved exactly once, at the top, and the resolved pair
    is what the lock, the validation and the receipt read below all see: a
    ``PathLike`` whose ``__fspath__`` answers differently on a second call
    cannot make the render and the validator disagree about which ledger and
    which key are in play.

    Since the render-sheet fix this entry point no longer refuses a bundle that
    ``validate_bundle`` accepts.  It used to require exactly one typography,
    code-font, logo and lockup candidate and one report shared per font and per
    logo pair ("sheet requires all five independently routed decision domains");
    that was the renderer's limit, not a rule of the bundle, and it made the
    sheet for a bundle without logos - the case the playbook describes -
    unreachable.  Each candidate's own report is now read and re-validated by
    ``_render_sheet``.
    """
    workspace, brief, candidates, candidate_bytes, observation, logo_authentication = _verified_render_inputs(
        project_root, run_id, now, sidecar_root=sidecar_root, affinity_auth=affinity_auth)
    return _render_sheet(workspace / "sheet", workspace, brief, candidates, observation, sha256_bytes(candidate_bytes), logo_authentication=logo_authentication)


# --- the playground -------------------------------------------------------------
#
# The owner wants to combine colour, typography and code freely, for any number
# of candidates.  The sheet cannot do that and must not: it is the static,
# byte-bound review artifact of the confirmation, with no script at all.  The
# playground is a SECOND, separate artifact rendered from the same validated
# bundle into ``playground/`` beside ``sheet/``.  It is non-normative, it keeps
# no state beyond the open page, and it is never a review artifact.  The
# adapter no longer keeps a blocklist for it: ``prepare-visual-confirmation``
# binds the review positively - it has to be ``<workspace>/sheet/index.html``
# whose ``sheet/manifest.json`` passes the sheet validator for exactly the bound
# bundle, brief and Affinity observation - and every playground path, copy and
# look-alike fails that statement instead of being named by a list.
#
# Scripts are allowed here, so the hardening is its own and is stated as equality
# rather than as a scan wherever it can be:
#
# * ``index.html`` and ``app.js`` are module constants and have exactly one
#   producer.  The validator compares them byte for byte.  Before that it scans
#   both for the sink, evaluation, storage, network and URL tokens the brief
#   names, so a deviation of that kind is refused by name rather than only as
#   "differs"; the scan is the explanation, the equality is the gate.
# * ``data.js`` is data only: a fixed prefix that deep-freezes one value, a
#   JSON document, a fixed suffix.  The middle is parsed as strict JSON, checked
#   against its closed schema and re-encoded; only the canonical encoding - pure
#   ASCII with ``<``, ``>`` and ``&`` escaped - is accepted, so no byte of it
#   can be code.  Owner-supplied text (family names from the name table,
#   candidate labels, recommendation rationales) reaches the page only through
#   ``textContent``.
# * ``style.css`` is regenerated from the validated data and compared byte for
#   byte, after the sheet's offline CSS scan has read it.
# * The policy is PLAYGROUND_CSP, delivered by the one meta element; the page
#   has no inline script, style or handler, no form, no link, no frame and no
#   remote URL, and every file it loads is a manifest member.
PLAYGROUND_MARKER = "<!--brandkit-visual-playground-->"
PLAYGROUND_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self'; "
                  "connect-src 'none'; form-action 'none'; base-uri 'none'; require-trusted-types-for 'script'")
PLAYGROUND_ROOT_MEMBERS = ("app.js", "data.js", "index.html", "style.css")
PLAYGROUND_ASSET_MEMBER = re.compile(r"assets/([0-9a-f]{64})\.(woff2|svg)")
PLAYGROUND_DATA_SCHEMA = "brandkit-visual-playground-data.v1.schema.json"

PLAYGROUND_INDEX_HTML = f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="{PLAYGROUND_CSP}">{PLAYGROUND_MARKER}<title>BrandKit Visual Playground</title><link rel="stylesheet" href="style.css"><script src="data.js" defer></script><script src="app.js" defer></script></head>
<body><main><h1>Visueller Playground</h1><p class="notice"><strong>Nicht normativ.</strong> Dieser Playground kombiniert validierte Kandidaten nur zur Ansicht. Er trifft keine Auswahl, speichert nichts und ist kein Prüfartefakt der Bestätigung. Entschieden wird ausschließlich über Candidate-IDs im Interviewdialog, auf Grundlage des statischen Entscheidungsbogens.</p><p id="status">Der Playground braucht die lokalen Skripte app.js und data.js. Ohne sie bleibt diese Seite leer; der statische Entscheidungsbogen ist davon unberührt.</p><div id="controls"></div><p id="combination"></p><div id="preview"></div><div id="details"></div><div id="notes"></div></main></body></html>
"""

#: The numeral labels and the digit run, handed to the script from the ONE place
#: that defines them, so the twins cannot name the same feature differently.
#: ``ensure_ascii=False`` on purpose: the script member may carry no backslash at
#: all, and an escaped umlaut would be one.
_NUMERAL_LABELS_JS = json.dumps(NUMERAL_LABELS, ensure_ascii=False, sort_keys=True)
_NUMERAL_SAMPLE_JS = json.dumps(NUMERAL_SAMPLE, ensure_ascii=False)
#: classification -> the weight the disclosed x-height of such a face is stated
#: for, handed to the script from the ONE place that pairs a role with a weight.
_FACE_WEIGHT_JS = json.dumps({klass: PLAYGROUND_ROLE_WEIGHTS[role] for klass, role in sorted(FACE_ROLE.items())},
                             ensure_ascii=False, sort_keys=True)

PLAYGROUND_APP_JS = f"""/*{PLAYGROUND_MARKER}*/
"use strict";
(function () {{
  const LABELS = Object.freeze({{"color": "Farbe", "typography": "Typografie", "code-font": "Code-Font", "logo": "Logo", "lockup": "Lockup"}});
  const TAGS = new Set(["div", "p", "h2", "h3", "label", "select", "option", "table", "thead", "tbody", "tr", "th", "td", "ul", "li", "pre", "strong", "span", "img"]);
  const ATTRIBUTES = new Set(["class", "id", "for", "value", "src", "alt"]);
  const DIGEST = /^[0-9a-f]{{64}}$/;
  const REFERENCE = "ref";
  const status = document.getElementById("status");

  function make(tag, attributes, text) {{
    if (!TAGS.has(tag)) {{
      throw new TypeError("element is not allowed: " + tag);
    }}
    const node = document.createElement(tag);
    for (const name of Object.keys(attributes || {{}})) {{
      if (!ATTRIBUTES.has(name)) {{
        throw new TypeError("attribute is not allowed: " + name);
      }}
      node.setAttribute(name, String(attributes[name]));
    }}
    if (text !== undefined) {{
      node.textContent = String(text);
    }}
    return node;
  }}

  function add(parent, children) {{
    for (const child of children) {{
      parent.appendChild(child);
    }}
    return parent;
  }}

  function clear(node) {{
    while (node.firstChild) {{
      node.removeChild(node.firstChild);
    }}
  }}

  function table(head, rows) {{
    const headRow = make("tr");
    for (const cell of head) {{
      headRow.appendChild(make("th", {{}}, cell));
    }}
    const body = make("tbody");
    for (const row of rows) {{
      const line = make("tr");
      row.forEach(function (cell, position) {{
        line.appendChild(make(position === 0 ? "th" : "td", {{}}, cell));
      }});
      body.appendChild(line);
    }}
    return add(make("div", {{"class": "pg-table"}}), [add(make("table"), [add(make("thead"), [headRow]), body])]);
  }}

  if (typeof BRANDKIT_PLAYGROUND_DATA === "undefined" || BRANDKIT_PLAYGROUND_DATA === null || BRANDKIT_PLAYGROUND_DATA.schema_version !== "brandkit-visual-playground-data.v1") {{
    status.textContent = "Die lokale Datendatei fehlt oder gehört nicht zu diesem Playground.";
    return;
  }}
  const source = BRANDKIT_PLAYGROUND_DATA;
  const content = source.content;
  const axes = new Map();
  for (const axis of source.axes) {{
    if (!Object.prototype.hasOwnProperty.call(LABELS, axis.domain) || axes.has(axis.domain)) {{
      throw new TypeError("axis domain is not allowed");
    }}
    axes.set(axis.domain, axis.candidates);
  }}
  const state = new Map();
  for (const domain of axes.keys()) {{
    state.set(domain, REFERENCE);
  }}

  function current(domain) {{
    const choice = state.get(domain);
    return choice === undefined || choice === REFERENCE ? null : axes.get(domain)[choice];
  }}

  function face(font) {{
    if (!Number.isInteger(font.face) || font.face < 0 || font.face >= source.faces.length) {{
      throw new RangeError("font face is out of range");
    }}
    return "pg-face-" + font.face;
  }}

  function image(digest, text) {{
    if (typeof digest !== "string" || !DIGEST.test(digest)) {{
      throw new TypeError("asset digest is malformed");
    }}
    return make("img", {{"src": "assets/" + digest + ".svg", "alt": text}});
  }}

  function controls() {{
    const root = document.getElementById("controls");
    clear(root);
    for (const [domain, candidates] of axes) {{
      const id = "axis-" + domain;
      const select = make("select", {{"id": id}});
      select.appendChild(make("option", {{"value": REFERENCE}}, "Referenz, kein Kandidat"));
      candidates.forEach(function (candidate, position) {{
        select.appendChild(make("option", {{"value": String(position)}}, candidate.id + " · " + candidate.label));
      }});
      select.addEventListener("change", function () {{
        if (select.value === REFERENCE) {{
          state.set(domain, REFERENCE);
        }} else {{
          const position = Number(select.value);
          if (!Number.isInteger(position) || position < 0 || position >= candidates.length) {{
            throw new RangeError("candidate is out of range");
          }}
          state.set(domain, position);
        }}
        render();
      }});
      root.appendChild(add(make("div", {{"class": "pg-control"}}), [make("label", {{"for": id}}, LABELS[domain]), select]));
    }}
  }}

  function preview() {{
    const color = current("color");
    const typography = current("typography");
    const code = current("code-font");
    const sans = typography === null ? "pg-face-ref-sans" : face(typography.sans);
    const serif = typography === null ? "pg-face-ref-serif" : face(typography.serif);
    const mono = code === null ? "pg-face-ref-mono" : face(code.mono);
    const root = document.getElementById("preview");
    root.setAttribute("class", "pg-preview " + (color === null ? "pg-pal-ref" : "pg-pal-" + state.get("color")));
    clear(root);
    for (const domain of ["logo", "lockup"]) {{
      const candidate = current(domain);
      if (candidate !== null) {{
        root.appendChild(add(make("div", {{"class": "pg-logos"}}), candidate.items.map(function (item) {{
          return image(item.asset_sha256, LABELS[domain] + " " + candidate.id + " " + item.variant);
        }})));
      }}
    }}
    root.appendChild(add(make("div", {{"class": sans}}), [make("p", {{"class": "pg-heading"}}, content.heading), make("p", {{"class": "pg-subheading"}}, content.subheading)]));
    const body = make("div", {{"class": serif}});
    for (const paragraph of content.paragraphs) {{
      body.appendChild(make("p", {{"class": "pg-body"}}, paragraph));
    }}
    body.appendChild(add(make("ul"), content.list.map(function (item) {{
      return make("li", {{}}, item);
    }})));
    body.appendChild(make("p", {{"class": "pg-quote"}}, content.quote));
    root.appendChild(body);
    root.appendChild(add(make("div", {{"class": "pg-actions " + sans}}), [
      make("p", {{"class": "pg-action pg-primary"}}, content.action_primary),
      make("p", {{"class": "pg-action pg-secondary"}}, content.action_secondary),
      make("p", {{"class": "pg-action pg-focus"}}, content.action_focus)
    ]));
    root.appendChild(add(make("div", {{"class": "pg-kpi " + sans}}), [make("p", {{"class": "pg-kpi-value"}}, content.kpi_value), make("p", {{"class": "pg-kpi-label"}}, content.kpi_label)]));
    root.appendChild(add(make("p", {{"class": "pg-state pg-warning " + serif}}), [make("strong", {{}}, content.notice_label), make("span", {{}}, " " + content.notice)]));
    root.appendChild(add(make("p", {{"class": "pg-state pg-danger " + serif}}), [make("strong", {{}}, content.error_label), make("span", {{}}, " " + content.error)]));
    root.appendChild(add(make("div", {{"class": serif}}), [table(content.table_head, content.table_rows)]));
    const blocks = make("div", {{"class": mono}});
    if (code !== null && code.mono.ligatures.length > 0) {{
      add(blocks, [
        make("p", {{"class": "pg-note"}}, "Ligaturen aktiv, GSUB " + code.mono.ligatures.join(", ")),
        make("pre", {{"class": "pg-code pg-liga"}}, content.code),
        make("p", {{"class": "pg-note"}}, "Ligaturen abgeschaltet"),
        make("pre", {{"class": "pg-code pg-noliga"}}, content.code)
      ]);
    }} else {{
      blocks.appendChild(make("pre", {{"class": "pg-code"}}, content.code));
    }}
    root.appendChild(blocks);
  }}

  function missing(font) {{
    return font.missing.length === 0 ? "alle Zeichen im Font" : "nicht im Font: " + font.missing.join(", ");
  }}

  const NUMERALS = Object.freeze({_NUMERAL_LABELS_JS});

  function numeralRows(font, faceClass) {{
    const box = make("div", {{"class": "pg-numerals-set " + faceClass}});
    for (const tag of font.numerals) {{
      const row = make("p", {{"class": "pg-numerals pg-num-" + tag}}, {_NUMERAL_SAMPLE_JS});
      row.appendChild(make("span", {{"class": "pg-numerals-name"}}, NUMERALS[tag] + " (" + tag + ")"));
      box.appendChild(row);
    }}
    return box;
  }}

  const FACE_WEIGHT = Object.freeze({_FACE_WEIGHT_JS});

  function faceNote(font, what) {{
    const f = source.faces[font.face];
    const quelle = f.x_height_source === "os2-sxheight" ? "OS/2.sxHeight" : "gemessen an der Glyphe x";
    const weight = FACE_WEIGHT[f.classification];
    const stated = f.x_heights[String(weight)];
    const ratio = (stated / f.units_per_em).toFixed(3);
    const offered = f.weight_axis ? "variable Achse wght " + f.weights[0] + " bis " + f.weights[1]
      : (f.weights[0] === f.weights[1] ? "starr " + f.weights[0]
      : "statisch " + f.weights[0] + " bis " + f.weights[1]);
    const reserve = f.weights_set.length > 1 ? "" : " - kein Zulegen in Überschriften möglich";
    return what + " " + font.family + ": x-Höhe " + stated + " von " + f.units_per_em +
      " Einheiten (" + ratio + " em, " + quelle + "), gelesen an der gezeichneten Instanz bei Gewicht " +
      weight + ". Gewichte: " + offered + "; gesetzt werden " + f.weights_set.join(", ") + reserve +
      ". Auf gleiche x-Höhe normalisiert, Grad " +
      f.size_rem + " rem, Gewichtsbereich " + f.weights[0] + " bis " + f.weights[1] +
      ". Der Grad folgt aus der Normalisierung und ist keine Empfehlung.";
  }}

  function details() {{
    const names = [];
    for (const domain of axes.keys()) {{
      const candidate = current(domain);
      names.push(LABELS[domain] + " " + (candidate === null ? "Referenz" : candidate.id));
    }}
    document.getElementById("combination").textContent = "Angezeigt, nicht ausgewählt: " + names.join(" · ");
    const root = document.getElementById("details");
    clear(root);
    const typography = current("typography");
    if (typography !== null) {{
      root.appendChild(make("p", {{}}, "Typografie " + typography.id + ": Überschriften " + typography.sans.family + " (" + missing(typography.sans) + "), Fließtext " + typography.serif.family + " (" + missing(typography.serif) + ")"));
      root.appendChild(make("p", {{}}, faceNote(typography.sans, "Überschriften")));
      root.appendChild(make("p", {{}}, faceNote(typography.serif, "Fließtext")));
      if (typography.serif.numerals.length > 0) {{
        root.appendChild(make("p", {{}}, "Ziffernvarianten im Fließtext, " + typography.serif.numerals.join(", ") + ", je Zeile dieselben zehn Ziffern in derselben Größe"));
        root.appendChild(numeralRows(typography.serif, face(typography.serif)));
      }} else {{
        root.appendChild(make("p", {{}}, "Der Fließtext-Font trägt keine der Ziffernfeatures " + Object.keys(NUMERALS).join(", ") + ". Das ist eine Aussage der Fonttabellen, kein fehlender Befund."));
      }}
    }}
    const code = current("code-font");
    if (code !== null) {{
      root.appendChild(make("p", {{}}, "Code-Font " + code.id + ": " + code.mono.family + " (" + missing(code.mono) + ")"));
      root.appendChild(make("p", {{}}, faceNote(code.mono, "Code")));
      if (code.mono.numerals.length > 0) {{
        root.appendChild(make("p", {{}}, "Ziffernvarianten im Code, " + code.mono.numerals.join(", ")));
        root.appendChild(numeralRows(code.mono, face(code.mono)));
      }} else {{
        root.appendChild(make("p", {{}}, "Der Code-Font trägt keine der Ziffernfeatures " + Object.keys(NUMERALS).join(", ") + "."));
      }}
    }}
    const color = current("color");
    if (color !== null) {{
      root.appendChild(make("h3", {{}}, "WCAG-Ergebnisse aus dem validierten Farbreport " + color.id));
      root.appendChild(table(["Vordergrund", "Hintergrund", "Verwendung", "Minimum", "Ratio", "Status"], color.tests.map(function (test) {{
        return [test.foreground_role, test.background_role, test.usage, String(test.minimum_ratio) + ":1", test.ratio_display, test.passed ? "bestanden" : "nicht bestanden"];
      }})));
    }}
  }}

  function notes() {{
    const root = document.getElementById("notes");
    clear(root);
    const items = [];
    for (const item of source.recommendations) {{
      items.push("Unverbindliche Empfehlung, nicht vorausgewählt: " + item.candidate_id + " – " + item.rationale);
    }}
    for (const item of source.blocked) {{
      items.push("Blockiert und nicht kombinierbar: " + item.candidate_id + " (" + LABELS[item.domain] + ")");
    }}
    for (const domain of source.open_without_candidates) {{
      items.push("Offen ohne kombinierbaren Kandidaten: " + LABELS[domain]);
    }}
    if (items.length > 0) {{
      root.appendChild(make("h2", {{}}, "Hinweise aus dem Bundle"));
      root.appendChild(add(make("ul"), items.map(function (text) {{
        return make("li", {{}}, text);
      }})));
    }}
  }}

  function render() {{
    preview();
    details();
  }}

  controls();
  notes();
  render();
  status.textContent = "Bereit. Die Umschalter ändern nur diese Ansicht; nichts wird gespeichert.";
}})();
"""

_PLAYGROUND_DATA_PREFIX = (f"/*{PLAYGROUND_MARKER}*/\n\"use strict\";\n"
                           "const BRANDKIT_PLAYGROUND_DATA = (function freeze(value) {\n"
                           "  if (value !== null && typeof value === \"object\") {\n"
                           "    Object.values(value).forEach(freeze);\n"
                           "    Object.freeze(value);\n"
                           "  }\n"
                           "  return value;\n"
                           "})(").encode("ascii")
_PLAYGROUND_DATA_SUFFIX = b");\n"

#: The sheet's control class, on bytes, for the playground's own members.  The
#: C0 half is the original statement; the separators and bidi controls are the
#: same addition ``_SHEET_CONTROL`` carries, spelled in UTF-8 because this scan
#: runs before any decode.  ``_playground_json`` writes ASCII, so today no
#: member can carry one of the two-byte and three-byte forms - which is exactly
#: why the statement is made: it holds if that encoding ever changes, instead of
#: resting on it.
_PLAYGROUND_CONTROL = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
                                 rb"|\xc2\x85|\xd8\x9c|\xe2\x80[\x8e\x8f\xa8\xa9\xaa-\xae]|\xe2\x81[\xa6-\xa9]")
#: Refused in both code members, each by name.  Every pattern over-approximates
#: its category - a spelling it cannot see is still refused by the byte equality
#: that follows - so a pattern only ever adds a named refusal.
_PLAYGROUND_CODE_REFUSALS = (
    (re.compile(rb"innerHTML|outerHTML|insertAdjacentHTML|document\s*\.\s*write|srcdoc|createContextualFragment|DOMParser|parseFromString|setHTMLUnsafe|parseHTMLUnsafe", re.IGNORECASE),
     "an HTML parsing sink"),
    (re.compile(rb"\beval\b|setTimeout|setInterval|setImmediate|execScript|importScripts|\bimport\s*\(|Worklet", re.IGNORECASE), "dynamic code evaluation"),
    (re.compile(rb"\bFunction\b"), "dynamic code evaluation"),
    (re.compile(rb"localStorage|sessionStorage|indexedDB|cookie|caches|CacheStorage|serviceWorker|navigator\s*\.\s*storage|FileSystem|showSaveFilePicker|showDirectoryPicker", re.IGNORECASE),
     "a storage API"),
    (re.compile(rb"fetch|XMLHttpRequest|WebSocket|WebTransport|EventSource|sendBeacon|RTCPeerConnection|postMessage|BroadcastChannel|\bopen\s*\(|location|history|\bdownload\b|createObjectURL|\bBlob\b|FileReader|\bsubmit\b|requestSubmit|navigator|\bWorker\b", re.IGNORECASE),
     "a network, navigation or export API"),
    (re.compile(rb"https?:|javascript:|data:|blob:|file:|ftp:|\bwss?:|//", re.IGNORECASE), "a remote or unsafe URL"),
)
_PLAYGROUND_HTML_REFUSALS = (
    (re.compile(rb"<script(?![^>]*\ssrc=\"[a-z]+\.js\")[^>]*>|<script[^>]*>[^<]", re.IGNORECASE), "an inline script"),
    (re.compile(rb"\son[a-z]+\s*=", re.IGNORECASE), "an inline event handler"),
    (re.compile(rb"\sstyle\s*=|<style", re.IGNORECASE), "an inline style"),
    (re.compile(rb"<(?:iframe|frame|object|embed|form|base|portal|a|button|input|textarea)[\s>/]", re.IGNORECASE), "a frame, embed, form, base or navigation element"),
)
_PLAYGROUND_SCRIPT_REFUSALS = (
    # JavaScript reads a Unicode escape in an identifier as the identifier it
    # spells (e\\u0076al is eval), which no token scan above could see.  The
    # script this module writes carries no backslash at all.
    (re.compile(rb"\\"), "an escape sequence"),
)


def _playground_json(data: dict[str, Any]) -> bytes:
    """The one encoding of the playground data: sorted, ASCII, markup characters escaped."""
    text = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").encode("ascii")


def _playground_css(data: dict[str, Any]) -> str:
    """The playground stylesheet, as a pure function of its validated data.

    Its table wrapper carries the same overflow rule as the sheet's, and for the
    same measured reason (see :func:`_sheet_css`): ``hidden`` is what the engine
    computes here, so ``hidden`` is what is written.
    """
    pg_facts = [{"x_heights": {int(key): int(value) for key, value in face["x_heights"].items()},
                 "units_per_em": int(face["units_per_em"]),
                 "ascent": int(face["ascent"]), "descent": int(face["descent"])}
                for face in data["faces"]]
    pg_targets = _baseline_targets([(facts, face["classification"]) for facts, face in zip(pg_facts, data["faces"])],
                                   PLAYGROUND_ROLE_SELECTORS, PLAYGROUND_ROLE_WEIGHTS,
                                   PLAYGROUND_ROLE_CLASSES, "playground")
    faces = "\n".join(
        f'@font-face{{font-family:"PlaygroundFace{index}";src:url("assets/{face["asset_sha256"]}.woff2") format("woff2");font-display:block;'
        f'font-weight:{_weight_descriptor((face["weights"][0], face["weights"][1]))}}} '
        f'.pg-face-{index},.pg-face-{index} pre{{font-family:"PlaygroundFace{index}",{GENERIC_FAMILIES[face["classification"]]};font-optical-sizing:none}} '
        + _face_weight_rules("pg-face", index, (face["weights"][0], face["weights"][1]), PLAYGROUND_WEIGHT_SELECTORS) + " "
        + _face_metric_rules("pg-face", index, pg_facts[index], PLAYGROUND_ROLE_SELECTORS,
                             PLAYGROUND_ROLE_WEIGHTS, pg_targets)
        for index, face in enumerate(data["faces"]))
    colors = next((axis["candidates"] for axis in data["axes"] if axis["domain"] == "color"), [])
    palettes = "\n".join(f".pg-pal-{index}{{" + ";".join(f"--{role}:{value}" for role, value in sorted(candidate["roles"].items())) + "}"
                         for index, candidate in enumerate(colors))
    reference = ";".join(f"--{role}:{value}" for role, value in sorted(data["reference_palette"].items()))
    numerals = " ".join(f'.pg-num-{tag}{{font-feature-settings:"{tag}" 1}}' for tag in sorted(NUMERAL_FEATURES))
    return f"""/*{PLAYGROUND_MARKER}*/
{faces}
{palettes}
.pg-pal-ref{{{reference}}}
:root{{color-scheme:light}} *{{box-sizing:border-box}} body{{margin:0;background:#fff;color:#171717;font:16px/1.5 sans-serif}} main{{max-width:72rem;margin:auto;padding:1rem;overflow-wrap:anywhere;hyphens:auto}} h1,h2,h3{{line-height:1.15}} .notice{{border:.2rem solid #111;padding:1rem}} #status,#combination{{font-weight:700;overflow-wrap:anywhere}}
#controls{{display:grid;grid-template-columns:1fr;gap:.75rem;margin:1rem 0}} .pg-control{{display:flex;flex-direction:column;gap:.25rem;min-width:0}} select{{font:inherit;max-width:100%;padding:.35rem}} select:focus-visible{{outline:3px solid #1c1c1c;outline-offset:2px}}
.pg-preview{{background:var(--canvas);color:var(--text);border:1px solid var(--border);padding:1rem;margin:1rem 0;overflow-wrap:anywhere}} .pg-logos{{display:flex;flex-wrap:wrap;gap:1rem;align-items:center;margin-bottom:1rem}} .pg-logos img{{display:block;height:3rem;max-width:100%}}
.pg-heading{{font-size:1.6rem;font-weight:700;line-height:1.2;margin:0 0 .25rem}} .pg-subheading{{font-size:1.2rem;font-weight:600;margin:0 0 .75rem}} .pg-body{{font-size:1.05rem;line-height:1.6}} .pg-quote{{border-left:.25rem solid var(--border);padding-left:.75rem;font-style:italic}}
.pg-actions{{display:flex;flex-wrap:wrap;gap:.75rem;margin:.75rem 0}} .pg-action{{margin:0;padding:.5rem 1rem;border-radius:.375rem;font-weight:700}} .pg-primary{{background:var(--primary);color:var(--on-primary)}} .pg-secondary{{background:var(--secondary);color:var(--on-secondary)}} .pg-focus{{background:var(--surface);color:var(--text);outline:3px solid var(--focus);outline-offset:3px}}
.pg-kpi{{background:var(--accent);color:var(--on-accent);padding:.75rem 1rem;margin:.75rem 0}} .pg-kpi-value{{font-size:2.2rem;font-weight:700;margin:0}} .pg-kpi-label{{margin:0}} .pg-state{{padding:.5rem .75rem;margin:.5rem 0}} .pg-warning{{background:var(--warning);color:var(--on-warning)}} .pg-danger{{background:var(--danger);color:var(--on-danger)}}
.pg-table{{overflow-x:auto;overflow-y:hidden;max-width:100%;margin:.75rem 0}} table{{border-collapse:collapse;min-width:100%}} th,td{{border:1px solid var(--border,#d4d4d4);padding:.3rem .5rem;text-align:left;vertical-align:top;overflow-wrap:normal;hyphens:manual}} td{{text-align:right;font-variant-numeric:tabular-nums}} .pg-preview th{{background:var(--surface-strong)}} .pg-preview td{{background:var(--surface)}}
.pg-face-ref-sans{{font-family:sans-serif}} .pg-face-ref-serif,.pg-face-ref-serif pre{{font-family:serif}} .pg-face-ref-mono,.pg-face-ref-mono pre{{font-family:monospace}}
.pg-code{{white-space:pre-wrap;overflow-wrap:break-word;overflow-x:auto;max-width:100%;font-size:.95rem;line-height:1.5;background:var(--surface);color:var(--text);border:1px solid var(--border);padding:.75rem;margin:.25rem 0 .75rem;tab-size:4}} .pg-liga{{font-variant-ligatures:normal;font-feature-settings:"liga" 1,"calt" 1}} .pg-noliga{{font-variant-ligatures:none;font-feature-settings:"liga" 0,"calt" 0}} .pg-note{{font-weight:700;margin:.5rem 0 0}}
{numerals}
.pg-numerals-set{{margin:.5rem 0 .75rem}} .pg-numerals{{margin:.15rem 0;white-space:pre-wrap;font-variant-numeric:normal;letter-spacing:.01em}} .pg-numerals-name{{font-family:sans-serif;font-size:.875rem;font-weight:400;font-variant-numeric:normal;font-feature-settings:normal;letter-spacing:normal;margin-left:1rem;color:var(--text-muted)}}
@media(min-width:768px){{main{{padding:2rem}}#controls{{grid-template-columns:repeat(3,minmax(0,1fr))}}}}
"""


def _validate_playground_data(data: Any, manifest: dict[str, Any]) -> set[str]:
    """The closed playground data, and the asset paths it references."""
    _validate_schema_file(data, PLAYGROUND_DATA_SCHEMA, "playground data")
    if data["candidate_set_sha256"] != manifest["candidate_set_sha256"]:
        raise VisualDecisionError("playground data does not bind the manifest's candidate set")
    domains = [axis["domain"] for axis in data["axes"]]
    if domains != [kind for kind in SHEET_DOMAINS if kind in domains]:
        raise VisualDecisionError("playground axes are duplicated or out of domain order")
    if set(data["open_without_candidates"]) & set(domains):
        raise VisualDecisionError("playground names a domain both open without candidates and combinable")
    identifiers = {axis["domain"]: [candidate["id"] for candidate in axis["candidates"]] for axis in data["axes"]}
    if any(len(values) != len(set(values)) for values in identifiers.values()):
        raise VisualDecisionError("playground candidate ids are not unique within their domain")
    if any(item["candidate_id"] in identifiers.get(item["domain"], []) for item in data["blocked"]):
        raise VisualDecisionError("playground offers a blocked candidate for combination")
    if any(item["candidate_id"] not in identifiers.get(item["kind"], []) for item in data["recommendations"]):
        raise VisualDecisionError("playground recommendation cites a candidate it does not offer")
    faces = [(face["asset_sha256"], face["classification"]) for face in data["faces"]]
    if len(faces) != len(set(faces)):
        raise VisualDecisionError("playground font faces are duplicated")
    # The face facts are held to the same bounds the sheet holds a font to, and
    # the disclosed size is RECOMPUTED rather than believed: it is the one field
    # here that is derived, so it is the one field a reader could otherwise be
    # told a different number from the one the stylesheet sets.
    for index, face in enumerate(data["faces"]):
        units_per_em = int(face["units_per_em"])
        heights = {int(key): int(value) for key, value in face["x_heights"].items()}
        # The twin has to state its x-height for exactly the instances the
        # artifacts draw - no fewer, so no role is left without one, and no more,
        # so nothing here is data nobody reads.
        if sorted(heights) != sorted(ASKED_WEIGHTS):
            raise VisualDecisionError(f"playground face does not state its x-height for the drawn weights: {index}")
        for value in (int(face["x_height"]), *heights.values()):
            if not 0 < value <= units_per_em:
                raise VisualDecisionError(f"playground face states no usable x-height: {index}")
            # The same floor the sheet holds a font to.  The outline cross-check
            # cannot be repeated here - this validator reads the data file, not
            # the font bytes - so the floor is the part that carries over, and
            # the stylesheet it generates is held byte for byte besides.
            if value * X_HEIGHT_FLOOR.denominator < units_per_em * X_HEIGHT_FLOOR.numerator:
                raise VisualDecisionError(f"playground face states an x-height under {X_HEIGHT_FLOOR} of its em: {index}")
        low, high = int(face["weights"][0]), int(face["weights"][1])
        if not 1 <= low <= high <= 1000:
            raise VisualDecisionError(f"playground face states no usable weight range: {index}")
        # The weights the twin says it SETS are recomputed from the range rather
        # than believed, exactly as the disclosed size is: they are derived, so
        # a hand-edited data file cannot name a weight the stylesheet does not
        # set.  Same for the vertical metric the common baseline rests on - a
        # face that states an unusable ascent is refused here, not laid out.
        if tuple(int(value) for value in face["weights_set"]) != _weights_set((low, high), PLAYGROUND_WEIGHT_SELECTORS):
            raise VisualDecisionError(f"playground face states weights its range does not give: {index}")
        ascent, descent = int(face["ascent"]), int(face["descent"])
        if ascent <= 0 or ascent - descent <= 0:
            raise VisualDecisionError(f"playground face states no usable vertical metrics: {index}")
        role = FACE_ROLE[face["classification"]]
        ratio = Fraction(heights[PLAYGROUND_ROLE_WEIGHTS[role]], units_per_em)
        if face["size_rem"] != _rem(_normalised_size(ROLE_METRICS[role][0], ratio)):
            raise VisualDecisionError(f"playground face discloses a size its x-height does not give: {index}")
    used: set[int] = set()
    references = {f"assets/{digest}.woff2" for digest, _classification in faces}
    for axis in data["axes"]:
        for candidate in axis["candidates"]:
            slots = {"typography": ("sans", "serif"), "code-font": ("mono",)}.get(axis["domain"], ())
            for slot in slots:
                index = candidate[slot]["face"]
                if index >= len(faces) or faces[index][1] != slot:
                    raise VisualDecisionError(f"playground font face does not fit its slot: {candidate['id']}")
                used.add(index)
            references |= {f"assets/{item['asset_sha256']}.svg" for item in candidate.get("items", [])}
    if used != set(range(len(faces))):
        raise VisualDecisionError("playground carries a font face no candidate uses")
    return references


def validate_playground_manifest(manifest: dict[str, Any], root: Path) -> None:
    """Hold one rendered playground to the one form this module writes (see the comment above PLAYGROUND_MARKER)."""
    validate_schema(manifest, "playground")
    paths = [item["path"] for item in manifest["files"]]
    if len(paths) != len(set(paths)) or not set(PLAYGROUND_ROOT_MEMBERS).issubset(paths):
        raise VisualDecisionError("playground manifest file inventory is incomplete or duplicated")
    for binding in manifest["files"]:
        if binding["path"] in PLAYGROUND_ROOT_MEMBERS:
            continue
        match = PLAYGROUND_ASSET_MEMBER.fullmatch(binding["path"])
        if match is None:
            raise VisualDecisionError(f"playground manifest member is not an allowed playground file: {binding['path']}")
        if match.group(1) != binding["sha256"]:
            raise VisualDecisionError(f"playground asset is not named by its SHA-256: {binding['path']}")
    contents = {binding["path"]: _verify_bound_file(root, binding, f"playground file {binding['path']}") for binding in manifest["files"]}
    for path in PLAYGROUND_ROOT_MEMBERS:
        if contents[path].startswith(_BYTE_ORDER_MARKS):
            raise VisualDecisionError(f"playground member carries a byte order mark: {path}")
        if _PLAYGROUND_CONTROL.search(contents[path]):
            raise VisualDecisionError(f"playground member contains a forbidden control character: {path}")
    for path, refusals in (("index.html", _PLAYGROUND_CODE_REFUSALS + _PLAYGROUND_HTML_REFUSALS), ("app.js", _PLAYGROUND_CODE_REFUSALS + _PLAYGROUND_SCRIPT_REFUSALS)):
        for pattern, what in refusals:
            if pattern.search(contents[path]):
                raise VisualDecisionError(f"playground {path} contains {what}")
    if contents["index.html"] != PLAYGROUND_INDEX_HTML.encode("utf-8"):
        raise VisualDecisionError("playground index.html differs from the generated document")
    if contents["app.js"] != PLAYGROUND_APP_JS.encode("utf-8"):
        raise VisualDecisionError("playground app.js differs from the generated script")
    raw = contents["data.js"]
    if len(raw) < len(_PLAYGROUND_DATA_PREFIX) + len(_PLAYGROUND_DATA_SUFFIX) or not raw.startswith(_PLAYGROUND_DATA_PREFIX) or not raw.endswith(_PLAYGROUND_DATA_SUFFIX):
        raise VisualDecisionError("playground data.js is not the generated data-only file")
    middle = raw[len(_PLAYGROUND_DATA_PREFIX):len(raw) - len(_PLAYGROUND_DATA_SUFFIX)]
    data = load_json_bytes(middle, "playground data")
    references = _validate_playground_data(data, manifest)
    if _playground_json(data) != middle:
        raise VisualDecisionError("playground data.js is not in its canonical data-only encoding")
    css = _playground_css(data)
    css_references = _scan_sheet_css("style.css", css)
    if contents["style.css"] != css.encode("utf-8"):
        raise VisualDecisionError("playground style.css differs from the stylesheet its data generates")
    if css_references - references or references != {path for path in contents if path.startswith("assets/")}:
        raise VisualDecisionError("playground asset references differ from the closed manifest")


def _render_playground(
    output: Path,
    artifact_root: Path,
    brief: dict[str, Any],
    candidate_set: dict[str, Any],
    affinity_observation: dict[str, Any],
    candidate_file_sha256: str | None = None,
    *,
    logo_authentication: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the playground for reports this function re-validates itself (see _sheet_view)."""
    view = _sheet_view(artifact_root, brief, candidate_set, affinity_observation, logo_authentication=logo_authentication)
    content = _sample_content(brief)
    candidate_set_sha256 = candidate_file_sha256 or sha256_bytes(canonical_json(candidate_set))
    assets = _sheet_assets(view, artifact_root)
    faces, facts, typeset_text = assets["faces"], assets["facts"], _typeset_text(content)

    def font(item: dict[str, Any], text: str) -> dict[str, Any]:
        digest = item["asset"]["sha256"]
        return {"font_id": item["id"], "family": item["family"], "face": faces[(digest, item["classification"])],
                "missing": _missing_codepoints(facts[digest], text),
                "numerals": list(facts[digest]["numerals"])}

    axes = []
    for kind in SHEET_DOMAINS:
        offered = []
        for entry in (entry for entry in view["domains"][kind] if entry["eligible"]):
            record: dict[str, Any] = {"id": entry["candidate"]["id"], "label": entry["candidate"]["label"]}
            if kind == "color":
                record["roles"] = entry["report"]["roles"]
                record["tests"] = [{name: test[name] for name in ("foreground_role", "background_role", "usage", "minimum_ratio", "ratio_display", "passed")}
                                   for test in entry["report"]["tests"]]
            elif kind == "typography":
                by_class = {item["classification"]: item for item in entry["fonts"]}
                record["sans"], record["serif"] = font(by_class["sans"], typeset_text), font(by_class["serif"], typeset_text)
            elif kind == "code-font":
                item = entry["fonts"][0]
                record["mono"] = {**font(item, content["code"]), "ligatures": list(facts[item["asset"]["sha256"]]["ligatures"])}
            else:
                record["items"] = [{"variant": item["variant"], "asset_sha256": item["asset"]["sha256"]} for item in entry["items"]]
            offered.append(record)
        if offered:
            axes.append({"domain": kind, "candidates": offered})
    data = {
        "schema_version": "brandkit-visual-playground-data.v1",
        "candidate_set_sha256": candidate_set_sha256,
        "locale": brief["locale"],
        "normative": False,
        "content": content,
        "reference_palette": REFERENCE_PALETTE,
        "faces": [{"asset_sha256": digest, "classification": classification,
                   "weights": [facts[digest]["weights"][0], facts[digest]["weights"][1]],
                   "weight_axis": facts[digest]["weight_axis"],
                   "weights_set": list(_weights_set(facts[digest]["weights"], PLAYGROUND_WEIGHT_SELECTORS)),
                   "ascent": facts[digest]["ascent"], "descent": facts[digest]["descent"],
                   "x_height": facts[digest]["x_height"],
                   "x_heights": {str(weight): value for weight, value in sorted(facts[digest]["x_heights"].items())},
                   "units_per_em": facts[digest]["units_per_em"],
                   "x_height_source": facts[digest]["x_height_source"],
                   "size_rem": _rem(_normalised_size(
                       ROLE_METRICS[FACE_ROLE[classification]][0],
                       _x_height_ratio(facts[digest], PLAYGROUND_ROLE_WEIGHTS[FACE_ROLE[classification]])))}
                  for digest, classification in faces],
        "axes": axes,
        "blocked": [{"domain": kind, "candidate_id": entry["candidate"]["id"]} for kind in SHEET_DOMAINS for entry in view["domains"][kind] if not entry["eligible"]],
        "open_without_candidates": [kind for kind in view["open"] if not any(entry["eligible"] for entry in view["domains"][kind])],
        "recommendations": [{"kind": item["kind"], "candidate_id": item["candidate_id"], "rationale": item["rationale"]} for item in candidate_set["recommendations"]],
    }
    data_bytes = _PLAYGROUND_DATA_PREFIX + _playground_json(data) + _PLAYGROUND_DATA_SUFFIX
    members = {"index.html": PLAYGROUND_INDEX_HTML.encode("utf-8"), "app.js": PLAYGROUND_APP_JS.encode("utf-8"),
               "data.js": data_bytes, "style.css": _playground_css(data).encode("utf-8")}
    output_fd = _safe_output_dir(output, "playground")
    copied = _write_assets(output_fd, assets)
    try:
        for name in PLAYGROUND_ROOT_MEMBERS:
            _write_new(output_fd, name, members[name])
    except Exception:
        os.close(output_fd); raise
    files = {**{name: sha256_bytes(members[name]) for name in PLAYGROUND_ROOT_MEMBERS}, **copied}
    manifest = {
        "schema_version": "brandkit-visual-playground-manifest.v1",
        "generator": PLAYGROUND_MARKER,
        "brief_sha256": sha256_bytes(canonical_json(brief)),
        "candidate_set_sha256": candidate_set_sha256,
        "affinity_observation_sha256": sha256_bytes(canonical_json(affinity_observation)),
        "files": [{"path": path, "sha256": digest} for path, digest in sorted(files.items())],
        "offline": True,
        "interactive_controls": True,
        "persistence": "none",
        "preselection": False,
        "normative": False,
        "confirmation_artifact": False,
    }
    validate_schema(manifest, "playground")
    _write_new(output_fd, "manifest.json", canonical_json(manifest)); os.close(output_fd)
    validate_playground_manifest(manifest, output)
    return manifest


def render_playground(project_root: Path, run_id: str, now: int, *, sidecar_root: Path | None = None, affinity_auth: dict[str, Any] | None = None) -> dict[str, Any]:
    """Render the non-normative playground for a bundle this module accepts.

    Same trust inputs, same verification under the ledger lock and the same
    re-validated reports as :func:`render_sheet` - the two artifacts come from
    one validated bundle - but a different, separate output directory, a
    manifest that calls itself interactive, non-persistent and no confirmation
    artifact, and a validator of its own.  Rendering spends nothing.
    """
    workspace, brief, candidates, candidate_bytes, observation, logo_authentication = _verified_render_inputs(
        project_root, run_id, now, sidecar_root=sidecar_root, affinity_auth=affinity_auth)
    return _render_playground(workspace / "playground", workspace, brief, candidates, observation, sha256_bytes(candidate_bytes), logo_authentication=logo_authentication)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--kind", choices=sorted(SCHEMAS), required=True)
    validate_parser.add_argument("--path", required=True, help="normalized path below --root")
    validate_parser.add_argument("--root", type=Path, default=ROOT)
    validate_parser.add_argument("--affinity-observation", help="required by logo and affinity-mutation")
    for command in ("validate-bundle", "render-sheet", "render-playground"):
        operation = subparsers.add_parser(command); operation.add_argument("--project", type=Path, required=True); operation.add_argument("--run-id", required=True); operation.add_argument("--now", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            value = load_json(Path(), args.root, args.path, args.kind)
            observation = None
            if args.affinity_observation:
                observation = load_json(Path(), args.root, args.affinity_observation, "Affinity observation")
            if args.kind == "brief":
                validate_brief(value, args.root)
            elif args.kind == "candidates":
                validate_candidate_set(value, args.root)
            elif args.kind == "color":
                validate_color_report(value)
            elif args.kind == "font":
                validate_font_report(value, args.root)
            elif args.kind == "logo":
                if observation is None:
                    raise VisualDecisionError("logo validation requires --affinity-observation")
                validate_logo_report(value, args.root, observation)
            elif args.kind == "sheet":
                validate_sheet_manifest(value, args.root)
            elif args.kind == "playground":
                validate_playground_manifest(value, args.root)
            elif args.kind == "affinity-observation":
                validate_affinity_observation(value, args.root)
            elif args.kind == "affinity-mutation":
                raise VisualDecisionError("mutation validation requires the authenticated operation API")
            print(json.dumps({"status": "ok", "kind": args.kind, "sha256": sha256_bytes(canonical_json(value))}, sort_keys=True))
            return 0
        if args.command == "validate-bundle":
            print(json.dumps(validate_bundle(args.project, args.run_id, args.now), ensure_ascii=False, sort_keys=True)); return 0
        if args.command == "render-sheet":
            print(json.dumps(render_sheet(args.project, args.run_id, args.now), ensure_ascii=False, sort_keys=True)); return 0
        if args.command == "render-playground":
            print(json.dumps(render_playground(args.project, args.run_id, args.now), ensure_ascii=False, sort_keys=True)); return 0
    except VisualDecisionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
