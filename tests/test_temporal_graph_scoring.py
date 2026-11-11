"""Unit tests for temporal_graph/scoring.py -- deterministic ranking functions.

Covers: _tokenize, lexical_relevance, lexical_relevance_from_tokens,
freshness_score, freshness_warning, score_fact, rank_results.
No LLM or file I/O required.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from atlas_plus.temporal_graph.models import TemporalFact, TemporalResult
from atlas_plus.temporal_graph.scoring import (
    _FRESHNESS_HALF_LIFE_DAYS,
    _tokenize,
    freshness_score,
    freshness_warning,
    lexical_relevance,
    lexical_relevance_from_tokens,
    rank_results,
    score_fact,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fact(fact_text: str = "Atlas uses Qwen3 as the Brain model.",
          confidence: float = 1.0,
          authority: str = "memory",
          observed_at: str = "2026-05-01T00:00:00+00:00",
          last_verified_at: str | None = None) -> TemporalFact:
    return TemporalFact(
        fact_id="test-fact",
        subject_id="atlas",
        predicate="uses",
        object_id="qwen3",
        fact_text=fact_text,
        valid_from=observed_at,
        observed_at=observed_at,
        ingested_at=observed_at,
        last_verified_at=last_verified_at,
        confidence=confidence,
        authority=authority,
    )


def _result(fact: TemporalFact | None = None) -> TemporalResult:
    return TemporalResult(fact=fact or _fact())


def _now_minus(days: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


# ---------------------------------------------------------------------------
# _tokenize
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_removes_stop_words(self):
        tokens = _tokenize("the quick brown fox")
        assert "the" not in tokens

    def test_removes_short_tokens(self):
        tokens = _tokenize("is it ok to go")
        assert "ok" not in tokens
        assert "go" not in tokens

    def test_lowercases(self):
        tokens = _tokenize("Atlas Brain Qwen3")
        assert "atlas" in tokens
        assert "brain" in tokens

    def test_includes_numbers(self):
        tokens = _tokenize("qwen3 model version 8b")
        assert "qwen3" in tokens
        assert "version" in tokens

    def test_empty_string_returns_empty_set(self):
        assert _tokenize("") == set()

    def test_only_stop_words_returns_empty(self):
        assert _tokenize("the and or if") == set()


# ---------------------------------------------------------------------------
# lexical_relevance
# ---------------------------------------------------------------------------

class TestLexicalRelevance:
    def test_perfect_overlap(self):
        score = lexical_relevance("atlas brain model", "atlas brain model is running")
        assert score == 1.0

    def test_zero_overlap(self):
        score = lexical_relevance("unrelated query words", "atlas memory temporal graph")
        assert score == 0.0

    def test_partial_overlap(self):
        score = lexical_relevance("atlas brain scheduler", "atlas memory brain server")
        # query tokens: atlas, brain, scheduler (3)
        # fact tokens: atlas, memory, brain, server (4)
        # intersection: atlas, brain (2)
        assert abs(score - 2 / 3) < 0.01

    def test_empty_query_returns_zero(self):
        assert lexical_relevance("", "anything") == 0.0

    def test_empty_fact_returns_zero(self):
        assert lexical_relevance("atlas query", "") == 0.0


# ---------------------------------------------------------------------------
# lexical_relevance_from_tokens
# ---------------------------------------------------------------------------

class TestLexicalRelevanceFromTokens:
    def test_equivalent_to_string_variant(self):
        q = "atlas brain temporal"
        f = "atlas memory brain server temporal graph"
        from_string = lexical_relevance(q, f)
        from_tokens = lexical_relevance_from_tokens(_tokenize(q), _tokenize(f))
        assert abs(from_string - from_tokens) < 0.001

    def test_empty_q_tokens_returns_zero(self):
        assert lexical_relevance_from_tokens(set(), {"atlas", "brain"}) == 0.0

    def test_disjoint_sets_returns_zero(self):
        assert lexical_relevance_from_tokens({"abc"}, {"xyz"}) == 0.0


# ---------------------------------------------------------------------------
# freshness_score
# ---------------------------------------------------------------------------

class TestFreshnessScore:
    def test_recent_fact_scores_near_one(self):
        f = _fact(last_verified_at=_now_minus(1))
        score = freshness_score(f)
        assert score > 0.99

    def test_half_life_fact_scores_near_half(self):
        f = _fact(last_verified_at=_now_minus(_FRESHNESS_HALF_LIFE_DAYS))
        score = freshness_score(f)
        assert abs(score - 0.5) < 0.02

    def test_very_old_fact_scores_near_zero(self):
        f = _fact(observed_at=_now_minus(1000), last_verified_at=None)
        score = freshness_score(f)
        assert score < 0.01

    def test_no_timestamp_returns_half(self):
        f = TemporalFact(
            fact_id="x", subject_id="s", predicate="p", object_id="o",
            fact_text="text", valid_from="2026-01-01T00:00:00+00:00",
            observed_at="", ingested_at="2026-01-01T00:00:00+00:00",
        )
        assert freshness_score(f) == 0.5

    def test_last_verified_takes_precedence_over_observed(self):
        old_obs = _now_minus(500)
        recent_verified = _now_minus(5)
        f = _fact(observed_at=old_obs, last_verified_at=recent_verified)
        score = freshness_score(f)
        assert score > 0.94

    def test_monotonically_decreasing_with_age(self):
        scores = [freshness_score(_fact(last_verified_at=_now_minus(d))) for d in [1, 30, 90, 180, 365]]
        for i in range(len(scores) - 1):
            assert scores[i] > scores[i + 1]


# ---------------------------------------------------------------------------
# freshness_warning
# ---------------------------------------------------------------------------

class TestFreshnessWarning:
    def test_no_warning_for_fresh_fact(self):
        f = _fact(last_verified_at=_now_minus(5))
        assert freshness_warning(f) is None

    def test_warning_after_30_days(self):
        f = _fact(last_verified_at=_now_minus(45))
        w = freshness_warning(f)
        assert w is not None
        assert "45" in w or "days" in w

    def test_stale_warning_after_180_days(self):
        f = _fact(last_verified_at=_now_minus(200))
        w = freshness_warning(f)
        assert w is not None
        assert "stale" in w.lower() or "200" in w

    def test_no_timestamp_returns_provenance_warning(self):
        f = TemporalFact(
            fact_id="x", subject_id="s", predicate="p", object_id="o",
            fact_text="text", valid_from="2026-01-01T00:00:00+00:00",
            observed_at="", ingested_at="2026-01-01T00:00:00+00:00",
        )
        w = freshness_warning(f)
        assert w is not None
        assert "provenance" in w.lower() or "timestamp" in w.lower()


# ---------------------------------------------------------------------------
# score_fact
# ---------------------------------------------------------------------------

class TestScoreFact:
    def test_higher_relevance_raises_score(self):
        f = _fact(fact_text="atlas brain qwen3 scheduler worker temporal graph",
                  last_verified_at=_now_minus(5))
        s_high = score_fact("atlas brain qwen3", f)
        s_low = score_fact("unrelated obscure query", f)
        assert s_high > s_low

    def test_score_bounded_0_to_1(self):
        f = _fact(last_verified_at=_now_minus(5))
        s = score_fact("atlas brain", f)
        assert 0.0 <= s <= 1.0

    def test_uses_provided_tokens(self):
        f = _fact(fact_text="atlas brain")
        q_tokens = {"atlas", "brain"}
        f_tokens = {"atlas", "brain", "server"}
        s_tokens = score_fact("atlas brain", f, q_tokens=q_tokens, f_tokens=f_tokens)
        s_string = score_fact("atlas brain", f)
        assert abs(s_tokens - s_string) < 0.05

    def test_lower_confidence_lowers_score(self):
        f_high = _fact(confidence=1.0, last_verified_at=_now_minus(5))
        f_low = _fact(confidence=0.0, last_verified_at=_now_minus(5))
        assert score_fact("atlas", f_high) > score_fact("atlas", f_low)

    def test_authority_memory_beats_fixture(self):
        f_mem = _fact(authority="memory", last_verified_at=_now_minus(5))
        f_fix = _fact(authority="fixture", last_verified_at=_now_minus(5))
        assert score_fact("query", f_mem) > score_fact("query", f_fix)


# ---------------------------------------------------------------------------
# rank_results
# ---------------------------------------------------------------------------

class TestRankResults:
    def test_sorted_by_score_descending(self):
        now = _now_minus(5)
        r_high = _result(_fact(fact_text="atlas brain qwen3 scheduler", last_verified_at=now))
        r_low = _result(_fact(fact_text="completely unrelated text", last_verified_at=now))
        ranked = rank_results("atlas brain qwen3", [r_low, r_high])
        assert ranked[0].score >= ranked[1].score

    def test_empty_results_returns_empty(self):
        assert rank_results("atlas", []) == []

    def test_scores_are_set_on_results(self):
        r = _result(_fact(last_verified_at=_now_minus(5)))
        ranked = rank_results("atlas brain", [r])
        assert ranked[0].score > 0.0

    def test_freshness_warning_set_on_stale_result(self):
        r = _result(_fact(last_verified_at=_now_minus(200)))
        ranked = rank_results("atlas", [r])
        assert ranked[0].freshness_warning is not None

    def test_accepts_precomputed_query_tokens(self):
        r = _result(_fact(fact_text="atlas temporal graph entities"))
        q_tokens = _tokenize("atlas temporal graph")
        ranked = rank_results("atlas temporal graph", [r], q_tokens=q_tokens)
        assert ranked[0].score > 0.0

    def test_stable_sort_for_equal_scores(self):
        # Two identical facts should both appear in results
        r1 = _result(_fact(fact_text="identical fact text", last_verified_at=_now_minus(5)))
        r2 = _result(_fact(fact_text="identical fact text", last_verified_at=_now_minus(5)))
        ranked = rank_results("identical fact", [r1, r2])
        assert len(ranked) == 2
