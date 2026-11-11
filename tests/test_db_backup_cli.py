"""Tests for atlas_plus.db_backup.cli -- atlas-db-backup CLI commands."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

runner = CliRunner()


def _make_backup_entry(ts="20260521_030000", size_kb=512):
    return {"ts_str": ts, "size_kb": size_kb, "modified": "2026-05-21 03:00:00",
            "path": Path(f"/tmp/state-{ts}.sqlite")}


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

class TestSnapshotCmd:
    def test_shows_success_message(self, tmp_path):
        from atlas_plus.db_backup.cli import app
        dest = tmp_path / "state-20260521.sqlite"
        dest.write_bytes(b"x" * 1024)
        with patch("atlas_plus.db_backup.cli.snapshot", return_value=dest):
            result = runner.invoke(app, ["snapshot",
                                          "--source", str(tmp_path / "state.sqlite"),
                                          "--dir", str(tmp_path)])
        assert result.exit_code == 0
        assert "Snapshot created" in result.output or "snapshot" in result.output.lower()

    def test_missing_source_exits_with_error(self, tmp_path):
        from atlas_plus.db_backup.cli import app
        with patch("atlas_plus.db_backup.cli.snapshot",
                   side_effect=FileNotFoundError("source not found")):
            result = runner.invoke(app, ["snapshot",
                                          "--source", str(tmp_path / "nope.sqlite"),
                                          "--dir", str(tmp_path)])
        assert result.exit_code != 0
        assert "Error" in result.output or "not found" in result.output.lower()


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

class TestListCmd:
    def test_no_backups_shows_message(self, tmp_path):
        from atlas_plus.db_backup.cli import app
        with patch("atlas_plus.db_backup.cli.list_backups", return_value=[]):
            result = runner.invoke(app, ["list", "--dir", str(tmp_path)])
        assert result.exit_code == 0
        assert "No snapshots" in result.output or "no snapshots" in result.output.lower()

    def test_shows_backup_table(self, tmp_path):
        from atlas_plus.db_backup.cli import app
        backups = [_make_backup_entry("20260521_030000")]
        with patch("atlas_plus.db_backup.cli.list_backups", return_value=backups):
            result = runner.invoke(app, ["list", "--dir", str(tmp_path)])
        assert result.exit_code == 0
        assert "20260521" in result.output


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------

class TestRestoreCmd:
    def test_backup_not_found_exits(self, tmp_path):
        from atlas_plus.db_backup.cli import app
        missing = tmp_path / "nonexistent.sqlite"
        result = runner.invoke(app, ["restore", str(missing), "--yes"])
        assert result.exit_code != 0

    def test_restore_with_yes_flag_succeeds(self, tmp_path):
        from atlas_plus.db_backup.cli import app
        backup = tmp_path / "backup.sqlite"
        backup.write_bytes(b"x" * 100)
        with patch("atlas_plus.db_backup.cli.restore",
                   return_value=tmp_path / "archived.sqlite"):
            result = runner.invoke(app, ["restore", str(backup),
                                          "--dest", str(tmp_path / "state.sqlite"),
                                          "--yes"])
        assert result.exit_code == 0
        assert "Restore complete" in result.output or "complete" in result.output.lower()


# ---------------------------------------------------------------------------
# prune
# ---------------------------------------------------------------------------

class TestPruneCmd:
    def test_no_pruning_needed_shows_message(self, tmp_path):
        from atlas_plus.db_backup.cli import app
        with patch("atlas_plus.db_backup.cli.prune", return_value=0):
            result = runner.invoke(app, ["prune", "--keep", "14",
                                          "--dir", str(tmp_path)])
        assert result.exit_code == 0
        assert "No snapshots pruned" in result.output or "within" in result.output.lower()

    def test_shows_pruned_count(self, tmp_path):
        from atlas_plus.db_backup.cli import app
        with patch("atlas_plus.db_backup.cli.prune", return_value=3):
            result = runner.invoke(app, ["prune", "--keep", "5",
                                          "--dir", str(tmp_path)])
        assert result.exit_code == 0
        assert "3" in result.output and "Pruned" in result.output


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

class TestStatusCmd:
    def test_no_backups_shows_no_snapshot_message(self, tmp_path):
        from atlas_plus.db_backup.cli import app
        with patch("atlas_plus.db_backup.cli.last_backup_info", return_value=None), \
             patch("atlas_plus.db_backup.cli.list_backups", return_value=[]):
            result = runner.invoke(app, ["status", "--dir", str(tmp_path),
                                          "--source", str(tmp_path / "state.sqlite")])
        assert result.exit_code == 0
        assert "No snapshots" in result.output or "not found" in result.output.lower()

    def test_shows_last_snapshot_info(self, tmp_path):
        from atlas_plus.db_backup.cli import app
        info = _make_backup_entry("20260521_030000")
        with patch("atlas_plus.db_backup.cli.last_backup_info", return_value=info), \
             patch("atlas_plus.db_backup.cli.list_backups", return_value=[info]):
            result = runner.invoke(app, ["status", "--dir", str(tmp_path),
                                          "--source", str(tmp_path / "state.sqlite")])
        assert result.exit_code == 0
        assert "20260521" in result.output or "Last snapshot" in result.output
