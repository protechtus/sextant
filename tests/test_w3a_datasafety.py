"""Regression tests for data-safety fixes (transcripts chain, fts lock, restore, backup guard)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_plus.transcripts.index import TranscriptIndex
from atlas_plus.transcripts.recovery import recover_all
from atlas_plus.transcripts.store import TranscriptStore, TurnRecord


# --- row 7: per-file chain across a UTC date boundary -----------------------

def test_restart_across_date_boundary_keeps_turns(tmp_path, monkeypatch):
    root = tmp_path / "t"
    monkeypatch.setenv("ATLAS_TRANSCRIPT_INDEX", str(root / "index.sqlite"))

    class Day1(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 8, 23, 59, tzinfo=timezone.utc)

    class Day2(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 9, 0, 1, tzinfo=timezone.utc)

    import atlas_plus.transcripts.store as st
    with patch.object(st, "datetime", Day1):
        s1 = TranscriptStore(root)
        s1.append(TurnRecord(conversation_id="c1", source="s", role="user", content="a"))
        s1.append(TurnRecord(conversation_id="c1", source="s", role="user", content="b"))
    # "restart": fresh store, new UTC date, same conversation
    with patch.object(st, "datetime", Day2):
        s2 = TranscriptStore(root)
        s2.append(TurnRecord(conversation_id="c1", source="s", role="user", content="c"))
        s2.append(TurnRecord(conversation_id="c1", source="s", role="user", content="d"))

    p2 = root / "s" / "2026-11-09" / "c1.ndjson.partial"
    size_before = p2.stat().st_size
    report = recover_all(store=TranscriptStore(root), index=TranscriptIndex(root / "index.sqlite"))
    assert report.torn_lines == 0
    assert report.indexed_turns == 4
    assert p2.stat().st_size == size_before > 0


def test_recovery_rewrites_journal_after_truncate(tmp_path, monkeypatch):
    root = tmp_path / "t"
    store = TranscriptStore(root)
    for c in "abc":
        store.append(TurnRecord(conversation_id="c1", source="s", role="user", content=c))
    partial = next(store.list_partials())
    with partial.open("ab") as f:
        f.write(b'{"torn":')
    recover_all(store=store, index=TranscriptIndex(root / "index.sqlite"))
    import json
    j = json.loads(store.journal_path_for("c1").read_text())
    assert j["offset"] == partial.stat().st_size
    # a fresh store resumes the chain cleanly
    s2 = TranscriptStore(root)
    s2.append(TurnRecord(conversation_id="c1", source="s", role="user", content="d"))
    report = recover_all(store=s2, index=TranscriptIndex(root / "index.sqlite"))
    assert report.torn_lines == 0 and report.indexed_turns == 4


# --- row 10: locked DB is not corruption ------------------------------------

def test_locked_fts_db_is_not_healed(tmp_path, monkeypatch):
    from atlas_plus.mcp.memory_server import fts
    monkeypatch.setattr(fts, "MEMORY_ROOT", tmp_path / "mem")
    (tmp_path / "mem").mkdir()
    db = tmp_path / "state" / "fts.sqlite"
    monkeypatch.setattr(fts, "FTS_DB", db)
    fts._connect().close()
    assert fts._is_corruption(sqlite3.OperationalError("database is locked")) is False
    assert fts._is_corruption(sqlite3.DatabaseError("database disk image is malformed")) is True
    assert fts._is_corruption(sqlite3.DatabaseError("file is not a database")) is True

    # second connection holding an exclusive lock; shrink wait so test is fast
    real = sqlite3.connect
    monkeypatch.setattr(fts.sqlite3, "connect", lambda p, **kw: real(p, **{**kw, "timeout": 0.1}))
    blocker = real(str(db), isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with patch.object(fts, "_heal_malformed") as heal:
            try:
                fts.search("anything")
            except sqlite3.Error:
                pass
            heal.assert_not_called()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert db.exists() and not list(db.parent.glob("*.corrupt-*"))


# --- row 21: restore safety -------------------------------------------------

def _mk(path: Path, val: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE t (v TEXT)")
    c.execute("INSERT INTO t VALUES (?)", (val,))
    c.commit()
    c.close()
    return path


def test_restore_refuses_with_wal_and_archives_uniquely(tmp_path):
    from atlas_plus.db_backup.core import restore
    bak = _mk(tmp_path / "bak.sqlite", "snap")
    dest = _mk(tmp_path / "state.sqlite", "live")
    wal = tmp_path / "state.sqlite-wal"
    wal.write_bytes(b"x" * 64)
    with pytest.raises(RuntimeError):
        restore(bak, dest, archive_dir=tmp_path / "arc")
    wal.unlink()
    a1 = restore(bak, dest, archive_dir=tmp_path / "arc")
    # make dest differ again, restore within the same second
    c = sqlite3.connect(str(dest)); c.execute("UPDATE t SET v='again'"); c.commit(); c.close()
    a2 = restore(bak, dest, archive_dir=tmp_path / "arc")
    assert a1 != a2 and a1.exists() and a2.exists()
    assert sqlite3.connect(str(a1)).execute("SELECT v FROM t").fetchone()[0] == "live"
    assert sqlite3.connect(str(a2)).execute("SELECT v FROM t").fetchone()[0] == "again"
    assert sqlite3.connect(str(dest)).execute("SELECT v FROM t").fetchone()[0] == "snap"


# --- row 23: sync guard -----------------------------------------------------

def test_now_refuses_tiny_source_and_passes_max_delete(tmp_path, monkeypatch):
    import atlas_plus.backup as b
    empty = tmp_path / "empty"; empty.mkdir()
    full = tmp_path / "full"; full.mkdir()
    for i in range(6):
        (full / f"f{i}.md").write_text("x")
    monkeypatch.setattr(b, "SYNC_TARGETS", [
        {"name": "empty", "source": str(empty), "dest": "r:e"},
        {"name": "full", "source": str(full), "dest": "r:f"},
    ])
    monkeypatch.setattr(b, "ARCHIVE_TARGETS", [])
    calls = []

    monkeypatch.setattr(b, "RCLONE_REMOTE", "r")  # a named remote (no default since the persona pass)
    class R:
        returncode = 0
        stdout = f"{b.RCLONE_REMOTE}:" + chr(10)  # listremotes: the remote exists
        stderr = ""

    monkeypatch.setattr(b, "_rclone", lambda *a, **k: calls.append(a) or R())
    monkeypatch.setattr(b, "_log", lambda *a, **k: None)
    try:
        b.now(dry_run=False, skip_archive=True)
    except BaseException:
        pass
    srcs = [c[1] for c in calls if c and c[0] == "sync"]
    assert str(empty) not in srcs and str(full) in srcs
    sync = next(c for c in calls if c[0] == "sync")
    assert "--max-delete" in sync and sync[sync.index("--max-delete") + 1] == "50"
