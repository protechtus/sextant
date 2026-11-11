"""The reference/research mirror is excluded from memory_server read/index paths (M1).

reference/research mirrors the standalone research library (~54% of the .memory corpus) and
has its own dedicated RAG (research_search_tool). It must not be indexed into the general
memory FTS or surfaced in list/catalog/topics, while real reference/* memory (glossary,
people, machines) is kept. Single source of truth: curation.scan.is_excluded_mirror.
"""
from __future__ import annotations


def _make_corpus(root):
    (root / "feedback").mkdir(parents=True, exist_ok=True)
    (root / "feedback" / "real.md").write_text("---\ntags: [alpha]\n---\nreal body\n", encoding="utf-8")
    (root / "reference").mkdir(parents=True, exist_ok=True)
    # a real, curatable reference file -- must be KEPT
    (root / "reference" / "glossary.md").write_text("---\ntags: [beta]\n---\nglossary\n", encoding="utf-8")
    # the mirror subtree -- must be EXCLUDED
    (root / "reference" / "research").mkdir(parents=True, exist_ok=True)
    (root / "reference" / "research" / "mirror.md").write_text("---\ntags: [gamma]\n---\nmirror\n", encoding="utf-8")


def test_fts_walk_excludes_mirror(tmp_path, monkeypatch):
    from atlas_plus.mcp.memory_server import fts
    _make_corpus(tmp_path)
    monkeypatch.setattr(fts, "MEMORY_ROOT", tmp_path)
    rels = [rel for rel, _ in fts._walk_memory()]
    assert "feedback/real.md" in rels
    assert "reference/glossary.md" in rels            # real reference memory kept
    assert all("reference/research" not in r for r in rels)   # mirror excluded


def test_memory_list_excludes_mirror(tmp_path, monkeypatch):
    from atlas_plus.mcp.memory_server.tools import list as listmod
    _make_corpus(tmp_path)
    monkeypatch.setattr(listmod, "MEMORY_ROOT", tmp_path)
    listmod._cache.clear()
    listmod._cache_ts.clear()
    paths = {r["path"] for r in listmod.memory_list(limit=200)}
    assert "feedback/real.md" in paths
    assert "reference/glossary.md" in paths
    assert all("reference/research" not in p for p in paths)


def test_topics_tool_excludes_mirror(tmp_path, monkeypatch):
    # memory_topics_tool aggregates tags across the corpus; the mirror's tag (gamma) must
    # not appear, while real files' tags (alpha, beta) do.
    from atlas_plus.mcp.memory_server import fts, server
    _make_corpus(tmp_path)
    monkeypatch.setattr(server, "MEMORY_ROOT", tmp_path)
    # The tool now aggregates from the FTS index (quality loop run 13), so
    # the index modules need the same tmp-world patch (frozen-import class).
    monkeypatch.setattr(fts, "MEMORY_ROOT", tmp_path)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "_state" / "fts.sqlite")
    fts.rebuild(force=True)
    fn = getattr(server.memory_topics_tool, "fn", server.memory_topics_tool)  # unwrap if FastMCP-wrapped
    out = fn(limit=50)
    tags = {t["tag"] for t in out["tags"]}
    assert "alpha" in tags and "beta" in tags
    assert "gamma" not in tags
