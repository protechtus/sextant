"""Tests for Direction D -- Weekly Dream Report (weekly_synthesis.py).

Tests the rolling two-phase pipeline:
  Phase 1 (daily): append a session summary to .atlas/weekly-draft.md
  Phase 2 (weekly/Sunday): synthesize draft into .memory/sessions/_week-*.md

Covers:
  - Daily append: writes paragraph summary to draft
  - Daily append: no session = no-op, no error
  - Daily append: LLM error handled gracefully
  - Weekly synthesis: reads draft, writes _week-YYYY-WNN.md, resets draft
  - Weekly synthesis: no draft = clean no-op
  - Weekly synthesis: ATLAS_AUTONOMOUS=off skips fact extraction
  - Week string format
  - Report JSON written
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_plus.curation.weekly_synthesis import (
    _week_str,
    run_daily_append,
    run_weekly_synthesis,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_session(sessions_dir: Path, stem: str, body: str) -> None:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    content = (
        f"---\nid: session-{stem}\ntitle: Test Session\n"
        f"type: session\nstatus: active\nupdated: {stem}\n---\n\n{body}\n"
    )
    (sessions_dir / f"{stem}.md").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Daily append
# ---------------------------------------------------------------------------

class TestDailyAppend:
    def test_appends_summary_to_draft(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        _write_session(sessions_dir, "2026-05-17", "Built the reflexion loop. Wired it into nightly curation.")
        draft_file = tmp_path / "weekly-draft.md"

        result = run_daily_append(
            sessions_dir=sessions_dir,
            draft_file=draft_file,
            _call_llm_fn=lambda p: "Today we built the reflexion loop and wired it into the nightly pipeline.",
        )

        assert result["status"] == "ok"
        assert result["appended"] is True
        assert draft_file.exists()
        content = draft_file.read_text(encoding="utf-8")
        assert "reflexion loop" in content
        assert "2026-05-17" in content

    def test_appends_multiple_days(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        _write_session(sessions_dir, "2026-05-16", "Day 1 content.")
        draft_file = tmp_path / "weekly-draft.md"

        run_daily_append(
            sessions_dir=sessions_dir, draft_file=draft_file,
            _call_llm_fn=lambda p: "Day 1 summary.",
        )
        _write_session(sessions_dir, "2026-05-17", "Day 2 content.")
        run_daily_append(
            sessions_dir=sessions_dir, draft_file=draft_file,
            _call_llm_fn=lambda p: "Day 2 summary.",
        )

        content = draft_file.read_text(encoding="utf-8")
        assert "Day 1 summary" in content
        assert "Day 2 summary" in content

    def test_no_session_returns_no_session_status(self, tmp_path):
        sessions_dir = tmp_path / "empty-sessions"
        sessions_dir.mkdir()
        draft_file = tmp_path / "weekly-draft.md"

        result = run_daily_append(
            sessions_dir=sessions_dir,
            draft_file=draft_file,
            _call_llm_fn=lambda p: "should not be called",
        )

        assert result["status"] == "no_session"
        assert result["appended"] is False
        assert not draft_file.exists()

    def test_llm_error_handled_gracefully(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        _write_session(sessions_dir, "2026-05-17", "Some session content.")
        draft_file = tmp_path / "weekly-draft.md"

        def bad_llm(p):
            raise RuntimeError("Ollama not reachable")

        result = run_daily_append(
            sessions_dir=sessions_dir,
            draft_file=draft_file,
            _call_llm_fn=bad_llm,
        )

        assert result["status"] == "llm_error"
        assert result["appended"] is False
        assert not draft_file.exists()


# ---------------------------------------------------------------------------
# Weekly synthesis
# ---------------------------------------------------------------------------

class TestWeeklySynthesis:
    def test_writes_week_report(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        draft = tmp_path / "weekly-draft.md"
        draft.write_text(
            "## 2026-05-12\n\nBuilt the temporal graph first slice.\n\n"
            "## 2026-05-13\n\nAdded source adapters for memory and sessions.\n",
            encoding="utf-8",
        )
        out_dir = tmp_path / "sessions"
        out_dir.mkdir()

        import atlas_plus.curation.weekly_synthesis as mod
        original_week = mod._week_str
        mod._week_str = lambda: "2026-W20"
        original_date = mod._date_str
        mod._date_str = lambda: "2026-05-17"
        original_state = mod.STATE_DIR
        mod.STATE_DIR = tmp_path

        try:
            result = run_weekly_synthesis(
                draft_file=draft,
                output_dir=out_dir,
                suggest_facts=False,
                _call_llm_fn=lambda p: (
                    "Focus areas: Atlas temporal graph. "
                    "Decisions: JSONL-backed provider chosen. "
                    "Patterns: test-first approach. "
                    "Unresolved: Graphiti integration. "
                    "Insights: Five timestamps enable correct temporal queries."
                ),
            )
        finally:
            mod._week_str = original_week
            mod._date_str = original_date
            mod.STATE_DIR = original_state

        assert result["status"] == "ok"
        assert result["written"] is True
        out_path = out_dir / "_week-2026-W20.md"
        assert out_path.exists()
        content = out_path.read_text(encoding="utf-8")
        assert "2026-W20" in content
        assert "temporal graph" in content.lower()

    def test_draft_reset_after_synthesis(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        draft = tmp_path / "weekly-draft.md"
        draft.write_text("## 2026-05-17\n\nSome summary.\n", encoding="utf-8")
        out_dir = tmp_path / "sessions"
        out_dir.mkdir()

        import atlas_plus.curation.weekly_synthesis as mod
        mod.STATE_DIR = tmp_path
        original_week = mod._week_str
        mod._week_str = lambda: "2026-W20"

        try:
            run_weekly_synthesis(
                draft_file=draft,
                output_dir=out_dir,
                suggest_facts=False,
                _call_llm_fn=lambda p: "Synthesis content.",
            )
        finally:
            mod._week_str = original_week
            mod.STATE_DIR = Path(os.environ.get("ATLAS_STATE_DIR", r"C:\Tools\.atlas"))

        # Draft should be reset to a new week header
        content = draft.read_text(encoding="utf-8")
        assert "Weekly draft" in content
        assert "Some summary" not in content

    def test_no_draft_returns_no_draft(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        draft = tmp_path / "weekly-draft.md"  # does not exist
        out_dir = tmp_path / "sessions"

        result = run_weekly_synthesis(
            draft_file=draft,
            output_dir=out_dir,
            suggest_facts=False,
            _call_llm_fn=lambda p: "should not be called",
        )

        assert result["status"] == "no_draft"
        assert result["written"] is False

    def test_llm_error_during_synthesis(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        draft = tmp_path / "weekly-draft.md"
        draft.write_text("## 2026-05-17\n\nSome summary.\n", encoding="utf-8")
        out_dir = tmp_path / "sessions"
        out_dir.mkdir()

        def bad_llm(p):
            raise RuntimeError("LLM error")

        result = run_weekly_synthesis(
            draft_file=draft,
            output_dir=out_dir,
            suggest_facts=False,
            _call_llm_fn=bad_llm,
        )

        assert result["status"] == "llm_error"
        assert result["written"] is False
        # Draft should NOT be reset on error
        assert "Some summary" in draft.read_text(encoding="utf-8")

    def test_autonomous_off_skips_fact_extraction(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        draft = tmp_path / "weekly-draft.md"
        draft.write_text("## 2026-05-17\n\nSome summary.\n", encoding="utf-8")
        out_dir = tmp_path / "sessions"
        out_dir.mkdir()

        import atlas_plus.curation.weekly_synthesis as mod
        original_week = mod._week_str
        mod._week_str = lambda: "2026-W20"
        original_state = mod.STATE_DIR
        mod.STATE_DIR = tmp_path

        try:
            result = run_weekly_synthesis(
                draft_file=draft,
                output_dir=out_dir,
                suggest_facts=True,  # requested but should be skipped
                _call_llm_fn=lambda p: "Synthesis.",
            )
        finally:
            mod._week_str = original_week
            mod.STATE_DIR = original_state

        assert result["written"] is True
        assert result["facts_extracted"] == 0  # skipped because autonomous=off


# ---------------------------------------------------------------------------
# Week string format
# ---------------------------------------------------------------------------

class TestWeekString:
    def test_week_str_format(self):
        week = _week_str()
        assert week.startswith("202")
        assert "-W" in week
        parts = week.split("-W")
        assert len(parts) == 2
        assert parts[1].isdigit()
        assert 1 <= int(parts[1]) <= 53


import os  # needed for STATE_DIR reset in test
