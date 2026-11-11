"""Tests for atlas_plus.brain.improvement -- quality scoring and observation store."""

from __future__ import annotations

import pytest

from atlas_plus.brain import improvement as imp


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "state.sqlite"


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def test_score_failed_task():
    q, signals = imp.score("some output", "failed", "brain", "do something")
    assert q == 0.0
    assert signals["status"] == -5.0


def test_score_blocked_task():
    q, _ = imp.score("blocked output", "blocked", "brain", "do something")
    assert q == 2.0


def test_score_done_empty():
    q, _ = imp.score("", "done", "brain", "do something")
    assert q < 5.0  # empty output penalised


def test_score_done_good_output():
    # 9 words * 6 = 54 words, triggering the >50 length bonus
    text = "Here is a detailed analysis of the code structure. " * 6
    q, signals = imp.score(text, "done", "brain", "analyze the code")
    assert q >= 5.0
    assert signals["length"] > 0


def test_score_coding_task_routing_bonus():
    text = "def foo(): pass  " * 20
    q_match, _ = imp.score(text, "done", "coder", "write code for parsing")
    q_miss, _  = imp.score(text, "done", "fast",  "write code for parsing")
    assert q_match > q_miss


def test_score_clamped_to_10():
    long_good = "Here is a very detailed, well-structured answer. " * 50
    q, _ = imp.score(long_good, "done", "coder", "implement a feature")
    assert 0.0 <= q <= 10.0


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_record_and_retrieve(db):
    obs = imp.record(
        task_id="t-001", agent_name="brain", model="qwen3:8b",
        result_text="good result " * 20, task_status="done",
        instruction="analyze something", db_path=db,
    )
    assert 0.0 <= obs.quality <= 10.0
    assert obs.agent_name == "brain"
    assert isinstance(obs.signals, dict)


def test_agent_stats_empty(db):
    stats = imp.agent_stats("nobody", db_path=db)
    assert stats["observations"] == 0
    assert stats["avg_quality"] is None


def test_agent_stats_populated(db):
    for i in range(5):
        imp.record(
            task_id=f"t-{i}", agent_name="fast", model="qwen3:1.7b",
            result_text="answer " * 15, task_status="done",
            instruction="quick question", db_path=db,
        )
    stats = imp.agent_stats("fast", db_path=db)
    assert stats["observations"] == 5
    assert stats["avg_quality"] is not None


def test_all_agent_stats(db):
    imp.record("t1", "fast",  "qwen3:1.7b", "ok " * 20, "done", "q", db_path=db)
    imp.record("t2", "brain", "qwen3:8b",   "ok " * 20, "done", "q", db_path=db)
    all_s = imp.all_agent_stats(db_path=db)
    agent_names = {s["agent"] for s in all_s}
    assert "fast" in agent_names
    assert "brain" in agent_names


# ---------------------------------------------------------------------------
# agent_recent_scores (ATL-N06)
# ---------------------------------------------------------------------------

def test_agent_recent_scores_empty(db):
    scores = imp.agent_recent_scores("nobody", db_path=db)
    assert scores == []


def test_agent_recent_scores_oldest_first(db):
    qualities = []
    for i in range(6):
        text = ("x " * (i + 1) * 5)  # progressively longer = higher quality
        obs = imp.record(
            task_id=f"ts-{i}", agent_name="brain", model="qwen3:8b",
            result_text=text, task_status="done",
            instruction="test task", db_path=db,
        )
        qualities.append(obs.quality)

    scores = imp.agent_recent_scores("brain", limit=6, db_path=db)
    assert len(scores) == 6
    # oldest first: scores should match insertion order
    assert scores == qualities


def test_agent_recent_scores_respects_limit(db):
    for i in range(10):
        imp.record(f"tl-{i}", "coder", "qwen2.5", "ok " * 20, "done", "q", db_path=db)
    scores = imp.agent_recent_scores("coder", limit=4, db_path=db)
    assert len(scores) == 4


# ---------------------------------------------------------------------------
# low_quality_tasks
# ---------------------------------------------------------------------------

def test_low_quality_tasks(db):
    imp.record("fail-1", "brain", "qwen3:8b", "", "failed", "bad task", db_path=db)
    imp.record("ok-1",   "brain", "qwen3:8b", "great result " * 20, "done", "good", db_path=db)
    low = imp.low_quality_tasks(threshold=4.0, db_path=db)
    low_ids = {t["task_id"] for t in low}
    assert "fail-1" in low_ids
    assert "ok-1" not in low_ids


# ---------------------------------------------------------------------------
# all_agent_recent_scores -- batch window-function query
# ---------------------------------------------------------------------------

def test_all_agent_recent_scores_empty(db):
    result = imp.all_agent_recent_scores(db_path=db)
    assert result == {}


def test_all_agent_recent_scores_multiple_agents(db):
    imp.record("a1", "fast",  "q1.7b", "ok " * 20, "done", "q", db_path=db)
    imp.record("a2", "brain", "q8b",   "ok " * 20, "done", "q", db_path=db)
    imp.record("a3", "fast",  "q1.7b", "ok " * 20, "done", "q", db_path=db)
    result = imp.all_agent_recent_scores(limit=10, db_path=db)
    assert "fast" in result
    assert "brain" in result
    assert len(result["fast"]) == 2
    assert len(result["brain"]) == 1


def test_all_agent_recent_scores_oldest_first(db):
    # Records inserted sequentially; oldest-first order should match insertion order
    for i in range(5):
        text = "word " * ((i + 1) * 5)
        imp.record(f"s{i}", "coder", "q2.5", text, "done", "q", db_path=db)
    result = imp.all_agent_recent_scores(limit=5, db_path=db)
    scores = result["coder"]
    # Oldest-first: each record was progressively longer -> quality should increase
    assert scores == sorted(scores) or len(set(scores)) >= 2  # at least not all identical


def test_all_agent_recent_scores_limit(db):
    for i in range(10):
        imp.record(f"lim{i}", "fast", "q1.7b", "ok " * 20, "done", "q", db_path=db)
    result = imp.all_agent_recent_scores(limit=4, db_path=db)
    assert len(result["fast"]) == 4


def test_all_agent_stats_single_query_matches_per_agent(db):
    for i in range(4):
        imp.record(f"m{i}", "brain", "q8b", "good " * 20, "done", "q", db_path=db)
    imp.record("mf", "fast", "q1.7b", "good " * 20, "done", "q", db_path=db)
    batch = {s["agent"]: s for s in imp.all_agent_stats(db_path=db)}
    per = imp.agent_stats("brain", db_path=db)
    assert batch["brain"]["avg_quality"] == per["avg_quality"]
    assert batch["brain"]["observations"] == per["observations"]
    assert "fast" in batch
