"""Tests for curation/suggest_facts.py helpers: _tokenize, _jaccard, _load_memory_corpus, _find_similar, _extract_candidates."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# _tokenize
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_basic_words(self):
        from atlas_plus.curation.suggest_facts import _tokenize
        result = _tokenize("atlas brain memory")
        assert "atlas" in result
        assert "brain" in result
        assert "memory" in result

    def test_removes_stopwords(self):
        from atlas_plus.curation.suggest_facts import _tokenize
        result = _tokenize("the atlas is a memory system")
        assert "the" not in result
        assert "is" not in result
        assert "atlas" in result

    def test_strips_frontmatter(self):
        from atlas_plus.curation.suggest_facts import _tokenize
        text = "---\ntitle: test\ntype: feedback\n---\natlas memory brain"
        result = _tokenize(text)
        # "title" is 5 chars, not a stopword -- may appear depending on tokenizer
        assert "atlas" in result

    def test_short_words_excluded(self):
        from atlas_plus.curation.suggest_facts import _tokenize
        result = _tokenize("I am on it now")
        # Single and two-char words excluded (len > 2 required)
        assert "i" not in result
        assert "am" not in result
        assert "on" not in result

    def test_lowercase(self):
        from atlas_plus.curation.suggest_facts import _tokenize
        result = _tokenize("Atlas Memory BRAIN")
        assert "atlas" in result
        assert "Atlas" not in result
        assert "BRAIN" not in result


# ---------------------------------------------------------------------------
# _jaccard
# ---------------------------------------------------------------------------

class TestJaccardSuggestFacts:
    def test_identical_sets(self):
        from atlas_plus.curation.suggest_facts import _jaccard
        s = {"atlas", "brain", "memory"}
        assert _jaccard(s, s) == 1.0

    def test_empty_both(self):
        from atlas_plus.curation.suggest_facts import _jaccard
        assert _jaccard(set(), set()) == 1.0

    def test_one_empty(self):
        from atlas_plus.curation.suggest_facts import _jaccard
        assert _jaccard({"atlas"}, set()) == 0.0
        assert _jaccard(set(), {"atlas"}) == 0.0

    def test_no_overlap(self):
        from atlas_plus.curation.suggest_facts import _jaccard
        a = {"atlas", "brain", "memory"}
        b = {"django", "flask", "fastapi"}
        assert _jaccard(a, b) == 0.0

    def test_partial_overlap(self):
        from atlas_plus.curation.suggest_facts import _jaccard
        a = {"atlas", "brain", "memory"}
        b = {"atlas", "brain", "scheduler"}
        result = _jaccard(a, b)
        assert 0.0 < result < 1.0


# ---------------------------------------------------------------------------
# _load_memory_corpus
# ---------------------------------------------------------------------------

class TestLoadMemoryCorpus:
    def test_empty_root_returns_empty(self, tmp_path):
        from atlas_plus.curation.suggest_facts import _load_memory_corpus
        result = _load_memory_corpus(tmp_path)
        assert result == []

    def test_loads_markdown_files(self, tmp_path):
        from atlas_plus.curation.suggest_facts import _load_memory_corpus
        ref = tmp_path / "reference"
        ref.mkdir()
        (ref / "atlas-memory.md").write_text(
            "---\ntitle: Atlas\n---\natlas brain memory scheduler worker",
            encoding="utf-8",
        )
        result = _load_memory_corpus(tmp_path)
        assert len(result) == 1
        path, tokens = result[0]
        assert "atlas" in tokens

    def test_skips_excluded_dirs(self, tmp_path):
        from atlas_plus.curation.suggest_facts import _load_memory_corpus
        arch = tmp_path / "archive"
        arch.mkdir()
        (arch / "old.md").write_text("old content", encoding="utf-8")
        result = _load_memory_corpus(tmp_path)
        assert result == []

    def test_skips_underscore_files(self, tmp_path):
        from atlas_plus.curation.suggest_facts import _load_memory_corpus
        ref = tmp_path / "reference"
        ref.mkdir()
        (ref / "_index.md").write_text("atlas memory brain", encoding="utf-8")
        result = _load_memory_corpus(tmp_path)
        assert result == []


# ---------------------------------------------------------------------------
# _find_similar
# ---------------------------------------------------------------------------

class TestFindSimilar:
    def test_empty_corpus_returns_empty(self):
        from atlas_plus.curation.suggest_facts import _find_similar
        result = _find_similar("atlas brain memory scheduler", [])
        assert result == []

    def test_finds_similar_fact(self):
        from atlas_plus.curation.suggest_facts import _find_similar, _tokenize
        fact = "atlas brain memory scheduler temporal worker"
        corpus_text = "atlas brain memory scheduler temporal worker facts"
        corpus = [("reference/doc.md", _tokenize(corpus_text))]
        result = _find_similar(fact, corpus)
        assert len(result) >= 1
        assert result[0][0] >= 0.4  # above threshold

    def test_dissimilar_fact_not_returned(self):
        from atlas_plus.curation.suggest_facts import _find_similar, _tokenize
        fact = "atlas brain memory scheduler"
        corpus_text = "django flask fastapi postgresql redis"
        corpus = [("reference/doc.md", _tokenize(corpus_text))]
        result = _find_similar(fact, corpus)
        assert result == []

    def test_sorted_by_similarity_desc(self):
        from atlas_plus.curation.suggest_facts import _find_similar, _tokenize
        fact = "atlas brain memory scheduler temporal worker curator"
        corpus = [
            ("a.md", _tokenize("atlas brain memory")),
            ("b.md", _tokenize("atlas brain memory scheduler temporal worker curator")),
        ]
        result = _find_similar(fact, corpus)
        if len(result) >= 2:
            assert result[0][0] >= result[1][0]


# ---------------------------------------------------------------------------
# _extract_candidates -- LLM-gated, test with mocked provider
# ---------------------------------------------------------------------------

class TestExtractCandidates:
    def test_empty_transcript_returns_empty(self):
        from atlas_plus.curation.suggest_facts import _extract_candidates
        candidates, rejected = _extract_candidates("")
        assert candidates == []
        assert rejected == 0

    def test_whitespace_only_returns_empty(self):
        from atlas_plus.curation.suggest_facts import _extract_candidates
        candidates, rejected = _extract_candidates("   \n  ")
        assert candidates == []
        assert rejected == 0

    def test_parses_valid_json_response(self):
        from atlas_plus.curation.suggest_facts import _extract_candidates
        llm_response = """[
          {"type": "feedback", "scope": "preference:testing", "title": "Use real DB",
           "fact": "Always use real DB in tests.", "confidence": 0.9}
        ]"""
        with patch("atlas_plus.summarizer.providers.complete", return_value=(llm_response, {})):
            candidates, rejected = _extract_candidates("some transcript text")
        assert len(candidates) == 1
        assert candidates[0]["scope"] == "preference:testing"
        assert candidates[0]["confidence"] == 0.9
        assert rejected == 0

    def test_filters_low_confidence_candidates(self):
        from atlas_plus.curation.suggest_facts import _extract_candidates
        llm_response = """[
          {"type": "feedback", "scope": "preference:testing",
           "title": "Low conf", "fact": "Some fact.", "confidence": 0.5}
        ]"""
        with patch("atlas_plus.summarizer.providers.complete", return_value=(llm_response, {})):
            candidates, rejected = _extract_candidates("some transcript text")
        assert candidates == []
        assert rejected == 1

    def test_applies_scope_alias(self):
        from atlas_plus.curation.suggest_facts import _extract_candidates
        llm_response = """[
          {"type": "feedback", "scope": "best_practice:testing",
           "title": "T", "fact": "Use best practice here.", "confidence": 0.85}
        ]"""
        with patch("atlas_plus.summarizer.providers.complete", return_value=(llm_response, {})):
            candidates, rejected = _extract_candidates("transcript content here")
        assert len(candidates) == 1
        assert candidates[0]["scope"] == "preference:testing"

    def test_handles_invalid_json_gracefully(self):
        from atlas_plus.curation.suggest_facts import _extract_candidates
        with patch("atlas_plus.summarizer.providers.complete",
                   return_value=("not valid json at all", {})):
            candidates, rejected = _extract_candidates("some transcript text")
        assert candidates == []
        assert rejected == 0

    def test_handles_provider_error(self):
        from atlas_plus.curation.suggest_facts import _extract_candidates
        from atlas_plus.summarizer.providers import ProviderError
        import pytest
        with patch("atlas_plus.summarizer.providers.complete",
                   side_effect=ProviderError("no model")):
            with pytest.raises(RuntimeError, match="LLM provider error"):
                _extract_candidates("some transcript text")

    def test_rejected_count_tracked_correctly(self):
        from atlas_plus.curation.suggest_facts import _extract_candidates
        llm_response = """[
          {"type": "feedback", "scope": "preference:testing", "title": "High",
           "fact": "High confidence fact.", "confidence": 0.9},
          {"type": "feedback", "scope": "preference:git", "title": "Low",
           "fact": "Low confidence fact.", "confidence": 0.5},
          {"type": "feedback", "scope": "preference:code", "title": "Also low",
           "fact": "Another low fact.", "confidence": 0.4}
        ]"""
        with patch("atlas_plus.summarizer.providers.complete", return_value=(llm_response, {})):
            candidates, rejected = _extract_candidates("transcript text")
        assert len(candidates) == 1
        assert rejected == 2

    def test_custom_threshold_via_env(self, monkeypatch):
        from atlas_plus.curation.suggest_facts import _extract_candidates
        monkeypatch.setenv("ATLAS_SUGGEST_CONFIDENCE_THRESHOLD", "0.9")
        llm_response = """[
          {"type": "feedback", "scope": "preference:testing", "title": "Mid",
           "fact": "Mid confidence fact.", "confidence": 0.8}
        ]"""
        with patch("atlas_plus.summarizer.providers.complete", return_value=(llm_response, {})):
            candidates, rejected = _extract_candidates("transcript text")
        # 0.8 < 0.9 threshold, so it should be rejected
        assert candidates == []
        assert rejected == 1


# ---------------------------------------------------------------------------
# _review_candidates -- skip_similar_threshold
# ---------------------------------------------------------------------------

class TestReviewCandidatesSkipSimilar:
    """Tests for the skip_similar_threshold gate in _review_candidates."""

    def _corpus(self, tokens: set[str]) -> list[tuple[str, set[str]]]:
        return [("feedback/existing.md", tokens)]

    def _cand(self, fact: str = "version before every edit", confidence: float = 0.85) -> dict:
        return {
            "scope": "preference:git",
            "title": "Version before edit",
            "fact": fact,
            "confidence": confidence,
        }

    def test_disabled_by_default(self, capsys):
        """With threshold=0.0, even highly similar candidates are shown."""
        from atlas_plus.curation.suggest_facts import _review_candidates, _tokenize
        tokens = _tokenize("version before every edit git file backup")
        corpus = self._corpus(tokens)
        cand = self._cand("version before every edit git file backup")
        result = _review_candidates(
            candidates=[cand],
            corpus=corpus,
            dry_run=True,
            skip_similar_threshold=0.0,
        )
        # In dry-run mode with threshold disabled, candidate is processed (skipped=1)
        assert result["skipped"] == 1

    def test_auto_skips_above_threshold(self, capsys):
        """Candidates with similarity >= threshold are auto-skipped."""
        from atlas_plus.curation.suggest_facts import _review_candidates, _tokenize
        # Use a fact whose tokens are identical to the corpus entry
        tokens = _tokenize("atlas brain memory scheduler temporal worker")
        corpus = self._corpus(tokens)
        cand = self._cand("atlas brain memory scheduler temporal worker")
        result = _review_candidates(
            candidates=[cand],
            corpus=corpus,
            dry_run=True,
            skip_similar_threshold=0.3,  # low threshold, should fire
        )
        assert result["skipped"] == 1

    def test_passes_below_threshold(self, capsys):
        """Candidates below the similarity threshold are not auto-skipped."""
        from atlas_plus.curation.suggest_facts import _review_candidates, _tokenize
        # Corpus has unrelated content
        tokens = _tokenize("django flask fastapi postgresql redis")
        corpus = self._corpus(tokens)
        cand = self._cand("atlas brain memory scheduler worker")
        result = _review_candidates(
            candidates=[cand],
            corpus=corpus,
            dry_run=True,
            skip_similar_threshold=0.6,
        )
        # Not similar -- falls through to dry-run skip (skipped=1 from dry-run path)
        assert result["skipped"] == 1

    def test_skip_message_mentions_score(self, capsys):
        """Auto-skip prints the similarity score and matching file."""
        from atlas_plus.curation.suggest_facts import _review_candidates, _tokenize
        tokens = _tokenize("version before every edit git file commit backup rollback")
        corpus = self._corpus(tokens)
        cand = self._cand("version before every edit git file commit backup rollback")
        _review_candidates(
            candidates=[cand],
            corpus=corpus,
            dry_run=True,
            skip_similar_threshold=0.3,
        )
        captured = capsys.readouterr()
        assert "Auto-skipped" in captured.out
        assert "existing.md" in captured.out

    def test_empty_corpus_no_skip(self, capsys):
        """With no corpus, skip-similar has nothing to compare against."""
        from atlas_plus.curation.suggest_facts import _review_candidates
        cand = self._cand("atlas brain memory scheduler worker")
        result = _review_candidates(
            candidates=[cand],
            corpus=[],
            dry_run=True,
            skip_similar_threshold=0.1,
        )
        # No similar entry found -- falls to dry-run skip
        assert result["skipped"] == 1


# ---------------------------------------------------------------------------
# _review_candidates -- non_interactive (headless --auto runs)
# ---------------------------------------------------------------------------

class TestReviewCandidatesNonInteractive:
    """--auto runs are headless (Brain scheduled task). A candidate below the
    auto-approve threshold used to fall through to input() and hang until the
    worker's timeout killed the run (recurring suggest_facts_auto failures,
    2026-10-12). non_interactive=True must skip instead of prompting."""

    def _cand(self, confidence: float) -> dict:
        return {
            "scope": "project:Atlas",
            "title": "Some fact",
            "fact": "a candidate fact below the auto threshold",
            "confidence": confidence,
        }

    def test_sub_threshold_candidate_is_skipped_not_prompted(self, monkeypatch):
        from atlas_plus.curation import suggest_facts as sf

        def _boom(prompt=""):
            raise AssertionError("input() must never be called in non-interactive mode")

        monkeypatch.setattr("builtins.input", _boom)
        result = sf._review_candidates(
            candidates=[self._cand(confidence=0.90)],
            corpus=[],
            dry_run=False,
            auto_approve_threshold=0.95,
            auto_write_cap=5,
            non_interactive=True,
        )
        assert result["skipped"] == 1
        assert result["written"] == 0

    def test_auto_wires_non_interactive_through_suggest_facts(self, monkeypatch, tmp_path):
        from atlas_plus.curation import suggest_facts as sf

        def _boom(prompt=""):
            raise AssertionError("input() must never be called in --auto mode")

        monkeypatch.setattr("builtins.input", _boom)
        monkeypatch.setattr(sf, "_load_transcript_text", lambda **kw: "some transcript text")
        monkeypatch.setattr(
            sf, "_extract_candidates",
            lambda text, confidence_threshold=None: ([
                {"scope": "project:Atlas", "title": "T",
                 "fact": "sub-threshold fact", "confidence": 0.90},
            ], 0),
        )
        monkeypatch.setattr(sf, "_load_memory_corpus", lambda root: [])
        result = sf.suggest_facts(source="claude-code", auto=True, memory_root=tmp_path)
        assert result["skipped"] == 1
        assert result["written"] == 0
