"""Tests for brain.task_facts -- fact extraction, storage, approval, rejection."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_plus.brain.task_facts import (
    PendingFact,
    approve_fact,
    extract_candidates,
    list_pending_facts,
    pending_count,
    reject_fact,
    _extract_sentences,
    _score_sentence,
)


class TestExtractSentences:
    def test_extracts_declarative_sentences(self):
        text = (
            "The system uses FAISS for indexing. "
            "This provides sub-millisecond query times. "
            "Reindexing takes approximately 5 minutes on the current hardware."
        )
        sentences = _extract_sentences(text)
        assert len(sentences) >= 2
        assert any("FAISS" in s for s in sentences)

    def test_filters_out_questions(self):
        text = "The model is fast. Would you like a demo? It processes 1000 docs per second."
        sentences = _extract_sentences(text)
        assert not any(s.endswith("?") for s in sentences)

    def test_strips_markdown_headers(self):
        text = "# Header\n\nThe fact is here. Another fact follows."
        sentences = _extract_sentences(text)
        assert not any(s.startswith("#") for s in sentences)

    def test_strips_code_blocks(self):
        text = "The API returns JSON.\n```python\nreturn {'key': 'val'}\n```\nThe response is fast."
        sentences = _extract_sentences(text)
        assert not any("return" in s and "key" in s for s in sentences)

    def test_short_result_returns_empty(self):
        text = "Short."
        sentences = _extract_sentences(text)
        # Short sentence should be filtered by word count
        assert len([s for s in sentences if len(s.split()) >= 8]) == 0


class TestScoreSentence:
    def test_longer_sentences_score_higher(self):
        short = "The system is fast and efficient overall."
        long = "The system processes requests in under 10 milliseconds using a FAISS index with 50k vectors at the current scale."
        assert _score_sentence(long, "system performance") > _score_sentence(short, "system performance")

    def test_on_topic_sentences_score_higher(self):
        on_topic = "The atlas memory system stores facts about the user's preferences and projects."
        off_topic = "The weather in Paris is quite pleasant during the spring season with mild temperatures."
        assert _score_sentence(on_topic, "atlas memory system") > _score_sentence(off_topic, "atlas memory system")

    def test_sentences_with_numbers_score_higher(self):
        with_num = "The index contains 15,000 documents and achieves 98% recall at this scale."
        without_num = "The index contains many documents and achieves good recall at this scale."
        assert _score_sentence(with_num, "index performance") > _score_sentence(without_num, "index performance")


class TestExtractCandidates:
    def test_short_result_returns_empty(self):
        db = Path(tempfile.mkdtemp()) / "state.sqlite"
        result = extract_candidates("task-1", "some instruction", "Too short.", db_path=db)
        assert result == []

    def test_substantial_result_returns_candidates(self):
        db = Path(tempfile.mkdtemp()) / "state.sqlite"
        long_result = " ".join([
            "The Atlas memory system stores facts about the user's preferences and projects in markdown files.",
            "This allows any LLM to access the same context without manual priming every session.",
            "The FTS5 index enables sub-second full-text search across hundreds of memory files.",
            "Brain agent quality scoring tracks performance over time using a 0-10 scale.",
            "The temporal graph captures 115 entities and 367 active facts as of 2026-05-19.",
            "Sessions are automatically logged to .memory/sessions/ at the end of each conversation.",
        ] * 3)
        candidates = extract_candidates("task-1", "explain atlas memory", long_result, db_path=db)
        assert len(candidates) > 0
        assert all(isinstance(c, PendingFact) for c in candidates)
        assert all(c.status == "pending" for c in candidates)
        assert len(candidates) <= 5  # capped at _MAX_CANDIDATES

    def test_candidates_are_persisted(self):
        db = Path(tempfile.mkdtemp()) / "state.sqlite"
        instruction = "describe the system"
        result = " ".join([
            "The system processes incoming requests using an async task queue backed by SQLite.",
            "Workers claim tasks with a lease mechanism to prevent double-processing in concurrent environments.",
            "Each task result is stored in the brain_tasks table with a maximum of 4000 characters.",
        ] * 4)
        extract_candidates("task-99", instruction, result, db_path=db)
        pending = list_pending_facts(status="pending", db_path=db)
        assert len(pending) > 0
        assert all(c.task_id == "task-99" for c in pending)


class TestApproveReject:
    def _seed(self, db: Path) -> PendingFact:
        result = " ".join([
            "The Atlas memory system uses markdown files as the canonical storage format for facts.",
            "This ensures that the memory is human-readable without any special tooling.",
            "The FTS5 index provides fast full-text search across all memory files.",
        ] * 4)
        candidates = extract_candidates("task-seed", "test instruction", result, db_path=db)
        return candidates[0]

    def test_reject_marks_rejected(self):
        db = Path(tempfile.mkdtemp()) / "state.sqlite"
        fact = self._seed(db)
        result = reject_fact(fact.id, db_path=db)
        assert result["ok"] is True
        rejected = list_pending_facts(status="rejected", db_path=db)
        assert any(r.id == fact.id for r in rejected)

    def test_approve_calls_memory_remember(self):
        db = Path(tempfile.mkdtemp()) / "state.sqlite"
        fact = self._seed(db)
        mock_result = {"ok": True, "file": "/memory/test.md", "version_copy": None, "error": None}

        with patch("atlas_plus.mcp.memory_server.tools.remember.memory_remember",
                   return_value=mock_result):
            result = approve_fact(fact.id, scope="reference:test", db_path=db)

        assert result["ok"] is True
        approved = list_pending_facts(status="approved", db_path=db)
        assert any(a.id == fact.id for a in approved)

    def test_approve_nonexistent_fact(self):
        db = Path(tempfile.mkdtemp()) / "state.sqlite"
        result = approve_fact("nonexistent-id", db_path=db)
        assert result["ok"] is False


class TestPendingCount:
    def test_count_increases_with_extraction(self):
        db = Path(tempfile.mkdtemp()) / "state.sqlite"
        assert pending_count(db_path=db) == 0
        long = " ".join([
            "The Atlas system records session context automatically at the end of each conversation.",
            "Memory files are versioned before any write operation occurs to prevent data loss.",
        ] * 6)
        extract_candidates("task-x", "explain atlas", long, db_path=db)
        assert pending_count(db_path=db) > 0
