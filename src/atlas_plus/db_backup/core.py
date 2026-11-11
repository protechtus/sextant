"""Atlas SQLite state database backup / restore core.

Safe to run while Brain is running: uses SQLite's VACUUM INTO which performs
an implicit WAL checkpoint before copying, producing a consistent snapshot
without locking the source database.

Storage layout:
    .atlas/backups/
        state_YYYY-MM-DD_HHmm.sqlite   -- timestamped snapshots
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from atlas_plus import paths as atlas_paths

_STATE_DIR = atlas_paths.state_dir()
_DB_PATH = _STATE_DIR / "state.sqlite"
_BACKUP_DIR = _STATE_DIR / "backups"

_TS_FORMAT = "%Y-%m-%d_%H%M"
_BACKUP_PREFIX = "state_"
_BACKUP_SUFFIX = ".sqlite"


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FORMAT)


def backup_path_for(ts: str) -> Path:
    """Return the expected snapshot path for a given timestamp string (e.g. '2026-05-21T03:00:00Z')."""
    return _BACKUP_DIR / f"{_BACKUP_PREFIX}{ts}{_BACKUP_SUFFIX}"


def snapshot(
    source: Path = _DB_PATH,
    backup_dir: Path = _BACKUP_DIR,
    ts: Optional[str] = None,
) -> Path:
    """Create a WAL-safe timestamped snapshot of the state database.

    Uses VACUUM INTO so the snapshot is already clean (no WAL residue),
    making it safe to copy without closing Brain. Does NOT require Brain
    to be stopped.

    Args:
        source:     Path to source state.sqlite.
        backup_dir: Directory to write snapshots into.
        ts:         Timestamp string (default: now in UTC).

    Returns:
        Path to the created backup file.

    Raises:
        FileNotFoundError: if source does not exist.
        sqlite3.Error:     if the backup fails.
    """
    if not source.exists():
        raise FileNotFoundError(f"Source DB not found: {source}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = ts or _now_ts()
    dest = backup_dir / f"{_BACKUP_PREFIX}{stamp}{_BACKUP_SUFFIX}"

    conn = sqlite3.connect(str(source))
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
    finally:
        conn.close()

    return dest


def list_backups(backup_dir: Path = _BACKUP_DIR) -> list[dict]:
    """Return all available SQLite snapshots sorted by most recent first.

    Returns list of {path, ts_str, size_kb, modified}.
    """
    if not backup_dir.exists():
        return []
    results = []
    for f in backup_dir.glob(f"{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}"):
        try:
            stat = f.stat()
            ts_str = f.name[len(_BACKUP_PREFIX):-len(_BACKUP_SUFFIX)]
            results.append({
                "path": f,
                "ts_str": ts_str,
                "size_kb": stat.st_size // 1024,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()[:19],
            })
        except OSError:
            continue
    results.sort(key=lambda r: r["ts_str"], reverse=True)
    return results


def restore(
    backup_path: Path,
    dest: Path = _DB_PATH,
    archive_dir: Optional[Path] = None,
) -> Path:
    """Restore a snapshot to dest, archiving the current DB first.

    Does NOT check if Brain is running -- caller should ensure Brain is stopped
    before calling restore. Restoring while Brain is running can corrupt both files.

    Args:
        backup_path:  Path to the .sqlite snapshot to restore.
        dest:         Where to restore to (default: .atlas/state.sqlite).
        archive_dir:  Where to archive the current DB before overwriting.
                      Defaults to .atlas/backups/pre-restore/.

    Returns:
        Path to the archive of the previous DB.

    Raises:
        FileNotFoundError: if backup_path does not exist.
    """
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup not found: {backup_path}")

    # A live -wal/-shm beside the destination means a process still has it
    # open (or crashed mid-write): restoring under it could replay a stale WAL
    # onto the restored DB. Refuse; the caller must stop the writer first.
    for suffix in ("-wal", "-shm"):
        side = dest.with_name(dest.name + suffix)
        if side.exists() and side.stat().st_size > 0:
            raise RuntimeError(
                f"Refusing to restore: {side.name} exists beside {dest.name}. "
                "Stop the process using the database (and let it checkpoint) first."
            )

    arc_dir = archive_dir or dest.parent / "backups" / "pre-restore"
    arc_dir.mkdir(parents=True, exist_ok=True)

    # Archive current DB before overwriting. Name carries seconds and is
    # created with O_EXCL so two restores can never overwrite an archive.
    archive_path: Optional[Path] = None
    if dest.exists():
        archive_path = _create_exclusive_archive(arc_dir)
        import shutil
        with open(str(dest), "rb") as src, os.fdopen(_archive_fd(archive_path), "wb") as out:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())

    # Restore through the sqlite3 backup API (page-level, consistent), into a
    # temp file that is fsynced and then atomically swapped in.
    tmp = dest.with_name(dest.name + ".restore-tmp")
    if tmp.exists():
        tmp.unlink()
    src_conn = sqlite3.connect(str(backup_path))
    try:
        dst_conn = sqlite3.connect(str(tmp))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    with open(str(tmp), "rb+") as f:
        f.flush()
        os.fsync(f.fileno())
    # Stale sidecars of an empty size are harmless but remove 0-byte ones so
    # the swapped-in file stands alone.
    for suffix in ("-wal", "-shm"):
        side = dest.with_name(dest.name + suffix)
        if side.exists():
            side.unlink()
    os.replace(str(tmp), str(dest))

    return archive_path or dest


def _create_exclusive_archive(arc_dir: Path) -> Path:
    """Reserve a unique pre-restore archive name (seconds + O_EXCL)."""
    base = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    n = 0
    while True:
        suffix = "" if n == 0 else f"_{n}"
        path = arc_dir / f"state_pre-restore_{base}{suffix}{_BACKUP_SUFFIX}"
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            n += 1
            continue
        os.close(fd)
        return path


def _archive_fd(path: Path) -> int:
    return os.open(str(path), os.O_WRONLY | os.O_TRUNC | getattr(os, "O_BINARY", 0))


def prune(
    keep: int = 14,
    backup_dir: Path = _BACKUP_DIR,
) -> int:
    """Delete oldest snapshots, keeping only the most recent `keep` files.

    Skips files in the pre-restore/ subdirectory (those are safety archives).

    Returns:
        Number of files deleted.
    """
    backups = list_backups(backup_dir)
    to_delete = backups[keep:]
    deleted = 0
    for entry in to_delete:
        try:
            entry["path"].unlink()
            deleted += 1
        except OSError:
            pass
    return deleted


def last_backup_info(backup_dir: Path = _BACKUP_DIR) -> Optional[dict]:
    """Return metadata for the most recent snapshot, or None if no backups exist."""
    backups = list_backups(backup_dir)
    return backups[0] if backups else None
