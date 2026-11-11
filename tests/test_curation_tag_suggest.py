"""Tests for atlas_plus.curation.tag_suggest -- deterministic tag suggestion.

Covers _slugify, _title_keywords, suggest_tags_for_file, inject_tags.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# TestSlugify
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_lowercases_word(self):
        from atlas_plus.curation.tag_suggest import _slugify
        assert _slugify("Flask") == "flask"

    def test_replaces_spaces_with_hyphens(self):
        from atlas_plus.curation.tag_suggest import _slugify
        assert _slugify("rate limit") == "rate-limit"

    def test_replaces_underscores_with_hyphens(self):
        from atlas_plus.curation.tag_suggest import _slugify
        assert _slugify("rate_limit") == "rate-limit"

    def test_removes_special_chars(self):
        from atlas_plus.curation.tag_suggest import _slugify
        assert _slugify("C++ code") == "c-code"

    def test_empty_string(self):
        from atlas_plus.curation.tag_suggest import _slugify
        assert _slugify("") == ""


# ---------------------------------------------------------------------------
# TestTitleKeywords
# ---------------------------------------------------------------------------

class TestTitleKeywords:
    def test_strips_research_prefix(self):
        from atlas_plus.curation.tag_suggest import _title_keywords
        result = _title_keywords("Research: SIP Registration")
        assert "research" not in result
        assert "registration" in result or "sip" in result

    def test_filters_short_words(self):
        from atlas_plus.curation.tag_suggest import _title_keywords
        result = _title_keywords("Alex on SIP auth flow")
        assert all(len(w) >= 4 for w in result)

    def test_filters_stopwords(self):
        from atlas_plus.curation.tag_suggest import _title_keywords
        result = _title_keywords("The atlas notes for memory should be updated")
        assert "the" not in result
        assert "atlas" not in result
        assert "memory" not in result
        assert "notes" not in result
        assert "should" not in result

    def test_respects_limit(self):
        from atlas_plus.curation.tag_suggest import _title_keywords
        result = _title_keywords("FastAPI WebSocket Authentication Authorization", limit=2)
        assert len(result) <= 2

    def test_deduplicates_keywords(self):
        from atlas_plus.curation.tag_suggest import _title_keywords
        result = _title_keywords("FastAPI FastAPI FastAPI")
        assert len([w for w in result if w == "fastapi"]) == 1

    def test_returns_slugified_words(self):
        from atlas_plus.curation.tag_suggest import _title_keywords
        result = _title_keywords("Rate Limit Configuration")
        assert all(w == w.lower() for w in result)


# ---------------------------------------------------------------------------
# TestSuggestTagsForFile
# ---------------------------------------------------------------------------

class TestSuggestTagsForFile:
    def _make_file(self, tmp_path: Path, name: str, content: str) -> Path:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_returns_empty_for_missing_file(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        missing = tmp_path / "missing.md"
        result = suggest_tags_for_file(missing)
        assert result == []

    def test_returns_empty_for_file_without_frontmatter(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        p = self._make_file(tmp_path, "plain.md", "# No frontmatter\n\nBody.")
        result = suggest_tags_for_file(p)
        assert result == []

    def test_returns_empty_for_file_with_existing_tags(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        p = self._make_file(tmp_path, "tagged.md",
                            "---\ntitle: Tagged\ntags: [workflow, coding]\n---\nBody.")
        result = suggest_tags_for_file(p)
        assert result == []

    def test_suggests_tags_from_type_field(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        p = self._make_file(tmp_path, "test.md",
                            "---\ntitle: My Decision\ntype: decision\n---\nBody.")
        result = suggest_tags_for_file(p)
        assert "decision" in result

    def test_suggests_tags_from_directory(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        feedback_dir = tmp_path / "feedback"
        feedback_dir.mkdir()
        p = self._make_file(feedback_dir, "pref.md",
                            "---\ntitle: My Preference\n---\nBody.")
        result = suggest_tags_for_file(p, memory_root=tmp_path)
        assert "feedback" in result

    def test_suggests_tags_from_title_keywords(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        p = self._make_file(tmp_path, "test.md",
                            "---\ntitle: FastAPI Authentication Guide\n---\nBody.")
        result = suggest_tags_for_file(p)
        # Should include meaningful keywords from the title
        assert len(result) >= 1
        combined = " ".join(result)
        assert any(kw in combined for kw in ["fastapi", "authentication", "guide"])

    def test_caps_at_max_tags(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        feedback_dir = tmp_path / "feedback"
        feedback_dir.mkdir()
        p = self._make_file(feedback_dir, "test.md",
                            "---\ntitle: FastAPI Authentication Guide Django\ntype: feedback\n---\nBody.")
        result = suggest_tags_for_file(p, memory_root=tmp_path, max_tags=2)
        assert len(result) <= 2

    def test_returns_unique_tags(self, tmp_path):
        from atlas_plus.curation.tag_suggest import suggest_tags_for_file
        # type=feedback + dir=feedback -> both try to add "feedback"
        feedback_dir = tmp_path / "feedback"
        feedback_dir.mkdir()
        p = self._make_file(feedback_dir, "test.md",
                            "---\ntitle: Workflow Test\ntype: feedback\n---\nBody.")
        result = suggest_tags_for_file(p, memory_root=tmp_path)
        assert len(result) == len(set(result))


# ---------------------------------------------------------------------------
# TestInjectTags
# ---------------------------------------------------------------------------

class TestInjectTags:
    def test_returns_false_for_empty_tags(self, tmp_path):
        from atlas_plus.curation.tag_suggest import inject_tags
        p = tmp_path / "test.md"
        p.write_text("---\ntitle: Test\n---\nBody.", encoding="utf-8")
        assert inject_tags(p, []) is False

    def test_returns_false_for_missing_file(self, tmp_path):
        from atlas_plus.curation.tag_suggest import inject_tags
        missing = tmp_path / "missing.md"
        assert inject_tags(missing, ["feedback"]) is False

    def test_injects_tags_into_frontmatter(self, tmp_path):
        from atlas_plus.curation.tag_suggest import inject_tags
        p = tmp_path / "test.md"
        p.write_text("---\ntitle: Test\ntype: feedback\n---\nBody.", encoding="utf-8")
        result = inject_tags(p, ["feedback", "workflow"])
        assert result is True
        content = p.read_text(encoding="utf-8")
        assert "tags:" in content
        assert "feedback" in content
        assert "workflow" in content

    def test_returns_false_when_tags_already_present(self, tmp_path):
        from atlas_plus.curation.tag_suggest import inject_tags
        p = tmp_path / "test.md"
        p.write_text("---\ntitle: T\ntags: [existing]\n---\nBody.", encoding="utf-8")
        assert inject_tags(p, ["new-tag"]) is False

    def test_preserves_body_content(self, tmp_path):
        from atlas_plus.curation.tag_suggest import inject_tags
        p = tmp_path / "test.md"
        body = "# My Section\n\nImportant content here."
        p.write_text(f"---\ntitle: Test\n---\n\n{body}", encoding="utf-8")
        inject_tags(p, ["workflow"])
        content = p.read_text(encoding="utf-8")
        assert "Important content here" in content
