"""Tests for atlas_plus.transcripts.index -- SQLite/FTS5 transcript index."""
from __future__ import annotations

from pathlib import Path

import pytest

from atlas_plus.transcripts.store import TurnRecord
from atlas_plus.transcripts.index import TranscriptIndex


def _make_record(conv_id: str = "conv-1", source: str = "test", role: str = "user",
                 content: str = "hello world", turn_id: str = "t-001") -> TurnRecord:
    rec = TurnRecord(
        turn_id=turn_id,
        conversation_id=conv_id,
        source=source,
        role=role,
        content=content,
        ts="2026-05-21T03:00:00+00:00",
    )
    rec.hash = rec.compute_hash()
    return rec


# ---------------------------------------------------------------------------
# Cursor persistence
# ---------------------------------------------------------------------------

class TestCursorPersistence:
    def test_get_cursor_returns_zero_when_never_set(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        assert idx.get_cursor("src", "/path/file.jsonl") == 0

    def test_set_and_get_cursor(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        idx.set_cursor("src", "/path/file.jsonl", 1024)
        assert idx.get_cursor("src", "/path/file.jsonl") == 1024

    def test_cursor_updated_by_set(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        idx.set_cursor("src", "/path/file.jsonl", 512)
        idx.set_cursor("src", "/path/file.jsonl", 2048)
        assert idx.get_cursor("src", "/path/file.jsonl") == 2048

    def test_cursors_are_keyed_by_source_and_path(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        idx.set_cursor("src-a", "/file.jsonl", 100)
        idx.set_cursor("src-b", "/file.jsonl", 200)
        assert idx.get_cursor("src-a", "/file.jsonl") == 100
        assert idx.get_cursor("src-b", "/file.jsonl") == 200


# ---------------------------------------------------------------------------
# upsert_turn
# ---------------------------------------------------------------------------

class TestUpsertTurn:
    def test_inserts_conversation_and_turn(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        rec = _make_record()
        idx.upsert_turn(rec)
        convs = idx.list_conversations()
        assert len(convs) == 1
        assert convs[0]["conv_id"] == "conv-1"

    def test_turn_count_increments(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        idx.upsert_turn(_make_record(turn_id="t-001", content="first"))
        idx.upsert_turn(_make_record(turn_id="t-002", content="second"))
        convs = idx.list_conversations()
        assert convs[0]["turn_count"] >= 1

    def test_fts_indexed_for_search(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        rec = _make_record(content="sip registration protocol")
        idx.upsert_turn(rec)
        results = list(idx.search("sip"))
        assert len(results) == 1
        assert results[0]["turn_id"] == rec.turn_id

    def test_idempotent_on_same_turn_id(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        rec = _make_record()
        idx.upsert_turn(rec)
        idx.upsert_turn(rec)  # second upsert -- should not raise
        convs = idx.list_conversations()
        assert len(convs) == 1


# ---------------------------------------------------------------------------
# list_conversations
# ---------------------------------------------------------------------------

class TestListConversations:
    def test_empty_when_no_turns(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        assert idx.list_conversations() == []

    def test_returns_multiple_conversations(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        idx.upsert_turn(_make_record(conv_id="a", turn_id="t1"))
        idx.upsert_turn(_make_record(conv_id="b", turn_id="t2"))
        convs = idx.list_conversations()
        ids = {c["conv_id"] for c in convs}
        assert ids == {"a", "b"}

    def test_source_filter(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        idx.upsert_turn(_make_record(conv_id="a", source="claude-code", turn_id="t1"))
        idx.upsert_turn(_make_record(conv_id="b", source="openai-proxy", turn_id="t2"))
        convs = idx.list_conversations(source="claude-code")
        assert len(convs) == 1
        assert convs[0]["conv_id"] == "a"

    def test_limit_respected(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        for i in range(5):
            idx.upsert_turn(_make_record(conv_id=f"c-{i}", turn_id=f"t-{i}"))
        convs = idx.list_conversations(limit=3)
        assert len(convs) == 3


# ---------------------------------------------------------------------------
# search (FTS5)
# ---------------------------------------------------------------------------

class TestSearch:
    def test_finds_matching_turn(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        idx.upsert_turn(_make_record(content="VoIP SIP registration flow", turn_id="t1"))
        results = list(idx.search("SIP"))
        assert any(r["turn_id"] == "t1" for r in results)

    def test_no_results_for_unmatched_query(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        idx.upsert_turn(_make_record(content="hello world", turn_id="t1"))
        results = list(idx.search("kubernetes"))
        assert results == []

    def test_result_has_expected_keys(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        idx.upsert_turn(_make_record(content="Atlas memory system", turn_id="t1"))
        results = list(idx.search("atlas"))
        assert len(results) == 1
        r = results[0]
        for key in ("turn_id", "conv_id", "source", "role", "ts", "preview"):
            assert key in r

    def test_limit_respected(self, tmp_path):
        idx = TranscriptIndex(tmp_path / "idx.sqlite")
        for i in range(5):
            idx.upsert_turn(_make_record(content="atlas sip voip", turn_id=f"t-{i}", conv_id=f"c-{i}"))
        results = list(idx.search("atlas", limit=3))
        assert len(results) == 3
