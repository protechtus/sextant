"""Unit tests for curation/contradictions.py.

Covers: ContradictionPair.as_dict, _contradiction_prompt formatting,
find_contradictions dry_run mode (no LLM), and the LLM pipeline
(mocked complete() call) including JSON parsing and markdown-fence recovery.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_plus.curation.contradictions import (
    ContradictionPair,
    _contradiction_prompt,
    find_contradictions,
)


# ---------------------------------------------------------------------------
# ContradictionPair
# ---------------------------------------------------------------------------

class TestContradictionPair:
    def test_as_dict_fields(self):
        p = ContradictionPair(
            file1="feedback/style.md",
            file2="reference/style.md",
            explanation="One says dark mode, other says light mode.",
            confidence="high",
        )
        d = p.as_dict()
        assert d["file1"] == "feedback/style.md"
        assert d["file2"] == "reference/style.md"
        assert "dark mode" in d["explanation"]
        assert d["confidence"] == "high"

    def test_as_dict_all_keys_present(self):
        p = ContradictionPair("a.md", "b.md", "", "low")
        assert set(p.as_dict().keys()) == {"file1", "file2", "explanation", "confidence"}


# ---------------------------------------------------------------------------
# _contradiction_prompt
# ---------------------------------------------------------------------------

class TestContradictionPrompt:
    def test_contains_pair_labels(self):
        pairs = [
            ("file1.md", "snippet A text", "file2.md", "snippet B text"),
        ]
        prompt = _contradiction_prompt(pairs)
        assert "Pair 1:" in prompt
        assert "file1.md" in prompt
        assert "snippet A text" in prompt

    def test_multiple_pairs_numbered(self):
        pairs = [
            ("a.md", "content a", "b.md", "content b"),
            ("c.md", "content c", "d.md", "content d"),
        ]
        prompt = _contradiction_prompt(pairs)
        assert "Pair 1:" in prompt
        assert "Pair 2:" in prompt

    def test_prompt_requests_json_output(self):
        pairs = [("a.md", "text a", "b.md", "text b")]
        prompt = _contradiction_prompt(pairs)
        assert "JSON" in prompt
        assert "contradicts" in prompt

    def test_empty_pairs_returns_prompt_with_empty_block(self):
        prompt = _contradiction_prompt([])
        assert "PAIRS:" in prompt
        assert "JSON:" in prompt


# ---------------------------------------------------------------------------
# find_contradictions -- dry_run (no LLM)
# ---------------------------------------------------------------------------

class TestFindContradictionsDryRun:
    def _write_md(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntitle: test\ntype: reference\n---\n\n{body}\n",
                        encoding="utf-8")

    def test_empty_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp)
            with patch("atlas_plus.curation.contradictions.MEMORY_ROOT", mem):
                result = find_contradictions(memory_root=mem, dry_run=True)
        assert result == []

    def test_single_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp)
            self._write_md(
                mem / "solo.md",
                "Atlas uses Qwen3 as brain model for task processing.",
            )
            with patch("atlas_plus.curation.contradictions.MEMORY_ROOT", mem):
                result = find_contradictions(memory_root=mem, dry_run=True)
        assert result == []

    def test_high_overlap_pair_flagged_in_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp)
            # Files share 10-12 content words (Jaccard ~0.3-0.4 after stop-word removal)
            # Need similarity in [RELATED_THRESHOLD=0.15, 0.85) -- near-duplicates excluded
            shared_words = "atlas memory temporal facts queries context inference provider"
            file_a_unique = "prefers dark interface workstation workflow pipeline orchestration"
            file_b_unique = "requires light interface keyboard shortcut productivity efficiency"
            self._write_md(mem / "file_a.md", shared_words + " " + file_a_unique)
            self._write_md(mem / "file_b.md", shared_words + " " + file_b_unique)
            with patch("atlas_plus.curation.contradictions.MEMORY_ROOT", mem):
                result = find_contradictions(memory_root=mem, dry_run=True)
        # Both files share vocabulary above RELATED_THRESHOLD (0.15) but below 0.85
        assert len(result) >= 1
        assert all(isinstance(p, ContradictionPair) for p in result)
        assert all("dry-run" in p.explanation for p in result)

    def test_low_overlap_pair_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp)
            self._write_md(
                mem / "personal.md",
                "prefers direct terse communication avoids corporate jargon values depth",
            )
            self._write_md(
                mem / "sip_ref.md",
                "registration invite ack bye cancel options message subscribe notify",
            )
            with patch("atlas_plus.curation.contradictions.MEMORY_ROOT", mem):
                result = find_contradictions(memory_root=mem, dry_run=True)
        assert result == []

    def test_dry_run_returns_low_confidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp)
            shared = "atlas memory temporal facts queries context inference provider"
            self._write_md(mem / "a.md", shared + " prefers dark interface workstation workflow")
            self._write_md(mem / "b.md", shared + " requires light interface keyboard shortcut")
            with patch("atlas_plus.curation.contradictions.MEMORY_ROOT", mem):
                result = find_contradictions(memory_root=mem, dry_run=True)
        for pair in result:
            assert pair.confidence == "low"

    def test_scope_restricts_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp)
            shared = (
                "atlas brain qwen model scheduler temporal facts queries memory "
                "annotations confidence inference session provider search archive"
            )
            self._write_md(mem / "feedback" / "style.md", shared)
            self._write_md(mem / "reference" / "style.md", shared + " more tokens")
            with patch("atlas_plus.curation.contradictions.MEMORY_ROOT", mem):
                # restrict to 'feedback' -- only one file there, so no pairs
                result = find_contradictions(memory_root=mem, scope="feedback", dry_run=True)
        assert result == []


# ---------------------------------------------------------------------------
# find_contradictions -- LLM path (mocked complete())
# ---------------------------------------------------------------------------

class TestFindContradictionsLLM:
    def _write_md(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntitle: test\ntype: reference\n---\n\n{body}\n",
                        encoding="utf-8")

    _SHARED = "atlas memory temporal facts queries context inference provider"

    def test_llm_contradiction_creates_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp)
            # Intermediate overlap: shared ~8 tokens, each file adds ~8 unique tokens
            self._write_md(mem / "a.md", self._SHARED + " prefers dark interface workstation pipeline")
            self._write_md(mem / "b.md", self._SHARED + " requires light interface keyboard shortcut")

            response = json.dumps([{
                "pair": 1, "contradicts": True,
                "confidence": "high",
                "explanation": "A says dark, B says light.",
            }])

            with patch("atlas_plus.curation.contradictions.MEMORY_ROOT", mem), \
                 patch("atlas_plus.summarizer.providers.complete", return_value=(response, {})):
                result = find_contradictions(memory_root=mem)

        assert len(result) == 1
        assert result[0].confidence == "high"
        assert "dark" in result[0].explanation.lower() or result[0].explanation

    def test_llm_no_contradiction_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp)
            self._write_md(mem / "a.md", self._SHARED + " prefers dark interface workstation pipeline")
            self._write_md(mem / "b.md", self._SHARED + " requires light interface keyboard shortcut")

            response = json.dumps([{
                "pair": 1, "contradicts": False,
                "confidence": "low", "explanation": "",
            }])

            with patch("atlas_plus.curation.contradictions.MEMORY_ROOT", mem), \
                 patch("atlas_plus.summarizer.providers.complete", return_value=(response, {})):
                result = find_contradictions(memory_root=mem)

        assert result == []

    def test_llm_markdown_fence_json_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp)
            self._write_md(mem / "a.md", self._SHARED + " prefers dark interface workstation pipeline")
            self._write_md(mem / "b.md", self._SHARED + " requires light interface keyboard shortcut")

            # LLM wraps response in markdown fences (common failure mode)
            items = json.dumps([{
                "pair": 1, "contradicts": True,
                "confidence": "medium", "explanation": "Conflicting theme preference.",
            }])
            fenced = f"```json\n{items}\n```"

            with patch("atlas_plus.curation.contradictions.MEMORY_ROOT", mem), \
                 patch("atlas_plus.summarizer.providers.complete", return_value=(fenced, {})):
                result = find_contradictions(memory_root=mem)

        assert len(result) == 1
        assert result[0].confidence == "medium"

    def test_llm_provider_error_continues_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp)
            self._write_md(mem / "a.md", self._SHARED + " prefers dark interface workstation pipeline")
            self._write_md(mem / "b.md", self._SHARED + " requires light interface keyboard shortcut")

            from atlas_plus.summarizer.providers import ProviderError
            with patch("atlas_plus.curation.contradictions.MEMORY_ROOT", mem), \
                 patch("atlas_plus.summarizer.providers.complete", side_effect=ProviderError("offline")):
                result = find_contradictions(memory_root=mem)

        assert result == []

    def test_llm_malformed_json_continues_gracefully(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp)
            self._write_md(mem / "a.md", self._SHARED + " prefers dark interface workstation pipeline")
            self._write_md(mem / "b.md", self._SHARED + " requires light interface keyboard shortcut")

            with patch("atlas_plus.curation.contradictions.MEMORY_ROOT", mem), \
                 patch("atlas_plus.summarizer.providers.complete", return_value=("not json at all", {})):
                result = find_contradictions(memory_root=mem)

        assert result == []
