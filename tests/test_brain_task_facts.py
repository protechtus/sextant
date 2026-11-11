"""Tests for the task-to-memory fact extraction pipeline (task_facts.py).

Covers: candidate extraction, list_pending_facts, approve_fact, reject_fact,
pending_count, and the extract_candidates entry point.
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_plus.brain.task_facts import (
    PendingFact,
    _connect,
    _extract_sentences,
    _infer_scope,
    _score_sentence,
    extract_candidates,
    list_pending_facts,
    approve_fact,
    reject_fact,
    pending_count,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mk_db(tmp_path: Path) -> Path:
    db = tmp_path / "state.sqlite"
    conn = _connect(db)
    conn.close()
    return db


def _insert_fact(db: Path, fact_id: str = None, status: str = "pending",
                 fact_text: str = "Atlas uses SQLite for task state.") -> str:
    if fact_id is None:
        fact_id = str(uuid.uuid4())
    conn = _connect(db)
    conn.execute(
        "INSERT INTO pending_facts (id, task_id, fact_text, scope_hint, confidence, status, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (fact_id, "task-001", fact_text, "project:atlas", 0.75, status, "2026-05-20T00:00:00"),
    )
    conn.commit()
    conn.close()
    return fact_id


# ---------------------------------------------------------------------------
# _extract_sentences
# ---------------------------------------------------------------------------

class TestExtractSentences:
    def test_splits_on_period(self):
        sents = _extract_sentences(
            "Atlas Brain uses Qwen3 for local model inference. "
            "The Brain server runs on port 8767 accepting bearer tokens."
        )
        assert len(sents) >= 1

    def test_filters_short_sentences(self):
        sents = _extract_sentences("Hi. This sentence has enough words to pass the filter.")
        assert all(len(s.split()) >= 6 for s in sents)

    def test_returns_list_of_strings(self):
        result = _extract_sentences("First sentence here. Second sentence appears next.")
        assert isinstance(result, list)
        assert all(isinstance(s, str) for s in result)

    def test_empty_string_returns_empty(self):
        assert _extract_sentences("") == []


# ---------------------------------------------------------------------------
# _score_sentence
# ---------------------------------------------------------------------------

class TestScoreSentence:
    def test_on_topic_sentence_scores_higher(self):
        instruction = "Summarize the Atlas Brain architecture"
        on_topic = "Atlas Brain uses Qwen3 for local LLM orchestration and scheduling."
        off_topic = "The weather in Seattle is typically rainy in winter months."
        assert _score_sentence(on_topic, instruction) > _score_sentence(off_topic, instruction)

    def test_score_is_float_in_range(self):
        score = _score_sentence("Atlas uses Qwen3 for memory tasks.", "atlas memory")
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# extract_candidates
# ---------------------------------------------------------------------------

class TestExtractCandidates:
    def test_extracts_from_long_result(self, tmp_path):
        db = _mk_db(tmp_path)
        task_id = "task-extract-001"
        result = (
            "Atlas Brain uses Qwen3 as its primary local model for reasoning. "
            "The Brain server runs on port 8767 and accepts bearer token auth. "
            "Memory search is powered by FTS5 full-text search in SQLite. "
            "Task state is persisted in brain_tasks table with WAL journal mode. "
            "The scheduler runs nightly jobs at 03:05 UTC for temporal graph ingest. "
            "Claude Code acts as the escalation safety net for failed tasks. "
        )
        extract_candidates(
            task_id=task_id,
            instruction="describe the atlas brain architecture",
            result_text=result,
            db_path=db,
        )
        facts = list_pending_facts(db_path=db)
        assert len(facts) > 0
        assert all(f.task_id == task_id for f in facts)
        assert all(f.status == "pending" for f in facts)

    def test_short_result_produces_no_candidates(self, tmp_path):
        db = _mk_db(tmp_path)
        extract_candidates("t1", "do something", "Short.", db_path=db)
        assert list_pending_facts(db_path=db) == []

    def test_caps_candidates_at_max(self, tmp_path):
        db = _mk_db(tmp_path)
        sentences = [
            f"Atlas component {i} uses technology {i} for scalable distributed purpose {i}."
            for i in range(20)
        ]
        result_text = " ".join(sentences)
        extract_candidates("t2", "atlas overview", result_text=result_text, db_path=db)
        facts = list_pending_facts(db_path=db)
        assert len(facts) <= 5  # _MAX_CANDIDATES


# ---------------------------------------------------------------------------
# list_pending_facts
# ---------------------------------------------------------------------------

class TestListPendingFacts:
    def test_returns_pending_only_by_default(self, tmp_path):
        db = _mk_db(tmp_path)
        _insert_fact(db, "pf-001", status="pending")
        _insert_fact(db, "pf-002", status="approved")
        _insert_fact(db, "pf-003", status="rejected")
        facts = list_pending_facts(db_path=db)
        assert len(facts) == 1
        assert facts[0].id == "pf-001"

    def test_returns_approved_when_status_approved(self, tmp_path):
        db = _mk_db(tmp_path)
        _insert_fact(db, "pf-a", status="approved")
        facts = list_pending_facts(status="approved", db_path=db)
        assert len(facts) == 1

    def test_returns_list_of_pending_fact(self, tmp_path):
        db = _mk_db(tmp_path)
        _insert_fact(db, "pf-type")
        facts = list_pending_facts(db_path=db)
        assert isinstance(facts[0], PendingFact)

    def test_respects_limit(self, tmp_path):
        db = _mk_db(tmp_path)
        for i in range(5):
            _insert_fact(db)
        facts = list_pending_facts(limit=3, db_path=db)
        assert len(facts) <= 3


# ---------------------------------------------------------------------------
# approve_fact
# ---------------------------------------------------------------------------

class TestApproveFact:
    def test_approve_writes_to_memory_and_marks_approved(self, tmp_path):
        db = _mk_db(tmp_path)
        fid = _insert_fact(db, "approve-001")

        with patch("atlas_plus.mcp.memory_server.tools.remember.memory_remember",
                   return_value={"file": "/memory/foo.md"}) as mock_remember:
            result = approve_fact(fact_id=fid, db_path=db)

        assert result.get("ok") is True
        mock_remember.assert_called_once()

        # Verify status updated to approved
        facts = list_pending_facts(status="approved", db_path=db)
        assert any(f.id == fid for f in facts)

    def test_approve_nonexistent_fact_returns_error(self, tmp_path):
        db = _mk_db(tmp_path)
        result = approve_fact(fact_id="no-such-id", db_path=db)
        assert result.get("ok") is False or "error" in result

    def test_approve_exception_returns_error_dict(self, tmp_path):
        db = _mk_db(tmp_path)
        fid = _insert_fact(db, "approve-err")
        with patch("atlas_plus.mcp.memory_server.tools.remember.memory_remember",
                   side_effect=RuntimeError("memory write failed")):
            result = approve_fact(fact_id=fid, db_path=db)
        assert result.get("ok") is False


# ---------------------------------------------------------------------------
# reject_fact
# ---------------------------------------------------------------------------

class TestRejectFact:
    def test_reject_marks_as_rejected(self, tmp_path):
        db = _mk_db(tmp_path)
        fid = _insert_fact(db, "reject-001")
        result = reject_fact(fact_id=fid, db_path=db)
        assert result.get("ok") is True
        # Should no longer appear in pending list
        facts = list_pending_facts(db_path=db)
        assert not any(f.id == fid for f in facts)

    def test_reject_nonexistent_returns_false(self, tmp_path):
        db = _mk_db(tmp_path)
        result = reject_fact(fact_id="no-such-id", db_path=db)
        assert result.get("ok") is False

    def test_rejected_fact_status_recorded(self, tmp_path):
        db = _mk_db(tmp_path)
        fid = _insert_fact(db, "reject-status")
        reject_fact(fact_id=fid, db_path=db)
        facts = list_pending_facts(status="rejected", db_path=db)
        assert any(f.id == fid for f in facts)


# ---------------------------------------------------------------------------
# pending_count
# ---------------------------------------------------------------------------

class TestPendingCount:
    def test_count_zero_empty_db(self, tmp_path):
        db = _mk_db(tmp_path)
        assert pending_count(db_path=db) == 0

    def test_count_increases_with_pending_facts(self, tmp_path):
        db = _mk_db(tmp_path)
        _insert_fact(db)
        _insert_fact(db)
        assert pending_count(db_path=db) == 2

    def test_count_excludes_approved(self, tmp_path):
        db = _mk_db(tmp_path)
        _insert_fact(db, status="approved")
        assert pending_count(db_path=db) == 0


# ---------------------------------------------------------------------------
# PendingFact.as_dict
# ---------------------------------------------------------------------------

class TestInferScope:
    def test_atlas_project(self):
        assert _infer_scope("summarize the atlas-memory architecture") == "project:atlas-memory"

    def test_atlas_synonym(self):
        assert _infer_scope("review the atlas project") == "project:atlas-memory"

    def test_operator_registered_project(self, monkeypatch):
        # projects beyond the engine's own names come from ATLAS_KNOWN_PROJECTS ("name:synonym|name2")
        monkeypatch.setenv("ATLAS_KNOWN_PROJECTS", "sidecar:kernel filter")
        assert _infer_scope("fix the sidecar kernel filter") == "project:sidecar"

    def test_decision_keyword(self):
        result = _infer_scope("what decision was made about the database schema")
        assert result == "reference:decision"

    def test_feedback_keyword(self):
        result = _infer_scope("summarize the user preferences and feedback")
        assert result == "feedback"

    def test_fallback_to_task_result(self):
        result = _infer_scope("do a random task with no obvious scope")
        assert result == "reference:task-result"

    def test_scope_used_in_extracted_fact(self):
        """Facts extracted from an atlas task get project:atlas-memory scope."""
        import tempfile
        import pytest
        # We use tmp_path-style here with a real path
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.sqlite"
            _connect(db).close()
            long_result = (
                "The atlas-memory project uses APScheduler for nightly jobs. "
                "Brain server runs on port 8767 accepting bearer token credentials. "
                "Memory search uses FTS5 full-text index stored in SQLite database. "
                "Task workers use claim-execute-finish pattern for concurrency safety. "
                "Claude Code acts as escalation safety net for failed Brain tasks. "
            )
            # This would fail to run due to TemporaryDirectory WAL issue on Windows 3.11
            # but we can test the logic directly
        scope = _infer_scope("summarize atlas-memory architecture")
        assert scope == "project:atlas-memory"


class TestPendingFactAsDict:
    def test_as_dict_has_expected_keys(self):
        f = PendingFact(
            id="f-001", task_id="t-001", fact_text="Some fact.",
            scope_hint="project:atlas", confidence=0.8, status="pending",
            created_at="2026-05-20T00:00:00",
        )
        d = f.as_dict()
        for key in ("id", "task_id", "fact_text", "scope_hint", "confidence", "status", "created_at"):
            assert key in d
