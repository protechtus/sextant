"""Tests for hybrid semantic search (G-2). Fake embedder; no Ollama in CI."""
from __future__ import annotations

import pytest

from atlas_plus.mcp.memory_server import embeddings


class FakeEmbedder:
    """Deterministic 4-dim embedder: maps known keywords onto axes."""

    AXES = ("auth", "deploy", "memory", "banana")

    def __init__(self):
        self.calls = 0

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        low = text.lower()
        vec = [1.0 if k in low else 0.01 for k in self.AXES]
        return vec


class FailingEmbedder:
    def embed(self, text: str) -> list[float]:
        raise ConnectionError("no ollama here")


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    state = tmp_path / "state"
    state.mkdir()
    for rel, body in {
        "reference/login.md": "How we handle auth and sessions for the app.",
        "projects/ship.md": "Deploy pipeline notes and rollout steps.",
        "reference/fruit.md": "The banana bread recipe collection.",
    }.items():
        p = mem / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\nid: x\n---\n\n{body}\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
    # fts/config freeze MEMORY_ROOT at import; patch the module constants
    from atlas_plus.mcp.memory_server import config, fts

    monkeypatch.setattr(config, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem, raising=False)
    monkeypatch.setattr(fts, "FTS_DB", state / "state.sqlite", raising=False)
    return mem


class TestIndex:
    def test_build_and_incremental(self, corpus):
        emb = FakeEmbedder()
        first = embeddings.rebuild_index(embedder=emb)
        assert first["added"] == 3 and first["failed"] == 0
        again = embeddings.rebuild_index(embedder=emb)
        assert again["added"] == 0 and again["skipped"] == 3

    def test_changed_file_reembeds(self, corpus):
        emb = FakeEmbedder()
        embeddings.rebuild_index(embedder=emb)
        (corpus / "reference" / "login.md").write_text(
            "---\nid: x\n---\n\nauth flows rewritten today\n", encoding="utf-8"
        )
        result = embeddings.rebuild_index(embedder=emb)
        assert result["updated"] == 1

    def test_deleted_file_removed(self, corpus):
        embeddings.rebuild_index(embedder=FakeEmbedder())
        (corpus / "reference" / "fruit.md").unlink()
        result = embeddings.rebuild_index(embedder=FakeEmbedder())
        assert result["removed"] == 1

    def test_embed_failure_counts_not_raises(self, corpus):
        result = embeddings.rebuild_index(embedder=FailingEmbedder())
        assert result["failed"] == 3 and result["added"] == 0

    def test_status(self, corpus):
        embeddings.rebuild_index(embedder=FakeEmbedder())
        status = embeddings.index_status()
        assert status["indexed"] == 3 and status["corpus_files"] == 3


class TestTruncationVisibility:
    """Over-length files embed only their head; the count must be visible
    to the operator, never silent (expert pass finding 10, run 34)."""

    def test_truncated_count_reported(self, corpus):
        from atlas_plus.mcp.memory_server import embeddings as E
        big = corpus / "reference" / "big.md"
        big.write_text("---\nid: b\n---\n\n" + ("memory " * 2000), encoding="utf-8")
        result = E.rebuild_index(embedder=FakeEmbedder())
        assert result["truncated_to_head"] == 1

    def test_small_files_not_counted(self, corpus):
        from atlas_plus.mcp.memory_server import embeddings as E
        result = E.rebuild_index(embedder=FakeEmbedder())
        assert result["truncated_to_head"] == 0


class TestVectorCache:
    """Run-31 cache: unpacked vectors cached on the DB's (mtime_ns, size);
    any DB write invalidates. numpy is an optional fast path -- the pure
    fallback must return identical results."""

    def test_second_query_uses_cache(self, corpus, monkeypatch):
        from atlas_plus.mcp.memory_server import embeddings as E
        E.rebuild_index(embedder=FakeEmbedder())
        E.vector_search("alpha", embedder=FakeEmbedder())
        assert E._vec_cache is not None
        calls = {"n": 0}
        real = E._unpack

        def counting(blob, dim):
            calls["n"] += 1
            return real(blob, dim)

        monkeypatch.setattr(E, "_unpack", counting)
        E.vector_search("alpha", embedder=FakeEmbedder())
        assert calls["n"] == 0, "cache hit must not re-unpack blobs"

    def test_db_write_invalidates_cache(self, corpus):
        from atlas_plus.mcp.memory_server import embeddings as E
        E.rebuild_index(embedder=FakeEmbedder())
        first = E.vector_search("alpha", embedder=FakeEmbedder())
        # a new file + re-index rewrites the DB -> stat key changes
        extra = corpus / "reference" / "new.md"
        extra.write_text("---\nid: n\n---\n\nbrand new zeta topic\n", encoding="utf-8")
        E.rebuild_index(embedder=FakeEmbedder())
        paths, *_ = E._load_vectors()
        assert "reference/new.md" in paths
        assert first  # old result still well-formed

    def test_numpy_and_pure_paths_agree(self, corpus, monkeypatch):
        from atlas_plus.mcp.memory_server import embeddings as E
        E.rebuild_index(embedder=FakeEmbedder())
        with_np = E.vector_search("alpha", embedder=FakeEmbedder())
        monkeypatch.setattr(E, "_np", None)
        E._vec_cache = None  # rebuild cache without the matrix
        without_np = E.vector_search("alpha", embedder=FakeEmbedder())
        assert [r["path"] for r in with_np] == [r["path"] for r in without_np]
        for a, b in zip(with_np, without_np):
            assert abs(a["score"] - b["score"]) < 1e-3


class TestVectorSearch:
    def test_semantic_hit_without_keyword(self, corpus):
        embeddings.rebuild_index(embedder=FakeEmbedder())
        results = embeddings.vector_search("auth question", embedder=FakeEmbedder())
        assert results and results[0]["path"] == "reference/login.md"
        assert 0.0 <= results[0]["score"] <= 1.0

    def test_scope_filter(self, corpus):
        embeddings.rebuild_index(embedder=FakeEmbedder())
        results = embeddings.vector_search(
            "deploy", scope="reference", embedder=FakeEmbedder()
        )
        assert all(r["path"].startswith("reference/") for r in results)

    def test_failure_returns_empty(self, corpus):
        embeddings.rebuild_index(embedder=FakeEmbedder())
        assert embeddings.vector_search("x", embedder=FailingEmbedder()) == []


class TestHybrid:
    def test_hybrid_mode_and_semantic_recall(self, corpus):
        embeddings.rebuild_index(embedder=FakeEmbedder())
        # "auth" never appears in ship.md/fruit.md; query word "auth" hits
        # login.md via BOTH retrievers; mode must be hybrid.
        results, mode = embeddings.hybrid_search("auth", embedder=FakeEmbedder())
        assert mode == "hybrid"
        assert results[0]["path"] == "reference/login.md"
        assert "vector" in results[0]["sources"]
        assert results[0].get("snippet")

    def test_no_index_degrades_to_fts(self, corpus):
        results, mode = embeddings.hybrid_search("auth", embedder=FakeEmbedder())
        assert mode == "fts-only"

    def test_embedder_down_degrades_to_fts(self, corpus):
        embeddings.rebuild_index(embedder=FakeEmbedder())
        results, mode = embeddings.hybrid_search("auth", embedder=FailingEmbedder())
        assert mode == "fts-only"

    def test_kill_switch(self, corpus, monkeypatch):
        embeddings.rebuild_index(embedder=FakeEmbedder())
        monkeypatch.setenv("ATLAS_SEMANTIC_SEARCH", "off")
        results, mode = embeddings.hybrid_search("auth", embedder=FakeEmbedder())
        assert mode == "fts-only"

    def test_scores_stay_in_confidence_band_range(self, corpus):
        embeddings.rebuild_index(embedder=FakeEmbedder())
        results, mode = embeddings.hybrid_search("auth", embedder=FakeEmbedder())
        assert all(0.0 <= r["score"] <= 1.0 for r in results)


class TestSchemeAllowlist:
    """Bandit B310 hardening (quality loop run 7): ATLAS_EMBED_URL is user
    config; file:// and custom schemes must refuse before any urlopen."""

    def test_file_scheme_refused(self, monkeypatch):
        from atlas_plus.mcp.memory_server.embeddings import OllamaEmbedder
        import pytest as _pytest
        with _pytest.raises(ValueError, match="http"):
            OllamaEmbedder(base_url="file:///C:/Windows/win.ini")

    def test_https_accepted(self):
        from atlas_plus.mcp.memory_server.embeddings import OllamaEmbedder
        e = OllamaEmbedder(base_url="https://my-ollama.example:11434")
        assert e.base_url.startswith("https://")


class TestNoLegacyEmbeddingsEndpoint:
    """Static gate (quality loop run 26): newer Ollama hard-errors the legacy
    /api/embeddings endpoint on over-context input instead of truncating --
    which silently dropped every corpus file larger than the embed model's
    context from the semantic index (found 2026-10-04 in TWO call sites:
    the memory embedder and the Brain's Ollama provider). No source file may
    reintroduce the legacy endpoint; /api/embed truncates gracefully."""

    def test_no_source_calls_legacy_endpoint(self):
        import re
        from pathlib import Path
        src_root = Path(__file__).resolve().parents[1] / "src" / "atlas_plus"
        offenders = []
        for py in src_root.rglob("*.py"):
            text = py.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                # URL-literal usage only (the endpoint string closing a
                # quote); comments documenting the ban are fine.
                if re.search(r"""/api/embeddings["']""", line):
                    offenders.append(f"{py.relative_to(src_root)}:{i}")
        assert not offenders, (
            "legacy Ollama /api/embeddings endpoint reintroduced (use "
            f"/api/embed -- it truncates instead of 500ing): {offenders}"
        )
