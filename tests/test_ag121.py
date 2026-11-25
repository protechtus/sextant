"""AG121 (sweep 36, MN93 F1/F3, lesson LES-092): a parse change heals the rows an older build wrote.

An index built before AG119 kept a rule-line top block out of a note's indexed body; the incremental update
only compared (mtime, size), so after an upgrade the block's words gave "No hits" until a forced rebuild. The
index now carries a parse revision tied to `last_indexed`; an older or missing stamp makes the next incremental
update re-index exactly the notes whose row the current parser would write differently, once.
"""
from __future__ import annotations

import sqlite3

import pytest

_RULE_TOP = "---\nThis top block is a plain sentence about the quokkarule.\n---\nThe body under it.\n"


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    mem, state = tmp_path / "ws" / ".memory", tmp_path / "ws" / ".atlas"
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
    monkeypatch.setenv("ATLAS_EMBED_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ATLAS_OLLAMA_URL", "http://127.0.0.1:9")
    from atlas_plus.mcp.memory_server import fts

    notes = {
        "rules/rule-0.md": _RULE_TOP,
        "rules/rule-1.md": "---\nA second sentence names the marmotrule.\n---\nMore body.\n",
        "lists/top.md": "---\n- alpha\n- pelicanlist\n---\nBody under a list.\n",
        "notes/fm.md": "---\ntitle: Router\ntags: [homelab]\n---\nThe router firmware is 4.2.\n",
        "notes/cjk.md": "東京の天気は晴れです。\n",
    }
    for i in range(6):
        notes[f"notes/plain-{i}.md"] = f"# Plain {i}\n\nThe staging box {i} reboots on Fridays.\n"
    for rel, text in notes.items():
        p = mem / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "FTS_DB", state / "ag121-fts.sqlite")
    fts.mark_dirty()
    return mem


def _old_parse(fts):
    """`_parse_md` as 0.1.0 had it: a top block that is not key/value pairs was dropped from the body."""
    cur = fts._parse_md

    def parse(raw):
        fm_json, body, ts = cur(raw)
        if fm_json == "{}" and raw.startswith("---"):
            end = raw.find("\n---", 3)
            if end != -1:
                body = raw[end + 4:].strip()
        return fm_json, body, ts
    return parse


def _forget_stamp(fts):
    conn = sqlite3.connect(str(fts.FTS_DB))
    try:
        conn.execute("DELETE FROM atlas_meta WHERE key='parse_revision'")   # 0.1.0 never wrote the key
        conn.commit()
    finally:
        conn.close()


def _build_the_old_way(fts, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(fts, "_parse_md", _old_parse(fts))
        fts.rebuild(force=True)
        assert fts.search("quokkarule") == []
    _forget_stamp(fts)


def _rows(fts):
    conn = sqlite3.connect(str(fts.FTS_DB))
    try:
        return (sorted(conn.execute("SELECT rowid, path, frontmatter_json, body FROM memory_fts")),
                sorted(conn.execute("SELECT * FROM memory_files")),
                sorted(conn.execute("SELECT * FROM atlas_meta")))
    finally:
        conn.close()


def _count_reads(fts, monkeypatch):
    from atlas_plus import _readahead
    seen = []
    real = _readahead.read_texts

    def counting(paths, *a, **k):
        paths = list(paths)
        seen.extend(paths)
        return real(paths, *a, **k)
    monkeypatch.setattr(_readahead, "read_texts", counting)
    return seen


def _count_writes(fts, monkeypatch):
    written = []
    real = fts._parse_md

    def parse(raw):
        written.append(raw)
        return real(raw)
    monkeypatch.setattr(fts, "_parse_md", parse)
    return written


def test_an_old_index_heals_exactly_the_affected_notes_once(corpus, monkeypatch):
    from atlas_plus.mcp.memory_server import fts

    _build_the_old_way(fts, monkeypatch)
    assert fts.update_incremental() == 3      # the two rule-line notes and the list top block, nothing else
    assert [h["path"] for h in fts.search("quokkarule")] == ["rules/rule-0.md"]
    assert [h["path"] for h in fts.search("marmotrule")] == ["rules/rule-1.md"]
    assert [h["path"] for h in fts.search("pelicanlist")] == ["lists/top.md"]
    assert len(fts.search("staging")) == 6
    before = _rows(fts)
    reads = _count_reads(fts, monkeypatch)
    assert fts.update_incremental() == 0      # healed: the second pass writes nothing ...
    assert reads == []                        # ... and reads nothing
    assert _rows(fts) == before


def test_an_index_this_build_wrote_never_runs_the_pass(corpus, monkeypatch):
    from atlas_plus.mcp.memory_server import fts

    fts.rebuild(force=True)
    before = _rows(fts)
    reads = _count_reads(fts, monkeypatch)
    assert fts.update_incremental() == 0
    assert reads == []
    assert _rows(fts) == before


def test_an_index_of_ordinary_notes_reindexes_nothing(corpus, monkeypatch):
    from atlas_plus.mcp.memory_server import fts

    for p in list((corpus / "rules").iterdir()) + [corpus / "lists" / "top.md"]:
        p.unlink()
    _build_the_old_way_ok = fts.rebuild(force=True)
    assert _build_the_old_way_ok == 8
    _forget_stamp(fts)
    writes = _count_writes(fts, monkeypatch)
    rows = _rows(fts)[:2]
    assert fts.update_incremental() == 0      # every row already matches: read and compared, none rewritten
    assert len(writes) == 8                   # one parse per note, the healing comparison only
    assert _rows(fts)[:2] == rows
    writes.clear()
    assert fts.update_incremental() == 0
    assert writes == []                       # stamped now: the pass never runs again


def test_a_killed_healing_pass_resumes(corpus, monkeypatch):
    from atlas_plus.mcp.memory_server import fts

    _build_the_old_way(fts, monkeypatch)
    monkeypatch.setattr(fts, "_BATCH_NOTES", 1)   # commit after every healed note
    real = fts._index_body
    calls = {"n": 0}

    def dying(fm_json, body):
        calls["n"] += 1
        if calls["n"] == 11 + 2:   # 11 comparisons, then the second healed row dies
            raise KeyboardInterrupt
        return real(fm_json, body)
    with monkeypatch.context() as m:
        m.setattr(fts, "_index_body", dying)
        with pytest.raises(KeyboardInterrupt):
            fts.update_incremental()
    conn = sqlite3.connect(str(fts.FTS_DB))
    try:
        assert conn.execute("SELECT value FROM atlas_meta WHERE key='parse_revision'").fetchone() is None
    finally:
        conn.close()
    assert fts.update_incremental() == 2          # the one healed before the kill is not written again
    assert [h["path"] for h in fts.search("quokkarule")] == ["rules/rule-0.md"]
    assert [h["path"] for h in fts.search("pelicanlist")] == ["lists/top.md"]
    assert fts.update_incremental() == 0


def test_a_rebuild_by_an_older_build_is_healed_again(corpus, monkeypatch):
    """MN93 F3: 0.1.0 keeps the key it does not know but rewrites `last_indexed`; its old-style rows heal."""
    from atlas_plus.mcp.memory_server import fts

    fts.rebuild(force=True)
    assert [h["path"] for h in fts.search("quokkarule")] == ["rules/rule-0.md"]
    conn = sqlite3.connect(str(fts.FTS_DB))
    try:
        stamp = conn.execute("SELECT value FROM atlas_meta WHERE key='parse_revision'").fetchone()
    finally:
        conn.close()
    with monkeypatch.context() as m:   # the old build's rebuild: old parse, the key it does not know left as it was
        m.setattr(fts, "_parse_md", _old_parse(fts))
        fts.rebuild(force=True)
        conn = sqlite3.connect(str(fts.FTS_DB))
        try:
            if stamp is not None:
                conn.execute("INSERT OR REPLACE INTO atlas_meta(key,value) VALUES ('parse_revision',?)", stamp)
            conn.commit()
        finally:
            conn.close()
    fts.mark_dirty()
    assert [h["path"] for h in fts.search("quokkarule")] == ["rules/rule-0.md"]


def test_if_stale_sees_an_old_parse_stamp(corpus, monkeypatch):
    """`sextant-fts-rebuild --if-stale` said "already fresh" on a 0.1.0-built index (MN93 F1)."""
    from atlas_plus.mcp.memory_server import fts

    _build_the_old_way(fts, monkeypatch)
    assert fts._needs_rebuild() is True
    assert fts.rebuild(force=False) == 11
    assert [h["path"] for h in fts.search("quokkarule")] == ["rules/rule-0.md"]
    assert fts._needs_rebuild() is False


class _FakeEmbedder:
    def __init__(self):
        self.texts = []

    def embed(self, text):
        self.texts.append(text)
        return [float(len(text) % 7 + 1), 1.0, 0.5]


def test_embeddings_embed_the_top_block_and_reembed_only_those_notes(corpus, monkeypatch):
    """Sibling: the semantic pass cut every top block; its content hash now differs for exactly those notes."""
    from atlas_plus.mcp.memory_server import embeddings, fts

    fts.rebuild(force=True)
    with monkeypatch.context() as m:
        m.setattr(fts, "_parse_md", _old_parse(fts))   # the old embed text
        embeddings.rebuild_index(embedder=_FakeEmbedder())
    emb = _FakeEmbedder()
    res = embeddings.rebuild_index(embedder=emb)
    assert res["updated"] == 3, res
    assert any("quokkarule" in t for t in emb.texts)
    assert any("pelicanlist" in t for t in emb.texts)
    emb2 = _FakeEmbedder()
    assert embeddings.rebuild_index(embedder=emb2)["updated"] == 0 and emb2.texts == []


def test_the_healing_pass_says_one_line_before_a_large_corpus(corpus, monkeypatch, capsys):
    """Added at the fold: the pass read 20,000 notes for 35 s without a line (the first search after an update
    looked hung). Above the threshold it prints one line on stderr, once; a stamped index prints nothing."""
    from atlas_plus.mcp.memory_server import fts
    fts.rebuild(force=True)
    monkeypatch.setattr(fts, "_HEAL_NOTICE_MIN_NOTES", 3)
    capsys.readouterr()
    assert fts.update_incremental() == 0
    assert "checking" not in capsys.readouterr().err          # this build wrote the index: no pass, no line
    _forget_stamp(fts)                                        # as an index an older build wrote
    fts.mark_dirty()
    assert fts.update_incremental() == 0
    err = capsys.readouterr().err
    assert err.count("notes against the search index after an update") == 1 and "checking 11 notes" in err
    fts.mark_dirty()
    assert fts.update_incremental() == 0
    assert "checking" not in capsys.readouterr().err          # once
