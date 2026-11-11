"""P7-T4: Stale file detection — finds .memory/ files with old 'updated:' frontmatter."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from atlas_plus.mcp.memory_server.config import EXCLUDED_DIRS, MEMORY_ROOT

DEFAULT_STALE_MONTHS = 6


@dataclass
class StaleFile:
    """A memory file that has not been updated within the configured staleness threshold."""

    path: str
    updated: str
    age_days: int
    title: str

    def as_dict(self) -> dict:
        """Return a JSON-serialisable dict of the stale file fields."""
        return {
            "path": self.path,
            "updated": self.updated,
            "age_days": self.age_days,
            "title": self.title,
        }


def _parse_updated(fm: dict) -> Optional[date]:
    raw = fm.get("updated")
    if not raw:
        return None
    try:
        if isinstance(raw, (date, datetime)):
            return raw if isinstance(raw, date) else raw.date()
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _read_frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---"):
            return {}
        end = text.find("---", 3)
        if end == -1:
            return {}
        return yaml.safe_load(text[3:end].strip()) or {}
    except Exception:
        return {}


def find_stale(
    memory_root: Optional[Path] = None,
    months: int = DEFAULT_STALE_MONTHS,
    scope: str = "",
) -> list[StaleFile]:
    """Return .memory/ files whose 'updated:' frontmatter is older than *months*.

    Args:
        memory_root: Root of the corpus. Defaults to MEMORY_ROOT.
        months: Files older than this many months are flagged. Default 6.
        scope: Optional subdirectory to restrict search.

    Returns list of StaleFile sorted by age descending (oldest first).
    """
    root = memory_root or MEMORY_ROOT
    search_root = root / scope if scope else root
    # Cutoff approximated via days. (A previous exact-month computation via
    # date.replace(month=...) was dead code AND a calendar time-bomb: it
    # raised "day is out of range for month" whenever today's day-of-month
    # did not exist in the target month, e.g. any month-end run. Found by CI
    # running on the 31st, 2026-07-30.)
    cutoff_days = months * 30
    today = date.today()

    results: list[StaleFile] = []
    for f in search_root.rglob("*.md"):
        rel = f.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if f.name.startswith("_"):
            continue

        fm = _read_frontmatter(f)
        updated = _parse_updated(fm)
        if updated is None:
            continue

        age_days = (today - updated).days
        if age_days > cutoff_days:
            results.append(StaleFile(
                path=str(rel).replace("\\", "/"),
                updated=str(updated),
                age_days=age_days,
                title=fm.get("title", f.stem),
            ))

    results.sort(key=lambda s: s.age_days, reverse=True)
    return results


def archive_file(
    path: str,
    memory_root: Optional[Path] = None,
    label: str = "curation",
) -> str:
    """Move a .memory/ file to the archive directory with provenance.

    Returns the archive destination path.
    """
    root = (memory_root or MEMORY_ROOT).resolve()
    # pathlib accepts "/" separators on every platform; converting to "\\"
    # created literal-backslash filenames on POSIX (CI find, 2026-07-30).
    src = (root / path).resolve()

    # Reject path traversal
    try:
        src.relative_to(root)
    except ValueError:
        raise ValueError(f"Path escapes memory root: {path!r}")

    if not src.exists():
        raise FileNotFoundError(f"Not found: {src}")

    date_str = date.today().isoformat()
    archive_dir = root / "archive" / f"{date_str}-{label}"
    archive_dir.mkdir(parents=True, exist_ok=True)

    dest = archive_dir / src.name
    # Avoid clobbering if name already exists in archive
    if dest.exists():
        stem, suffix = src.stem, src.suffix
        dest = archive_dir / f"{stem}-{date_str}{suffix}"
        n = 2
        while dest.exists():  # never overwrite a previously archived file
            dest = archive_dir / f"{stem}-{date_str}-{n}{suffix}"
            n += 1

    shutil.move(str(src), dest)
    return str(dest.relative_to(root)).replace("\\", "/")
