"""Scoring and ranking for temporal graph query results.

First-slice scoring is deterministic (no LLM, no embeddings):
  - lexical_relevance: word overlap between query and fact_text
  - freshness_score: exponential decay from last_verified_at
  - confidence: from the fact itself
  - authority_weight: by source system

semantic_relevance and graph_neighborhood_boost are stubbed at 0.0.
Add embedding-based semantic scoring in a later slice.

Final score weights:
  lexical    0.40
  freshness  0.30
  confidence 0.20
  authority  0.10
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Optional

from atlas_plus.temporal_graph.models import TemporalFact, TemporalResult


_STOP_WORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "it", "that",
    "this", "for", "be", "with", "on", "at", "by", "from", "not", "are",
    "was", "were", "as", "if", "but", "all", "any", "has", "have", "had",
}

_AUTHORITY_WEIGHTS = {
    "memory": 1.0,
    "session_log": 0.9,
    "transcript": 0.8,
    "transcript_mining": 0.75,
    "fixture": 0.7,
}

_FRESHNESS_HALF_LIFE_DAYS = 90.0


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOP_WORDS}


def _parse_iso(ts: str) -> datetime:
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    elif len(ts) == 10:
        ts = ts + "T00:00:00+00:00"
    return datetime.fromisoformat(ts)


def lexical_relevance(query: str, fact_text: str) -> float:
    """Recall-based token overlap: fraction of query tokens present in fact_text."""
    q_tokens = _tokenize(query)
    if not q_tokens:
        return 0.0
    f_tokens = _tokenize(fact_text)
    return len(q_tokens & f_tokens) / len(q_tokens)


def lexical_relevance_from_tokens(q_tokens: set[str], f_tokens: set[str]) -> float:
    """Faster variant using pre-tokenized sets. Used by FileTemporalProvider."""
    if not q_tokens:
        return 0.0
    return len(q_tokens & f_tokens) / len(q_tokens)


def freshness_score(fact: TemporalFact) -> float:
    """Exponential decay score (0-1) based on days since last_verified_at or observed_at.

    Half-life: 30 days (configurable via _FRESHNESS_HALF_LIFE_DAYS). Returns 0.5
    when no timestamp is available.
    """
    ts_str = fact.last_verified_at or fact.observed_at
    if not ts_str:
        return 0.5
    try:
        age_days = (datetime.now(timezone.utc) - _parse_iso(ts_str)).total_seconds() / 86400
        return math.exp(-age_days * math.log(2) / _FRESHNESS_HALF_LIFE_DAYS)
    except (ValueError, OverflowError):
        return 0.5


def freshness_warning(fact: TemporalFact) -> Optional[str]:
    """Return a human-readable staleness warning, or None if fact is fresh (< 30 days)."""
    ts_str = fact.last_verified_at or fact.observed_at
    if not ts_str:
        return "No verification timestamp -- provenance incomplete."
    try:
        age_days = (datetime.now(timezone.utc) - _parse_iso(ts_str)).total_seconds() / 86400
        if age_days > 180:
            return f"Fact unverified for {int(age_days)} days -- may be stale."
        if age_days > 30:
            return f"Fact last verified {int(age_days)} days ago."
    except (ValueError, OverflowError):
        pass
    return None


def score_fact(query: str, fact: TemporalFact, q_tokens: Optional[set[str]] = None, f_tokens: Optional[set[str]] = None) -> float:
    """Composite relevance score (0-1) combining lexical (40%), freshness (30%), confidence (20%), authority (10%)."""
    if q_tokens is not None and f_tokens is not None:
        lex = lexical_relevance_from_tokens(q_tokens, f_tokens)
    else:
        lex = lexical_relevance(query, fact.fact_text)
    fresh = freshness_score(fact)
    authority = _AUTHORITY_WEIGHTS.get(fact.authority, 0.7)
    return (lex * 0.40) + (fresh * 0.30) + (fact.confidence * 0.20) + (authority * 0.10)


def rank_results(query: str, results: list[TemporalResult], q_tokens: Optional[set[str]] = None) -> list[TemporalResult]:
    """Score and sort TemporalResult objects descending by composite score. Mutates results in place."""
    _q = q_tokens if q_tokens is not None else _tokenize(query)
    for r in results:
        r.score = score_fact(query, r.fact, q_tokens=_q, f_tokens=None)
        r.freshness_warning = freshness_warning(r.fact)
    return sorted(results, key=lambda r: r.score, reverse=True)
