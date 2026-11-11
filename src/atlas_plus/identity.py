"""Resolve the operator's identity file and display name.

Release-prep (Sextant public fork): the identity file used to be hardcoded
to a personal filename in the Brain system prompt and both session hooks. Any
install now resolves it in this order:

1. ``ATLAS_IDENTITY_PATH`` env var -- absolute path, or relative to the
   memory root (e.g. ``identity/me.md``).
2. ``<memory_root>/identity/user.md`` -- the documented default.
3. The best ``*.md`` file under ``<memory_root>/identity/`` -- highest
   frontmatter ``priority:`` first (critical > high > normal > low), then
   alphabetical. Covers legacy installs whose identity file predates the
   default name and whose identity dir holds several profile facets.
4. Fall back to the (possibly nonexistent) default path; callers already
   guard with ``.exists()``.

The display name comes from ``ATLAS_USER_NAME``, then the ``title:``
frontmatter key of the resolved identity file, then a generic fallback.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

DEFAULT_IDENTITY_RELPATH = Path("identity") / "user.md"

GENERIC_USER_NAME = "the user"

_PRIORITY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}


def _frontmatter_value(path: Path, key: str) -> Optional[str]:
    """Extract a top-level scalar value from a file's YAML frontmatter, if any."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    for line in text[3:end].splitlines():
        k, sep, value = line.partition(":")
        if sep and k.strip() == key:
            value = value.strip().strip("\"'")
            if value:
                return value
    return None


def resolve_identity_path(memory_root: Path) -> Path:
    """Return the identity file path for this install (may not exist)."""
    env = os.environ.get("ATLAS_IDENTITY_PATH", "").strip()
    if env:
        p = Path(env)
        return p if p.is_absolute() else memory_root / p

    default = memory_root / DEFAULT_IDENTITY_RELPATH
    if default.exists():
        return default

    identity_dir = memory_root / "identity"
    try:
        candidates = sorted(identity_dir.glob("*.md"))
    except OSError:
        candidates = []
    if candidates:
        # Multi-file identity dirs hold profile facets (working style, comms
        # style, ...); the person file carries the highest priority.
        def rank(p: Path) -> tuple[int, str]:
            prio = (_frontmatter_value(p, "priority") or "normal").lower()
            return (_PRIORITY_RANK.get(prio, 2), p.name)

        return min(candidates, key=rank)

    return default


def _name_from_title(title: str) -> str:
    """Trim a descriptive title ('A. Person -- Identity & Profile') to the name."""
    for sep in ("—", "–", " -- ", " - ", "|", ","):
        if sep in title:
            title = title.split(sep, 1)[0]
    return title.strip()


def user_display_name(memory_root: Path) -> str:
    """Return the operator's display name, or a generic fallback."""
    env = os.environ.get("ATLAS_USER_NAME", "").strip()
    if env:
        return env
    identity = resolve_identity_path(memory_root)
    if identity.exists():
        title = _frontmatter_value(identity, "title")
        if title:
            name = _name_from_title(title)
            if name:
                return name
    return GENERIC_USER_NAME
