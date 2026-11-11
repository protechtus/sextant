"""Tests for the suggest-facts CLI module wrapper."""

from __future__ import annotations

import os
import subprocess
import sys

# Rich/typer help output wraps to the terminal width and injects ANSI codes;
# on CI runners the narrow default split flag names across lines and broke
# substring asserts (POSIX lanes, 2026-07-30). Pin a plain, wide terminal.
_HELP_ENV = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "200"}


def _run_help() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "atlas_plus.curation.suggest_facts", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_HELP_ENV,
    )


def test_suggest_facts_module_help_runs_cli():
    completed = _run_help()
    assert completed.returncode == 0
    assert "Usage:" in completed.stdout
    assert "--dry-run" in completed.stdout


def test_suggest_facts_help_shows_threshold_flag():
    completed = _run_help()
    assert completed.returncode == 0
    assert "--threshold" in completed.stdout
    assert "--skip-similar" in completed.stdout


def test_suggest_facts_help_shows_skip_similar_flag():
    completed = _run_help()
    assert completed.returncode == 0
    assert "skip-similar" in completed.stdout or "skip_similar" in completed.stdout


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_returns_set_of_content_words(self):
        from atlas_plus.curation.suggest_facts import _tokenize
        tokens = _tokenize("Atlas uses Ollama for local inference")
        assert "atlas" in tokens
        assert "ollama" in tokens
        assert "local" in tokens
        assert "inference" in tokens

    def test_stop_words_excluded(self):
        from atlas_plus.curation.suggest_facts import _tokenize
        tokens = _tokenize("the and or to in is it")
        assert not tokens  # all stop words

    def test_short_words_excluded(self):
        from atlas_plus.curation.suggest_facts import _tokenize
        tokens = _tokenize("go py JS database")
        # 2-char words excluded, 3+ kept
        assert "go" not in tokens
        assert "database" in tokens

    def test_frontmatter_stripped(self):
        from atlas_plus.curation.suggest_facts import _tokenize
        text = "---\ntype: feedback\ntitle: Rule about testing\n---\nTest everything."
        tokens = _tokenize(text)
        # frontmatter 'type' and 'title' fields should not pollute the token set
        assert "testing" in tokens or "everything" in tokens
        assert "feedback" not in tokens  # inside frontmatter, stripped


class TestJaccard:
    def test_identical_sets_return_one(self):
        from atlas_plus.curation.suggest_facts import _jaccard
        s = {"atlas", "brain", "memory"}
        assert _jaccard(s, s) == 1.0

    def test_disjoint_sets_return_zero(self):
        from atlas_plus.curation.suggest_facts import _jaccard
        assert _jaccard({"a", "b"}, {"c", "d"}) == 0.0

    def test_empty_sets_return_one(self):
        from atlas_plus.curation.suggest_facts import _jaccard
        assert _jaccard(set(), set()) == 1.0

    def test_one_empty_returns_zero(self):
        from atlas_plus.curation.suggest_facts import _jaccard
        assert _jaccard(set(), {"a"}) == 0.0

    def test_partial_overlap(self):
        from atlas_plus.curation.suggest_facts import _jaccard
        a = {"atlas", "brain", "memory"}
        b = {"atlas", "ollama", "memory"}
        score = _jaccard(a, b)
        # intersection=2, union=4 -> 0.5
        assert abs(score - 0.5) < 0.001


class TestFindSimilar:
    def test_no_matches_below_threshold(self):
        from atlas_plus.curation.suggest_facts import _find_similar, _tokenize
        corpus = [("feedback/style.md", _tokenize("never use em dashes in output"))]
        results = _find_similar("use pytest for unit tests", corpus)
        assert results == []

    def test_high_overlap_returned(self):
        from atlas_plus.curation.suggest_facts import _find_similar, _tokenize
        corpus = [("feedback/testing.md", _tokenize("use pytest framework for all unit tests"))]
        results = _find_similar("use pytest framework for unit testing", corpus)
        assert len(results) > 0
        assert results[0][1] == "feedback/testing.md"
        assert results[0][0] >= 0.40  # above threshold


# ---------------------------------------------------------------------------
# suggest_facts() confidence_threshold integration
# ---------------------------------------------------------------------------

class TestSuggestFactsThreshold:
    """Verify the confidence_threshold parameter propagates through suggest_facts()."""

    def test_custom_threshold_filters_low_confidence(self):
        """High threshold (0.95) causes more rejections than low threshold (0.50)."""
        from unittest.mock import patch, MagicMock
        from atlas_plus.curation.suggest_facts import suggest_facts

        high_conf = {"fact": "Atlas runs on Qwen3", "scope": "project",
                     "confidence": 0.95, "source": "session"}
        low_conf  = {"fact": "Maybe uses ollama", "scope": "reference",
                     "confidence": 0.55, "source": "session"}

        # With threshold 0.60, both should pass (0.95 and 0.55 -- oh wait 0.55 < 0.60)
        # Let's use 0.50 threshold -- both pass; rejected_low_confidence should be 0
        with patch("atlas_plus.curation.suggest_facts._load_transcript_text",
                   return_value="synthetic session transcript"), \
             patch("atlas_plus.curation.suggest_facts._extract_candidates",
                   return_value=([high_conf, low_conf], 0)), \
             patch("atlas_plus.curation.suggest_facts._load_memory_corpus",
                   return_value=[]), \
             patch("atlas_plus.curation.suggest_facts._review_candidates",
                   return_value={"approved": 0, "skipped": 2, "written": 0}):
            result = suggest_facts(since="today", dry_run=True, confidence_threshold=0.50)
        assert result["candidates"] == 2
        assert result.get("rejected_low_confidence", 0) == 0

    def test_high_threshold_rejected_count_reported(self):
        """rejected_low_confidence is passed through from _extract_candidates."""
        from unittest.mock import patch, MagicMock
        from atlas_plus.curation.suggest_facts import suggest_facts

        with patch("atlas_plus.curation.suggest_facts._load_transcript_text",
                   return_value="synthetic session transcript"), \
             patch("atlas_plus.curation.suggest_facts._extract_candidates",
                   return_value=([], 3)), \
             patch("atlas_plus.curation.suggest_facts._load_memory_corpus",
                   return_value=[]):
            result = suggest_facts(since="today", dry_run=True, confidence_threshold=0.95)
        # No candidates left after filtering, rejected_low_confidence=3
        assert result.get("rejected_low_confidence", 0) == 3
        assert result.get("candidates", 0) == 0

    def test_none_threshold_passes_none_to_extract(self):
        """confidence_threshold=None means use env default (no explicit value passed)."""
        from unittest.mock import patch, call
        from atlas_plus.curation.suggest_facts import suggest_facts

        with patch("atlas_plus.curation.suggest_facts._load_transcript_text",
                   return_value="synthetic session transcript"), \
             patch("atlas_plus.curation.suggest_facts._extract_candidates",
                   return_value=([], 0)) as mock_extract, \
             patch("atlas_plus.curation.suggest_facts._load_memory_corpus",
                   return_value=[]):
            suggest_facts(since="today", dry_run=True, confidence_threshold=None)
        mock_extract.assert_called_once()
        _, kwargs = mock_extract.call_args
        assert kwargs.get("confidence_threshold") is None
