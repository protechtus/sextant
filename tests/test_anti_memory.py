"""Tests for atlas_plus.mcp.memory_server.tools.anti -- anti-memory logic.

Covers _extract_keywords, _flag_frontmatter, propagate_anti_rejection,
anti_remember (with autonomous gate mock), and list_anti.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# TestExtractKeywords
# ---------------------------------------------------------------------------

class TestExtractKeywords:
    def test_filters_short_words(self):
        from atlas_plus.mcp.memory_server.tools.anti import _extract_keywords
        result = _extract_keywords("do not use it at all")
        # "use" (3 chars) should be filtered; "not" is in stopwords
        assert all(len(w) >= 4 for w in result)

    def test_removes_stopwords(self):
        from atlas_plus.mcp.memory_server.tools.anti import _extract_keywords
        result = _extract_keywords("the framework should never be used here")
        assert "the" not in result
        assert "should" not in result
        assert "never" not in result

    def test_extracts_meaningful_words(self):
        from atlas_plus.mcp.memory_server.tools.anti import _extract_keywords
        result = _extract_keywords("avoid using requests library for async code")
        assert "avoid" in result
        assert "requests" in result
        assert "library" in result
        assert "async" in result
        assert "code" in result

    def test_returns_lowercase(self):
        from atlas_plus.mcp.memory_server.tools.anti import _extract_keywords
        result = _extract_keywords("Flask Django FastAPI frameworks")
        assert all(w == w.lower() for w in result)

    def test_empty_input_returns_empty(self):
        from atlas_plus.mcp.memory_server.tools.anti import _extract_keywords
        assert _extract_keywords("") == []
        assert _extract_keywords("   ") == []

    def test_handles_hyphenated_words(self):
        from atlas_plus.mcp.memory_server.tools.anti import _extract_keywords
        result = _extract_keywords("rate-limiting and auto-scaling patterns")
        # The pattern r'[a-zA-Z][a-zA-Z0-9_-]*' includes hyphens
        assert any("rate" in w or "limiting" in w or "rate-limiting" in w for w in result)


# ---------------------------------------------------------------------------
# TestFlagFrontmatter
# ---------------------------------------------------------------------------

class TestFlagFrontmatter:
    def _make_md(self, extra: str = "") -> str:
        return f"---\ntitle: Test\ntype: feedback{extra}\n---\n\nBody content here."

    def test_adds_anti_flagged_field(self):
        from atlas_plus.mcp.memory_server.tools.anti import _flag_frontmatter
        md = self._make_md()
        result = _flag_frontmatter(md, "libraries", "avoid requests for async")
        assert result is not None
        assert "anti_flagged: true" in result

    def test_adds_anti_reason_field(self):
        from atlas_plus.mcp.memory_server.tools.anti import _flag_frontmatter
        md = self._make_md()
        result = _flag_frontmatter(md, "libraries", "avoid requests for async")
        assert "anti_reason:" in result
        assert "avoid requests" in result

    def test_returns_none_for_already_flagged(self):
        from atlas_plus.mcp.memory_server.tools.anti import _flag_frontmatter
        md = self._make_md("\nanti_flagged: true")
        result = _flag_frontmatter(md, "libraries", "avoid requests for async")
        assert result is None

    def test_returns_none_for_file_without_frontmatter(self):
        from atlas_plus.mcp.memory_server.tools.anti import _flag_frontmatter
        md = "# No frontmatter\n\nJust body text."
        result = _flag_frontmatter(md, "libraries", "avoid requests")
        assert result is None

    def test_preserves_original_content(self):
        from atlas_plus.mcp.memory_server.tools.anti import _flag_frontmatter
        md = self._make_md()
        result = _flag_frontmatter(md, "libraries", "avoid requests for async")
        assert "Body content here" in result

    def test_truncates_long_reason(self):
        from atlas_plus.mcp.memory_server.tools.anti import _flag_frontmatter
        long_reason = "avoid " + "x" * 200
        md = self._make_md()
        result = _flag_frontmatter(md, "libraries", long_reason)
        # anti_reason should be truncated to 120 chars
        # (single-quoted YAML scalar since the 2026-11-09 data-safety fix)
        import yaml
        fm = yaml.safe_load(result.split("---")[1])
        assert len(fm["anti_reason"]) <= 120


# ---------------------------------------------------------------------------
# TestPropagateAntiRejection
# ---------------------------------------------------------------------------

class TestPropagateAntiRejection:
    def test_returns_empty_when_root_missing(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.anti import propagate_anti_rejection
        missing = tmp_path / "no-such-dir"
        result = propagate_anti_rejection("avoid requests", "libraries", missing)
        assert result == {"flagged": 0, "skipped": 0, "paths": []}

    def test_returns_empty_for_short_pattern(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.anti import propagate_anti_rejection
        result = propagate_anti_rejection("ok", "misc", tmp_path)
        assert result["flagged"] == 0

    def test_flags_matching_file(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.anti import propagate_anti_rejection
        mem_root = tmp_path / "memory"
        feedback_dir = mem_root / "feedback"
        feedback_dir.mkdir(parents=True)
        # Create a file that mentions "requests library" and "async"
        test_file = feedback_dir / "http-lib.md"
        test_file.write_text(
            "---\ntitle: HTTP Library\ntype: feedback\n---\n\n"
            "We have been using requests library for async code repeatedly.\n",
            encoding="utf-8",
        )
        result = propagate_anti_rejection(
            "avoid requests library for async code", "libraries", mem_root
        )
        assert result["flagged"] == 1
        content = test_file.read_text(encoding="utf-8")
        assert "anti_flagged: true" in content

    def test_skips_anti_directory_files(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.anti import propagate_anti_rejection
        mem_root = tmp_path / "memory"
        anti_dir = mem_root / "anti"
        anti_dir.mkdir(parents=True)
        anti_file = anti_dir / "libraries.md"
        anti_file.write_text(
            "---\ntitle: Anti\ntype: anti\n---\n\nrequests library async code pattern\n",
            encoding="utf-8",
        )
        result = propagate_anti_rejection(
            "avoid requests library for async code", "libraries", mem_root
        )
        assert result["flagged"] == 0
        # Anti file itself should not be modified
        assert "anti_flagged" not in anti_file.read_text(encoding="utf-8")

    def test_does_not_flag_file_with_insufficient_keyword_matches(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.anti import propagate_anti_rejection
        mem_root = tmp_path / "memory"
        feedback_dir = mem_root / "feedback"
        feedback_dir.mkdir(parents=True)
        # File only contains one of the keywords
        test_file = feedback_dir / "unrelated.md"
        test_file.write_text(
            "---\ntitle: Unrelated\ntype: feedback\n---\n\nSomething about databases.\n",
            encoding="utf-8",
        )
        result = propagate_anti_rejection(
            "avoid requests library for async code", "libraries", mem_root
        )
        assert result["flagged"] == 0


# ---------------------------------------------------------------------------
# TestAntiRemember
# ---------------------------------------------------------------------------

class TestAntiRemember:
    def test_returns_error_when_autonomous_off(self):
        from atlas_plus.mcp.memory_server.tools.anti import anti_remember
        with patch("atlas_plus.autonomous.require_autonomous_writes",
                   side_effect=RuntimeError("ATLAS_AUTONOMOUS is off")):
            result = anti_remember("libraries", "avoid requests")
        assert result["ok"] is False
        assert "error" in result

    def test_returns_error_for_empty_scope(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.anti import anti_remember
        with patch("atlas_plus.mcp.memory_server.tools.anti.ANTI_ROOT", tmp_path / "anti"), \
             patch("atlas_plus.autonomous.require_autonomous_writes"):
            result = anti_remember("", "avoid requests")
        assert result["ok"] is False

    def test_returns_error_for_empty_pattern(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.anti import anti_remember
        with patch("atlas_plus.mcp.memory_server.tools.anti.ANTI_ROOT", tmp_path / "anti"), \
             patch("atlas_plus.autonomous.require_autonomous_writes"):
            result = anti_remember("libraries", "   ")
        assert result["ok"] is False

    def test_returns_error_for_invalid_applies_to(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.anti import anti_remember
        with patch("atlas_plus.mcp.memory_server.tools.anti.ANTI_ROOT", tmp_path / "anti"), \
             patch("atlas_plus.autonomous.require_autonomous_writes"):
            result = anti_remember("libs", "avoid requests", applies_to="global")
        assert result["ok"] is False

    def test_creates_anti_file(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.anti import anti_remember
        anti_root = tmp_path / "anti"
        with patch("atlas_plus.mcp.memory_server.tools.anti.ANTI_ROOT", anti_root), \
             patch("atlas_plus.mcp.memory_server.tools.anti._MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.autonomous.require_autonomous_writes"):
            result = anti_remember("libraries", "avoid requests for async code")
        assert result["ok"] is True
        assert (anti_root / "libraries.md").exists()

    def test_records_duplicate_without_rewriting(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.anti import anti_remember
        anti_root = tmp_path / "anti"
        with patch("atlas_plus.mcp.memory_server.tools.anti.ANTI_ROOT", anti_root), \
             patch("atlas_plus.mcp.memory_server.tools.anti._MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.autonomous.require_autonomous_writes"):
            anti_remember("libraries", "avoid requests for async code")
            result2 = anti_remember("libraries", "avoid requests for async code")
        assert result2["ok"] is True
        assert result2.get("note") == "already recorded"


# ---------------------------------------------------------------------------
# TestListAnti
# ---------------------------------------------------------------------------

class TestListAnti:
    def test_returns_empty_when_no_anti_dir(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.anti import list_anti
        with patch("atlas_plus.mcp.memory_server.tools.anti.ANTI_ROOT",
                   tmp_path / "no-anti"):
            result = list_anti()
        assert result == []

    def test_returns_entries_from_anti_files(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.anti import list_anti
        anti_root = tmp_path / "anti"
        anti_root.mkdir()
        (anti_root / "libraries.md").write_text(
            "---\nid: anti-libraries\ntitle: Anti: libraries\ntype: anti\nstatus: active\n---\n\n"
            "# Do not suggest\n\n---\nreject_pattern: avoid requests\napplies_to: workspace\nadded: '2026-05-21'\n",
            encoding="utf-8",
        )
        with patch("atlas_plus.mcp.memory_server.tools.anti.ANTI_ROOT", anti_root):
            result = list_anti()
        assert len(result) >= 1
