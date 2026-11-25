"""SEX-048 item 1b: a state directory that is a FILE is not reported as a busy or locked database."""
from __future__ import annotations

import logging
import sqlite3

from atlas_plus.mcp.memory_server import fts


def test_a_state_dir_that_is_a_file_is_named_as_the_cause(tmp_path, monkeypatch, caplog):
    state = tmp_path / "state"
    state.write_text("not a folder", encoding="utf-8")
    monkeypatch.setattr(fts, "FTS_DB", state / "search.sqlite")
    monkeypatch.setattr(fts, "_last_fresh_sweep", 0.0)
    with caplog.at_level(logging.WARNING, logger=fts.log.name):
        assert fts.search("anything") == []
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "is a file, not a directory" in text
    assert "busy" not in text and "locked" not in text
    assert "ATLAS_STATE_DIR" in text and "move" in text
    assert "is a file, not a directory" in fts.last_connect_error()


def test_a_locked_database_keeps_its_own_label():
    assert "busy or locked" in fts._open_failure(sqlite3.OperationalError("database is locked"))
    assert "ATLAS_STATE_DIR" in fts._open_failure(
        sqlite3.OperationalError("state directory /home/user/.atlas is a file, not a directory; move it aside"))
