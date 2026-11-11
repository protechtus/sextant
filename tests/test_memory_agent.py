"""Unit tests for memory_agent.py core scan functions.

Covers scan_decisions_to_revisit, scan_stale_projects, format_push_summary,
MemoryAgentReport helpers, and create_review_tickets without requiring
external services or a real .memory/ tree.
"""
from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_decision(decisions_dir: Path, slug: str, title: str, made: str,
                    status: str = "active", revisit_if: list | None = None) -> None:
    decisions_dir.mkdir(parents=True, exist_ok=True)
    if revisit_if:
        revisit_block = "revisit_if:\n" + "\n".join(f"  - {r}" for r in revisit_if)
    else:
        revisit_block = "revisit_if: []"
    content = (
        f"---\nid: {slug}\ntitle: {title}\ntype: decision\nstatus: {status}\n"
        f"made: {made}\n{revisit_block}\n---\n\nbody\n"
    )
    (decisions_dir / f"{slug}.md").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# MemoryAgentReport
# ---------------------------------------------------------------------------

class TestMemoryAgentReport:
    def test_total_issues_sums_all_lists(self):
        from atlas_plus.memory_agent import MemoryAgentReport
        r = MemoryAgentReport(
            stale_projects=[{"project": "a"}],
            decayed_facts=[{"path": "x.md"}, {"path": "y.md"}],
            decisions_to_revisit=[{"slug": "z"}],
        )
        assert r.total_issues() == 4

    def test_total_issues_zero_when_empty(self):
        from atlas_plus.memory_agent import MemoryAgentReport
        assert MemoryAgentReport().total_issues() == 0


# ---------------------------------------------------------------------------
# scan_decisions_to_revisit
# ---------------------------------------------------------------------------

class TestScanDecisionsToRevisit:
    def test_empty_when_no_decisions_dir(self, tmp_path):
        from atlas_plus.memory_agent import scan_decisions_to_revisit
        with patch("atlas_plus.memory_agent.MEMORY_ROOT", tmp_path):
            result = scan_decisions_to_revisit()
        assert result == []

    def test_returns_old_decision_with_revisit_if(self, tmp_path):
        from atlas_plus.memory_agent import scan_decisions_to_revisit
        old_date = (date.today() - timedelta(days=200)).isoformat()
        _write_decision(tmp_path / "decisions", "old-choice", "Old Choice",
                        old_date, revisit_if=["If team grows"])
        with patch("atlas_plus.memory_agent.MEMORY_ROOT", tmp_path):
            result = scan_decisions_to_revisit()
        assert len(result) == 1
        assert result[0]["slug"] == "old-choice"
        assert result[0]["age_days"] >= 200

    def test_skips_recent_decision(self, tmp_path):
        from atlas_plus.memory_agent import scan_decisions_to_revisit
        recent = (date.today() - timedelta(days=10)).isoformat()
        _write_decision(tmp_path / "decisions", "fresh", "Fresh", recent,
                        revisit_if=["condition"])
        with patch("atlas_plus.memory_agent.MEMORY_ROOT", tmp_path):
            result = scan_decisions_to_revisit()
        assert result == []

    def test_skips_inactive_decision(self, tmp_path):
        from atlas_plus.memory_agent import scan_decisions_to_revisit
        old_date = (date.today() - timedelta(days=200)).isoformat()
        _write_decision(tmp_path / "decisions", "archived", "Archived",
                        old_date, status="archived", revisit_if=["cond"])
        with patch("atlas_plus.memory_agent.MEMORY_ROOT", tmp_path):
            result = scan_decisions_to_revisit()
        assert result == []

    def test_skips_decision_with_empty_revisit_if(self, tmp_path):
        from atlas_plus.memory_agent import scan_decisions_to_revisit
        old_date = (date.today() - timedelta(days=200)).isoformat()
        _write_decision(tmp_path / "decisions", "no-cond", "No Conditions",
                        old_date, revisit_if=[])
        with patch("atlas_plus.memory_agent.MEMORY_ROOT", tmp_path):
            result = scan_decisions_to_revisit()
        assert result == []

    def test_sorted_by_age_descending(self, tmp_path):
        from atlas_plus.memory_agent import scan_decisions_to_revisit
        old = (date.today() - timedelta(days=300)).isoformat()
        older = (date.today() - timedelta(days=400)).isoformat()
        _write_decision(tmp_path / "decisions", "old", "Old", old, revisit_if=["c"])
        _write_decision(tmp_path / "decisions", "older", "Older", older, revisit_if=["c"])
        with patch("atlas_plus.memory_agent.MEMORY_ROOT", tmp_path):
            result = scan_decisions_to_revisit()
        assert result[0]["age_days"] >= result[1]["age_days"]


# ---------------------------------------------------------------------------
# scan_stale_projects
# ---------------------------------------------------------------------------

class TestScanStaleProjects:
    def test_returns_stale_when_no_memory_file(self, tmp_path):
        from atlas_plus.memory_agent import scan_stale_projects

        history = tmp_path / "memory" / "sessions" / "_history.md"
        history.parent.mkdir(parents=True, exist_ok=True)
        today_str = date.today().isoformat()
        history.write_text(
            f"## {today_str} - atlas-memory+ feature development\n\n(session notes)\n",
            encoding="utf-8",
        )

        projects_dir = tmp_path / "projects"
        (projects_dir / "atlas-memory+").mkdir(parents=True, exist_ok=True)

        with patch("atlas_plus.memory_agent.MEMORY_ROOT", tmp_path / "memory"), \
             patch("atlas_plus.memory_agent.PROJECTS_ROOT", projects_dir):
            result = scan_stale_projects(since_days=14)

        names = [r["project"] for r in result]
        assert "atlas-memory+" in names

    def test_empty_when_no_history(self, tmp_path):
        from atlas_plus.memory_agent import scan_stale_projects
        (tmp_path / "memory" / "sessions").mkdir(parents=True, exist_ok=True)
        with patch("atlas_plus.memory_agent.MEMORY_ROOT", tmp_path / "memory"), \
             patch("atlas_plus.memory_agent.PROJECTS_ROOT", tmp_path / "projects"):
            result = scan_stale_projects(since_days=14)
        assert result == []


# ---------------------------------------------------------------------------
# format_push_summary
# ---------------------------------------------------------------------------

class TestFormatPushSummary:
    def test_all_clear_when_no_issues(self):
        from atlas_plus.memory_agent import MemoryAgentReport, format_push_summary
        r = MemoryAgentReport(generated_at="2026-05-20T03:50:00+00:00")
        msg = format_push_summary(r)
        assert "All clear" in msg

    def test_mentions_stale_projects(self):
        from atlas_plus.memory_agent import MemoryAgentReport, format_push_summary
        r = MemoryAgentReport(
            generated_at="2026-05-20T03:50:00+00:00",
            stale_projects=[{"project": "gatekeeper"}],
        )
        msg = format_push_summary(r)
        assert "gatekeeper" in msg

    def test_mentions_decisions_count(self):
        from atlas_plus.memory_agent import MemoryAgentReport, format_push_summary
        r = MemoryAgentReport(
            generated_at="2026-05-20T03:50:00+00:00",
            decisions_to_revisit=[{"slug": "a"}, {"slug": "b"}],
        )
        msg = format_push_summary(r)
        assert "2" in msg

    def test_date_prefix_in_header(self):
        from atlas_plus.memory_agent import MemoryAgentReport, format_push_summary
        r = MemoryAgentReport(generated_at="2026-05-20T03:50:00+00:00")
        msg = format_push_summary(r)
        assert "2026-05-20" in msg


# ---------------------------------------------------------------------------
# create_review_tickets
# ---------------------------------------------------------------------------

class TestCreateReviewTickets:
    def test_creates_ticket_for_stale_project(self, tmp_path):
        from atlas_plus.memory_agent import MemoryAgentReport, create_review_tickets

        mock_ticket = MagicMock()
        mock_ticket.id = "ATL-T01"

        with patch("atlas_plus.tickets.store.create_ticket", return_value=mock_ticket):
            r = MemoryAgentReport(
                stale_projects=[{"project": "atlas-memory+", "reason": "no memory file", "days_old": None}],
            )
            ids = create_review_tickets(r)

        assert "ATL-T01" in ids

    def test_creates_ticket_for_decision_to_revisit(self, tmp_path):
        from atlas_plus.memory_agent import MemoryAgentReport, create_review_tickets

        mock_ticket = MagicMock()
        mock_ticket.id = "ATL-T02"

        with patch("atlas_plus.tickets.store.create_ticket", return_value=mock_ticket):
            r = MemoryAgentReport(
                decisions_to_revisit=[{
                    "slug": "use-faiss",
                    "title": "Use FAISS",
                    "project": "atlas-memory+",
                    "made": "2025-10-01",
                    "age_days": 230,
                    "revisit_if": ["corpus updates weekly"],
                }],
            )
            ids = create_review_tickets(r)

        assert "ATL-T02" in ids

    def test_returns_empty_list_when_nothing_actionable(self):
        from atlas_plus.memory_agent import MemoryAgentReport, create_review_tickets
        r = MemoryAgentReport()
        ids = create_review_tickets(r)
        assert ids == []
