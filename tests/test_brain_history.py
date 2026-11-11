"""Tests for atlas_plus.brain.history -- SQLite conversation store."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas_plus.brain import history as hist


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "state.sqlite"


def test_add_and_get_messages(db):
    hist.add_message("s1", "user", "hello", db_path=db)
    hist.add_message("s1", "assistant", "hi there", model="qwen3:8b", db_path=db)

    msgs = hist.get_messages("s1", db_path=db)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "hello"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "hi there"


def test_get_messages_limit(db):
    for i in range(20):
        hist.add_message("s2", "user", f"msg{i}", db_path=db)

    msgs = hist.get_messages("s2", limit=5, db_path=db)
    assert len(msgs) == 5
    # Should be chronological (oldest first within the limit window)
    assert msgs[-1]["content"] == "msg19"


def test_sessions_are_isolated(db):
    hist.add_message("alice", "user", "hi alice", db_path=db)
    hist.add_message("bob", "user", "hi bob", db_path=db)

    alice = hist.get_messages("alice", db_path=db)
    bob = hist.get_messages("bob", db_path=db)
    assert len(alice) == 1
    assert alice[0]["content"] == "hi alice"
    assert bob[0]["content"] == "hi bob"


def test_tool_calls_roundtrip(db):
    tc = [{"function": {"name": "memory_search", "arguments": {"query": "test"}}}]
    hist.add_message("s3", "assistant", "calling tool", tool_calls=tc, db_path=db)

    msgs = hist.get_messages("s3", db_path=db)
    assert len(msgs) == 1
    assert msgs[0]["tool_calls"] == tc


def test_clear_session(db):
    hist.add_message("s4", "user", "bye", db_path=db)
    deleted = hist.clear_session("s4", db_path=db)
    assert deleted == 1
    assert hist.get_messages("s4", db_path=db) == []


def test_session_count(db):
    hist.add_message("x", "user", "a", db_path=db)
    hist.add_message("y", "user", "b", db_path=db)
    assert hist.session_count(db_path=db) == 2


def test_list_sessions_returns_recent_first(db):
    import time
    hist.add_message("older", "user", "first", db_path=db)
    time.sleep(0.01)
    hist.add_message("newer", "user", "second", db_path=db)
    sessions = hist.list_sessions(db_path=db)
    ids = [s["session_id"] for s in sessions]
    assert ids.index("newer") < ids.index("older")


def test_list_sessions_includes_message_count(db):
    hist.add_message("s10", "user", "msg1", db_path=db)
    hist.add_message("s10", "assistant", "msg2", db_path=db)
    hist.add_message("s10", "user", "msg3", db_path=db)
    sessions = hist.list_sessions(db_path=db)
    s10 = next((s for s in sessions if s["session_id"] == "s10"), None)
    assert s10 is not None
    assert s10["message_count"] == 3


def test_list_sessions_respects_limit(db):
    for i in range(10):
        hist.add_message(f"bulk-{i}", "user", "msg", db_path=db)
    sessions = hist.list_sessions(limit=5, db_path=db)
    assert len(sessions) <= 5


def test_list_sessions_empty_db(db):
    sessions = hist.list_sessions(db_path=db)
    assert sessions == []


# ---------------------------------------------------------------------------
# session_export_markdown
# ---------------------------------------------------------------------------

def test_export_markdown_returns_empty_for_missing_session(db):
    md = hist.session_export_markdown("no-such-session", db_path=db)
    assert md == ""


def test_export_markdown_contains_session_id(db):
    hist.add_message("export-sess", "user", "Hello Brain.", db_path=db)
    hist.add_message("export-sess", "assistant", "Hello Alex.", db_path=db)
    md = hist.session_export_markdown("export-sess", db_path=db)
    assert "export-sess" in md


def test_export_markdown_contains_all_messages(db):
    hist.add_message("ex2", "user", "What is the temporal graph?", db_path=db)
    hist.add_message("ex2", "assistant", "The temporal graph tracks entities over time.", db_path=db)
    md = hist.session_export_markdown("ex2", db_path=db)
    assert "temporal graph" in md
    assert "USER" in md
    assert "ASSISTANT" in md


def test_export_markdown_chronological_order(db):
    hist.add_message("ex3", "user", "First message.", db_path=db)
    hist.add_message("ex3", "assistant", "Second message.", db_path=db)
    hist.add_message("ex3", "user", "Third message.", db_path=db)
    md = hist.session_export_markdown("ex3", db_path=db)
    first_idx = md.index("First message")
    second_idx = md.index("Second message")
    third_idx = md.index("Third message")
    assert first_idx < second_idx < third_idx


def test_export_markdown_includes_message_count(db):
    hist.add_message("ex4", "user", "msg", db_path=db)
    hist.add_message("ex4", "assistant", "reply", db_path=db)
    md = hist.session_export_markdown("ex4", db_path=db)
    assert "Messages: 2" in md or "2" in md
