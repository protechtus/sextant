"""Small filesystem helpers shared by the indexer, the doctor scan and the importer."""
from __future__ import annotations

import os
from pathlib import Path


def is_link(path: Path) -> bool:
    """A symlink on any OS, or a Windows name-surrogate reparse point (junction, symlink).

    Cloud placeholders, dedup and WOF files also carry the reparse attribute but stay where
    they are; only name surrogates (bit 29 of the tag) redirect to another place, so only
    those count. False when the path does not exist.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    return bool(getattr(st, "st_reparse_tag", 0) & 0x20000000)


def inside(path: Path, root: Path) -> bool:
    """True when ``path`` resolves to a location inside ``root`` (both resolved)."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False
