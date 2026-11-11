"""Tests for atlas_plus.curation.rederive -- re-derivable memory detection.

Covers _tokenize, _parse_frontmatter_body, _index_doc_files,
RederiveCandidate.as_dict, and find_rederivable.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# TestTokenize
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_extracts_words_min_length_4(self):
        from atlas_plus.curation.rederive import _tokenize
        result = _tokenize("SIP registration and auth flow")
        assert "registration" in result
        assert "auth" in result
        assert "flow" in result
        # "and" (3 chars) should NOT be included
        assert "and" not in result

    def test_lowercases(self):
        from atlas_plus.curation.rederive import _tokenize
        result = _tokenize("FastAPI WebSocket")
        assert "fastapi" in result
        assert "websocket" in result

    def test_handles_hyphens_and_underscores(self):
        from atlas_plus.curation.rederive import _tokenize
        result = _tokenize("rate-limit auto_scaling")
        assert any("rate" in w or "limit" in w for w in result)

    def test_returns_set(self):
        from atlas_plus.curation.rederive import _tokenize
        result = _tokenize("test test test again")
        assert isinstance(result, set)
        # Sets deduplicate
        assert len([x for x in result if x == "test"]) <= 1

    def test_empty_string(self):
        from atlas_plus.curation.rederive import _tokenize
        assert _tokenize("") == set()


# ---------------------------------------------------------------------------
# TestParseFrontmatterBody
# ---------------------------------------------------------------------------

class TestParseFrontmatterBody:
    def test_parses_frontmatter_and_body(self, tmp_path):
        from atlas_plus.curation.rederive import _parse_frontmatter_body
        p = tmp_path / "test.md"
        p.write_text("---\ntitle: Test\ntype: feedback\n---\n\nThis is the body.", encoding="utf-8")
        fm, body = _parse_frontmatter_body(p)
        assert fm["title"] == "Test"
        assert fm["type"] == "feedback"
        assert "body" in body.lower()

    def test_returns_empty_for_missing_file(self, tmp_path):
        from atlas_plus.curation.rederive import _parse_frontmatter_body
        missing = tmp_path / "missing.md"
        fm, body = _parse_frontmatter_body(missing)
        assert fm == {}
        assert body == ""

    def test_returns_raw_as_body_for_no_frontmatter(self, tmp_path):
        from atlas_plus.curation.rederive import _parse_frontmatter_body
        p = tmp_path / "plain.md"
        p.write_text("# No frontmatter\n\nContent here.", encoding="utf-8")
        fm, body = _parse_frontmatter_body(p)
        assert fm == {}
        assert "Content here" in body

    def test_returns_raw_for_unclosed_frontmatter(self, tmp_path):
        from atlas_plus.curation.rederive import _parse_frontmatter_body
        p = tmp_path / "bad.md"
        p.write_text("---\ntitle: No closing\n", encoding="utf-8")
        fm, body = _parse_frontmatter_body(p)
        assert fm == {}


# ---------------------------------------------------------------------------
# TestIndexDocFiles
# ---------------------------------------------------------------------------

class TestIndexDocFiles:
    def test_returns_empty_for_no_matching_files(self, tmp_path):
        from atlas_plus.curation.rederive import _index_doc_files
        result = _index_doc_files(tmp_path, ["*.md"])
        assert result == []

    def test_indexes_files_with_enough_tokens(self, tmp_path):
        from atlas_plus.curation.rederive import _index_doc_files
        doc = tmp_path / "README.md"
        # Need 20+ tokens -- write a substantive doc
        doc.write_text(
            "# Project README\n\nThis project provides authentication middleware "
            "for FastAPI applications. It supports OAuth, JWT tokens, session "
            "management, and rate limiting. Configuration is done via environment "
            "variables or a config file. Installation requires Python 3.11 and pip.\n",
            encoding="utf-8",
        )
        result = _index_doc_files(tmp_path, ["*.md"])
        assert len(result) == 1
        path, tokens = result[0]
        assert path == doc
        assert "fastapi" in tokens or "authentication" in tokens

    def test_skips_files_with_too_few_tokens(self, tmp_path):
        from atlas_plus.curation.rederive import _index_doc_files
        tiny = tmp_path / "tiny.md"
        tiny.write_text("Short.", encoding="utf-8")
        result = _index_doc_files(tmp_path, ["*.md"])
        assert result == []

    def test_respects_max_files(self, tmp_path):
        from atlas_plus.curation.rederive import _index_doc_files
        content = "word " * 30  # 30 tokens
        for i in range(5):
            (tmp_path / f"doc{i}.md").write_text(content, encoding="utf-8")
        result = _index_doc_files(tmp_path, ["*.md"], max_files=2)
        assert len(result) <= 2


# ---------------------------------------------------------------------------
# TestRederiveCandidate
# ---------------------------------------------------------------------------

class TestRederiveCandidate:
    def test_as_dict_contains_expected_keys(self):
        from atlas_plus.curation.rederive import RederiveCandidate
        c = RederiveCandidate(
            path="feedback/test.md",
            title="Test Fact",
            memory_type="feedback",
            matched_doc="README.md",
            similarity=0.456,
            sample_overlap=["fastapi", "authentication", "middleware", "session", "config", "extra"],
        )
        d = c.as_dict()
        assert d["path"] == "feedback/test.md"
        assert d["title"] == "Test Fact"
        assert d["similarity"] == 0.456
        assert d["matched_doc"] == "README.md"

    def test_as_dict_truncates_sample_overlap_to_5(self):
        from atlas_plus.curation.rederive import RederiveCandidate
        c = RederiveCandidate(
            path="test.md", title="T", memory_type="feedback",
            matched_doc="doc.md", similarity=0.5,
            sample_overlap=["a", "b", "c", "d", "e", "f", "g"],
        )
        assert len(c.as_dict()["sample_overlap"]) == 5

    def test_as_dict_rounds_similarity(self):
        from atlas_plus.curation.rederive import RederiveCandidate
        c = RederiveCandidate(
            path="test.md", title="T", memory_type="feedback",
            matched_doc="doc.md", similarity=0.12345678,
            sample_overlap=[],
        )
        assert c.as_dict()["similarity"] == 0.123


# ---------------------------------------------------------------------------
# TestFindRederivable
# ---------------------------------------------------------------------------

class TestFindRederivable:
    def test_returns_empty_when_no_doc_files(self, tmp_path):
        from atlas_plus.curation.rederive import find_rederivable
        mem_root = tmp_path / "memory"
        mem_root.mkdir()
        ws_root = tmp_path / "workspace"
        ws_root.mkdir()
        with patch("atlas_plus.curation.rederive.MEMORY_ROOT", mem_root):
            result = find_rederivable(workspace_root=ws_root)
        assert result == []

    def test_returns_empty_when_memory_root_missing(self, tmp_path):
        from atlas_plus.curation.rederive import find_rederivable
        mem_root = tmp_path / "missing-memory"
        ws_root = tmp_path / "workspace"
        ws_root.mkdir()
        # Create doc file with enough tokens
        content = "word " * 30
        (ws_root / "README.md").write_text(content, encoding="utf-8")
        with patch("atlas_plus.curation.rederive.MEMORY_ROOT", mem_root):
            result = find_rederivable(workspace_root=ws_root)
        assert result == []

    def test_flags_high_overlap_memory_file(self, tmp_path):
        from atlas_plus.curation.rederive import find_rederivable
        mem_root = tmp_path / "memory"
        feedback = mem_root / "feedback"
        feedback.mkdir(parents=True)
        ws_root = tmp_path / "workspace"
        ws_root.mkdir()

        # Build 25+ distinct tokens (each >= 4 chars) for the shared content block.
        shared_words = (
            "fastapi websocket authentication authorization middleware "
            "configuration installation environment variables ratelimit "
            "bootstrap retry timeout decorator handler request response "
            "session token refresh bearer claims payload validation "
            "database postgres sqlite redis cache invalidate"
        )
        doc = ws_root / "README.md"
        doc.write_text(f"# Project\n\n{shared_words}\n", encoding="utf-8")

        # Memory file contains mostly the same vocabulary -> high Jaccard overlap.
        mem_file = feedback / "fastapi-auth.md"
        mem_file.write_text(
            f"---\ntitle: FastAPI Auth Notes\ntype: feedback\n---\n\n{shared_words}\n",
            encoding="utf-8",
        )

        with patch("atlas_plus.curation.rederive.MEMORY_ROOT", mem_root), \
             patch("atlas_plus.curation.rederive._DOC_GLOBS", ["*.md"]):
            result = find_rederivable(threshold=0.3, workspace_root=ws_root)

        assert len(result) >= 1
        assert result[0].path is not None
        assert result[0].similarity >= 0.3


# Reused content with 25+ distinct tokens (>= 4 chars) to clear the < 20-token index filter.
_VOCAB = (
    "fastapi websocket authentication authorization middleware "
    "configuration installation environment variables ratelimit "
    "bootstrap retry timeout decorator handler request response "
    "session token refresh bearer claims payload validation "
    "database postgres sqlite redis cache invalidate"
)


class TestIndexDocFilesPruning:
    """_index_doc_files must prune vendored/heavy dirs from recursive (**) globs."""

    def test_prunes_venv_and_versions(self, tmp_path):
        from atlas_plus.curation.rederive import _index_doc_files
        docs = tmp_path / "projects" / "p" / "docs"
        docs.mkdir(parents=True)
        (docs / "guide.md").write_text(_VOCAB, encoding="utf-8")
        venv = tmp_path / "projects" / "p" / ".venv" / "Lib" / "site-packages"
        venv.mkdir(parents=True)
        (venv / "lib.md").write_text(_VOCAB, encoding="utf-8")
        versions = tmp_path / "projects" / "p" / "versions"
        versions.mkdir(parents=True)
        (versions / "old.md").write_text(_VOCAB, encoding="utf-8")

        result = _index_doc_files(tmp_path, ["projects/**/*.md"])
        paths = {p.as_posix() for p, _ in result}

        assert any(pp.endswith("docs/guide.md") for pp in paths)
        assert not any(".venv" in pp for pp in paths)
        assert not any("/versions/" in pp for pp in paths)

    def test_bounded_glob_unchanged(self, tmp_path):
        from atlas_plus.curation.rederive import _index_doc_files
        (tmp_path / "README.md").write_text(_VOCAB, encoding="utf-8")
        result = _index_doc_files(tmp_path, ["*.md"])
        assert len(result) == 1
        assert result[0][0].name == "README.md"


class TestFindRederivableMirrorExclusion:
    """find_rederivable must skip the reference/research mirror on the memory side."""

    def test_research_mirror_not_flagged(self, tmp_path):
        from atlas_plus.curation.rederive import find_rederivable
        ws_root = tmp_path / "workspace"
        ws_root.mkdir()
        (ws_root / "README.md").write_text(f"# Project\n\n{_VOCAB}\n", encoding="utf-8")

        mem_root = tmp_path / "memory"
        mirror = mem_root / "reference" / "research"
        mirror.mkdir(parents=True)
        (mirror / "mirror.md").write_text(
            f"---\ntitle: Mirror\ntype: reference\n---\n\n{_VOCAB}\n", encoding="utf-8")
        (mem_root / "reference" / "real.md").write_text(
            f"---\ntitle: Real\ntype: reference\n---\n\n{_VOCAB}\n", encoding="utf-8")

        with patch("atlas_plus.curation.rederive.MEMORY_ROOT", mem_root), \
             patch("atlas_plus.curation.rederive._DOC_GLOBS", ["*.md"]):
            result = find_rederivable(threshold=0.3, workspace_root=ws_root)

        paths = {c.path for c in result}
        assert not any("reference/research/" in p for p in paths)  # mirror skipped
        assert any("real.md" in p for p in paths)                  # non-mirror still scanned
