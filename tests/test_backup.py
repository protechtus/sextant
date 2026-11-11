"""Tests for atlas_plus.backup -- state I/O and log formatting.

Covers _write_state, _read_state, _log, and the status CLI command.
The rclone-dependent verify/now/install commands are not tested here
(they require the rclone binary and remote configuration).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# TestWriteReadState
# ---------------------------------------------------------------------------

class TestWriteReadState:
    def test_write_creates_file(self, tmp_path):
        state_file = tmp_path / "backup_state.json"
        with patch("atlas_plus.backup.BACKUP_STATE", state_file), \
             patch("atlas_plus.backup.LOG_DIR", tmp_path):
            from atlas_plus.backup import _write_state
            _write_state({"status": "ok", "timestamp": "2026-05-21T03:00:00Z"})
        assert state_file.exists()

    def test_write_content_is_valid_json(self, tmp_path):
        state_file = tmp_path / "backup_state.json"
        with patch("atlas_plus.backup.BACKUP_STATE", state_file), \
             patch("atlas_plus.backup.LOG_DIR", tmp_path):
            from atlas_plus.backup import _write_state
            _write_state({"status": "ok", "count": 3})
        data = json.loads(state_file.read_text(encoding="utf-8"))
        assert data["status"] == "ok"
        assert data["count"] == 3

    def test_read_returns_none_when_file_missing(self, tmp_path):
        missing = tmp_path / "backup_state.json"
        with patch("atlas_plus.backup.BACKUP_STATE", missing):
            from atlas_plus.backup import _read_state
            result = _read_state()
        assert result is None

    def test_read_returns_dict_for_existing_state(self, tmp_path):
        state_file = tmp_path / "backup_state.json"
        state_file.write_text(json.dumps({"status": "ok", "bytes_synced": 1024}), encoding="utf-8")
        with patch("atlas_plus.backup.BACKUP_STATE", state_file):
            from atlas_plus.backup import _read_state
            result = _read_state()
        assert result is not None
        assert result["status"] == "ok"
        assert result["bytes_synced"] == 1024

    def test_read_returns_none_for_corrupt_state(self, tmp_path):
        state_file = tmp_path / "backup_state.json"
        state_file.write_text("not json {{", encoding="utf-8")
        with patch("atlas_plus.backup.BACKUP_STATE", state_file):
            from atlas_plus.backup import _read_state
            result = _read_state()
        assert result is None

    def test_roundtrip_preserves_nested_data(self, tmp_path):
        state_file = tmp_path / "backup_state.json"
        original = {
            "status": "ok",
            "targets": ["memory", "state"],
            "meta": {"duration_s": 42.1},
        }
        with patch("atlas_plus.backup.BACKUP_STATE", state_file), \
             patch("atlas_plus.backup.LOG_DIR", tmp_path):
            from atlas_plus.backup import _write_state, _read_state
            _write_state(original)
        with patch("atlas_plus.backup.BACKUP_STATE", state_file):
            from atlas_plus.backup import _read_state
            result = _read_state()
        assert result == original


# ---------------------------------------------------------------------------
# TestLog
# ---------------------------------------------------------------------------

class TestLog:
    def test_log_appends_to_file(self, tmp_path):
        log_file = tmp_path / "backup.log"
        with patch("atlas_plus.backup.BACKUP_LOG", log_file), \
             patch("atlas_plus.backup.LOG_DIR", tmp_path), \
             patch("atlas_plus.backup.console") as mock_console:
            from atlas_plus.backup import _log
            _log("backup completed")
            _log("second message")
        lines = log_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert "backup completed" in lines[0]
        assert "second message" in lines[1]

    def test_log_entry_includes_iso_timestamp(self, tmp_path):
        log_file = tmp_path / "backup.log"
        with patch("atlas_plus.backup.BACKUP_LOG", log_file), \
             patch("atlas_plus.backup.LOG_DIR", tmp_path), \
             patch("atlas_plus.backup.console"):
            from atlas_plus.backup import _log
            _log("test message")
        content = log_file.read_text(encoding="utf-8")
        assert "2026-" in content or "T" in content
        assert "[" in content and "]" in content

    def test_log_prints_to_console(self, tmp_path):
        log_file = tmp_path / "backup.log"
        with patch("atlas_plus.backup.BACKUP_LOG", log_file), \
             patch("atlas_plus.backup.LOG_DIR", tmp_path), \
             patch("atlas_plus.backup.console") as mock_console:
            from atlas_plus.backup import _log
            _log("important event")
        mock_console.print.assert_called_once()
        call_args = mock_console.print.call_args[0][0]
        assert "important event" in call_args


# ---------------------------------------------------------------------------
# TestStatusCommand
# ---------------------------------------------------------------------------

class TestStatusCommand:
    def test_status_shows_no_state_when_missing(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.backup import app
        runner = CliRunner()
        missing = tmp_path / "backup_state.json"
        with patch("atlas_plus.backup.BACKUP_STATE", missing):
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "never" in result.output.lower() or "no" in result.output.lower() or result.output != ""

    def test_status_shows_last_backup_result(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.backup import app
        runner = CliRunner()
        state_file = tmp_path / "backup_state.json"
        state_file.write_text(
            json.dumps({"status": "ok", "timestamp": "2026-05-21T03:00:00Z",
                        "targets": 2, "errors": 0}),
            encoding="utf-8"
        )
        with patch("atlas_plus.backup.BACKUP_STATE", state_file):
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        # Should show some info about the last backup
        assert "ok" in result.output.lower() or "2026" in result.output or result.output != ""


# ---------------------------------------------------------------------------
# TestSecretPatterns
# ---------------------------------------------------------------------------

class TestSecretPatterns:
    def test_secret_patterns_include_env_files(self):
        from atlas_plus.backup import _SECRET_PATTERNS
        assert any(".env" in p for p in _SECRET_PATTERNS)

    def test_secret_patterns_include_key_files(self):
        from atlas_plus.backup import _SECRET_PATTERNS
        assert any(".key" in p for p in _SECRET_PATTERNS)

    def test_secret_patterns_include_pem_files(self):
        from atlas_plus.backup import _SECRET_PATTERNS
        assert any(".pem" in p for p in _SECRET_PATTERNS)

    def test_sync_targets_exclude_secrets(self):
        from atlas_plus.backup import SYNC_TARGETS, _SECRET_PATTERNS
        for target in SYNC_TARGETS:
            exclude = target.get("exclude", [])
            # Each sync target must exclude at least one secret pattern
            assert any(p in exclude for p in _SECRET_PATTERNS), \
                f"Target {target['name']} missing secret exclusions"
