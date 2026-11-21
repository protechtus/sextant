"""AG99 (sweep 26): index truth.

Each test runs the fixed path and checks the outcome; every one fails on 91964d6.
  1  provenance is metadata, not text: `<!-- source:... -->` tags and the frontmatter stamps (source, confidence,
     updated, imported_*) no longer make `human`, `direct`, `source` or `high` hit every note -- keyword search
     (fts, MCP tool, dashboard), the embedding text and `sextant context` matching -- while a body word `source`,
     a writer's own `source:` sentence and the title are still found.
  2  an interrupted index build keeps its progress: rows are committed in batches with their memory_files rows,
     the index carries `building:<format>` until complete, and the next search (or `sextant-fts-rebuild`) resumes
     instead of starting over -- the first build, the format-change rebuild and the incremental pass (a pull that
     lands many notes) alike.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap

import pytest

os.environ.setdefault("ATLAS_WEB_API_KEY", "test-key-abc")

from fastapi.testclient import TestClient  # noqa: E402

import atlas_plus.web.app as webapp  # noqa: E402

client = TestClient(webapp.app, raise_server_exceptions=False)
AUTH = {"Authorization": f"Bearer {webapp.API_KEY}"}


def _pin_corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("atlas_plus.web.app.BRAIN_URL", "http://127.0.0.1:9", raising=False)
    from atlas_plus.mcp.memory_server import config as _config, fts, routing as _routing, versioning as _versioning
    monkeypatch.setattr(_config, "MEMORY_ROOT", tmp_path / "mem")
    monkeypatch.setattr(_routing, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    monkeypatch.setattr(_routing, "ROUTING_CONFIG", tmp_path / "mem" / "_atlas" / "routing.yaml", raising=False)
    monkeypatch.setattr(_versioning, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "search.sqlite", raising=False)
    monkeypatch.setattr(fts, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    monkeypatch.setattr(webapp, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    from atlas_plus import init_cmd
    init_cmd.run_init()
    fts.mark_dirty()
    return tmp_path / "mem"


def _remembered_and_imported(tmp_path, monkeypatch):
    mem = _pin_corpus(tmp_path, monkeypatch)
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    for scope, fact in (("project:kitchen", "Kettle model boils water in ninety seconds."),
                        ("reference:bakery", "The bakery opens at seven on weekdays."),
                        ("reference:notes", "Bike chain wants oil every month.")):
        r = memory_remember(scope, fact, source="human-direct", session_id="s1", task_id="t1", interactive=True)
        assert r["ok"], r
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "garden.md").write_text("---\ntitle: Garden plan\n---\nPlant tomatoes in May.\n", encoding="utf-8")
    (vault / "cite.md").write_text("---\ntitle: River notes\nsource: Field notes of Ada, p. 4\n---\n"
                                   "The source of the river is a spring.\n", encoding="utf-8")
    from atlas_plus import import_cmd
    out = import_cmd.import_obsidian(vault, "reference:obsidian", execute=True)
    assert out.get("imported"), out
    from atlas_plus.mcp.memory_server import fts
    fts.mark_dirty()
    return mem


PROVENANCE_WORDS = ("human", "direct", "high", "s1", "t1", "confidence", "imported")


# ---------------------------------------------------------------- 1: provenance is not text
def test_provenance_words_hit_no_note(tmp_path, monkeypatch):
    _remembered_and_imported(tmp_path, monkeypatch)
    from atlas_plus.mcp.memory_server import fts

    for word in PROVENANCE_WORDS:
        assert fts.search(word, limit=20) == [], word
    # `source` is in every remembered note's tag and every imported note's frontmatter KEY; only cite.md says it
    assert [h["path"] for h in fts.search("source", limit=20)] == ["reference/obsidian/cite.md"]
    # the content, a writer's own `source:` sentence and the title are still found
    assert [h["path"] for h in fts.search("kettle")] == ["projects/kitchen.md"]
    assert [h["path"] for h in fts.search("tomatoes")] == ["reference/obsidian/garden.md"]
    assert [h["path"] for h in fts.search("Ada")] == ["reference/obsidian/cite.md"]
    assert [h["path"] for h in fts.search("Garden plan")] == ["reference/obsidian/garden.md"]
    snip = fts.search("kettle")[0]["snippet"]
    assert "Kettle" in snip and "human" not in snip and "  " not in snip


def test_mcp_tool_and_dashboard_agree(tmp_path, monkeypatch):
    _remembered_and_imported(tmp_path, monkeypatch)
    from atlas_plus.mcp.memory_server import server

    assert server.memory_search_tool("human")["results"] == []
    assert [r["path"] for r in server.memory_search_tool("bakery")["results"]] == ["reference/bakery.md"]
    r = client.get("/api/memory/search", params={"q": "direct"}, headers=AUTH)
    assert r.status_code == 200 and "kitchen.md" not in r.text and "bakery.md" not in r.text, r.text
    r = client.get("/api/memory/search", params={"q": "bakery"}, headers=AUTH)
    assert r.status_code == 200 and "bakery.md" in r.text


def test_the_marker_is_still_in_the_file_and_readable(tmp_path, monkeypatch):
    mem = _remembered_and_imported(tmp_path, monkeypatch)
    from atlas_plus.mcp.memory_server import fts
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    assert fts.search("human") == [] and [h["path"] for h in fts.search("kettle")] == ["projects/kitchen.md"]
    text = (mem / "projects" / "kitchen.md").read_text(encoding="utf-8")
    assert "<!-- source:human-direct session:s1 task:t1 -->" in text and "source: human-direct" in text
    # the code that reads the marker still sees it: the same fact again is "already remembered"
    again = memory_remember("project:kitchen", "Kettle model boils water in ninety seconds.",
                            source="human-direct", interactive=True)
    assert again["ok"] and again["already_remembered"]


def test_embedding_text_and_context_matching_skip_the_tag(tmp_path, monkeypatch):
    mem = _remembered_and_imported(tmp_path, monkeypatch)
    from atlas_plus.hooks import prime
    from atlas_plus.mcp.memory_server import embeddings

    body = embeddings._body_of(mem / "reference" / "bakery.md")
    assert "bakery opens" in body and "source:" not in body and "human" not in body
    monkeypatch.setattr(prime, "_related_facts", lambda *a, **k: [])
    out = prime.build_prime_context("direct human", memory_root=mem)
    assert "## Matching notes" not in out, out
    out = prime.build_prime_context("bakery weekdays", memory_root=mem)
    assert "reference/bakery.md" in out


# ---------------------------------------------------------------- 2: an interrupted build keeps its progress
def _many(mem, n, start=0):
    d = mem / "projects" / "bulk"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(start, start + n):
        (d / f"n{i:03d}.md").write_text(f"---\ntitle: Bulk {i}\n---\nword{i} shared text\n", encoding="utf-8")


def _rows(db):
    c = sqlite3.connect(str(db))
    try:
        files = c.execute("SELECT path, fts_rowid FROM memory_files").fetchall()
        fts_rows = c.execute("SELECT rowid, path FROM memory_fts").fetchall()
        fmt = c.execute("SELECT value FROM atlas_meta WHERE key='index_format'").fetchone()
        last = c.execute("SELECT value FROM atlas_meta WHERE key='last_indexed'").fetchone()
    finally:
        c.close()
    return files, fts_rows, (fmt[0] if fmt else None), (last[0] if last else None)


def _consistent(db):
    """Every committed note has exactly one FTS row, the one memory_files names; no orphan rows."""
    files, fts_rows, _f, _l = _rows(db)
    by_rowid = dict(fts_rows)
    assert len(fts_rows) == len(files) == len({p for _r, p in fts_rows})
    assert all(by_rowid.get(rid) == path for path, rid in files)
    return len(files)


_KILLER = textwrap.dedent("""
    import os, sys
    from atlas_plus.mcp.memory_server import fts
    fts._BATCH_NOTES = 5
    calls = {"n": 0}
    real = fts._parse_md
    def dying(raw):
        calls["n"] += 1
        if calls["n"] > int(sys.argv[1]):
            os._exit(9)          # a hard kill: no rollback, no finally, like closing the terminal
        return real(raw)
    fts._parse_md = dying
    fts.search("word1")
""")


def _killed_first_search(tmp_path, mem, after):
    env = dict(os.environ, ATLAS_MEMORY_ROOT=str(mem), ATLAS_STATE_DIR=str(tmp_path / "state"),
               ATLAS_WORKSPACE_ROOT=str(tmp_path))
    r = subprocess.run([sys.executable, "-c", _KILLER, str(after)], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 9, r.stderr


def test_a_killed_first_build_keeps_its_batches_and_resumes(tmp_path, monkeypatch):
    mem = _pin_corpus(tmp_path, monkeypatch)
    _many(mem, 40)
    from atlas_plus.mcp.memory_server import fts

    db = tmp_path / "state" / "search.sqlite"
    _killed_first_search(tmp_path, mem, after=23)
    kept = _consistent(db)
    assert kept >= 20, kept                                  # four batches of five survived the kill
    assert _rows(db)[2] == "building:" + fts.INDEX_FORMAT and _rows(db)[3] is None
    assert fts._needs_rebuild()                               # an unfinished build is never "built"
    parsed = []
    real_body = fts._index_body
    monkeypatch.setattr(fts, "_index_body", lambda fm, body: (parsed.append(1), real_body(fm, body))[1])
    fts.mark_dirty()
    hits = fts.search("word39")
    assert [h["path"] for h in hits] == ["projects/bulk/n039.md"]
    total = _consistent(db)
    assert len(parsed) == total - kept                        # resumed: only the notes the kill left
    assert _rows(db)[2] == fts.INDEX_FORMAT and _rows(db)[3] is not None
    assert len(fts.search("shared", limit=50)) == 40


def test_fts_rebuild_command_resumes_an_interrupted_build(tmp_path, monkeypatch):
    mem = _pin_corpus(tmp_path, monkeypatch)
    _many(mem, 30)
    from atlas_plus.mcp.memory_server import fts

    db = tmp_path / "state" / "search.sqlite"
    _killed_first_search(tmp_path, mem, after=12)
    kept = _consistent(db)
    assert kept >= 10
    parsed = []
    real_body = fts._index_body
    monkeypatch.setattr(fts, "_index_body", lambda fm, body: (parsed.append(1), real_body(fm, body))[1])
    n = fts.rebuild(force=True)                               # `sextant-fts-rebuild`
    assert n == _consistent(db) and len(parsed) == n - kept
    assert _rows(db)[2] == fts.INDEX_FORMAT
    parsed.clear()
    assert fts.rebuild(force=True) == n and len(parsed) == n   # a finished index rebuilds from scratch, as before


def test_format_change_rebuild_and_big_incremental_pass_keep_progress(tmp_path, monkeypatch):
    mem = _pin_corpus(tmp_path, monkeypatch)
    _many(mem, 10)
    from atlas_plus.mcp.memory_server import fts

    db = tmp_path / "state" / "search.sqlite"
    fts.mark_dirty()
    assert len(fts.search("shared", limit=50)) == 10
    old_last = _rows(db)[3]
    # a pull lands 30 notes at once; the pass is interrupted after 17 of them
    _many(mem, 30, start=10)
    monkeypatch.setattr(fts, "_BATCH_NOTES", 5, raising=False)
    real = fts._parse_md
    calls = {"n": 0}

    def dying(raw):
        calls["n"] += 1
        if calls["n"] > 17:
            raise KeyboardInterrupt
        return real(raw)
    monkeypatch.setattr(fts, "_parse_md", dying)
    with pytest.raises(KeyboardInterrupt):
        fts.update_incremental()
    assert _consistent(db) >= 25                              # 10 old + three batches of the new ones
    assert _rows(db)[3] == old_last                           # the pass is not recorded as complete
    monkeypatch.setattr(fts, "_parse_md", real)
    fts.update_incremental()
    assert _consistent(db) == 41 and len(fts.search("shared", limit=50)) == 40
    # a format change: the rebuild it triggers is interrupted, then resumed by the next search
    c = sqlite3.connect(str(db))
    c.execute("UPDATE atlas_meta SET value='0' WHERE key='index_format'")
    c.commit()
    c.close()
    calls["n"] = 0
    monkeypatch.setattr(fts, "_parse_md", dying)
    fts.mark_dirty()
    with pytest.raises(KeyboardInterrupt):
        fts.search("shared")
    kept = _consistent(db)
    assert kept >= 15 and _rows(db)[2] == "building:" + fts.INDEX_FORMAT
    parsed = []
    monkeypatch.setattr(fts, "_parse_md", real)
    real_body = fts._index_body
    monkeypatch.setattr(fts, "_index_body", lambda fm, body: (parsed.append(1), real_body(fm, body))[1])
    fts.mark_dirty()
    assert len(fts.search("shared", limit=50)) == 40
    assert len(parsed) == 41 - kept and _rows(db)[2] == fts.INDEX_FORMAT
