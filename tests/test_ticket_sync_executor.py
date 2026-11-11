"""Tests for the _ticket_sync Brain executor.

Covers: close/fix parsing, conventional-commit heuristic, already-done skip,
missing-ticket skip, git error handling, and ExecutionResult shape.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from atlas_plus.brain.executors import ExecutionResult, ExecutorContext, run_executor
from atlas_plus.brain.tasks import Task


def _ctx(tmp: Path) -> tuple[Task, ExecutorContext]:
    task = MagicMock(spec=Task)
    task.task_id = "test-ticket-sync-001"
    task.kind = "ticket_sync"
    task.instruction = "Sync ticket statuses from git commit history"
    ctx = ExecutorContext(
        task=task,
        artifact_root=tmp / "artifacts",
        db_path=tmp / "state.sqlite",
        worker_id="test-worker",
    )
    return task, ctx


def _fake_ticket(status: str = "todo"):
    t = MagicMock()
    t.status = status
    return t


class TestTicketSyncClosePatterns:
    """_ticket_sync closes tickets that appear after closes/fixes keyword."""

    def test_closes_keyword_closes_ticket(self, tmp_path):
        task, ctx = _ctx(tmp_path)
        git_output = "closes ATL-042 fix regression in brain worker"

        with (
            patch("subprocess.run") as mock_run,
            patch("atlas_plus.tickets.store.get_ticket") as mock_get,
            patch("atlas_plus.tickets.store.update_ticket_status") as mock_update,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=git_output)
            mock_get.return_value = _fake_ticket("in-progress")

            result = run_executor(task, ctx)

        assert result.status == "done"
        mock_update.assert_called_once_with("ATL-042", "done")
        assert "closed 1" in result.text

    def test_fixes_keyword_closes_ticket(self, tmp_path):
        task, ctx = _ctx(tmp_path)
        git_output = "fix: broken parser -- fixes ATL-007"

        with (
            patch("subprocess.run") as mock_run,
            patch("atlas_plus.tickets.store.get_ticket") as mock_get,
            patch("atlas_plus.tickets.store.update_ticket_status") as mock_update,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=git_output)
            mock_get.return_value = _fake_ticket("todo")

            result = run_executor(task, ctx)

        assert result.status == "done"
        mock_update.assert_called_once_with("ATL-007", "done")

    def test_conventional_commit_prefix_closes_ticket(self, tmp_path):
        """feat:/fix:/docs: etc. title containing an ID counts as a ship signal."""
        task, ctx = _ctx(tmp_path)
        git_output = "feat(brain): add ticket_sync executor ATL-008"

        with (
            patch("subprocess.run") as mock_run,
            patch("atlas_plus.tickets.store.get_ticket") as mock_get,
            patch("atlas_plus.tickets.store.update_ticket_status") as mock_update,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=git_output)
            mock_get.return_value = _fake_ticket("in-progress")

            result = run_executor(task, ctx)

        assert result.status == "done"
        mock_update.assert_called_once_with("ATL-008", "done")

    def test_already_done_ticket_is_skipped(self, tmp_path):
        """Tickets already in 'done' status must not be updated again."""
        task, ctx = _ctx(tmp_path)
        git_output = "closes ATL-099 already shipped"

        with (
            patch("subprocess.run") as mock_run,
            patch("atlas_plus.tickets.store.get_ticket") as mock_get,
            patch("atlas_plus.tickets.store.update_ticket_status") as mock_update,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=git_output)
            mock_get.return_value = _fake_ticket("done")

            result = run_executor(task, ctx)

        assert result.status == "done"
        mock_update.assert_not_called()
        assert "closed 0" in result.text

    def test_missing_ticket_is_skipped(self, tmp_path):
        """If get_ticket returns None the executor must not crash."""
        task, ctx = _ctx(tmp_path)
        git_output = "closes NONEXISTENT-001"

        with (
            patch("subprocess.run") as mock_run,
            patch("atlas_plus.tickets.store.get_ticket", return_value=None),
            patch("atlas_plus.tickets.store.update_ticket_status") as mock_update,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=git_output)

            result = run_executor(task, ctx)

        assert result.status == "done"
        mock_update.assert_not_called()

    def test_git_error_recorded_not_raised(self, tmp_path):
        """A failing git command records an error string but returns status=done."""
        task, ctx = _ctx(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")

            result = run_executor(task, ctx)

        assert result.status == "done"
        assert "errors" in result.text or "error" in result.text.lower()

    def test_bare_mention_goes_to_referenced_not_closed(self, tmp_path):
        """An ID in a commit without a close/fix/ship signal is referenced, not closed."""
        task, ctx = _ctx(tmp_path)
        git_output = "update docs, see ATL-010 for context"

        with (
            patch("subprocess.run") as mock_run,
            patch("atlas_plus.tickets.store.get_ticket") as mock_get,
            patch("atlas_plus.tickets.store.update_ticket_status") as mock_update,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=git_output)
            mock_get.return_value = _fake_ticket("todo")

            result = run_executor(task, ctx)

        assert result.status == "done"
        mock_update.assert_not_called()
        assert "referenced 1" in result.text

    def test_repo_paths_keep_windows_drive_letters(self, tmp_path, monkeypatch):
        """Regression: repo parsing must not split on ':' -- it shredded C:\\... paths."""
        task, ctx = _ctx(tmp_path)
        monkeypatch.setenv("ATLAS_TICKET_SYNC_REPOS", r"C:\Tools;D:\Work\repo two")

        with (
            patch("subprocess.run") as mock_run,
            patch("atlas_plus.tickets.store.get_ticket", return_value=None),
            patch("atlas_plus.tickets.store.update_ticket_status"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            run_executor(task, ctx)

        # gitsafe scans repo config (a separate git call) before each log; count the log calls
        cwds = [c.kwargs.get("cwd") for c in mock_run.call_args_list if "log" in c.args[0]]
        assert cwds == [r"C:\Tools", r"D:\Work\repo two"]

    def test_result_includes_artifact_path(self, tmp_path):
        """ExecutionResult.artifact_path must be set (ticket-sync.md written)."""
        task, ctx = _ctx(tmp_path)

        with (
            patch("subprocess.run") as mock_run,
            patch("atlas_plus.tickets.store.get_ticket", return_value=None),
            patch("atlas_plus.tickets.store.update_ticket_status"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="")

            result = run_executor(task, ctx)

        assert result.artifact_path is not None
        assert "ticket-sync" in str(result.artifact_path)
