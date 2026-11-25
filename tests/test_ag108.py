"""AG108 (sweep 30): the search index build reads notes ahead on threads, and `--if-stale` sees every change.

Each test runs the fixed path and checks the outcome; every one fails on 21ef3a6.
  a  the index built through the read-ahead (atlas_plus._readahead) is row for row the index a plain sequential
     reader builds, on a corpus with an unreadable note, an undecodable one, a non-ASCII name and an empty note;
     a full build opens each note exactly once.
  b  an unchanged corpus costs no reads: `rebuild(force=False)` (the command's `--if-stale`) and the search
     path's incremental pass open no note.
  c  a changed, removed, added and renamed note each make `--if-stale` rebuild, and the result equals a fresh
     build of the same corpus (a removed or renamed note answered "already fresh" and kept the old path).
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from atlas_plus import _readahead
from atlas_plus.mcp.memory_server import fts

N_NOTES = 80  # above the read-ahead threshold, so the pool runs


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    (mem / "reference").mkdir(parents=True)
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem, raising=False)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "search.sqlite", raising=False)
    (tmp_path / "state").mkdir()
    for i in range(N_NOTES):
        (mem / "reference" / f"note-{i:03d}.md").write_text(
            f"---\ntitle: note {i}\nupdated: 2026-01-0{1 + i % 9}\n---\nThe staging cluster note {i} "
            + "kettle " * (i % 5) + "\n", encoding="utf-8")
    (mem / "reference" / "café-日本.md").write_text("---\ntitle: Café\n---\n大阪城 kettle\n",
                                                               encoding="utf-8")
    (mem / "reference" / "empty.md").write_text("", encoding="utf-8")
    (mem / "reference" / "bytes.md").write_bytes(b"---\ntitle: raw\n---\nbad \xff\xfe bytes kettle\n")
    (mem / "reference" / "locked.md").write_text("---\ntitle: locked\n---\nnever readable\n", encoding="utf-8")
    return mem


@pytest.fixture
def opened(monkeypatch):
    """Every Path.read_text under the corpus, by name; `locked.md` raises PermissionError."""
    seen: dict[str, int] = {}
    real = Path.read_text

    def read_text(self, *a, **k):
        if self.name.endswith(".md"):
            seen[self.name] = seen.get(self.name, 0) + 1
            if self.name == "locked.md":
                raise PermissionError(13, "denied", str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    return seen


def _rows() -> list:
    conn = sqlite3.connect(str(fts.FTS_DB))
    try:
        files = conn.execute("SELECT path, mtime, last_confirmed, size FROM memory_files ORDER BY path").fetchall()
        body = conn.execute("SELECT f.path, t.frontmatter_json, t.body FROM memory_files f JOIN memory_fts t "
                            "ON t.rowid = f.fts_rowid ORDER BY f.path").fetchall()
        orphans = conn.execute("SELECT COUNT(*) FROM memory_fts WHERE rowid NOT IN "
                               "(SELECT fts_rowid FROM memory_files)").fetchone()[0]
        fmt = conn.execute("SELECT value FROM atlas_meta WHERE key='index_format'").fetchone()
    finally:
        conn.close()
    return [files, body, orphans, fmt]


def _oracle_rows(monkeypatch) -> list:
    """A fresh full build with a plain sequential reader in place of the read-ahead."""
    def sequential(paths, encoding="utf-8", threads=None, read=None):
        for p in paths:
            try:
                yield p, p.read_text(encoding=encoding, errors="replace")
            except OSError as exc:
                yield p, exc

    with monkeypatch.context() as m:
        m.setattr(_readahead, "read_texts", sequential)
        for suffix in ("", "-wal", "-shm"):
            Path(str(fts.FTS_DB) + suffix).unlink(missing_ok=True)
        fts.rebuild(force=True)
        return _rows()


def test_read_ahead_build_equals_sequential_oracle_and_opens_each_note_once(corpus, opened, monkeypatch):
    used = []
    real = _readahead.read_texts
    monkeypatch.setattr(_readahead, "read_texts", lambda paths, *a, **k: (used.append(len(paths)), real(paths, *a, **k))[1])
    n = fts.rebuild(force=True)
    assert n == N_NOTES + 3   # every note but the unreadable one
    assert used and sum(used) == N_NOTES + 4
    assert opened == {name: 1 for name in opened} and len(opened) == N_NOTES + 4
    got = _rows()
    names = [r[0] for r in got[0]]
    assert "reference/café-日本.md" in names and "reference/empty.md" in names
    assert "reference/locked.md" not in names
    assert any("�" in r[2] for r in got[1] if r[0] == "reference/bytes.md")
    assert got == _oracle_rows(monkeypatch)
    hits = [h["path"] for h in fts.search("kettle", limit=50)]
    assert "reference/café-日本.md" in hits and "reference/bytes.md" in hits


def test_unchanged_corpus_reads_nothing(corpus, opened):
    (corpus / "reference" / "locked.md").unlink()  # an unreadable note is retried by every pass (as before)
    fts.rebuild(force=True)
    opened.clear()
    assert fts.rebuild(force=False) == -1
    assert fts.update_incremental() == 0
    assert not opened


@pytest.mark.parametrize("change", ["changed", "removed", "added", "renamed"])
def test_if_stale_sees_every_change(corpus, opened, monkeypatch, change):
    fts.rebuild(force=True)
    ref = corpus / "reference"
    if change == "changed":
        p = ref / "note-005.md"
        p.write_text(p.read_text(encoding="utf-8") + "zebra line\n", encoding="utf-8")
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # coarse clocks: a later mtime, as a real edit
    elif change == "removed":
        (ref / "note-006.md").unlink()
    elif change == "added":
        (ref / "note-new.md").write_text("---\ntitle: new\n---\nzebra arrives\n", encoding="utf-8")
        os.utime(ref / "note-new.md", (1_000_000_000, 1_000_000_000))  # an old mtime: a copied or restored note
    else:
        (ref / "note-007.md").rename(ref / "note-renamed.md")   # a rename keeps the mtime
    assert fts.rebuild(force=False) != -1, "--if-stale said the index was fresh"
    got = _rows()
    assert got == _oracle_rows(monkeypatch)
    paths = [r[0] for r in got[0]]
    assert ("reference/note-006.md" in paths) is (change != "removed")
    assert ("reference/note-007.md" in paths) is (change != "renamed")
