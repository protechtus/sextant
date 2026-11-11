"""Tests for the _ticket_health_check Brain executor.

Covers:
- Flags in-progress tickets stale > ATLAS_TICKET_STALE_IP_DAYS (default 7)
- Flags blocked tickets needing attention > ATLAS_TICKET_STALE_BL_DAYS (default 3)
- Auto-resets orphaned in-progress tickets (non-human, > ATLAS_TICKET_ORPHAN_DAYS)
- Does not reset human-assigned in-progress tickets
- Reports healthy when no stale tickets found
- ExecutionResult shape: status=done, artifact written
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from atlas_plus.brain.executors import ExecutionResult, ExecutorContext, run_executor
from atlas_plus.brain.tasks import Task


def _ctx(tmp: Path) -> tuple[Task, ExecutorContext]:
    task = MagicMock(spec=Task)
    task.task_id = "test-health-001"
    task.kind = "ticket_health_check"
    task.instruction = "Audit ticket health"
    ctx = ExecutorContext(
        task=task,
        artifact_root=tmp / "artifacts",
        db_path=tmp / "state.sqlite",
        worker_id="test-worker",
    )
    return task, ctx


def _make_ticket(
    id: str = "ATL-001",
    status: str = "todo",
    assignee: str = "claude-code",
    updated: str | None = None,
    priority: str = "normal",
    title: str = "Test ticket",
    project: str = "atlas-memory+",
) -> MagicMock:
    t = MagicMock()
    t.id = id
    t.status = status
    t.assignee = assignee
    t.updated = updated or date.today().isoformat()
    t.priority = priority
    t.title = title
    t.project = project
    return t


def _stale_date(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


class TestTicketHealthStaleInProgress:
    """Flags in-progress tickets updated more than 7 days ago."""

    def test_fresh_in_progress_not_flagged(self, tmp_path):
        task, ctx = _ctx(tmp_path)
        tickets = [_make_ticket("ATL-001", status="in-progress", updated=_stale_date(3))]

        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets), \
             patch("atlas_plus.tickets.store.update_ticket_status") as mock_update:
            result = run_executor(task, ctx)

        assert result.status == "done"
        mock_update.assert_not_called()
        assert "0 stale in-progress" in result.text

    def test_stale_in_progress_flagged(self, tmp_path):
        task, ctx = _ctx(tmp_path)
        tickets = [_make_ticket("ATL-002", status="in-progress", updated=_stale_date(10))]

        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets), \
             patch("atlas_plus.tickets.store.update_ticket_status"):
            result = run_executor(task, ctx)

        assert result.status == "done"
        assert "1 stale in-progress" in result.text

    def test_stale_in_progress_appears_in_artifact(self, tmp_path):
        task, ctx = _ctx(tmp_path)
        tickets = [_make_ticket("ATL-003", status="in-progress", updated=_stale_date(15), title="Brain refactor")]

        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets), \
             patch("atlas_plus.tickets.store.update_ticket_status"):
            result = run_executor(task, ctx)

        assert result.artifact_path is not None
        artifact = Path(result.artifact_path)
        assert artifact.exists()
        content = artifact.read_text()
        assert "ATL-003" in content
        assert "Brain refactor" in content


class TestTicketHealthBlockedAttention:
    """Flags blocked tickets not updated for > 3 days."""

    def test_fresh_blocked_not_flagged(self, tmp_path):
        task, ctx = _ctx(tmp_path)
        tickets = [_make_ticket("ATL-010", status="blocked", updated=_stale_date(2))]

        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets), \
             patch("atlas_plus.tickets.store.update_ticket_status") as mock_update:
            result = run_executor(task, ctx)

        mock_update.assert_not_called()
        assert "0 blocked" in result.text

    def test_stale_blocked_flagged(self, tmp_path):
        task, ctx = _ctx(tmp_path)
        tickets = [_make_ticket("ATL-011", status="blocked", updated=_stale_date(5))]

        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets), \
             patch("atlas_plus.tickets.store.update_ticket_status"):
            result = run_executor(task, ctx)

        assert "1 blocked tickets need attention" in result.text


class TestTicketHealthOrphanReset:
    """Auto-resets orphaned in-progress non-human tickets past orphan threshold."""

    def test_orphan_reset_to_todo(self, tmp_path):
        task, ctx = _ctx(tmp_path)
        tickets = [_make_ticket("ATL-020", status="in-progress", assignee="claude-code", updated=_stale_date(20))]

        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets), \
             patch("atlas_plus.tickets.store.update_ticket_status") as mock_update:
            result = run_executor(task, ctx)

        mock_update.assert_called_once_with("ATL-020", "todo")
        assert "1 orphaned tickets auto-reset" in result.text

    def test_human_assigned_not_auto_reset(self, tmp_path):
        task, ctx = _ctx(tmp_path)
        tickets = [_make_ticket("ATL-021", status="in-progress", assignee="human", updated=_stale_date(30))]

        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets), \
             patch("atlas_plus.tickets.store.update_ticket_status") as mock_update:
            result = run_executor(task, ctx)

        mock_update.assert_not_called()

    def test_fresh_non_human_not_reset(self, tmp_path):
        task, ctx = _ctx(tmp_path)
        tickets = [_make_ticket("ATL-022", status="in-progress", assignee="codex", updated=_stale_date(5))]

        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets), \
             patch("atlas_plus.tickets.store.update_ticket_status") as mock_update:
            result = run_executor(task, ctx)

        mock_update.assert_not_called()


class TestTicketHealthResultShape:
    """ExecutionResult correctness: status=done, text summary, artifact path."""

    def test_healthy_result_when_no_stale_tickets(self, tmp_path):
        task, ctx = _ctx(tmp_path)
        tickets = [
            _make_ticket("ATL-030", status="todo", updated=_stale_date(1)),
            _make_ticket("ATL-031", status="in-progress", updated=_stale_date(2)),
        ]

        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets), \
             patch("atlas_plus.tickets.store.update_ticket_status"):
            result = run_executor(task, ctx)

        assert result.status == "done"
        assert result.artifact_path is not None
        artifact_text = Path(result.artifact_path).read_text()
        assert "healthy" in artifact_text.lower()

    def test_empty_ticket_list_returns_done(self, tmp_path):
        task, ctx = _ctx(tmp_path)

        with patch("atlas_plus.tickets.store.list_tickets", return_value=[]), \
             patch("atlas_plus.tickets.store.update_ticket_status"):
            result = run_executor(task, ctx)

        assert result.status == "done"

    def test_import_failure_returns_failed(self, tmp_path):
        task, ctx = _ctx(tmp_path)

        with patch("atlas_plus.tickets.store.list_tickets", side_effect=ImportError("no module")):
            result = run_executor(task, ctx)

        assert result.status == "failed"
        assert "ticket_health_check" in result.text
