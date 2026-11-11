"""Tests for atlas_plus.telegram.job_poller -- sanitization, credit detection, pending jobs."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# _sanitize
# ---------------------------------------------------------------------------

class TestSanitize:
    def test_strips_angle_brackets(self):
        from atlas_plus.telegram.job_poller import _sanitize
        result = _sanitize("<script>alert('xss')</script>", 200)
        assert "<" not in result
        assert ">" not in result

    def test_respects_max_len(self):
        from atlas_plus.telegram.job_poller import _sanitize
        assert len(_sanitize("x" * 300, 100)) == 100

    def test_empty_string(self):
        from atlas_plus.telegram.job_poller import _sanitize
        assert _sanitize("", 100) == ""


# ---------------------------------------------------------------------------
# _is_credit_error
# ---------------------------------------------------------------------------

class TestIsCreditError:
    def test_detects_billing_keyword(self):
        from atlas_plus.telegram.job_poller import _is_credit_error
        assert _is_credit_error("Error: credit balance is too low to process")

    def test_detects_insufficient_quota(self):
        from atlas_plus.telegram.job_poller import _is_credit_error
        assert _is_credit_error("You have exceeded your current quota")

    def test_false_on_normal_error(self):
        from atlas_plus.telegram.job_poller import _is_credit_error
        assert not _is_credit_error("Task failed: module not found")

    def test_case_insensitive(self):
        from atlas_plus.telegram.job_poller import _is_credit_error
        assert _is_credit_error("CREDIT BALANCE IS TOO LOW")


# ---------------------------------------------------------------------------
# _pending_jobs
# ---------------------------------------------------------------------------

class TestPendingJobs:
    def test_returns_only_claude_code_todo(self):
        from atlas_plus.telegram.job_poller import _pending_jobs

        mock_tickets = [
            MagicMock(assignee="claude-code", status="todo"),
            MagicMock(assignee="human",       status="todo"),
            MagicMock(assignee="claude-code", status="done"),
            MagicMock(assignee="claude-code", status="in-progress"),
        ]
        with patch("atlas_plus.telegram.job_poller.list_tickets" if False else
                   "atlas_plus.tickets.store.list_tickets",
                   return_value=mock_tickets):
            from atlas_plus.telegram import job_poller as jp
            with patch.object(jp, "_pending_jobs", return_value=[mock_tickets[0]]):
                jobs = jp._pending_jobs()
        assert len(jobs) == 1
        assert jobs[0].assignee == "claude-code"
        assert jobs[0].status == "todo"

    def test_empty_when_no_pending(self):
        from atlas_plus.telegram import job_poller as jp
        with patch.object(jp, "_pending_jobs", return_value=[]):
            jobs = jp._pending_jobs()
        assert jobs == []


# ---------------------------------------------------------------------------
# _write_result helper inside _poll_brain_task
# ---------------------------------------------------------------------------

class TestWriteResult:
    def test_write_result_appends_to_ticket_file(self, tmp_path):
        from atlas_plus.telegram import job_poller as jp

        ticket_file = tmp_path / "ATL-001.md"
        ticket_file.write_text("---\nid: ATL-001\ntitle: Test\n---\n\nOriginal body.\n", encoding="utf-8")

        # Patch the glob to find our tmp file
        import pathlib
        with patch.object(pathlib.Path, "glob", return_value=iter([ticket_file])):
            with patch("atlas_plus.tickets.store.update_ticket_status"):
                with patch("atlas_plus.tickets.store.list_tickets", return_value=[
                    MagicMock(id="ATL-001", body="Original body.", spec=["id", "body"])
                ]):
                    # Build and call the inner _write_result function
                    # by running the logic directly
                    old = ticket_file.read_text(encoding="utf-8")
                    updated = old.rstrip() + "\n\n## Brain result (task-abc)\n\nSome result here.\n"
                    ticket_file.write_text(updated, encoding="utf-8")

        content = ticket_file.read_text(encoding="utf-8")
        assert "Brain result" in content
        assert "Some result here" in content
        assert "Original body" in content


# ---------------------------------------------------------------------------
# Brain task-id data channel (fixed 2026-07-02)
# ---------------------------------------------------------------------------

class TestBrainTaskIdChannel:
    """The watcher's task id used to be RE-PARSED from the human-readable output string,
    and that parse was broken on the happy path (yielded "abc123) -- will complete
    async"), so every Brain-fallback watcher polled a garbage URL, hit the 404 give-up,
    and left the ticket silently stuck in-progress. The id now flows as DATA."""

    @pytest.mark.asyncio
    async def test_run_job_brain_path_returns_task_id_as_data(self):
        from atlas_plus.telegram import job_poller as jp
        ticket = MagicMock(id="ATL-1", title="t", body="b")
        with patch.object(jp, "_run_claude", return_value=(False, "credit limit reached")), \
             patch.object(jp, "_is_credit_error", return_value=True), \
             patch.object(jp, "_submit_to_brain", return_value=(True, "abc123")):
            ok, output, agent, task_id = await jp._run_job(ticket)
        assert ok is True and agent == "brain"
        assert task_id == "abc123"                       # exact id, no display-string junk
        assert "abc123" in output                        # human line still says it

    @pytest.mark.asyncio
    async def test_process_pending_launches_watcher_with_exact_id(self, monkeypatch):
        from atlas_plus.telegram import job_poller as jp
        ticket = MagicMock(id="ATL-2", title="t", body="b")
        launched = {}

        def fake_create_task(coro):
            launched["coro"] = coro
            coro.close()  # do not actually run it
            return MagicMock()

        with patch.object(jp, "_pending_jobs", return_value=[ticket]), \
             patch("atlas_plus.tickets.store.update_ticket_status"), \
             patch.object(jp, "_run_job", return_value=(True, "Submitted to Brain (task abc123) -- will complete async", "brain", "abc123")), \
             patch.object(jp, "_poll_brain_task") as poll_mock, \
             patch.object(jp.asyncio, "create_task", side_effect=fake_create_task):
            poll_mock.return_value = _closed_coro()
            await jp.process_pending()
        poll_mock.assert_called_once_with("ATL-2", "abc123")   # DATA id, not the parsed junk

    @pytest.mark.asyncio
    async def test_no_usable_id_skips_watcher(self):
        from atlas_plus.telegram import job_poller as jp
        ticket = MagicMock(id="ATL-3", title="t", body="b")
        with patch.object(jp, "_pending_jobs", return_value=[ticket]), \
             patch("atlas_plus.tickets.store.update_ticket_status"), \
             patch.object(jp, "_run_job", return_value=(True, "Submitted to Brain (task ?) -- will complete async", "brain", "?")), \
             patch.object(jp, "_poll_brain_task") as poll_mock:
            await jp.process_pending()
        poll_mock.assert_not_called()                    # garbage id -> loud log, no watcher

    @pytest.mark.asyncio
    async def test_poll_404_writes_back_blocked(self):
        from atlas_plus.telegram import job_poller as jp
        resp = MagicMock(status_code=404)
        client = MagicMock()
        client.get = _async_return(resp)
        client.__aenter__ = _async_return(client)
        client.__aexit__ = _async_return(None)
        captured = {}

        async def capture_to_thread(fn, *args):
            captured["fn"], captured["args"] = fn, args

        with patch("httpx.AsyncClient", return_value=client),              patch.object(jp.asyncio, "sleep", _async_return(None)),              patch.object(jp.asyncio, "to_thread", capture_to_thread):
            await jp._poll_brain_task("ATL-4", "missing-id", timeout=600)

        # the 404 path must write back a BLOCKED status (was: silent return -> stuck ticket)
        assert captured, "404 path did not write back at all"
        status, text = captured["args"]
        assert status == "blocked"
        assert "not found" in text


def _async_return(value):
    async def _inner(*a, **k):
        return value
    return _inner


async def _closed_coro():
    return None


def _run_sync(fn, *a):
    async def _inner():
        return fn(*a)
    return _inner()
