"""Tests for session_narrative -- cross-session momentum analysis."""
from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_plus.session_narrative import (
    NarrativeReport,
    _compute_energy,
    _extract_active_projects,
    _extract_direction_signals,
    _extract_open_loops,
    _parse_dated_sessions,
    build_narrative,
    run_narrative,
    write_narrative,
)


def _session_file(sessions_dir: Path, date_str: str, body: str) -> Path:
    f = sessions_dir / f"{date_str}.md"
    f.write_text(body, encoding="utf-8")
    return f


class TestParseDateSessions:
    def test_empty_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp) / "sessions"
            sessions_dir.mkdir()
            with patch("atlas_plus.session_narrative._SESSIONS_DIR", sessions_dir):
                result = _parse_dated_sessions(since_days=30)
        assert result == []

    def test_recent_file_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp) / "sessions"
            sessions_dir.mkdir()
            today = date.today().isoformat()
            _session_file(sessions_dir, today, "# Session today\n\nWorking on atlas-memory+.")
            with patch("atlas_plus.session_narrative._SESSIONS_DIR", sessions_dir):
                result = _parse_dated_sessions(since_days=7)
        assert len(result) == 1
        assert result[0].date_str == today

    def test_old_file_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp) / "sessions"
            sessions_dir.mkdir()
            old = (date.today() - timedelta(days=30)).isoformat()
            _session_file(sessions_dir, old, "# Old session\n\nSome work.")
            with patch("atlas_plus.session_narrative._SESSIONS_DIR", sessions_dir):
                result = _parse_dated_sessions(since_days=7)
        assert result == []

    def test_non_date_files_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp) / "sessions"
            sessions_dir.mkdir()
            (sessions_dir / "_history.md").write_text("history content")
            (sessions_dir / "not-a-date.md").write_text("other content")
            with patch("atlas_plus.session_narrative._SESSIONS_DIR", sessions_dir):
                result = _parse_dated_sessions(since_days=30)
        assert result == []


class TestExtractOpenLoops:
    def test_detects_need_to(self):
        from atlas_plus.session_narrative import SessionEntry
        e = SessionEntry("2026-05-19", "t", "We need to fix the routing bug before next release.")
        result = _extract_open_loops([e])
        assert len(result) >= 1
        assert any("need to" in r.lower() for r in result)

    def test_detects_blocked_on(self):
        from atlas_plus.session_narrative import SessionEntry
        e = SessionEntry("2026-05-19", "t", "The feature is blocked on ATL-AUD-03 completion.")
        result = _extract_open_loops([e])
        assert any("blocked on" in r.lower() for r in result)

    def test_deduplicates_similar_lines(self):
        from atlas_plus.session_narrative import SessionEntry
        body = "Need to update the README file.\nNeed to update the README file."
        e = SessionEntry("2026-05-19", "t", body)
        result = _extract_open_loops([e])
        assert len(result) == 1

    def test_caps_at_15(self):
        from atlas_plus.session_narrative import SessionEntry
        lines = [f"Need to complete task number {i} in the backlog soon." for i in range(30)]
        e = SessionEntry("2026-05-19", "t", "\n".join(lines))
        result = _extract_open_loops([e])
        assert len(result) <= 15


class TestExtractDirectionSignals:
    def test_detects_building(self):
        from atlas_plus.session_narrative import SessionEntry
        e = SessionEntry("2026-05-19", "t", "Building the session narrative module for Atlas.")
        result = _extract_direction_signals([e])
        assert any("building" in r.lower() for r in result)

    def test_detects_implementing(self):
        from atlas_plus.session_narrative import SessionEntry
        e = SessionEntry("2026-05-19", "t", "Implementing the cross-session momentum analysis feature.")
        result = _extract_direction_signals([e])
        assert any("implementing" in r.lower() for r in result)


class TestComputeEnergy:
    def test_neutral_with_no_entries(self):
        result = _compute_energy([])
        assert result == "neutral"

    def test_neutral_with_one_entry(self):
        from atlas_plus.session_narrative import SessionEntry
        result = _compute_energy([SessionEntry("2026-05-19", "t", "some content")])
        assert result == "neutral"

    def test_returns_valid_value(self):
        from atlas_plus.session_narrative import SessionEntry
        entries = [
            SessionEntry(f"2026-05-{i:02d}", "t", "atlas-memory+ work building features")
            for i in range(1, 8)
        ]
        result = _compute_energy(entries)
        assert result in ("converging", "diverging", "neutral")


class TestBuildAndWriteNarrative:
    def test_build_no_sessions_returns_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp) / "sessions"
            sessions_dir.mkdir()
            with patch("atlas_plus.session_narrative._SESSIONS_DIR", sessions_dir):
                report = build_narrative(since_days=7)
        assert report.sessions_analyzed == 0

    def test_build_with_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp) / "sessions"
            sessions_dir.mkdir()
            today = date.today().isoformat()
            _session_file(sessions_dir, today,
                "# Work day\n\nBuilding the narrative module. Need to add tests. "
                "Working on atlas-memory+ and brain scheduler.")
            with patch("atlas_plus.session_narrative._SESSIONS_DIR", sessions_dir):
                report = build_narrative(since_days=7)
        assert report.sessions_analyzed == 1
        assert report.week_label.startswith("20")
        assert report.energy in ("converging", "diverging", "neutral")

    def test_write_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp) / "sessions"
            sessions_dir.mkdir()
            report = NarrativeReport(
                week_label="2026-W21",
                sessions_analyzed=3,
                open_loops=["Need to fix the routing bug."],
                direction_signals=["Building session narrative module."],
                active_projects=["atlas-memory+"],
                energy="converging",
                generated_at="2026-05-19T03:55+00:00",
            )
            with patch("atlas_plus.session_narrative._SESSIONS_DIR", sessions_dir):
                path = write_narrative(report)
            assert path.exists()  # assert inside the with block while tempdir is alive
            content = path.read_text()
            assert "2026-W21" in content
            assert "converging" in content
            assert "Need to fix the routing bug." in content

    def test_run_narrative_no_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp) / "sessions"
            sessions_dir.mkdir()
            with patch("atlas_plus.session_narrative._SESSIONS_DIR", sessions_dir):
                result = run_narrative(since_days=7)
        assert result["ok"] is False
        assert "no sessions" in result["reason"]

    def test_run_narrative_with_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions_dir = Path(tmp) / "sessions"
            sessions_dir.mkdir()
            today = date.today().isoformat()
            _session_file(sessions_dir, today,
                "Building the atlas-memory+ narrative module. Need to wire into scheduler.")
            with patch("atlas_plus.session_narrative._SESSIONS_DIR", sessions_dir):
                result = run_narrative(since_days=7)
        assert result["ok"] is True
        assert result["sessions"] == 1
        assert "path" in result
