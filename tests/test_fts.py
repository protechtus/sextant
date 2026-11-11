"""Unit tests for mcp/memory_server/fts.py -- SQLite FTS5 index.

Covers: rebuild (force, no-op when fresh, count), search (basic match,
scope filter, empty result, LIKE wildcard escape), and _needs_rebuild logic.
Uses a per-test temp directory for both MEMORY_ROOT and FTS_DB so the real
index is never touched.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_md(path: Path, title: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {path.stem}\ntitle: {title}\ntype: reference\nstatus: active\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _patch_fts(mem_root: Path, fts_db: Path):
    """Patch fts.py module-level paths; returns list of patch context managers."""
    import atlas_plus.mcp.memory_server.fts as fts_mod
    return [
        patch.object(fts_mod, "MEMORY_ROOT", mem_root),
        patch.object(fts_mod, "FTS_DB", fts_db),
    ]


# ---------------------------------------------------------------------------
# rebuild
# ---------------------------------------------------------------------------

class TestRebuild:
    def test_force_rebuild_indexes_all_files(self, tmp_path):
        mem = tmp_path / "memory"
        db = tmp_path / "fts.db"
        _write_md(mem / "feedback" / "style.md", "Style Guide", "Prefers terse responses.")
        _write_md(mem / "reference" / "tools.md", "Tools", "Uses ripgrep for search.")

        patches = _patch_fts(mem, db)
        for p in patches:
            p.start()
        try:
            from atlas_plus.mcp.memory_server.fts import rebuild
            count = rebuild(force=True)
        finally:
            for p in patches:
                p.stop()

        assert count == 2

    def test_noop_when_no_files_changed(self, tmp_path):
        mem = tmp_path / "memory"
        db = tmp_path / "fts.db"
        _write_md(mem / "feedback" / "style.md", "Style", "Some content.")

        patches = _patch_fts(mem, db)
        for p in patches:
            p.start()
        try:
            from atlas_plus.mcp.memory_server.fts import rebuild
            rebuild(force=True)  # initial build
            result = rebuild(force=False)  # should be no-op
        finally:
            for p in patches:
                p.stop()

        assert result == -1  # no-op sentinel

    def test_skips_excluded_dirs(self, tmp_path):
        mem = tmp_path / "memory"
        db = tmp_path / "fts.db"
        _write_md(mem / "feedback" / "real.md", "Real", "Real content here.")
        _write_md(mem / "archive" / "old.md", "Old", "Archived content.")

        patches = _patch_fts(mem, db)
        for p in patches:
            p.start()
        try:
            from atlas_plus.mcp.memory_server.fts import rebuild
            count = rebuild(force=True)
        finally:
            for p in patches:
                p.stop()

        assert count == 1  # archive/ excluded

    def test_empty_memory_root_returns_zero(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        db = tmp_path / "fts.db"

        patches = _patch_fts(mem, db)
        for p in patches:
            p.start()
        try:
            from atlas_plus.mcp.memory_server.fts import rebuild
            count = rebuild(force=True)
        finally:
            for p in patches:
                p.stop()

        assert count == 0


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

class TestSearch:
    def _setup(self, tmp_path: Path):
        mem = tmp_path / "memory"
        db = tmp_path / "fts.db"
        return mem, db

    def test_basic_search_returns_match(self, tmp_path):
        mem, db = self._setup(tmp_path)
        _write_md(mem / "reference" / "ripgrep.md", "Ripgrep", "ripgrep is the preferred search tool for the workspace.")

        patches = _patch_fts(mem, db)
        for p in patches:
            p.start()
        try:
            from atlas_plus.mcp.memory_server.fts import search
            results = search("ripgrep")
        finally:
            for p in patches:
                p.stop()

        assert len(results) >= 1
        assert any("ripgrep" in r["path"] for r in results)

    def test_no_match_returns_empty(self, tmp_path):
        mem, db = self._setup(tmp_path)
        _write_md(mem / "feedback" / "style.md", "Style", "Prefers terse responses only.")

        patches = _patch_fts(mem, db)
        for p in patches:
            p.start()
        try:
            from atlas_plus.mcp.memory_server.fts import search
            results = search("completelydifferenttermnomatch")
        finally:
            for p in patches:
                p.stop()

        assert results == []

    def test_scope_filter_restricts_results(self, tmp_path):
        mem, db = self._setup(tmp_path)
        _write_md(mem / "feedback" / "search.md", "Search Feedback", "prefers ripgrep for searching content")
        _write_md(mem / "reference" / "search.md", "Search Ref", "prefers ripgrep for searching reference docs")

        patches = _patch_fts(mem, db)
        for p in patches:
            p.start()
        try:
            from atlas_plus.mcp.memory_server.fts import search
            all_results = search("ripgrep")
            scoped_results = search("ripgrep", scope="feedback")
        finally:
            for p in patches:
                p.stop()

        assert len(all_results) == 2
        assert len(scoped_results) == 1
        assert all(r["path"].startswith("feedback/") for r in scoped_results)

    def test_scope_with_like_wildcards_escaped(self, tmp_path):
        mem, db = self._setup(tmp_path)
        # Create a scope name that looks like a LIKE pattern -- search should not crash
        risky_scope = "feed%back"
        _write_md(mem / "feedback" / "item.md", "Item", "some content about memory")

        patches = _patch_fts(mem, db)
        for p in patches:
            p.start()
        try:
            from atlas_plus.mcp.memory_server.fts import search
            # This should not raise -- the % in scope should be escaped
            results = search("memory", scope=risky_scope)
        finally:
            for p in patches:
                p.stop()

        # No match expected since scope doesn't actually exist with that name
        assert isinstance(results, list)

    def test_result_has_expected_keys(self, tmp_path):
        mem, db = self._setup(tmp_path)
        _write_md(mem / "reference" / "atlas.md", "Atlas", "Atlas is the memory system for cross-LLM context.")

        patches = _patch_fts(mem, db)
        for p in patches:
            p.start()
        try:
            from atlas_plus.mcp.memory_server.fts import search
            results = search("Atlas")
        finally:
            for p in patches:
                p.stop()

        assert len(results) >= 1
        for r in results:
            assert "path" in r
            assert "snippet" in r
            assert "score" in r

    def test_empty_memory_returns_empty(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        db = tmp_path / "fts.db"

        patches = _patch_fts(mem, db)
        for p in patches:
            p.start()
        try:
            from atlas_plus.mcp.memory_server.fts import search
            results = search("anything")
        finally:
            for p in patches:
                p.stop()

        assert results == []


# ---------------------------------------------------------------------------
# _confirmed_score
# ---------------------------------------------------------------------------

class TestConfirmedScore:
    """Tests for the last_confirmed scoring helper."""

    def test_none_returns_neutral(self):
        import time
        from atlas_plus.mcp.memory_server.fts import _confirmed_score, _CONFIRMED_NEUTRAL
        result = _confirmed_score(None, time.time())
        assert result == _CONFIRMED_NEUTRAL

    def test_very_recent_returns_one(self):
        import time
        from atlas_plus.mcp.memory_server.fts import _confirmed_score
        # Confirmed 1 day ago
        ts = time.time() - 24 * 3600
        result = _confirmed_score(ts, time.time())
        assert result == 1.0

    def test_confirmed_today_returns_one(self):
        import time
        from atlas_plus.mcp.memory_server.fts import _confirmed_score
        result = _confirmed_score(time.time(), time.time())
        assert result == 1.0

    def test_confirmed_over_year_returns_zero(self):
        import time
        from atlas_plus.mcp.memory_server.fts import _confirmed_score
        # Confirmed 2 years ago
        ts = time.time() - 2 * 365 * 24 * 3600
        result = _confirmed_score(ts, time.time())
        assert result == 0.0

    def test_confirmed_midpoint_returns_partial(self):
        import time
        from atlas_plus.mcp.memory_server.fts import _confirmed_score, _CONFIRMED_FULL_BOOST_SECS, _CONFIRMED_MAX_AGE_SECS
        # Midpoint between 30d and 365d
        mid_secs = (_CONFIRMED_FULL_BOOST_SECS + _CONFIRMED_MAX_AGE_SECS) / 2
        ts = time.time() - mid_secs
        result = _confirmed_score(ts, time.time())
        assert 0.0 < result < 1.0

    def test_scores_decrease_with_age(self):
        import time
        from atlas_plus.mcp.memory_server.fts import _confirmed_score
        now = time.time()
        ts_recent = now - 60 * 24 * 3600   # 60 days ago
        ts_old    = now - 200 * 24 * 3600  # 200 days ago
        assert _confirmed_score(ts_recent, now) > _confirmed_score(ts_old, now)


# ---------------------------------------------------------------------------
# _parse_confirmed_ts
# ---------------------------------------------------------------------------

class TestParseConfirmedTs:
    def test_parses_iso_date(self):
        from atlas_plus.mcp.memory_server.fts import _parse_confirmed_ts
        result = _parse_confirmed_ts({"last_confirmed": "2026-05-20"})
        assert result is not None
        assert result > 0

    def test_returns_none_when_missing(self):
        from atlas_plus.mcp.memory_server.fts import _parse_confirmed_ts
        result = _parse_confirmed_ts({})
        assert result is None

    def test_returns_none_on_invalid_date(self):
        from atlas_plus.mcp.memory_server.fts import _parse_confirmed_ts
        result = _parse_confirmed_ts({"last_confirmed": "not-a-date"})
        assert result is None

    def test_accepts_full_iso_datetime(self):
        from atlas_plus.mcp.memory_server.fts import _parse_confirmed_ts
        # Should use only the date portion
        result = _parse_confirmed_ts({"last_confirmed": "2026-05-20T14:00:00+00:00"})
        assert result is not None


# ---------------------------------------------------------------------------
# rebuild stores last_confirmed
# ---------------------------------------------------------------------------

class TestRebuildLastConfirmed:
    def test_rebuild_stores_confirmed_ts_from_frontmatter(self, tmp_path):
        import sqlite3
        mem = tmp_path / "memory"
        db_path = tmp_path / "fts.db"
        # Write a file with last_confirmed in frontmatter
        p = mem / "feedback" / "confirmed.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "---\nid: test\ntype: feedback\nlast_confirmed: 2026-05-01\n---\n\n"
            "Some confirmed feedback about coding style and practices.",
            encoding="utf-8",
        )
        patches = _patch_fts(mem, db_path)
        for pt in patches:
            pt.start()
        try:
            from atlas_plus.mcp.memory_server.fts import rebuild
            rebuild(force=True)
            conn = sqlite3.connect(str(db_path))
            row = conn.execute(
                "SELECT last_confirmed FROM memory_files WHERE path=?",
                ("feedback/confirmed.md",),
            ).fetchone()
            conn.close()
        finally:
            for pt in patches:
                pt.stop()
        assert row is not None
        assert row[0] is not None  # last_confirmed should be stored as unix timestamp

    def test_rebuild_stores_null_when_no_last_confirmed(self, tmp_path):
        import sqlite3
        mem = tmp_path / "memory"
        db_path = tmp_path / "fts.db"
        p = mem / "reference" / "unconfirmed.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "---\nid: test\ntype: reference\n---\n\n"
            "Some reference content without any confirmation date.",
            encoding="utf-8",
        )
        patches = _patch_fts(mem, db_path)
        for pt in patches:
            pt.start()
        try:
            from atlas_plus.mcp.memory_server.fts import rebuild
            rebuild(force=True)
            conn = sqlite3.connect(str(db_path))
            row = conn.execute(
                "SELECT last_confirmed FROM memory_files WHERE path=?",
                ("reference/unconfirmed.md",),
            ).fetchone()
            conn.close()
        finally:
            for pt in patches:
                pt.stop()
        assert row is not None
        assert row[0] is None  # no last_confirmed frontmatter field


class TestIncrementalUpdate:
    """ROADMAP item 4: search-path freshness re-indexes ONLY changed files.
    The old path ran a full rebuild when any one file changed -- the
    documented cliff at large corpus size."""

    def _world(self, tmp_path, monkeypatch):
        import atlas_plus.mcp.memory_server.fts as fts
        mem = tmp_path / "mem"
        (mem / "reference").mkdir(parents=True)
        (mem / "reference" / "a.md").write_text(
            "---\ntitle: A\n---\n\nalpha aardvark", encoding="utf-8")
        (mem / "reference" / "b.md").write_text(
            "---\ntitle: B\n---\n\nbeta binturong", encoding="utf-8")
        monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
        monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "fts.sqlite")
        fts.rebuild(force=True)
        return fts, mem

    def test_noop_when_fresh(self, tmp_path, monkeypatch):
        fts, _ = self._world(tmp_path, monkeypatch)
        assert fts.update_incremental() == 0

    def test_new_file_indexed_without_touching_others(self, tmp_path, monkeypatch):
        import os, sqlite3
        fts, mem = self._world(tmp_path, monkeypatch)
        conn = sqlite3.connect(str(fts.FTS_DB))
        before = dict(conn.execute("SELECT path, indexed FROM memory_files"))
        conn.close()
        (mem / "reference" / "c.md").write_text(
            "---\ntitle: C\n---\n\ngamma capybara", encoding="utf-8")
        assert fts.update_incremental() == 1
        assert any(r["path"] == "reference/c.md" for r in fts.search("capybara"))
        conn = sqlite3.connect(str(fts.FTS_DB))
        after = dict(conn.execute("SELECT path, indexed FROM memory_files"))
        conn.close()
        # untouched rows keep their original indexed stamp = they were not re-read
        assert after["reference/a.md"] == before["reference/a.md"]
        assert after["reference/b.md"] == before["reference/b.md"]

    def test_modified_file_reindexed(self, tmp_path, monkeypatch):
        import os, time as _t
        fts, mem = self._world(tmp_path, monkeypatch)
        f = mem / "reference" / "a.md"
        f.write_text("---\ntitle: A\n---\n\nalpha zephyr", encoding="utf-8")
        os.utime(f, (_t.time() + 5, _t.time() + 5))  # force mtime change on coarse FS clocks
        assert fts.update_incremental() == 1
        assert any(r["path"] == "reference/a.md" for r in fts.search("zephyr"))
        assert not any(r["path"] == "reference/a.md" for r in fts.search("aardvark"))

    def test_deleted_file_dropped(self, tmp_path, monkeypatch):
        fts, mem = self._world(tmp_path, monkeypatch)
        (mem / "reference" / "b.md").unlink()
        assert fts.update_incremental() == 1
        assert not fts.search("binturong")

    def test_empty_index_falls_back_to_full_rebuild(self, tmp_path, monkeypatch):
        import atlas_plus.mcp.memory_server.fts as fts
        mem = tmp_path / "mem"
        (mem / "reference").mkdir(parents=True)
        (mem / "reference" / "solo.md").write_text(
            "---\ntitle: S\n---\n\nsolitary quokka", encoding="utf-8")
        monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
        monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "fts.sqlite")
        assert fts.update_incremental() == 1  # full-rebuild fallback, no deadlock
        assert any(r["path"] == "reference/solo.md" for r in fts.search("quokka"))


class TestWalkParity:
    """The scandir fast path must see exactly the files rglob saw --
    exclusions included (quality loop v2 run 1)."""

    def test_stats_walk_matches_walk(self, tmp_path, monkeypatch):
        import atlas_plus.mcp.memory_server.fts as fts
        mem = tmp_path / "mem"
        (mem / "reference").mkdir(parents=True)
        (mem / "reference" / "a.md").write_text("---\nt: a\n---\nx", encoding="utf-8")
        (mem / "projects").mkdir()
        (mem / "projects" / "b.md").write_text("---\nt: b\n---\ny", encoding="utf-8")
        (mem / "reference" / "research").mkdir()  # excluded mirror subtree
        (mem / "reference" / "research" / "c.md").write_text("z", encoding="utf-8")
        for d in fts.EXCLUDED_DIRS:
            (mem / d).mkdir(exist_ok=True)
            (mem / d / "d.md").write_text("w", encoding="utf-8")
            break
        monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
        slow = {rel for rel, _ in fts._walk_memory()}
        fast = {rel for rel, _m, _s in fts._walk_memory_stats()}
        assert fast == slow
        assert "reference/research/c.md" not in fast

    def test_stats_walk_mtimes_accurate(self, tmp_path, monkeypatch):
        import atlas_plus.mcp.memory_server.fts as fts
        mem = tmp_path / "mem"
        (mem / "reference").mkdir(parents=True)
        f = mem / "reference" / "a.md"
        f.write_text("x", encoding="utf-8")
        monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
        [(rel, mtime, size)] = list(fts._walk_memory_stats())
        assert rel == "reference/a.md"
        assert mtime == f.stat().st_mtime
        assert size == f.stat().st_size


class TestQuerySyntaxFallback:
    """Impatient-persona finding (quality loop run 4): human text that happens
    to be invalid FTS5 syntax must fall back to a literal phrase search, not
    fail with a message blaming a 'corrupt' index."""

    def _world(self, tmp_path, monkeypatch):
        import atlas_plus.mcp.memory_server.fts as fts
        mem = tmp_path / "mem"
        (mem / "reference").mkdir(parents=True)
        (mem / "reference" / "a.md").write_text(
            '---\ntitle: A\n---\n\nthe NEAR/3 syntax "quoted" (parens) note about capybaras',
            encoding="utf-8")
        monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
        monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "fts.sqlite")
        fts.rebuild(force=True)
        return fts

    def test_unbalanced_quote_falls_back(self, tmp_path, monkeypatch):
        fts = self._world(tmp_path, monkeypatch)
        assert fts.search('"quoted') == [] or True  # must not raise
        # A query that IS findable as a phrase still returns the hit:
        hits = fts.search('note about capybaras (parens')
        # fallback quotes the whole thing as a phrase; that exact phrase does
        # not exist, so empty is acceptable -- the contract is NO exception
        # and NO corrupt-index warning path. Findable phrase proves retry ran:
        assert fts.search('NEAR/3 syntax')  # operator-looking human text

    def test_operator_soup_returns_cleanly(self, tmp_path, monkeypatch):
        fts = self._world(tmp_path, monkeypatch)
        assert isinstance(fts.search("NEAR/3 AND OR NOT ("), list)


class TestSameMtimeRewrite:
    """Expert review 2026-09-27: mtime alone misses a same-mtime rewrite on
    coarse-timestamp mounts; the (mtime, size) pair catches size changes."""

    def test_same_mtime_different_size_reindexed(self, tmp_path, monkeypatch):
        import os
        import atlas_plus.mcp.memory_server.fts as fts
        mem = tmp_path / "mem"
        (mem / "reference").mkdir(parents=True)
        f = mem / "reference" / "a.md"
        f.write_text("---\nt: a\n---\noriginal words", encoding="utf-8")
        monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
        monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "fts.sqlite")
        fts.rebuild(force=True)
        frozen = f.stat().st_mtime
        f.write_text("---\nt: a\n---\nreplacement porcupine", encoding="utf-8")
        os.utime(f, (frozen, frozen))  # simulate a coarse-mount same-mtime rewrite
        assert fts.update_incremental() >= 1
        assert any(r["path"] == "reference/a.md" for r in fts.search("porcupine"))


class TestMalformedSelfHeal:
    """FTS5 shadow-table corruption found LIVE on the operator's index
    (run 39): passes PRAGMA integrity_check, throws 'database disk image
    is malformed' on every MATCH, and escaped the OperationalError-only
    guard. The index is derived -- a corrupt one self-heals by rebuild."""

    def _world(self, tmp_path, monkeypatch):
        import atlas_plus.mcp.memory_server.fts as fts
        mem = tmp_path / "mem"
        (mem / "reference").mkdir(parents=True)
        (mem / "reference" / "a.md").write_text(
            "---\nt: a\n---\nthe resilient capybara fact", encoding="utf-8")
        monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
        monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state.sqlite")
        fts.rebuild(force=True)
        return fts

    def _corrupt_shadow(self, fts, total=False):
        import sqlite3
        conn = sqlite3.connect(str(fts.FTS_DB))
        try:
            if total:
                conn.execute("UPDATE memory_fts_data SET block = X'DEADBEEFDEADBEEF'")
            else:
                # Realistic partial corruption (what the live index had):
                # MATCH throws, DROP still succeeds -> in-place heal.
                conn.execute("UPDATE memory_fts_data SET block = X'DEADBEEF'"
                             " WHERE id = (SELECT MAX(id) FROM memory_fts_data)")
            conn.commit()
        finally:
            conn.close()

    def test_search_self_heals_corrupt_shadow_tables(self, tmp_path, monkeypatch):
        fts = self._world(tmp_path, monkeypatch)
        self._corrupt_shadow(fts)
        import sqlite3
        conn = sqlite3.connect(str(fts.FTS_DB))
        try:
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute("SELECT * FROM memory_fts WHERE memory_fts MATCH 'capybara'").fetchall()
        finally:
            conn.close()
        results = fts.search("capybara")  # must heal + answer, not raise
        assert any("a.md" in r["path"] for r in results)

    def test_total_corruption_recovers_via_quarantine(self, tmp_path, monkeypatch):
        """When even DROP throws (file-level damage), the DB is
        quarantined (renamed, never deleted) and search still answers
        from a fresh rebuild.

        The damage must be FILE-level (not-a-database bytes): trashing
        FTS5 shadow rows stopped forcing this branch on sqlite >= 3.50,
        which drops corrupted FTS tables cleanly and heals IN PLACE
        (observed on Python 3.14 / sqlite 3.50.4, 2026-10-09 -- a
        better outcome, and the in-place path has its own test above).
        Overwriting the file header forces the quarantine branch on
        every sqlite version."""
        fts = self._world(tmp_path, monkeypatch)
        fts.FTS_DB.write_bytes(b"this is not a sqlite database -- file-level corruption")
        for suffix in ("-wal", "-shm"):
            side = fts.FTS_DB.with_name(fts.FTS_DB.name + suffix)
            if side.exists():
                side.unlink()
        results = fts.search("capybara")
        assert any("a.md" in r["path"] for r in results)
        quarantined = list(tmp_path.glob("state.sqlite.corrupt-*"))
        assert len(quarantined) == 1, "corrupt file must be quarantined, not deleted"
        # The evidence survives with its bytes intact.
        assert quarantined[0].read_bytes().startswith(b"this is not a sqlite")

    def test_trashed_shadow_rows_still_heal_and_answer(self, tmp_path, monkeypatch):
        """Fully-trashed FTS5 shadow blocks heal on every sqlite version:
        3.42 raises on DROP and quarantines; 3.50+ drops cleanly and heals
        in place. Either way search answers and no data path is left dark."""
        fts = self._world(tmp_path, monkeypatch)
        self._corrupt_shadow(fts, total=True)
        results = fts.search("capybara")
        assert any("a.md" in r["path"] for r in results)

    def test_coresident_tables_survive_the_heal(self, tmp_path, monkeypatch):
        """The vectors share the DB file; an in-place heal must not
        destroy them (quarantine is only for file-level corruption)."""
        import sqlite3
        fts = self._world(tmp_path, monkeypatch)
        conn = sqlite3.connect(str(fts.FTS_DB))
        conn.execute("CREATE TABLE IF NOT EXISTS memory_vectors"
                     " (path TEXT PRIMARY KEY, content_hash TEXT, dim INT, vec BLOB)")
        conn.execute("INSERT INTO memory_vectors VALUES ('x.md', 'h', 2, X'0000')")
        conn.commit()
        conn.close()
        self._corrupt_shadow(fts)
        fts.search("capybara")
        conn = sqlite3.connect(str(fts.FTS_DB))
        try:
            n = conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]
        finally:
            conn.close()
        assert n == 1, "in-place heal destroyed a co-resident table"


class TestConfidenceBandScores:
    """Launch blocker (competitive-sweep raw/18a, fixed run 38): a single
    FTS hit skipped re-ranking and kept its raw NEGATIVE FTS5 rank, which
    the server's bands read as 'starved' -- one perfect unique match told
    the model to ask for clarification instead of answering."""

    def _bands(self, score):
        if score < 0.2:
            return "starved"
        if score < 0.4:
            return "low"
        if score < 0.7:
            return "medium"
        return "high"

    def test_single_hit_scores_in_zero_one_and_is_not_starved(self, tmp_path, monkeypatch):
        import atlas_plus.mcp.memory_server.fts as fts
        mem = tmp_path / "mem"
        (mem / "reference").mkdir(parents=True)
        (mem / "reference" / "only.md").write_text(
            "---\nt: x\n---\nthe unique zygomorphic fact", encoding="utf-8")
        monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
        monkeypatch.setattr(fts, "FTS_DB", tmp_path / "f.sqlite")
        fts.rebuild(force=True)
        r = fts.search("zygomorphic")
        assert len(r) == 1
        score = float(r[0]["score"])
        assert 0.0 <= score <= 1.2  # authority multiplier can exceed 1.0
        assert self._bands(min(score, 1.0)) in ("medium", "high"), (
            f"lone perfect hit must answer, not starve (score={score})")

    def test_all_results_carry_normalized_scores(self, tmp_path, monkeypatch):
        import atlas_plus.mcp.memory_server.fts as fts
        mem = tmp_path / "mem"
        (mem / "reference").mkdir(parents=True)
        for i in range(3):
            (mem / "reference" / f"n{i}.md").write_text(
                f"---\nt: x\n---\nquince marmalade recipe {i}", encoding="utf-8")
        monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
        monkeypatch.setattr(fts, "FTS_DB", tmp_path / "f.sqlite")
        fts.rebuild(force=True)
        for r in fts.search("quince"):
            assert float(r["score"]) >= 0.0, "raw negative FTS rank leaked"


class TestFreshnessDebounce:
    """The per-search freshness sweep debounces (130ms of every 200ms search
    at ~2,200 files, quality loop run 27): bursts pay once, mark_dirty()
    (wired to the write primitive) voids the window so remember-then-search
    stays exact."""

    def _world(self, tmp_path, monkeypatch):
        import atlas_plus.mcp.memory_server.fts as fts
        mem = tmp_path / "mem"
        (mem / "reference").mkdir(parents=True)
        (mem / "reference" / "a.md").write_text(
            "---\nt: a\n---\nquokka facts", encoding="utf-8")
        monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
        monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "fts.sqlite")
        fts.rebuild(force=True)
        fts.mark_dirty()  # each test starts with a void window
        return fts, mem

    def _count_sweeps(self, fts, monkeypatch):
        calls = {"n": 0}
        real = fts.update_incremental

        def counting():
            calls["n"] += 1
            return real()

        monkeypatch.setattr(fts, "update_incremental", counting)
        return calls

    def test_burst_sweeps_once(self, tmp_path, monkeypatch):
        fts, _ = self._world(tmp_path, monkeypatch)
        calls = self._count_sweeps(fts, monkeypatch)
        for _ in range(5):
            fts.search("quokka")
        assert calls["n"] == 1

    def test_mark_dirty_forces_resweep(self, tmp_path, monkeypatch):
        fts, _ = self._world(tmp_path, monkeypatch)
        calls = self._count_sweeps(fts, monkeypatch)
        fts.search("quokka")
        fts.mark_dirty()
        fts.search("quokka")
        assert calls["n"] == 2

    def test_write_primitive_voids_window(self, tmp_path, monkeypatch):
        fts, mem = self._world(tmp_path, monkeypatch)
        fts.search("quokka")  # arm the window
        from atlas_plus.mcp.memory_server import routing
        monkeypatch.setattr(routing, "MEMORY_ROOT", mem)
        routing.append_fact(mem / "reference" / "fresh.md",
                            "brand new xylophone fact",
                            {"type": "reference"}, interactive=True)
        # The debounced window must NOT hide the write we just made.
        assert any(r["path"] == "reference/fresh.md"
                   for r in fts.search("xylophone"))

    def test_window_zero_disables_debounce(self, tmp_path, monkeypatch):
        fts, _ = self._world(tmp_path, monkeypatch)
        monkeypatch.setenv("ATLAS_FTS_FRESHNESS_WINDOW", "0")
        calls = self._count_sweeps(fts, monkeypatch)
        fts.search("quokka")
        fts.search("quokka")
        assert calls["n"] == 2

    def test_window_inf_is_capped(self, tmp_path, monkeypatch):
        """'inf' must not disable freshness forever (expert pass 2026-10-05)."""
        fts, _ = self._world(tmp_path, monkeypatch)
        monkeypatch.setenv("ATLAS_FTS_FRESHNESS_WINDOW", "inf")
        assert fts._freshness_window() == 3600.0

    def test_sweep_does_not_mask_concurrent_mark_dirty(self, tmp_path, monkeypatch):
        """A mark_dirty() landing DURING a sweep must survive it (expert
        pass 2026-10-05: zeroing a timestamp let the finishing sweep stamp
        over the writer's signal; generations do not)."""
        fts, _ = self._world(tmp_path, monkeypatch)
        real = fts.update_incremental
        calls = {"n": 0}

        def sweeping_while_written():
            calls["n"] += 1
            if calls["n"] == 1:
                fts.mark_dirty()  # a writer lands mid-sweep
            return real()

        monkeypatch.setattr(fts, "update_incremental", sweeping_while_written)
        fts.search("quokka")   # sweep 1; write lands during it
        fts.search("quokka")   # must sweep AGAIN for the mid-sweep write
        assert calls["n"] == 2

    def test_all_write_paths_void_window(self, tmp_path, monkeypatch):
        """Expert pass 2026-10-05 M1: decisions, anti-memories, and
        confirms must be searchable immediately, not only append_fact."""
        from unittest.mock import patch as _patch
        fts, mem = self._world(tmp_path, monkeypatch)
        from atlas_plus.mcp.memory_server import routing

        monkeypatch.setattr(routing, "MEMORY_ROOT", mem)
        # arm the window
        fts.search("quokka")

        # 1. decision tool
        with _patch("atlas_plus.mcp.memory_server.tools.decision.MEMORY_ROOT", mem), \
             _patch("atlas_plus.mcp.memory_server.tools.decision.require_autonomous_writes", lambda _: None), \
             _patch("atlas_plus.mcp.memory_server.tools.decision.version_before_write", return_value=None):
            from atlas_plus.mcp.memory_server.tools.decision import memory_remember_decision
            memory_remember_decision(title="Pick zebra lib", decision="zebra wins",
                                     conditions=None, revisit_if=None, project="t")
        assert any("zebra" in r.get("snippet", "") or "pick-zebra" in r["path"]
                   for r in fts.search("zebra")), "decision invisible to search"

        # 2. confirm_fact (goes through _atomic_write_text)
        fts.search("quokka")  # re-arm
        target = mem / "reference" / "a.md"
        assert routing.confirm_fact(target)
        # the window was voided; next search sweeps (observable via counter)
        calls = self._count_sweeps(fts, monkeypatch)
        fts.search("quokka")
        assert calls["n"] == 1, "confirm_fact did not void the window"
