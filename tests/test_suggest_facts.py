"""Tests for atlas suggest-facts (P3-T6) transcript-to-memory pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas_plus.curation.suggest_facts import (
    _extract_candidates,
    _find_similar,
    _jaccard,
    _load_memory_corpus,
    _tokenize,
    suggest_facts,
)


# ---------------------------------------------------------------------------
# _tokenize and _jaccard
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_basic(self):
        tokens = _tokenize("integration tests hit real database")
        assert "integration" in tokens
        assert "database" in tokens
        assert "the" not in tokens

    def test_strips_frontmatter(self):
        text = "---\ntitle: x\n---\ndatabase migrations"
        assert "database" in _tokenize(text)
        assert "title" not in _tokenize(text)


class TestJaccard:
    def test_identical(self):
        s = {"a", "b", "c"}
        assert _jaccard(s, s) == 1.0

    def test_disjoint(self):
        assert _jaccard({"a"}, {"b"}) == 0.0

    def test_partial(self):
        a = {"a", "b", "c"}
        b = {"b", "c", "d"}
        assert abs(_jaccard(a, b) - 2 / 4) < 1e-9

    def test_empty_both(self):
        assert _jaccard(set(), set()) == 1.0


# ---------------------------------------------------------------------------
# _load_memory_corpus
# ---------------------------------------------------------------------------

class TestLoadMemoryCorpus:
    def test_loads_files(self, tmp_path):
        (tmp_path / "feedback").mkdir()
        (tmp_path / "feedback" / "testing.md").write_text(
            "---\ntitle: Testing\n---\nDo not mock the database.", encoding="utf-8"
        )
        corpus = _load_memory_corpus(tmp_path)
        paths = [p for p, _ in corpus]
        assert any("testing.md" in p for p in paths)

    def test_excludes_archive(self, tmp_path):
        (tmp_path / "archive").mkdir()
        (tmp_path / "archive" / "old.md").write_text("old stuff", encoding="utf-8")
        corpus = _load_memory_corpus(tmp_path)
        paths = [p for p, _ in corpus]
        assert not any("archive" in p for p in paths)

    def test_excludes_underscore_files(self, tmp_path):
        (tmp_path / "_history.md").write_text("history", encoding="utf-8")
        corpus = _load_memory_corpus(tmp_path)
        paths = [p for p, _ in corpus]
        assert not any("_history" in p for p in paths)


# ---------------------------------------------------------------------------
# _find_similar
# ---------------------------------------------------------------------------

class TestFindSimilar:
    def test_returns_similar(self):
        corpus = [("feedback/testing.md", {"integration", "tests", "database", "real"})]
        results = _find_similar("integration tests use a real database", corpus)
        assert len(results) == 1
        assert results[0][0] >= 0.40

    def test_no_match_below_threshold(self):
        corpus = [("reference/examplecorp.md", {"voip", "platform", "examplecorp"})]
        results = _find_similar("completely unrelated topic here", corpus)
        assert len(results) == 0


# ---------------------------------------------------------------------------
# _extract_candidates
# ---------------------------------------------------------------------------

class TestExtractCandidates:
    def _make_llm_response(self, candidates: list[dict]) -> str:
        return json.dumps(candidates)

    def test_valid_candidates_returned(self):
        llm_out = self._make_llm_response([
            {
                "type": "feedback",
                "scope": "feedback/testing",
                "title": "No database mocking",
                "fact": "Integration tests must hit a real database. Mocks hide migration bugs.",
                "confidence": 0.95,
            }
        ])
        with patch("atlas_plus.summarizer.providers.complete", return_value=(llm_out, "ollama")):
            candidates, rejected = _extract_candidates("USER: ran tests. ASSISTANT: mocked database.")
        assert len(candidates) == 1
        assert candidates[0]["scope"] == "feedback/testing"
        assert candidates[0]["confidence"] == 0.95
        assert rejected == 0

    def test_low_confidence_filtered(self):
        llm_out = self._make_llm_response([
            {
                "type": "feedback",
                "scope": "feedback/testing",
                "title": "Low confidence fact",
                "fact": "Maybe use real DB.",
                "confidence": 0.55,
            }
        ])
        with patch("atlas_plus.summarizer.providers.complete", return_value=(llm_out, "ollama")):
            candidates, rejected = _extract_candidates("some transcript")
        assert len(candidates) == 0
        assert rejected == 1

    def test_empty_transcript_returns_empty(self):
        candidates, rejected = _extract_candidates("")
        assert candidates == []
        assert rejected == 0

    def test_llm_returns_empty_array(self):
        with patch("atlas_plus.summarizer.providers.complete", return_value=("[]", "ollama")):
            candidates, rejected = _extract_candidates("some transcript")
        assert candidates == []
        assert rejected == 0

    def test_malformed_json_is_a_failed_run(self):
        import pytest as _pt
        with patch("atlas_plus.summarizer.providers.complete", return_value=("not json at all", "ollama")):
            with _pt.raises(RuntimeError):  # a junk reply must not mark the session done (AG43)
                _extract_candidates("some transcript")

    def test_missing_fact_or_scope_filtered(self):
        llm_out = self._make_llm_response([
            {"type": "feedback", "scope": "", "title": "x", "fact": "something", "confidence": 0.9},
            {"type": "feedback", "scope": "feedback/x", "title": "y", "fact": "", "confidence": 0.9},
        ])
        with patch("atlas_plus.summarizer.providers.complete", return_value=(llm_out, "ollama")):
            candidates, rejected = _extract_candidates("transcript")
        assert candidates == []


# ---------------------------------------------------------------------------
# suggest_facts integration (dry-run, no LLM, no writes)
# ---------------------------------------------------------------------------

class TestSuggestFactsIntegration:
    def test_no_transcript_content(self, tmp_path):
        with patch("atlas_plus.curation.suggest_facts._load_transcript_text", return_value=""):
            stats = suggest_facts(memory_root=tmp_path)
        assert stats["candidates"] == 0
        assert stats["written"] == 0

    def test_no_candidates_extracted(self, tmp_path):
        with (
            patch("atlas_plus.curation.suggest_facts._load_transcript_text", return_value="hello world"),
            patch("atlas_plus.curation.suggest_facts._extract_candidates", return_value=([], 0)),
        ):
            stats = suggest_facts(memory_root=tmp_path)
        assert stats["candidates"] == 0

    def test_dry_run_writes_nothing(self, tmp_path):
        fake_cand = [{
            "type": "feedback",
            "scope": "feedback/testing",
            "title": "No mocking",
            "fact": "Use real DB in tests.",
            "confidence": 0.95,
        }]
        with (
            patch("atlas_plus.curation.suggest_facts._load_transcript_text", return_value="transcript"),
            patch("atlas_plus.curation.suggest_facts._extract_candidates", return_value=(fake_cand, 0)),
        ):
            stats = suggest_facts(dry_run=True, memory_root=tmp_path)
        assert stats["candidates"] == 1
        assert stats["written"] == 0

    def test_auto_approve_writes_fact(self, tmp_path):
        """Auto mode writes facts at confidence >= 0.85."""
        fake_cand = [{
            "type": "feedback",
            "scope": "feedback/testing",
            "title": "No mocking",
            "fact": "Use real DB in tests.",
            "confidence": 0.95,
        }]
        write_calls = []

        def fake_write(scope, fact, confidence=None):
            write_calls.append((scope, fact))
            return {"ok": True, "file": "fake.md"}

        with (
            patch("atlas_plus.curation.suggest_facts._load_transcript_text", return_value="transcript"),
            patch("atlas_plus.curation.suggest_facts._extract_candidates", return_value=(fake_cand, 0)),
            patch("atlas_plus.curation.suggest_facts._write_fact", side_effect=fake_write),
        ):
            stats = suggest_facts(auto=True, memory_root=tmp_path)

        assert stats["written"] == 1
        assert write_calls[0][0] == "feedback/testing"
