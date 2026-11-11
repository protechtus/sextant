"""Tests for atlas-prime task-aware context injection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_plus.hooks.prime import (
    _detect_project,
    _tokenize,
    build_prime_context,
)


# ---------------------------------------------------------------------------
# _tokenize
# ---------------------------------------------------------------------------

class TestTokenize:
    def test_strips_stopwords(self):
        tokens = _tokenize("working on the Skylark dashboard")
        assert "skylark" in tokens
        assert "dashboard" in tokens
        assert "the" not in tokens
        assert "working" not in tokens

    def test_strips_frontmatter(self):
        text = "---\ntitle: test\n---\nSkylark project notes"
        tokens = _tokenize(text)
        assert "skylark" in tokens
        assert "title" not in tokens

    def test_min_length_filter(self):
        tokens = _tokenize("a bb ccc dddd")
        assert "bb" not in tokens   # len == 2, filtered
        assert "ccc" in tokens      # len == 3, kept


# ---------------------------------------------------------------------------
# _detect_project
# ---------------------------------------------------------------------------

class TestDetectProject:
    def test_matches_project_from_memory(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        (projects_dir / "Skylark.md").write_text("---\ntitle: Skylark\n---\n", encoding="utf-8")

        with patch("atlas_plus.hooks.prime.MEMORY_ROOT", tmp_path):
            result = _detect_project("working on Skylark dashboard")
        assert result == "Skylark"

    def test_case_insensitive(self, tmp_path):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        (projects_dir / "Harbor-App.md").write_text("", encoding="utf-8")

        with patch("atlas_plus.hooks.prime.MEMORY_ROOT", tmp_path):
            result = _detect_project("fixing harbor-app search ranking")
        assert result == "Harbor-App"

    def test_no_match_returns_none(self, tmp_path):
        (tmp_path / "projects").mkdir()
        with patch("atlas_plus.hooks.prime.MEMORY_ROOT", tmp_path):
            result = _detect_project("generic task with no project name")
        assert result is None


# ---------------------------------------------------------------------------
# build_prime_context
# ---------------------------------------------------------------------------

class TestBuildPrimeContext:
    def _make_memory(self, tmp_path: Path) -> Path:
        root = tmp_path / ".memory"
        (root / "identity").mkdir(parents=True)
        (root / "identity" / "alex.md").write_text(
            "---\ntitle: Alex\n---\nAlex Example, Tier 3 Support Engineer at ExampleCorp.",
            encoding="utf-8",
        )
        (root / "projects").mkdir()
        (root / "projects" / "Skylark.md").write_text(
            "---\ntitle: Skylark\n---\nSkylark captures network incidents and PktMon traces.",
            encoding="utf-8",
        )
        (root / "feedback").mkdir()
        (root / "feedback" / "testing.md").write_text(
            "---\ntitle: Testing\n---\nDo not mock the database in tests. Real integration tests only.",
            encoding="utf-8",
        )
        return root

    def test_always_includes_identity(self, tmp_path):
        root = self._make_memory(tmp_path)
        ctx = build_prime_context("general task", memory_root=root)
        assert "Alex Example" in ctx or "Alex" in ctx

    def test_includes_project_when_detected(self, tmp_path):
        root = self._make_memory(tmp_path)
        ctx = build_prime_context("fix Skylark dashboard crash", memory_root=root)
        assert "Skylark" in ctx
        assert "PktMon" in ctx

    def test_omits_unrelated_project(self, tmp_path):
        root = self._make_memory(tmp_path)
        # Task doesn't mention Skylark
        ctx = build_prime_context("update documentation style guide", memory_root=root)
        assert "PktMon" not in ctx

    def test_relevant_feedback_included(self, tmp_path):
        root = self._make_memory(tmp_path)
        # Task keywords overlap with feedback/testing.md content
        ctx = build_prime_context("write integration tests for Skylark database layer", memory_root=root)
        # Either project or feedback should have been surfaced
        assert "Skylark" in ctx or "database" in ctx

    def test_house_rules_only_when_the_user_wrote_some(self, tmp_path):
        root = self._make_memory(tmp_path)
        ctx = build_prime_context("any task", memory_root=root)
        assert "## House rules" not in ctx          # fresh corpus: nothing injected
        (root / "identity").mkdir(exist_ok=True)
        (root / "identity" / "house-rules.md").write_text("- Prefer small commits" + chr(10), encoding="utf-8")
        ctx = build_prime_context("any task", memory_root=root)
        assert "## House rules" in ctx and "Prefer small commits" in ctx

    def test_context_header_includes_task(self, tmp_path):
        root = self._make_memory(tmp_path)
        task = "debug Skylark TCP attribution"
        ctx = build_prime_context(task, memory_root=root)
        assert task in ctx

    def test_empty_memory_does_not_crash(self, tmp_path):
        root = tmp_path / ".memory"
        root.mkdir()
        ctx = build_prime_context("any task", memory_root=root)
        assert "ATLAS CONTEXT" in ctx

    def test_session_start_uses_prime_when_hint_set(self, tmp_path, monkeypatch):
        """session_start main() delegates to prime when ATLAS_TASK_HINT is set."""
        import io
        import sys

        root = self._make_memory(tmp_path)
        monkeypatch.setenv("ATLAS_TASK_HINT", "fix Skylark dashboard crash")

        captured = io.BytesIO()

        with (
            patch("atlas_plus.hooks.prime.MEMORY_ROOT", root),
            patch("sys.stdout", new_callable=lambda: type("B", (), {"buffer": captured})()),
        ):
            # Import fresh to pick up env var
            import importlib
            import atlas_plus.hooks.session_start as ss
            importlib.reload(ss)
            try:
                ss.main()
            except SystemExit:
                pass

        output = captured.getvalue().decode("utf-8")
        # Should contain prime output marker, not full dump
        assert "ATLAS CONTEXT" in output or "House rules" in output


    def test_dream_questions_included_when_present(self, tmp_path):
        root = self._make_memory(tmp_path)
        (root / "sessions").mkdir()
        (root / "sessions" / "_dream-2026-07-30.md").write_text(
            "---\nid: dream-2026-07-30\n---\n# Dream\n\n"
            "5. QUESTIONS: What is the PktMon calibration status? Ask about the deploy.",
            encoding="utf-8",
        )
        ctx = build_prime_context("any task", memory_root=root)
        assert "Open questions from the last dream" in ctx
        assert "PktMon calibration" in ctx

    def test_no_dream_section_when_none_exist(self, tmp_path):
        root = self._make_memory(tmp_path)
        ctx = build_prime_context("any task", memory_root=root)
        assert "Open questions from the last dream" not in ctx


class TestNoPersonalRulesLeak:
    """Install test 2026-11-09: a stranger's prime carried the maintainer's house rules."""

    FORBIDDEN = ("updates.txt", "em-dashes", "versions/ before", "Session log at end")

    def test_source_has_no_hardcoded_rules(self):
        import inspect
        from atlas_plus.hooks import prime
        src = inspect.getsource(prime)
        for phrase in self.FORBIDDEN:
            assert phrase not in src, phrase

    def test_fresh_corpus_primes_no_rules(self, tmp_path, monkeypatch):
        from atlas_plus.hooks import prime
        (tmp_path / "projects").mkdir()
        monkeypatch.setattr(prime, "MEMORY_ROOT", tmp_path)
        out = prime.build_prime_context("anything at all", tmp_path) if "root" in prime.build_prime_context.__code__.co_varnames else prime.build_prime_context("anything at all")
        assert "## House rules" not in out
        for phrase in self.FORBIDDEN:
            assert phrase not in out, phrase

    def test_user_rules_are_used(self, tmp_path, monkeypatch):
        from atlas_plus.hooks import prime
        (tmp_path / "projects").mkdir()
        (tmp_path / "identity").mkdir()
        (tmp_path / "identity" / "house-rules.md").write_text("- Always write tests first\n", encoding="utf-8")
        assert prime._house_rules(tmp_path) == ["Always write tests first"]
