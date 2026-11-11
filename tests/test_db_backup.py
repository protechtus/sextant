"""Tests for atlas_plus.db_backup.core -- SQLite backup/restore/prune."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(path: Path) -> Path:
    """Create a minimal SQLite DB for testing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)")
    conn.execute("INSERT INTO test VALUES (1, 'hello')")
    conn.commit()
    conn.close()
    return path


# ---------------------------------------------------------------------------
# snapshot()
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_creates_backup_file(self, tmp_path):
        from atlas_plus.db_backup.core import snapshot
        db = _make_db(tmp_path / "state.sqlite")
        backup_dir = tmp_path / "backups"
        dest = snapshot(source=db, backup_dir=backup_dir)
        assert dest.exists()

    def test_backup_is_valid_sqlite(self, tmp_path):
        from atlas_plus.db_backup.core import snapshot
        db = _make_db(tmp_path / "state.sqlite")
        dest = snapshot(source=db, backup_dir=tmp_path / "backups")
        # Must be openable and contain the test table
        conn = sqlite3.connect(str(dest))
        rows = conn.execute("SELECT val FROM test").fetchall()
        conn.close()
        assert rows[0][0] == "hello"

    def test_raises_when_source_missing(self, tmp_path):
        from atlas_plus.db_backup.core import snapshot
        with pytest.raises(FileNotFoundError):
            snapshot(source=tmp_path / "missing.sqlite", backup_dir=tmp_path / "bak")

    def test_custom_timestamp_in_filename(self, tmp_path):
        from atlas_plus.db_backup.core import snapshot
        db = _make_db(tmp_path / "state.sqlite")
        dest = snapshot(source=db, backup_dir=tmp_path / "backups", ts="2026-05-20_0200")
        assert "2026-05-20_0200" in dest.name

    def test_backup_dir_created_if_missing(self, tmp_path):
        from atlas_plus.db_backup.core import snapshot
        db = _make_db(tmp_path / "state.sqlite")
        new_dir = tmp_path / "deep" / "nested" / "backups"
        dest = snapshot(source=db, backup_dir=new_dir)
        assert dest.exists()


# ---------------------------------------------------------------------------
# list_backups()
# ---------------------------------------------------------------------------

class TestListBackups:
    def test_empty_when_no_dir(self, tmp_path):
        from atlas_plus.db_backup.core import list_backups
        result = list_backups(backup_dir=tmp_path / "nonexistent")
        assert result == []

    def test_returns_entries_sorted_newest_first(self, tmp_path):
        from atlas_plus.db_backup.core import snapshot, list_backups
        db = _make_db(tmp_path / "state.sqlite")
        backup_dir = tmp_path / "backups"
        snapshot(source=db, backup_dir=backup_dir, ts="2026-05-18_0200")
        snapshot(source=db, backup_dir=backup_dir, ts="2026-05-20_0200")
        result = list_backups(backup_dir)
        assert result[0]["ts_str"] == "2026-05-20_0200"
        assert result[1]["ts_str"] == "2026-05-18_0200"

    def test_entry_has_expected_keys(self, tmp_path):
        from atlas_plus.db_backup.core import snapshot, list_backups
        db = _make_db(tmp_path / "state.sqlite")
        backup_dir = tmp_path / "backups"
        snapshot(source=db, backup_dir=backup_dir)
        entry = list_backups(backup_dir)[0]
        assert "path" in entry
        assert "ts_str" in entry
        assert "size_kb" in entry
        assert "modified" in entry


# ---------------------------------------------------------------------------
# prune()
# ---------------------------------------------------------------------------

class TestPrune:
    def test_prunes_excess_backups(self, tmp_path):
        from atlas_plus.db_backup.core import snapshot, prune, list_backups
        db = _make_db(tmp_path / "state.sqlite")
        backup_dir = tmp_path / "backups"
        for i in range(5):
            snapshot(source=db, backup_dir=backup_dir, ts=f"2026-05-{i+10:02d}_0200")
        deleted = prune(keep=3, backup_dir=backup_dir)
        assert deleted == 2
        remaining = list_backups(backup_dir)
        assert len(remaining) == 3

    def test_no_prune_when_within_limit(self, tmp_path):
        from atlas_plus.db_backup.core import snapshot, prune
        db = _make_db(tmp_path / "state.sqlite")
        backup_dir = tmp_path / "backups"
        snapshot(source=db, backup_dir=backup_dir, ts="2026-05-20_0200")
        deleted = prune(keep=14, backup_dir=backup_dir)
        assert deleted == 0

    def test_keeps_newest_when_pruning(self, tmp_path):
        from atlas_plus.db_backup.core import snapshot, prune, list_backups
        db = _make_db(tmp_path / "state.sqlite")
        backup_dir = tmp_path / "backups"
        for ts in ["2026-05-15_0200", "2026-05-18_0200", "2026-05-20_0200"]:
            snapshot(source=db, backup_dir=backup_dir, ts=ts)
        prune(keep=2, backup_dir=backup_dir)
        remaining = list_backups(backup_dir)
        ts_values = [r["ts_str"] for r in remaining]
        assert "2026-05-20_0200" in ts_values
        assert "2026-05-18_0200" in ts_values
        assert "2026-05-15_0200" not in ts_values


# ---------------------------------------------------------------------------
# restore()
# ---------------------------------------------------------------------------

class TestRestore:
    def test_restore_replaces_current_db(self, tmp_path):
        from atlas_plus.db_backup.core import snapshot, restore
        db = _make_db(tmp_path / "state.sqlite")
        backup_dir = tmp_path / "backups"
        bak = snapshot(source=db, backup_dir=backup_dir)

        # Modify the current DB after backup
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO test VALUES (2, 'modified after backup')")
        conn.commit()
        conn.close()

        restore(backup_path=bak, dest=db, archive_dir=tmp_path / "pre-restore")

        # Should be back to just 1 row (as of backup time)
        conn2 = sqlite3.connect(str(db))
        rows = conn2.execute("SELECT COUNT(*) FROM test").fetchone()
        conn2.close()
        assert rows[0] == 1

    def test_restore_archives_current_db(self, tmp_path):
        from atlas_plus.db_backup.core import snapshot, restore
        db = _make_db(tmp_path / "state.sqlite")
        bak = snapshot(source=db, backup_dir=tmp_path / "backups")
        arc_dir = tmp_path / "pre-restore"
        archive_path = restore(backup_path=bak, dest=db, archive_dir=arc_dir)
        # Archive should exist
        assert archive_path is not None
        assert archive_path.exists()

    def test_restore_raises_when_backup_missing(self, tmp_path):
        from atlas_plus.db_backup.core import restore
        db = _make_db(tmp_path / "state.sqlite")
        with pytest.raises(FileNotFoundError):
            restore(backup_path=tmp_path / "nonexistent.sqlite", dest=db)


# ---------------------------------------------------------------------------
# last_backup_info()
# ---------------------------------------------------------------------------

class TestLastBackupInfo:
    def test_returns_none_when_no_backups(self, tmp_path):
        from atlas_plus.db_backup.core import last_backup_info
        result = last_backup_info(backup_dir=tmp_path / "empty")
        assert result is None

    def test_returns_most_recent(self, tmp_path):
        from atlas_plus.db_backup.core import snapshot, last_backup_info
        db = _make_db(tmp_path / "state.sqlite")
        backup_dir = tmp_path / "backups"
        snapshot(source=db, backup_dir=backup_dir, ts="2026-05-18_0200")
        snapshot(source=db, backup_dir=backup_dir, ts="2026-05-20_0200")
        info = last_backup_info(backup_dir=backup_dir)
        assert info["ts_str"] == "2026-05-20_0200"
