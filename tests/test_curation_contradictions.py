"""Tests for atlas_plus.curation.contradictions -- contradiction detection.

Covers ContradictionPair.as_dict, _contradiction_prompt, and
find_contradictions dry_run mode (LLM calls never made in tests).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# TestContradictionPair
# ---------------------------------------------------------------------------

class TestContradictionPair:
    def test_as_dict_contains_all_fields(self):
        from atlas_plus.curation.contradictions import ContradictionPair
        pair = ContradictionPair(
            file1="feedback/pref-a.md",
            file2="feedback/pref-b.md",
            explanation="A says always use X; B says never use X.",
            confidence="high",
        )
        d = pair.as_dict()
        assert d["file1"] == "feedback/pref-a.md"
        assert d["file2"] == "feedback/pref-b.md"
        assert "always use X" in d["explanation"]
        assert d["confidence"] == "high"

    def test_as_dict_is_json_serialisable(self):
        import json
        from atlas_plus.curation.contradictions import ContradictionPair
        pair = ContradictionPair("a.md", "b.md", "conflict", "medium")
        assert json.dumps(pair.as_dict())  # must not raise


# ---------------------------------------------------------------------------
# TestContradictionPrompt
# ---------------------------------------------------------------------------

class TestContradictionPrompt:
    def test_includes_pair_header(self):
        from atlas_plus.curation.contradictions import _contradiction_prompt
        pairs = [("a.md", "snippet A", "b.md", "snippet B")]
        result = _contradiction_prompt(pairs)
        assert "Pair 1" in result

    def test_includes_file_names(self):
        from atlas_plus.curation.contradictions import _contradiction_prompt
        pairs = [("feedback/pref.md", "text A", "feedback/ref.md", "text B")]
        result = _contradiction_prompt(pairs)
        assert "feedback/pref.md" in result
        assert "feedback/ref.md" in result

    def test_includes_snippets(self):
        from atlas_plus.curation.contradictions import _contradiction_prompt
        pairs = [("a.md", "always use Flask", "b.md", "never use Flask")]
        result = _contradiction_prompt(pairs)
        assert "always use Flask" in result
        assert "never use Flask" in result

    def test_instructs_json_output(self):
        from atlas_plus.curation.contradictions import _contradiction_prompt
        result = _contradiction_prompt([("a.md", "x", "b.md", "y")])
        assert "JSON" in result
        assert "contradicts" in result

    def test_handles_multiple_pairs(self):
        from atlas_plus.curation.contradictions import _contradiction_prompt
        pairs = [
            ("a.md", "snippet A", "b.md", "snippet B"),
            ("c.md", "snippet C", "d.md", "snippet D"),
        ]
        result = _contradiction_prompt(pairs)
        assert "Pair 1" in result
        assert "Pair 2" in result


# ---------------------------------------------------------------------------
# TestFindContradictionsDryRun
# ---------------------------------------------------------------------------

class TestFindContradictionsDryRun:
    def _make_memory_root(self, tmp_path: Path) -> Path:
        mem_root = tmp_path / "memory"
        feedback = mem_root / "feedback"
        feedback.mkdir(parents=True)
        return mem_root

    def test_returns_empty_for_empty_memory(self, tmp_path):
        from atlas_plus.curation.contradictions import find_contradictions
        mem_root = self._make_memory_root(tmp_path)
        result = find_contradictions(memory_root=mem_root, dry_run=True)
        assert result == []

    def test_dry_run_does_not_call_llm(self, tmp_path):
        from atlas_plus.curation.contradictions import find_contradictions
        mem_root = self._make_memory_root(tmp_path)
        # Add two related files
        shared = "fastapi authentication middleware session token bearer"
        (mem_root / "feedback" / "a.md").write_text(
            f"---\ntitle: A\n---\n{shared} prefer flask instead.", encoding="utf-8"
        )
        (mem_root / "feedback" / "b.md").write_text(
            f"---\ntitle: B\n---\n{shared} always use django instead.", encoding="utf-8"
        )
        with patch("atlas_plus.summarizer.providers.complete") as mock_complete:
            find_contradictions(memory_root=mem_root, dry_run=True)
        mock_complete.assert_not_called()

    def test_dry_run_returns_candidates_with_dry_run_note(self, tmp_path):
        from atlas_plus.curation.contradictions import find_contradictions
        mem_root = self._make_memory_root(tmp_path)
        shared = "fastapi authentication middleware session token bearer"
        (mem_root / "feedback" / "a.md").write_text(
            f"---\ntitle: A\n---\n{shared} prefer flask", encoding="utf-8"
        )
        (mem_root / "feedback" / "b.md").write_text(
            f"---\ntitle: B\n---\n{shared} prefer django", encoding="utf-8"
        )
        result = find_contradictions(memory_root=mem_root, dry_run=True)
        if result:
            assert all("dry-run" in r.explanation for r in result)
            assert all(r.confidence == "low" for r in result)

    def test_scope_restricts_to_subdirectory(self, tmp_path):
        from atlas_plus.curation.contradictions import find_contradictions
        mem_root = self._make_memory_root(tmp_path)
        ref_dir = mem_root / "reference"
        ref_dir.mkdir()
        shared = "fastapi authentication middleware session token bearer"
        (mem_root / "feedback" / "a.md").write_text(
            f"---\ntitle: A\n---\n{shared}", encoding="utf-8"
        )
        (ref_dir / "b.md").write_text(
            f"---\ntitle: B\n---\n{shared}", encoding="utf-8"
        )
        # Scope to "reference" only -- no pairs possible with only 1 file
        result = find_contradictions(memory_root=mem_root, scope="reference", dry_run=True)
        # Only b.md is in scope; no pairs with 1 file
        assert len(result) == 0
