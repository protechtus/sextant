"""AG117 (sweep 35, ticket SEX-052 leftovers).

- Item 9: an index built before AG111 kept the init template's row of identity/user.md as a search
  hit until that note changed or the index was rebuilt. The incremental update now heals that row
  once (keyword index) and the embeddings pass drops a vector whose note became empty to the index.
"""
from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    mem, state = tmp_path / "ws" / ".memory", tmp_path / "ws" / ".atlas"
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
    monkeypatch.setenv("ATLAS_EMBED_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ATLAS_OLLAMA_URL", "http://127.0.0.1:9")
    from atlas_plus import init_cmd
    from atlas_plus.mcp.memory_server import fts

    init_cmd.run_init()
    assert (mem / "identity" / "user.md").is_file()
    (mem / "notes").mkdir(parents=True, exist_ok=True)
    (mem / "notes" / "deploy.md").write_text("# Deploy\n\nThe staging box reboots on Fridays.\n", encoding="utf-8")
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "FTS_DB", state / "ag117-fts.sqlite")
    fts.mark_dirty()
    return mem


def _rows(fts):
    conn = sqlite3.connect(str(fts.FTS_DB))
    try:
        return (sorted(conn.execute("SELECT rowid, path, frontmatter_json, body FROM memory_fts")),
                sorted(conn.execute("SELECT * FROM memory_files")),
                sorted(conn.execute("SELECT * FROM atlas_meta")))
    finally:
        conn.close()


def _build_the_old_way(fts, monkeypatch):
    """The index as the code before AG111 wrote it: the template body indexed as a memory."""
    with monkeypatch.context() as m:
        m.setattr(fts, "without_scaffold", lambda raw: None)
        fts.rebuild(force=True)
        assert [h["path"] for h in fts.search("collaborator")] == ["identity/user.md"]


def test_an_old_index_loses_the_template_hit_on_the_next_incremental_update(corpus, monkeypatch):
    from atlas_plus.mcp.memory_server import fts

    _build_the_old_way(fts, monkeypatch)
    assert fts.update_incremental() == 1          # the identity row, healed once
    assert fts.search("collaborator") == []
    assert fts.search("Introduce yourself") == []
    assert [h["path"] for h in fts.search("staging")] == ["notes/deploy.md"]
    before = _rows(fts)
    assert fts.update_incremental() == 0          # healed: no second write
    assert _rows(fts) == before


def test_an_index_without_the_template_row_is_untouched(corpus):
    from atlas_plus.mcp.memory_server import fts

    fts.rebuild(force=True)
    before = _rows(fts)
    assert fts.update_incremental() == 0
    assert _rows(fts) == before                   # same rows, same meta: nothing written


def test_a_note_whose_own_text_holds_the_phrase_is_not_reindexed_every_pass(corpus):
    from atlas_plus.mcp.memory_server import fts

    user = corpus / "identity" / "user.md"
    user.write_text("---\ntitle: Me\n---\nIntroduce yourself to your own memory, my old onboarding line.\n",
                    encoding="utf-8")
    fts.rebuild(force=True)
    assert [h["path"] for h in fts.search("onboarding")] == ["identity/user.md"]
    before = _rows(fts)
    assert fts.update_incremental() == 0
    assert _rows(fts) == before


def test_facts_appended_under_the_template_survive_the_heal(corpus, monkeypatch):
    from atlas_plus.mcp.memory_server import fts

    user = corpus / "identity" / "user.md"
    user.write_text(user.read_text(encoding="utf-8") + "\nGoes by Sam; on call for the payments team.\n",
                    encoding="utf-8")
    _build_the_old_way(fts, monkeypatch)
    assert fts.update_incremental() == 1
    assert fts.search("collaborator") == []
    assert [h["path"] for h in fts.search("payments")] == ["identity/user.md"]


class _FakeEmbedder:
    def embed(self, text):
        return [float(len(text) % 7 + 1), 1.0, 0.5]


def test_embeddings_drop_the_old_template_vector(corpus, monkeypatch):
    """Sibling path: the semantic index kept the vector it made from the template body."""
    from atlas_plus.mcp.memory_server import embeddings, fts

    fts.rebuild(force=True)
    with monkeypatch.context() as m:
        m.setattr(fts, "without_scaffold", lambda raw: None)   # the pre-AG111 embed pass
        embeddings.rebuild_index(embedder=_FakeEmbedder())

    def paths():
        conn = sqlite3.connect(str(fts.FTS_DB))
        try:
            return sorted(r[0] for r in conn.execute("SELECT path FROM memory_vectors"))
        finally:
            conn.close()

    assert "identity/user.md" in paths()
    res = embeddings.rebuild_index(embedder=_FakeEmbedder())
    assert "identity/user.md" not in paths()
    assert "notes/deploy.md" in paths()
    assert res["removed"] >= 1
