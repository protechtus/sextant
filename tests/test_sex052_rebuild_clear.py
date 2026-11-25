"""SEX-052 item 1: a forced rebuild of a built index clears it with a drop and re-create, not a row-by-row delete.

Measured on a built index of 20,000 notes: the delete 10.0 s, the drop 1.6 s. Each test runs `fts.rebuild`:
  a  the rebuilt index is row for row and search for search the index before, with the same tokenizer, and the
     statements the rebuild ran held no table-wide delete of the search table (fails before the change);
  b  the same on an index recorded with the fallback tokenizer (an older SQLite): the re-created table keeps it;
  c  a failure between the drop and the commit leaves the old index whole (one transaction).
"""
from __future__ import annotations

import sqlite3

import pytest

from atlas_plus.mcp.memory_server import fts

N_NOTES = 40


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    (mem / "reference").mkdir(parents=True)
    (tmp_path / "state").mkdir()
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem, raising=False)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "search.sqlite", raising=False)
    for i in range(N_NOTES):
        (mem / "reference" / f"note-{i:03d}.md").write_text(
            f"---\ntitle: note {i}\n---\nThe staging cluster note {i} " + "kettle " * (i % 5) + "\n",
            encoding="utf-8")
    return mem


def _snapshot() -> dict:
    conn = sqlite3.connect(str(fts.FTS_DB))
    try:
        return {
            "files": conn.execute("SELECT path, mtime, last_confirmed, size FROM memory_files ORDER BY path").fetchall(),
            "rows": conn.execute("SELECT f.path, t.frontmatter_json, t.body FROM memory_files f JOIN memory_fts t "
                                 "ON t.rowid = f.fts_rowid ORDER BY f.path").fetchall(),
            "n_fts": conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0],
            "table_sql": conn.execute("SELECT sql FROM sqlite_master WHERE name='memory_fts'").fetchone()[0],
            "meta": conn.execute("SELECT key, value FROM atlas_meta WHERE key NOT IN ('last_indexed', 'parse_revision') ORDER BY key").fetchall(),
        }
    finally:
        conn.close()


def _hits(query: str) -> list:
    return [r.get("path") for r in fts.search(query, limit=50)]


class _Traced:
    """A connection that records every statement and can fail on one (delegates the rest)."""

    def __init__(self, conn, log, fail_on=None):
        self._conn, self._log, self._fail_on = conn, log, fail_on

    def execute(self, sql, *args):
        self._log.append(" ".join(sql.split()))
        if self._fail_on and self._fail_on in sql:
            raise sqlite3.OperationalError("simulated failure inside the clearing transaction")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _trace(monkeypatch, fail_on=None) -> list:
    log: list = []
    real = fts._connect
    monkeypatch.setattr(fts, "_connect", lambda: _Traced(real(), log, fail_on))
    return log


def test_forced_rebuild_drops_and_recreates_with_identical_rows(corpus, monkeypatch):
    assert fts.rebuild(force=True) == N_NOTES
    before, hits = _snapshot(), _hits("kettle")
    assert before["n_fts"] == N_NOTES and hits

    with monkeypatch.context() as m:
        log = _trace(m)
        assert fts.rebuild(force=True) == N_NOTES
    assert "DELETE FROM memory_fts" not in log, "the built index was cleared row by row"
    assert any(s.startswith("DROP TABLE") and "memory_fts" in s for s in log)

    assert _snapshot() == before
    assert _hits("kettle") == hits


def test_recreated_table_keeps_the_fallback_tokenizer(corpus):
    fts.rebuild(force=True)
    # An index made by an older SQLite records the fallback tokenizer: put the index in that state.
    conn = sqlite3.connect(str(fts.FTS_DB))
    conn.execute("DROP TABLE memory_fts")
    conn.execute(fts._CREATE_FTS.format(tokenizer=fts._TOKENIZER_FALLBACK))
    conn.execute("UPDATE atlas_meta SET value=? WHERE key='tokenizer'", (fts._TOKENIZER_FALLBACK + fts._INDEX_TAG,))
    conn.execute("DELETE FROM memory_files")
    conn.commit()
    conn.close()
    assert fts.rebuild(force=True) == N_NOTES
    first = _snapshot()
    assert "remove_diacritics 1" in first["table_sql"]

    assert fts.rebuild(force=True) == N_NOTES
    again = _snapshot()
    assert again == first
    assert "remove_diacritics 1" in again["table_sql"]
    assert dict(again["meta"])["tokenizer"] == fts._TOKENIZER_FALLBACK + fts._INDEX_TAG
    assert _hits("kettle")


def test_failure_between_drop_and_commit_leaves_the_old_index(corpus, monkeypatch):
    fts.rebuild(force=True)
    before, hits = _snapshot(), _hits("kettle")

    with monkeypatch.context() as m:
        log = _trace(m, fail_on="VALUES ('index_format'")
        with pytest.raises(sqlite3.OperationalError, match="simulated failure"):
            fts.rebuild(force=True)
    assert any(s.startswith("DROP TABLE") for s in log), "the failure must come after the drop"

    assert _snapshot() == before
    assert _hits("kettle") == hits
