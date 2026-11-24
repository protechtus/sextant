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


def entry_is_link(entry: "os.DirEntry") -> bool:
    """``is_link`` for a directory-listing entry, from the listing's own data: no system call
    unless the entry IS a reparse point. ``is_link(Path(entry.path))`` cost two lstat calls per
    file, 3 s of the file tree and 2 s of doctor's scan at 20,000 notes on Windows (AG100)."""
    try:
        if entry.is_symlink():
            return True
        if os.name != "nt":
            return False
        st = entry.stat(follow_symlinks=False)  # cached from the listing on Windows
    except OSError:
        return False
    if not getattr(st, "st_file_attributes", 0) & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        return False
    return is_link(Path(entry.path))  # a reparse point (rare): the full rule


def inside(path: Path, root: Path) -> bool:
    """True when ``path`` resolves to a location inside ``root`` (both resolved)."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def make_private_dir(path: Path) -> bool:
    """``mkdir -p path``; when this call creates ``path`` on POSIX, it ends up mode 0700.

    Under the usual umask 022 a new corpus or state root was 0755 and every note, the search
    index and the sync device registry were readable by any local account (MN86, sweep 25).
    Only a directory this call creates is changed: an existing one keeps the owner's choice
    (doctor reports it). Windows is untouched (ACLs, not mode bits). Returns True if created.
    """
    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True)
    if not existed and os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return not existed


def loose_dir_modes(paths) -> list[dict]:
    """POSIX only: the directories in ``paths`` that exist and grant group or other any access."""
    out: list[dict] = []
    if os.name == "nt":
        return out
    import shlex
    import stat as _stat
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        if _stat.S_ISDIR(st.st_mode) and st.st_mode & 0o077:
            out.append({"path": str(p), "mode": oct(st.st_mode & 0o777)[2:], "fix": f"chmod 700 {shlex.quote(str(p))}"})
    return out
