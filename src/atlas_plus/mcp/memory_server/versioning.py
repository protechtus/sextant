"""P3-T4: Version-before-edit for all MCP memory writes.

Before any write to .memory/, copies the current file to
.memory/versions/<subdir>/<stem>_<YYYY-MM-DD>_v<N><ext>.
Idempotent: same content hash produces no new version copy.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import date
from pathlib import Path

from .config import MEMORY_ROOT


def _content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version_before_write(target: Path, root: Path | None = None) -> Path | None:
    """Copy target into its versions/ sibling dir. Returns the version path, or None if target didn't exist.

    root defaults to the MCP server's MEMORY_ROOT; callers operating on a
    different corpus root (e.g. atlas_plus.sync applying remote changes)
    pass it explicitly so version copies land inside that corpus.

    Edge probes 2026-10-07 (quality loop run 37) reshaped the internals:
    - Idempotence matching is string-based, NOT glob-based: the old glob
      pattern broke on extensionless files (trailing-dot pattern never
      matched -> a NEW copy on every identical write) and on filenames
      containing glob metacharacters ('notes [draft].md').
    - A target outside the corpus root returns None instead of letting
      ValueError crash the WRITE path (out-of-corpus = nothing to version
      into this corpus; the caller's own write proceeds).
    - The version slot is claimed atomically (O_CREAT|O_EXCL) and content
      lands via tmp + os.replace: concurrent writers can no longer pick
      the same slot and silently overwrite each other's copy, and a crash
      mid-copy leaves an empty claimed slot (hash-mismatched, so the next
      write takes the next slot) rather than a truncated copy posing as
      history.
    """
    if not target.exists():
        return None

    base = root if root is not None else MEMORY_ROOT
    try:
        rel = target.relative_to(base)
    except ValueError:
        return None  # outside this corpus: nothing to version here
    from atlas_plus._fsutil import inside, is_link
    vroot = base / "versions"
    if vroot.exists() and (is_link(vroot) or not inside(vroot, base)):
        # A junctioned versions/ carried every history copy outside the corpus (AG16, sweep 11).
        raise ValueError(f"{vroot} is a link that leaves the corpus; fix it before writing "
                         "(nothing was written)")
    try:
        versions_dir = base / "versions" / rel.parent
        versions_dir.mkdir(parents=True, exist_ok=True)
        if versions_dir != vroot and (is_link(versions_dir) or not inside(versions_dir, base)):
            raise ValueError(f"{versions_dir} is a link that leaves the corpus; fix it before writing "
                             "(nothing was written)")

        date_str = date.today().isoformat()
        stem = target.stem
        suffix = target.suffix

        # Idempotent: skip if an identical version already exists today.
        # String-match (stem + date + _vN + exact suffix) instead of glob.
        existing_hash = _content_hash(target)
        slot_re = re.compile(
            re.escape(f"{stem}_{date_str}_v") + r"(\d+)" + re.escape(suffix) + r"$"
        )
        max_seen = 0
        for entry in versions_dir.iterdir():
            m = slot_re.match(entry.name)
            if not m:
                continue
            max_seen = max(max_seen, int(m.group(1)))
            try:
                if _content_hash(entry) == existing_hash:
                    return entry
            except OSError:
                continue  # unreadable/empty claimed slot: not a match

        # Snapshot content first (tmp), then claim a slot atomically and
        # replace. Two concurrent writers can race the same n; O_EXCL
        # makes exactly one win, the loser advances.
        import threading
        tmp = versions_dir / f".{stem}.{os.getpid()}-{threading.get_ident()}.v-tmp"
        shutil.copy2(target, tmp)
        try:
            n = max_seen + 1
            while True:
                dest = versions_dir / f"{stem}_{date_str}_v{n}{suffix}"
                try:
                    fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    n += 1
                    continue
                os.close(fd)
                os.replace(tmp, dest)
                return dest
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    except FileNotFoundError:
        return None
