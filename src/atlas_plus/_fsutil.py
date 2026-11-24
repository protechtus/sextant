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


def inside(path: Path, root: Path, root_resolved: "Path | None" = None) -> bool:
    """True when ``path`` resolves to a location inside ``root`` (both resolved). A caller checking
    many paths against one root resolves the root once and passes it as ``root_resolved``: resolving
    it again per note was half of this check's time on a 20,000-note corpus (SEX-046)."""
    try:
        return path.resolve().is_relative_to(root_resolved if root_resolved is not None else root.resolve())
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


_NO_MODE_FS = {"drvfs", "vfat", "exfat", "msdos", "ntfs", "ntfs3", "fuseblk", "cifs", "smb3"}


def _mount_entry(path: str, mounts_text: str):
    """(mount point, fstype, options) of the longest mount point holding ``path``, or None."""
    best = None
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        mnt = parts[1].replace("\\040", " ")
        if path == mnt or path.startswith(mnt.rstrip("/") + "/"):
            if best is None or len(mnt) > len(best[0]):
                best = (mnt, parts[2], parts[3])
    return best


def mode_fix(path: str, mounts_text: str | None = None) -> str:
    """The Fix line for a loose directory: chmod, unless the folder sits on a mount where chmod does nothing."""
    import shlex
    if mounts_text is None:
        try:
            with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
                mounts_text = fh.read()
        except OSError:
            mounts_text = ""
    entry = _mount_entry(str(path), mounts_text)
    if entry is not None:
        mnt, fstype, opts = entry
        drvfs = fstype == "drvfs" or (fstype == "9p" and "aname=drvfs" in opts)
        if drvfs and "metadata" in opts.replace(";", ",").split(","):
            return f"chmod 700 {shlex.quote(str(path))}"  # a Windows drive mounted with metadata keeps modes: chmod works
        if fstype in _NO_MODE_FS or drvfs:
            return (f"this folder is on {mnt}, a Windows or FAT drive where chmod has no effect; move it under your "
                    f"Linux home (set ATLAS_WORKSPACE_ROOT, see docs/CONFIGURATION.md) or remount {mnt} with the "
                    f"metadata option")
    return f"chmod 700 {shlex.quote(str(path))}"


def loose_dir_modes(paths) -> list[dict]:
    """POSIX only: the directories in ``paths`` that exist and grant group or other any access."""
    out: list[dict] = []
    if os.name == "nt":
        return out
    import stat as _stat
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        if _stat.S_ISDIR(st.st_mode) and st.st_mode & 0o077:
            out.append({"path": str(p), "mode": oct(st.st_mode & 0o777)[2:], "fix": mode_fix(str(p))})
    return out
