"""Tests for atlas_plus.brain.dispatcher.

Covers: classify_result_quality, reopen_ticket_if_low_quality, dispatch_ticket
routing (research_gap vs agent kind), _active_task_exists, is_dispatch_enabled.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# classify_result_quality
# ---------------------------------------------------------------------------

class TestClassifyResultQuality:
    def test_empty_returns_zero(self):
        from atlas_plus.brain.dispatcher import classify_result_quality
        assert classify_result_quality("") == 0.0
        assert classify_result_quality(None) == 0.0

    def test_refusal_pattern_returns_one(self):
        from atlas_plus.brain.dispatcher import classify_result_quality
        assert classify_result_quality("I cannot complete this task as requested.") == 1.0

    def test_short_result_returns_two(self):
        from atlas_plus.brain.dispatcher import classify_result_quality
        assert classify_result_quality("ok") == 2.0

    def test_acceptable_result_returns_seven(self):
        from atlas_plus.brain.dispatcher import classify_result_quality
        long_result = "This is a substantive result that contains enough content. " * 5
        assert classify_result_quality(long_result) == 7.0

    def test_sorry_pattern_low_quality(self):
        from atlas_plus.brain.dispatcher import classify_result_quality
        assert classify_result_quality("Sorry, I don't have access to the information.") == 1.0


# ---------------------------------------------------------------------------
# reopen_ticket_if_low_quality
# ---------------------------------------------------------------------------

class TestReopenTicketIfLowQuality:
    def test_high_quality_result_no_reopen(self):
        from atlas_plus.brain.dispatcher import reopen_ticket_if_low_quality
        long_result = "Completed the task with full analysis. " * 5
        with patch("atlas_plus.tickets.store.update_ticket_status") as mock_update:
            result = reopen_ticket_if_low_quality("ticket-ATL-001", long_result)
        assert result is False
        mock_update.assert_not_called()

    def test_low_quality_result_reopens_ticket(self, monkeypatch):
        from atlas_plus.brain.dispatcher import reopen_ticket_if_low_quality
        monkeypatch.setenv("ATLAS_TICKET_QUALITY_THRESHOLD", "4.0")
        with patch("atlas_plus.tickets.store.update_ticket_status") as mock_update:
            result = reopen_ticket_if_low_quality("ticket-ATL-002", "I cannot complete this.")
        assert result is True
        mock_update.assert_called_once_with("ATL-002", "todo")

    def test_non_ticket_session_id_no_reopen(self):
        from atlas_plus.brain.dispatcher import reopen_ticket_if_low_quality
        with patch("atlas_plus.tickets.store.update_ticket_status") as mock_update:
            result = reopen_ticket_if_low_quality("scheduler-research", "bad result")
        assert result is False
        mock_update.assert_not_called()

    def test_empty_session_id_no_reopen(self):
        from atlas_plus.brain.dispatcher import reopen_ticket_if_low_quality
        with patch("atlas_plus.tickets.store.update_ticket_status") as mock_update:
            result = reopen_ticket_if_low_quality("", "bad result")
        assert result is False
        mock_update.assert_not_called()


# ---------------------------------------------------------------------------
# dispatch_ticket -- kind routing
# ---------------------------------------------------------------------------

class TestDispatchTicketKindRouting:
    def _make_ticket(self, title: str, assignee: str = "any",
                     status: str = "todo") -> MagicMock:
        t = MagicMock()
        t.id = "ATL-099"
        t.title = title
        t.body = "body text"
        t.status = status
        t.assignee = assignee
        t.priority = "normal"
        return t

    def test_research_gap_title_routes_to_research_gap_kind(self):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = self._make_ticket("Research gap: SIP OPTIONS handling")
        created = []

        def fake_create(**kw):
            created.append(kw)
            t = MagicMock()
            t.task_id = "fake-001"
            return t

        with patch("atlas_plus.brain.tasks.create_task", side_effect=fake_create), \
             patch("atlas_plus.brain.dispatcher._active_task_exists", return_value=False), \
             patch("atlas_plus.tickets.store.update_ticket_status"):
            dispatch_ticket(ticket)

        assert created and created[0].get("kind") == "research_gap"

    def test_regular_title_routes_to_agent_kind(self):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = self._make_ticket("Fix the SIP parser bug")
        created = []

        def fake_create(**kw):
            created.append(kw)
            t = MagicMock()
            t.task_id = "fake-002"
            return t

        with patch("atlas_plus.brain.tasks.create_task", side_effect=fake_create), \
             patch("atlas_plus.brain.dispatcher._active_task_exists", return_value=False), \
             patch("atlas_plus.tickets.store.update_ticket_status"):
            dispatch_ticket(ticket)

        assert created and created[0].get("kind") == "agent"

    def test_done_ticket_not_dispatched(self):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = self._make_ticket("Some task", status="done")
        with patch("atlas_plus.brain.tasks.create_task") as mock_create:
            result = dispatch_ticket(ticket)
        assert result is None
        mock_create.assert_not_called()

    def test_human_assignee_not_dispatched(self):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = self._make_ticket("Human task", assignee="human")
        with patch("atlas_plus.brain.tasks.create_task") as mock_create:
            result = dispatch_ticket(ticket)
        assert result is None
        mock_create.assert_not_called()

    def test_active_task_skips_dispatch(self):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = self._make_ticket("Already running task")
        with patch("atlas_plus.brain.dispatcher._active_task_exists", return_value=True), \
             patch("atlas_plus.brain.tasks.create_task") as mock_create:
            result = dispatch_ticket(ticket)
        assert result is None
        mock_create.assert_not_called()

    def test_ticket_marked_in_progress_after_dispatch(self):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = self._make_ticket("Some task")
        mock_task = MagicMock()
        mock_task.task_id = "created-001"

        with patch("atlas_plus.brain.tasks.create_task", return_value=mock_task), \
             patch("atlas_plus.brain.dispatcher._active_task_exists", return_value=False), \
             patch("atlas_plus.tickets.store.update_ticket_status") as mock_status:
            dispatch_ticket(ticket)

        mock_status.assert_called_once_with("ATL-099", "in-progress")


# ---------------------------------------------------------------------------
# is_dispatch_enabled
# ---------------------------------------------------------------------------

class TestIsDispatchEnabled:
    def test_disabled_by_default(self, monkeypatch):
        from atlas_plus.brain.dispatcher import is_dispatch_enabled
        monkeypatch.delenv("ATLAS_TICKET_DISPATCH", raising=False)
        assert is_dispatch_enabled() is False

    def test_enabled_when_on(self, monkeypatch):
        from atlas_plus.brain.dispatcher import is_dispatch_enabled
        monkeypatch.setenv("ATLAS_TICKET_DISPATCH", "on")
        assert is_dispatch_enabled() is True

    def test_enabled_when_1(self, monkeypatch):
        from atlas_plus.brain.dispatcher import is_dispatch_enabled
        monkeypatch.setenv("ATLAS_TICKET_DISPATCH", "1")
        assert is_dispatch_enabled() is True

    def test_disabled_when_off(self, monkeypatch):
        from atlas_plus.brain.dispatcher import is_dispatch_enabled
        monkeypatch.setenv("ATLAS_TICKET_DISPATCH", "off")
        assert is_dispatch_enabled() is False
