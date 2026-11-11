"""Tests for atlas_plus.telegram.formatting and telegram.notify."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# formatting.py
# ---------------------------------------------------------------------------

class TestEsc:
    def test_escapes_underscore(self):
        from atlas_plus.telegram.formatting import _esc
        assert _esc("hello_world") == "hello\\_world"

    def test_escapes_asterisk(self):
        from atlas_plus.telegram.formatting import _esc
        assert _esc("*bold*") == "\\*bold\\*"

    def test_escapes_backtick(self):
        from atlas_plus.telegram.formatting import _esc
        assert _esc("`code`") == "\\`code\\`"

    def test_plain_text_unchanged(self):
        from atlas_plus.telegram.formatting import _esc
        assert _esc("hello world") == "hello world"


class TestFmtTicket:
    def _make_ticket(self, **kw):
        from atlas_plus.tickets.store import Ticket
        defaults = dict(id="ATL-001", title="Fix login", project="atlas",
                        status="todo", assignee="claude-code", path=Path("t.md"))
        defaults.update(kw)
        return Ticket(**defaults)

    def test_includes_ticket_id(self):
        from atlas_plus.telegram.formatting import fmt_ticket
        t = self._make_ticket()
        text = fmt_ticket(t)
        assert "ATL-001" in text

    def test_includes_title(self):
        from atlas_plus.telegram.formatting import fmt_ticket
        t = self._make_ticket(title="Fix login bug")
        text = fmt_ticket(t)
        assert "Fix login bug" in text

    def test_includes_status(self):
        from atlas_plus.telegram.formatting import fmt_ticket
        t = self._make_ticket(status="in-progress")
        text = fmt_ticket(t)
        assert "in-progress" in text

    def test_human_assignee_omitted(self):
        from atlas_plus.telegram.formatting import fmt_ticket
        t = self._make_ticket(assignee="human")
        text = fmt_ticket(t)
        assert "@human" not in text

    def test_non_human_assignee_included(self):
        from atlas_plus.telegram.formatting import fmt_ticket
        t = self._make_ticket(assignee="claude-code")
        text = fmt_ticket(t)
        assert "@claude-code" in text

    def test_effort_included_when_set(self):
        from atlas_plus.telegram.formatting import fmt_ticket
        t = self._make_ticket(effort="M")
        text = fmt_ticket(t)
        assert "[M]" in text


class TestFmtTicketList:
    def _make_ticket(self, tid="ATL-001", title="A"):
        from atlas_plus.tickets.store import Ticket
        return Ticket(id=tid, title=title, project="atlas",
                      status="todo", path=Path("t.md"))

    def test_empty_list_shows_no_tickets(self):
        from atlas_plus.telegram.formatting import fmt_ticket_list
        text = fmt_ticket_list([])
        assert "No tickets" in text or "no tickets" in text.lower()

    def test_header_included_when_provided(self):
        from atlas_plus.telegram.formatting import fmt_ticket_list
        text = fmt_ticket_list([], header="Open Tickets:")
        assert "Open Tickets:" in text

    def test_lists_all_tickets(self):
        from atlas_plus.telegram.formatting import fmt_ticket_list
        tickets = [self._make_ticket("ATL-001", "First"),
                   self._make_ticket("ATL-002", "Second")]
        text = fmt_ticket_list(tickets)
        assert "ATL-001" in text and "ATL-002" in text


class TestFmtGitState:
    def test_not_git_repo_message(self):
        from atlas_plus.telegram.formatting import fmt_git_state
        text = fmt_git_state({"is_git_repo": False}, "myproject")
        assert "not a git repo" in text

    def test_shows_branch_and_dirty(self):
        from atlas_plus.telegram.formatting import fmt_git_state
        git = {"is_git_repo": True, "branch": "main", "dirty": True,
               "ahead": 2, "behind": 0}
        text = fmt_git_state(git, "atlas")
        assert "main" in text and "dirty" in text

    def test_shows_clean_when_not_dirty(self):
        from atlas_plus.telegram.formatting import fmt_git_state
        git = {"is_git_repo": True, "branch": "main", "dirty": False,
               "ahead": 0, "behind": 0}
        text = fmt_git_state(git, "atlas")
        assert "clean" in text

    def test_includes_project_name(self):
        from atlas_plus.telegram.formatting import fmt_git_state
        git = {"is_git_repo": True, "branch": "main", "dirty": False}
        text = fmt_git_state(git, "harbor-app")
        assert "harbor-app" in text


class TestEscapeMd:
    def test_escapes_parentheses(self):
        from atlas_plus.telegram.formatting import escape_md
        result = escape_md("hello (world)")
        assert "\\(" in result and "\\)" in result

    def test_escapes_underscore(self):
        from atlas_plus.telegram.formatting import escape_md
        result = escape_md("hello_world")
        assert "\\_" in result

    def test_plain_alphanumeric_unchanged(self):
        from atlas_plus.telegram.formatting import escape_md
        result = escape_md("hello world 123")
        assert result == "hello world 123"


# ---------------------------------------------------------------------------
# notify.py
# ---------------------------------------------------------------------------

class TestPush:
    @pytest.mark.asyncio
    async def test_returns_false_when_no_topic(self):
        from atlas_plus.telegram.notify import push
        with patch("atlas_plus.telegram.notify.NTFY_TOPIC", ""):
            result = await push("Title", "Message")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self):
        from atlas_plus.telegram.notify import push
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.notify.NTFY_TOPIC", "test-topic"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            result = await push("Title", "Message")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_http_error(self):
        from atlas_plus.telegram.notify import push
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=Exception("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.notify.NTFY_TOPIC", "test-topic"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            result = await push("Title", "Message")
        assert result is False

    @pytest.mark.asyncio
    async def test_uses_explicit_topic_override(self):
        from atlas_plus.telegram.notify import push
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.notify.NTFY_TOPIC", ""), \
             patch("httpx.AsyncClient", return_value=mock_client):
            result = await push("Title", "Message", topic="override-topic")
        assert result is True
        call_url = mock_client.post.call_args[0][0]
        assert "override-topic" in call_url


class TestPushHelpers:
    @pytest.mark.asyncio
    async def test_push_ticket_created_sends_low_priority(self):
        from atlas_plus.telegram.notify import push_ticket_created
        with patch("atlas_plus.telegram.notify.push", new_callable=AsyncMock) as mock_push:
            await push_ticket_created("ATL-001", "Fix login", "atlas")
        assert mock_push.called
        kwargs = mock_push.call_args.kwargs
        assert kwargs.get("priority") == "low" or mock_push.call_args[1].get("priority") == "low"

    @pytest.mark.asyncio
    async def test_push_job_done_calls_push(self):
        from atlas_plus.telegram.notify import push_job_done
        with patch("atlas_plus.telegram.notify.push", new_callable=AsyncMock) as mock_push:
            await push_job_done("ATL-001", "Task complete")
        assert mock_push.called

    @pytest.mark.asyncio
    async def test_push_job_failed_truncates_error(self):
        from atlas_plus.telegram.notify import push_job_failed
        long_error = "x" * 500
        with patch("atlas_plus.telegram.notify.push", new_callable=AsyncMock) as mock_push:
            await push_job_failed("ATL-001", long_error)
        assert mock_push.called
        msg_arg = mock_push.call_args[1].get("message") or mock_push.call_args[0][1]
        assert len(msg_arg) <= 300
