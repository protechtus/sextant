"""Tests for research report deduplication (find_similar_research_reports)."""
from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path
from unittest.mock import patch


def _write_report(d: Path, name: str, title: str, slug: str, report_date: str | None = None) -> Path:
    """Write a minimal research report with the given frontmatter.

    report_date defaults to today so fixtures stay inside the 30-day dedup
    window (a hardcoded date here became a time bomb once it aged out).
    """
    if report_date is None:
        report_date = date.today().isoformat()
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(
        f"---\ntitle: \"{title}\"\ndate: {report_date}\nslug: {slug}\n---\n\n# {title}\n\nReport body here.\n",
        encoding="utf-8",
    )
    return p


class TestFindSimilarResearchReports:
    def test_returns_empty_when_no_reports_dir(self, tmp_path):
        from atlas_plus.brain.research import find_similar_research_reports
        with patch("atlas_plus.brain.research._ATLAS_DIR", tmp_path / "nonexistent"):
            result = find_similar_research_reports("SIP registration flow")
        assert result == []

    def test_returns_empty_when_no_similar_reports(self, tmp_path):
        from atlas_plus.brain.research import find_similar_research_reports
        rdir = tmp_path / "reports"
        _write_report(rdir, "report.md", "Research: Docker networking", "docker-networking")
        with patch("atlas_plus.brain.research._ATLAS_DIR", tmp_path):
            result = find_similar_research_reports("SIP over TLS authentication protocol")
        assert result == []

    def test_finds_similar_report_above_threshold(self, tmp_path):
        from atlas_plus.brain.research import find_similar_research_reports
        rdir = tmp_path / "reports"
        _write_report(rdir, "sip.md", "Research: SIP registration flow", "sip-registration-flow")
        with patch("atlas_plus.brain.research._ATLAS_DIR", tmp_path):
            result = find_similar_research_reports("SIP registration flow over UDP", threshold=0.3)
        assert len(result) >= 1
        assert result[0]["file"] == "sip.md"
        assert result[0]["score"] >= 0.3

    def test_sorted_by_score_descending(self, tmp_path):
        from atlas_plus.brain.research import find_similar_research_reports
        rdir = tmp_path / "reports"
        _write_report(rdir, "exact.md", "Research: SIP registration flow", "sip-registration-flow")
        _write_report(rdir, "partial.md", "Research: SIP overview", "sip-overview")
        with patch("atlas_plus.brain.research._ATLAS_DIR", tmp_path):
            result = find_similar_research_reports("SIP registration flow overview", threshold=0.2)
        if len(result) >= 2:
            assert result[0]["score"] >= result[1]["score"]

    def test_excludes_reports_older_than_max_age(self, tmp_path):
        from atlas_plus.brain.research import find_similar_research_reports
        rdir = tmp_path / "reports"
        _write_report(rdir, "old.md", "Research: SIP registration flow", "sip-registration-flow",
                      report_date="2025-01-01")
        with patch("atlas_plus.brain.research._ATLAS_DIR", tmp_path):
            result = find_similar_research_reports("SIP registration flow", max_age_days=30)
        assert result == []

    def test_includes_recent_reports(self, tmp_path):
        from atlas_plus.brain.research import find_similar_research_reports
        from datetime import date, timedelta
        rdir = tmp_path / "reports"
        recent_date = (date.today() - timedelta(days=5)).isoformat()
        _write_report(rdir, "recent.md", "Research: SIP registration flow", "sip-registration-flow",
                      report_date=recent_date)
        with patch("atlas_plus.brain.research._ATLAS_DIR", tmp_path):
            result = find_similar_research_reports("SIP registration", threshold=0.3, max_age_days=30)
        assert len(result) >= 1

    def test_result_has_expected_keys(self, tmp_path):
        from atlas_plus.brain.research import find_similar_research_reports
        rdir = tmp_path / "reports"
        _write_report(rdir, "sip.md", "Research: SIP registration flow", "sip-registration-flow")
        with patch("atlas_plus.brain.research._ATLAS_DIR", tmp_path):
            result = find_similar_research_reports("SIP registration", threshold=0.2)
        if result:
            assert "file" in result[0]
            assert "title" in result[0]
            assert "date" in result[0]
            assert "score" in result[0]


class TestRunResearchDeduplication:
    def test_raises_when_similar_report_exists(self, tmp_path):
        from atlas_plus.brain.research import run_research
        import pytest

        existing = {"file": "sip.md", "title": "Research: SIP flow", "date": "2026-05-18", "score": 0.65}
        with patch("atlas_plus.brain.research.find_similar_research_reports", return_value=[existing]):
            with pytest.raises(ValueError, match="Similar research report"):
                run_research("SIP registration flow", session_id="test-session")

    def test_force_skips_deduplication(self, tmp_path):
        from atlas_plus.brain.research import run_research
        from unittest.mock import MagicMock

        existing = {"file": "sip.md", "title": "Research: SIP flow", "date": "2026-05-18", "score": 0.65}
        mock_chat_result = MagicMock()
        mock_chat_result.text = (
            "## Summary\nSIP research.\n\n## Key Findings\n- One.\n- Two.\n- Three.\n\n"
            "## Context\nContext.\n\n## Gaps and Open Questions\nGaps.\n\n## Recommendations\nRecs.\n"
        )
        mock_chat_result.tool_calls_count = 0

        with patch("atlas_plus.brain.research.find_similar_research_reports", return_value=[existing]), \
             patch("atlas_plus.brain.research._ATLAS_DIR", tmp_path / ".atlas"), \
             patch("atlas_plus.brain.research._MEMORY_ROOT", tmp_path / ".memory"), \
             patch("atlas_plus.brain.agent.chat", return_value=mock_chat_result):
            # Should not raise when force=True
            body, path = run_research("SIP registration flow", session_id="test-session", force=True)
        assert path.exists()

    def test_no_error_when_no_similar_reports(self, tmp_path):
        from atlas_plus.brain.research import run_research
        from unittest.mock import MagicMock

        mock_chat_result = MagicMock()
        mock_chat_result.text = (
            "## Summary\nSIP research.\n\n## Key Findings\n- One.\n- Two.\n\n"
            "## Context\nContext.\n\n## Gaps and Open Questions\nGaps.\n\n## Recommendations\nRecs.\n"
        )
        mock_chat_result.tool_calls_count = 0

        with patch("atlas_plus.brain.research.find_similar_research_reports", return_value=[]), \
             patch("atlas_plus.brain.research._ATLAS_DIR", tmp_path / ".atlas"), \
             patch("atlas_plus.brain.research._MEMORY_ROOT", tmp_path / ".memory"), \
             patch("atlas_plus.brain.agent.chat", return_value=mock_chat_result):
            body, path = run_research("SIP registration flow", session_id="test-session")
        assert path.exists()


# ---------------------------------------------------------------------------
# Body-recall secondary dedup signal
# ---------------------------------------------------------------------------

class TestBodyRecallDedup:
    """Tests for the secondary body-recall dedup signal added in v0.13-G.

    A report whose title doesn't overlap much with a new topic can still
    be flagged as similar when its body text contains most of the topic tokens.
    """

    def _write_report(self, d: Path, name: str, title: str, date_str: str, body: str) -> None:
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(
            f"---\ntitle: {title!r}\ndate: {date_str}\nslug: {name.replace('.md','')}\n---\n\n{body}\n",
            encoding="utf-8",
        )

    def test_body_recall_flags_differently_titled_report(self, tmp_path):
        from atlas_plus.brain.research import find_similar_research_reports
        import datetime

        rdir = tmp_path / ".atlas" / "reports"
        today = datetime.date.today().isoformat()
        # Title is generic but body covers the exact topic
        body = (
            "This report covers SIP registration flows in detail. "
            "The registration process involves many steps. "
            "SIP protocol registration is complex. "
            "Understanding registration authentication is critical. "
        ) * 10  # repeat to fill ~300+ body words

        self._write_report(rdir, "sip-overview.md", "Atlas Protocol Overview", today, body)

        with patch("atlas_plus.brain.research._ATLAS_DIR", tmp_path / ".atlas"):
            results = find_similar_research_reports(
                "SIP registration protocol",
                threshold=0.3,
                max_age_days=30,
            )
        # The report should be found via body recall (many topic tokens in body)
        assert len(results) >= 1
        assert "sip-overview.md" in {r["file"] for r in results}

    def test_title_only_match_still_found(self, tmp_path):
        from atlas_plus.brain.research import find_similar_research_reports
        import datetime

        rdir = tmp_path / ".atlas" / "reports"
        today = datetime.date.today().isoformat()
        self._write_report(
            rdir, "sip-registration.md",
            "SIP Registration Protocol Analysis",
            today,
            "Some body text unrelated to topic.",
        )

        with patch("atlas_plus.brain.research._ATLAS_DIR", tmp_path / ".atlas"):
            results = find_similar_research_reports(
                "SIP registration protocol",
                threshold=0.3,
                max_age_days=30,
            )
        assert any("sip-registration.md" in r["file"] for r in results)

    def test_unrelated_report_not_flagged(self, tmp_path):
        from atlas_plus.brain.research import find_similar_research_reports
        import datetime

        rdir = tmp_path / ".atlas" / "reports"
        today = datetime.date.today().isoformat()
        self._write_report(
            rdir, "unrelated.md",
            "Python Async Programming Guide",
            today,
            "Event loops, coroutines, tasks, asyncio. Nothing about SIP.",
        )

        with patch("atlas_plus.brain.research._ATLAS_DIR", tmp_path / ".atlas"):
            results = find_similar_research_reports(
                "SIP registration protocol",
                threshold=0.3,
                max_age_days=30,
            )
        assert len(results) == 0
