"""Discover intake candidates and prove they are safe to read.

Cloud drives (Google Drive for Desktop in the first deployment) present a
file before its bytes have finished arriving, and present native Docs as
tiny shortcut files whose bytes are a JSON pointer, not the document.
Both are rejected here so the engine only ever copies complete content.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ARCHIVE_DIRNAME = "_imported"
MANIFEST_NAME = "MANIFEST.md"

IGNORED_NAMES = {"desktop.ini", "thumbs.db", ".ds_store", MANIFEST_NAME.lower(), "readme.md"}
IGNORED_PREFIXES = ("~$", ".~", "._", ".")
# Google native documents are shortcuts; browser/office temp files are partial.
NATIVE_DOC_SUFFIXES = (".gdoc", ".gsheet", ".gslides", ".gdraw", ".gform", ".gmap", ".gsite", ".gjam", ".gscript", ".glink")
PARTIAL_SUFFIXES = (".tmp", ".crdownload", ".part", ".partial", ".download", ".sync-tmp")


@dataclass(frozen=True)
class Candidate:
    path: Path
    rel: str   # posix path relative to the source root
    size: int
    mtime_ns: int


def skip_reason(path: Path) -> str | None:
    """Why this path is not an intake candidate, or None when it is one."""
    name = path.name
    low = name.lower()
    if low in IGNORED_NAMES:
        return "ignored-name"
    if any(low.startswith(p) for p in IGNORED_PREFIXES):
        return "temp-or-hidden"
    if low.endswith(NATIVE_DOC_SUFFIXES):
        return "native-gdoc-shortcut"
    if low.endswith(PARTIAL_SUFFIXES):
        return "partial-download"
    return None


def discover(source: Path, recursive: bool = True) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """All importable files under ``source`` plus (rel, reason) for skipped ones.

    ``_imported/`` and any directory whose name starts with ``_`` or ``.``
    are never descended into: those are the archive and operator-private
    corners of the drop folder.
    """
    found: list[Candidate] = []
    skipped: list[tuple[str, str]] = []
    if not source.is_dir():
        return found, skipped
    for root, dirs, files in os.walk(source):
        dirs[:] = sorted(d for d in dirs if not d.startswith(("_", ".")))
        if not recursive:
            dirs[:] = []
        for fname in sorted(files):
            p = Path(root) / fname
            rel = p.relative_to(source).as_posix()
            reason = skip_reason(p)
            if reason:
                skipped.append((rel, reason))
                continue
            try:
                st = p.stat()
            except OSError as exc:
                skipped.append((rel, f"stat-failed: {exc.__class__.__name__}"))
                continue
            if st.st_size == 0:
                skipped.append((rel, "empty"))
                continue
            found.append(Candidate(p, rel, st.st_size, st.st_mtime_ns))
    return found, skipped


def is_stable(
    path: Path,
    *,
    settle_seconds: float = 2.0,
    min_age_seconds: float = 5.0,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> tuple[bool, str]:
    """(True, "") when the file is complete and readable; (False, why) otherwise.

    Complete means: mtime at least ``min_age_seconds`` old, size unchanged
    across two samples ``settle_seconds`` apart, and the first bytes readable
    (a cloud placeholder raises on open or read).
    """
    try:
        st1 = path.stat()
    except OSError as exc:
        return False, f"stat-failed: {exc.__class__.__name__}"
    if min_age_seconds > 0 and (now() - st1.st_mtime) < min_age_seconds:
        return False, "too-recent"
    if settle_seconds > 0:
        sleeper(settle_seconds)
        try:
            st2 = path.stat()
        except OSError as exc:
            return False, f"stat-failed: {exc.__class__.__name__}"
        if st2.st_size != st1.st_size or st2.st_mtime_ns != st1.st_mtime_ns:
            return False, "still-changing"
    try:
        with path.open("rb") as f:
            f.read(1)
    except OSError as exc:
        return False, f"unreadable: {exc.__class__.__name__}"
    return True, ""


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()
