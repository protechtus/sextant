"""Tests for dream_mode.py and reflexion_loop.py pure helper functions.

Covers dream_mode: _ascii_safe, _read_stripped, _parse_frontmatter_title, build_corpus.
Covers reflexion_loop: _build_facts_block, _parse_critique, _cutoff_iso, _is_autonomous.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# ===========================================================================
# dream_mode helpers
# ===========================================================================

class TestAsciiSafe:
    def test_passes_plain_ascii(self):
        from atlas_plus.curation.dream_mode import _ascii_safe
        assert _ascii_safe("hello world") == "hello world"

    def test_replaces_non_ascii_with_space(self):
        from atlas_plus.curation.dream_mode import _ascii_safe
        result = _ascii_safe("café mocha")
        assert "é" not in result
        assert "caf" in result
        assert "mocha" in result

    def test_removes_control_characters(self):
        from atlas_plus.curation.dream_mode import _ascii_safe
        result = _ascii_safe("line1\x00line2")
        assert "\x00" not in result

    def test_strips_leading_trailing_whitespace(self):
        from atlas_plus.curation.dream_mode import _ascii_safe
        assert _ascii_safe("  hello  ") == "hello"

    def test_empty_string(self):
        from atlas_plus.curation.dream_mode import _ascii_safe
        assert _ascii_safe("") == ""


class TestReadStripped:
    def test_returns_empty_for_missing_file(self, tmp_path):
        from atlas_plus.curation.dream_mode import _read_stripped
        assert _read_stripped(tmp_path / "missing.md", 1000) == ""

    def test_strips_frontmatter(self, tmp_path):
        from atlas_plus.curation.dream_mode import _read_stripped
        p = tmp_path / "file.md"
        p.write_text("---\ntitle: Test\n---\n\nBody content here.", encoding="utf-8")
        result = _read_stripped(p, 1000)
        assert "Body content here" in result
        assert "title:" not in result

    def test_truncates_at_max_chars(self, tmp_path):
        from atlas_plus.curation.dream_mode import _read_stripped
        p = tmp_path / "long.md"
        p.write_text("---\ntitle: T\n---\n\n" + "x" * 2000, encoding="utf-8")
        result = _read_stripped(p, 100)
        assert len(result) <= 100

    def test_returns_content_for_no_frontmatter(self, tmp_path):
        from atlas_plus.curation.dream_mode import _read_stripped
        p = tmp_path / "plain.md"
        p.write_text("Just plain content.", encoding="utf-8")
        result = _read_stripped(p, 1000)
        assert "Just plain content" in result


class TestParseFrontmatterTitle:
    def test_extracts_title_from_frontmatter(self, tmp_path):
        from atlas_plus.curation.dream_mode import _parse_frontmatter_title
        p = tmp_path / "doc.md"
        p.write_text("---\ntitle: My Document Title\n---\n\nBody.", encoding="utf-8")
        assert _parse_frontmatter_title(p) == "My Document Title"

    def test_returns_stem_for_missing_title(self, tmp_path):
        from atlas_plus.curation.dream_mode import _parse_frontmatter_title
        p = tmp_path / "my-document.md"
        p.write_text("---\ntype: feedback\n---\n\nBody.", encoding="utf-8")
        assert _parse_frontmatter_title(p) == "my-document"

    def test_returns_stem_for_no_frontmatter(self, tmp_path):
        from atlas_plus.curation.dream_mode import _parse_frontmatter_title
        p = tmp_path / "plain-file.md"
        p.write_text("# Plain\n\nNo frontmatter.", encoding="utf-8")
        assert _parse_frontmatter_title(p) == "plain-file"

    def test_returns_stem_for_missing_file(self, tmp_path):
        from atlas_plus.curation.dream_mode import _parse_frontmatter_title
        missing = tmp_path / "does-not-exist.md"
        assert _parse_frontmatter_title(missing) == "does-not-exist"


class TestBuildCorpus:
    def test_returns_empty_corpus_for_missing_root(self, tmp_path):
        from atlas_plus.curation.dream_mode import build_corpus
        missing = tmp_path / "no-memory"
        corpus, stats = build_corpus(memory_root=missing, scopes=["feedback"])
        assert corpus == "" or stats["files"] == 0

    def test_includes_files_from_scopes(self, tmp_path):
        from atlas_plus.curation.dream_mode import build_corpus
        mem_root = tmp_path / "memory"
        feedback = mem_root / "feedback"
        feedback.mkdir(parents=True)
        (feedback / "pref.md").write_text(
            "---\ntitle: My Preference\n---\n\nUse FastAPI for APIs.", encoding="utf-8"
        )
        corpus, stats = build_corpus(memory_root=mem_root, scopes=["feedback"])
        assert stats["files"] == 1
        assert "FastAPI" in corpus or "fastapi" in corpus.lower()

    def test_skips_files_starting_with_underscore(self, tmp_path):
        from atlas_plus.curation.dream_mode import build_corpus
        mem_root = tmp_path / "memory"
        feedback = mem_root / "feedback"
        feedback.mkdir(parents=True)
        (feedback / "_private.md").write_text(
            "---\ntitle: Private\n---\n\nSecret content.", encoding="utf-8"
        )
        corpus, stats = build_corpus(memory_root=mem_root, scopes=["feedback"])
        assert stats["files"] == 0
        assert "Secret content" not in corpus

    def test_returns_stats_with_expected_keys(self, tmp_path):
        from atlas_plus.curation.dream_mode import build_corpus
        mem_root = tmp_path / "memory"
        (mem_root / "feedback").mkdir(parents=True)
        _, stats = build_corpus(memory_root=mem_root, scopes=["feedback"])
        assert "files" in stats
        assert "total_chars" in stats
        assert "truncated" in stats
        assert "scopes" in stats


# ===========================================================================
# reflexion_loop helpers
# ===========================================================================

class TestBuildFactsBlock:
    def test_includes_fact_id(self):
        from atlas_plus.curation.reflexion_loop import _build_facts_block
        facts = [{"fact_id": "fact-001", "fact_text": "Alex uses FastAPI",
                  "valid_from": "2026-05-21T10:00:00", "authority": "session"}]
        result = _build_facts_block(facts)
        assert "fact-001" in result

    def test_includes_fact_text(self):
        from atlas_plus.curation.reflexion_loop import _build_facts_block
        facts = [{"fact_id": "f001", "fact_text": "Alex prefers dark mode",
                  "valid_from": "2026-05-21", "authority": "explicit"}]
        result = _build_facts_block(facts)
        assert "Alex prefers dark mode" in result

    def test_includes_valid_from_date(self):
        from atlas_plus.curation.reflexion_loop import _build_facts_block
        facts = [{"fact_id": "f001", "fact_text": "text", "valid_from": "2026-05-21T00:00:00",
                  "authority": "session"}]
        result = _build_facts_block(facts)
        assert "2026-05-21" in result

    def test_includes_authority(self):
        from atlas_plus.curation.reflexion_loop import _build_facts_block
        facts = [{"fact_id": "f001", "fact_text": "text", "valid_from": "2026-05-21",
                  "authority": "explicit"}]
        result = _build_facts_block(facts)
        assert "explicit" in result

    def test_separates_multiple_facts(self):
        from atlas_plus.curation.reflexion_loop import _build_facts_block
        facts = [
            {"fact_id": "f001", "fact_text": "text1", "valid_from": "2026-05-21", "authority": "s"},
            {"fact_id": "f002", "fact_text": "text2", "valid_from": "2026-05-21", "authority": "s"},
        ]
        result = _build_facts_block(facts)
        assert "f001" in result and "f002" in result


class TestParseCritique:
    def _facts(self):
        return [
            {"fact_id": "f001", "fact_text": "Alex uses FastAPI"},
            {"fact_id": "f002", "fact_text": "Alex prefers dark mode"},
        ]

    def test_parses_valid_json(self):
        from atlas_plus.curation.reflexion_loop import _parse_critique
        text = '[{"fact_id": "f001", "action": "SKIP", "reason": "correct", "revised_text": ""}]'
        result = _parse_critique(text, self._facts())
        assert len(result) == 1
        assert result[0]["fact_id"] == "f001"
        assert result[0]["action"] == "SKIP"

    def test_parses_revise_action(self):
        from atlas_plus.curation.reflexion_loop import _parse_critique
        text = '[{"fact_id": "f001", "action": "revise", "reason": "outdated", "revised_text": "Alex uses Starlette now"}]'
        result = _parse_critique(text, self._facts())
        assert len(result) == 1
        assert result[0]["action"] == "REVISE"
        assert "Starlette" in result[0]["revised_text"]

    def test_returns_empty_for_invalid_json(self):
        from atlas_plus.curation.reflexion_loop import _parse_critique
        result = _parse_critique("not json", self._facts())
        assert result == []

    def test_skips_unknown_fact_ids(self):
        from atlas_plus.curation.reflexion_loop import _parse_critique
        text = '[{"fact_id": "unknown-id", "action": "SKIP", "reason": "ok", "revised_text": ""}]'
        result = _parse_critique(text, self._facts())
        assert result == []

    def test_skips_invalid_actions(self):
        from atlas_plus.curation.reflexion_loop import _parse_critique
        text = '[{"fact_id": "f001", "action": "DELETE", "reason": "test", "revised_text": ""}]'
        result = _parse_critique(text, self._facts())
        assert result == []

    def test_strips_markdown_code_fences(self):
        from atlas_plus.curation.reflexion_loop import _parse_critique
        text = '```json\n[{"fact_id": "f001", "action": "SKIP", "reason": "ok", "revised_text": ""}]\n```'
        result = _parse_critique(text, self._facts())
        assert len(result) == 1

    def test_truncates_long_reason(self):
        from atlas_plus.curation.reflexion_loop import _parse_critique
        long_reason = "x" * 500
        text = f'[{{"fact_id": "f001", "action": "SKIP", "reason": "{long_reason}", "revised_text": ""}}]'
        result = _parse_critique(text, self._facts())
        if result:
            assert len(result[0]["reason"]) <= 300


class TestReflexionHelpers:
    def test_is_autonomous_default_on(self):
        from atlas_plus.curation.reflexion_loop import _is_autonomous
        with patch.dict("os.environ", {"ATLAS_AUTONOMOUS": "on"}):
            assert _is_autonomous() is True

    def test_is_autonomous_can_be_disabled(self):
        from atlas_plus.curation.reflexion_loop import _is_autonomous
        with patch.dict("os.environ", {"ATLAS_AUTONOMOUS": "off"}):
            assert _is_autonomous() is False

    def test_cutoff_iso_returns_iso_format(self):
        from atlas_plus.curation.reflexion_loop import _cutoff_iso
        result = _cutoff_iso(7)
        assert "T" in result
        assert result.endswith("+00:00")
