"""Tests for automated ticket dispatch (brain/dispatcher.py)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _make_ticket(
    ticket_id: str = "ATL-001",
    status: str = "todo",
    assignee: str = "claude-code",
    priority: str = "normal",
    title: str = "Implement feature X",
    body: str = "Detailed description here.",
):
    t = MagicMock()
    t.id = ticket_id
    t.status = status
    t.assignee = assignee
    t.priority = priority
    t.title = title
    t.body = body
    return t


def _make_task(task_id: str = "task-abc-123", agent: str = "claude-code"):
    t = MagicMock()
    t.task_id = task_id
    t.agent = agent
    return t


# ---------------------------------------------------------------------------
# _active_task_exists
# ---------------------------------------------------------------------------

class TestActiveTaskExists:
    def test_returns_false_when_no_tasks(self):
        from atlas_plus.brain.dispatcher import _active_task_exists
        with patch("atlas_plus.brain.dispatcher.list_tasks" if False else
                   "atlas_plus.brain.tasks.list_tasks") as mock:
            mock.return_value = []
            # Import the real function with patched internal
            from atlas_plus.brain import dispatcher
            with patch.object(dispatcher, "_active_task_exists", wraps=dispatcher._active_task_exists):
                # Direct test: patch the tasks module
                from unittest.mock import patch as _patch
                with _patch("atlas_plus.brain.tasks.list_tasks", return_value=[]):
                    result = dispatcher._active_task_exists("ticket-ATL-001")
        assert result is False

    def test_returns_true_when_matching_session_id_in_pending(self):
        from atlas_plus.brain import dispatcher
        mock_task = _make_task()
        mock_task.session_id = "ticket-ATL-001"
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[mock_task]):
            result = dispatcher._active_task_exists("ticket-ATL-001")
        assert result is True


# ---------------------------------------------------------------------------
# dispatch_ticket
# ---------------------------------------------------------------------------

class TestDispatchTicket:
    def test_skips_non_todo_ticket(self):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = _make_ticket(status="in-progress")
        result = dispatch_ticket(ticket)
        assert result is None

    def test_skips_human_assignee(self):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = _make_ticket(assignee="human")
        result = dispatch_ticket(ticket)
        assert result is None

    def test_skips_when_active_task_exists(self):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = _make_ticket()
        with patch("atlas_plus.brain.dispatcher._active_task_exists", return_value=True):
            result = dispatch_ticket(ticket)
        assert result is None

    def test_creates_task_for_claude_code(self):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = _make_ticket(assignee="claude-code", ticket_id="ATL-005")
        mock_task = _make_task(agent="claude-code")
        with patch("atlas_plus.brain.dispatcher._active_task_exists", return_value=False), \
             patch("atlas_plus.brain.tasks.create_task", return_value=mock_task) as mock_create, \
             patch("atlas_plus.tickets.store.update_ticket_status"):
            result = dispatch_ticket(ticket)
        assert result is not None
        assert mock_create.called
        call_kwargs = mock_create.call_args
        assert call_kwargs.kwargs.get("session_id") == "ticket-ATL-005"
        assert call_kwargs.kwargs.get("agent") == "claude-code"

    def test_creates_task_for_local_assignee_routes_to_brain(self):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = _make_ticket(assignee="local", ticket_id="ATL-006")
        mock_task = _make_task(agent="brain")
        with patch("atlas_plus.brain.dispatcher._active_task_exists", return_value=False), \
             patch("atlas_plus.brain.tasks.create_task", return_value=mock_task) as mock_create, \
             patch("atlas_plus.tickets.store.update_ticket_status"):
            dispatch_ticket(ticket)
        assert mock_create.call_args.kwargs.get("agent") == "brain"

    def test_creates_task_for_any_assignee_uses_auto_routing(self):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = _make_ticket(assignee="any", ticket_id="ATL-007")
        mock_task = _make_task(agent=None)
        with patch("atlas_plus.brain.dispatcher._active_task_exists", return_value=False), \
             patch("atlas_plus.brain.tasks.create_task", return_value=mock_task) as mock_create, \
             patch("atlas_plus.tickets.store.update_ticket_status"):
            dispatch_ticket(ticket)
        assert mock_create.call_args.kwargs.get("agent") is None

    def test_instruction_combines_title_and_body(self):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = _make_ticket(title="Fix bug", body="Steps to reproduce:\n1. Open app\n2. Crash")
        mock_task = _make_task()
        with patch("atlas_plus.brain.dispatcher._active_task_exists", return_value=False), \
             patch("atlas_plus.brain.tasks.create_task", return_value=mock_task) as mock_create, \
             patch("atlas_plus.tickets.store.update_ticket_status"):
            dispatch_ticket(ticket)
        # instruction is passed as first positional or keyword arg
        call = mock_create.call_args
        instruction = call.args[0] if call.args else call.kwargs.get("instruction", "")
        assert "Fix bug" in instruction
        assert "Steps to reproduce" in instruction

    def test_sets_ticket_in_progress_after_task_created(self):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = _make_ticket(ticket_id="ATL-008")
        mock_task = _make_task()
        with patch("atlas_plus.brain.dispatcher._active_task_exists", return_value=False), \
             patch("atlas_plus.brain.tasks.create_task", return_value=mock_task), \
             patch("atlas_plus.tickets.store.update_ticket_status") as mock_status:
            dispatch_ticket(ticket)
        mock_status.assert_called_once_with("ATL-008", "in-progress")

    def test_priority_mapping_critical(self):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = _make_ticket(priority="critical", ticket_id="ATL-009")
        mock_task = _make_task()
        with patch("atlas_plus.brain.dispatcher._active_task_exists", return_value=False), \
             patch("atlas_plus.brain.tasks.create_task", return_value=mock_task) as mock_create, \
             patch("atlas_plus.tickets.store.update_ticket_status"):
            dispatch_ticket(ticket)
        assert mock_create.call_args.kwargs.get("priority") == 1


# ---------------------------------------------------------------------------
# dispatch_all_eligible
# ---------------------------------------------------------------------------

class TestDispatchAllEligible:
    def test_returns_empty_when_no_eligible_tickets(self):
        from atlas_plus.brain.dispatcher import dispatch_all_eligible
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            result = dispatch_all_eligible()
        assert result == []

    def test_skips_human_tickets(self):
        from atlas_plus.brain.dispatcher import dispatch_all_eligible
        ticket = _make_ticket(assignee="human")
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[ticket]):
            result = dispatch_all_eligible()
        assert result == []

    def test_dispatches_eligible_ticket(self):
        from atlas_plus.brain.dispatcher import dispatch_all_eligible
        ticket = _make_ticket(assignee="claude-code", ticket_id="ATL-010")
        mock_task = _make_task(task_id="task-xyz-789", agent="claude-code")
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[ticket]), \
             patch("atlas_plus.brain.dispatcher._active_task_exists", return_value=False), \
             patch("atlas_plus.brain.tasks.create_task", return_value=mock_task), \
             patch("atlas_plus.tickets.store.update_ticket_status"):
            result = dispatch_all_eligible()
        assert len(result) == 1
        assert result[0]["ticket_id"] == "ATL-010"
        assert result[0]["task_id"] == "task-xyz-789"

    def test_returns_empty_on_list_tickets_error(self):
        from atlas_plus.brain.dispatcher import dispatch_all_eligible
        with patch("atlas_plus.tickets.store.list_tickets", side_effect=Exception("DB error")):
            result = dispatch_all_eligible()
        assert result == []


# ---------------------------------------------------------------------------
# is_dispatch_enabled
# ---------------------------------------------------------------------------

class TestIsDispatchEnabled:
    def test_off_by_default(self, monkeypatch):
        from atlas_plus.brain.dispatcher import is_dispatch_enabled
        monkeypatch.delenv("ATLAS_TICKET_DISPATCH", raising=False)
        assert is_dispatch_enabled() is False

    def test_on_when_env_set(self, monkeypatch):
        from atlas_plus.brain.dispatcher import is_dispatch_enabled
        monkeypatch.setenv("ATLAS_TICKET_DISPATCH", "on")
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        assert is_dispatch_enabled() is True

    def test_off_when_autonomous_off(self, monkeypatch):
        from atlas_plus.brain.dispatcher import is_dispatch_enabled
        monkeypatch.setenv("ATLAS_TICKET_DISPATCH", "on")
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        assert is_dispatch_enabled() is False


# ---------------------------------------------------------------------------
# classify_result_quality
# ---------------------------------------------------------------------------

class TestClassifyResultQuality:
    def test_empty_string_returns_zero(self):
        from atlas_plus.brain.dispatcher import classify_result_quality
        assert classify_result_quality("") == 0.0

    def test_none_equivalent_returns_zero(self):
        from atlas_plus.brain.dispatcher import classify_result_quality
        assert classify_result_quality(None) == 0.0  # type: ignore[arg-type]

    def test_whitespace_only_returns_zero(self):
        from atlas_plus.brain.dispatcher import classify_result_quality
        assert classify_result_quality("   \n  ") == 0.0

    def test_refusal_pattern_returns_one(self):
        from atlas_plus.brain.dispatcher import classify_result_quality
        assert classify_result_quality("I cannot help with that request.") == 1.0

    def test_i_do_not_know_pattern_returns_one(self):
        from atlas_plus.brain.dispatcher import classify_result_quality
        assert classify_result_quality("I do not know the answer.") == 1.0

    def test_as_an_ai_pattern_returns_one(self):
        from atlas_plus.brain.dispatcher import classify_result_quality
        assert classify_result_quality("As an AI language model, I cannot...") == 1.0

    def test_brain_error_pattern_returns_one(self):
        from atlas_plus.brain.dispatcher import classify_result_quality
        assert classify_result_quality("[brain] error: connection refused") == 1.0

    def test_short_result_returns_two(self):
        from atlas_plus.brain.dispatcher import classify_result_quality
        assert classify_result_quality("Done.") == 2.0

    def test_acceptable_result_returns_seven(self):
        from atlas_plus.brain.dispatcher import classify_result_quality
        text = (
            "Implemented the requested feature. Added input validation, unit tests, "
            "and updated the configuration schema. All 12 existing tests still pass."
        )
        assert classify_result_quality(text) == 7.0

    def test_case_insensitive_pattern_matching(self):
        from atlas_plus.brain.dispatcher import classify_result_quality
        # Uppercase I CANNOT should still match
        assert classify_result_quality("I CANNOT complete this task.") == 1.0


# ---------------------------------------------------------------------------
# reopen_ticket_if_low_quality
# ---------------------------------------------------------------------------

class TestReopenTicketIfLowQuality:
    def test_non_ticket_session_id_returns_false(self):
        from atlas_plus.brain.dispatcher import reopen_ticket_if_low_quality
        result = reopen_ticket_if_low_quality("scheduler-curation", "some result text")
        assert result is False

    def test_empty_session_id_returns_false(self):
        from atlas_plus.brain.dispatcher import reopen_ticket_if_low_quality
        result = reopen_ticket_if_low_quality("", "some result text")
        assert result is False

    def test_high_quality_result_does_not_reopen(self, monkeypatch):
        from atlas_plus.brain.dispatcher import reopen_ticket_if_low_quality
        monkeypatch.setenv("ATLAS_TICKET_QUALITY_THRESHOLD", "4.0")
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "atlas_plus.tickets.store.update_ticket_status"
        ) as mock_update:
            good_text = (
                "Implemented the feature as requested. Added 3 functions, "
                "updated docs, and all 20 tests pass. See attached artifact."
            )
            result = reopen_ticket_if_low_quality("ticket-ATL-020", good_text)
        assert result is False
        mock_update.assert_not_called()

    def test_low_quality_result_reopens_ticket(self, monkeypatch):
        from atlas_plus.brain.dispatcher import reopen_ticket_if_low_quality
        monkeypatch.setenv("ATLAS_TICKET_QUALITY_THRESHOLD", "4.0")
        updated = []
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "atlas_plus.tickets.store.update_ticket_status",
            side_effect=lambda tid, status: updated.append((tid, status)),
        ):
            result = reopen_ticket_if_low_quality("ticket-ATL-021", "I cannot do this.")
        assert result is True
        assert updated == [("ATL-021", "todo")]

    def test_empty_result_reopens_ticket(self, monkeypatch):
        from atlas_plus.brain.dispatcher import reopen_ticket_if_low_quality
        monkeypatch.setenv("ATLAS_TICKET_QUALITY_THRESHOLD", "4.0")
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "atlas_plus.tickets.store.update_ticket_status"
        ) as mock_update:
            result = reopen_ticket_if_low_quality("ticket-ATL-022", "")
        assert result is True
        mock_update.assert_called_once_with("ATL-022", "todo")

    def test_ticket_id_uppercased_in_store_call(self, monkeypatch):
        from atlas_plus.brain.dispatcher import reopen_ticket_if_low_quality
        monkeypatch.setenv("ATLAS_TICKET_QUALITY_THRESHOLD", "4.0")
        updated = []
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "atlas_plus.tickets.store.update_ticket_status",
            side_effect=lambda tid, status: updated.append(tid),
        ):
            reopen_ticket_if_low_quality("ticket-atl-023", "I cannot do this.")
        assert updated == ["ATL-023"]

    def test_store_exception_returns_false(self, monkeypatch):
        from atlas_plus.brain.dispatcher import reopen_ticket_if_low_quality
        monkeypatch.setenv("ATLAS_TICKET_QUALITY_THRESHOLD", "4.0")
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "atlas_plus.tickets.store.update_ticket_status",
            side_effect=Exception("DB error"),
        ):
            result = reopen_ticket_if_low_quality("ticket-ATL-024", "")
        assert result is False


# ---------------------------------------------------------------------------
# get_dispatch_stats / _record_dispatch_run
# ---------------------------------------------------------------------------

class TestGetDispatchStats:
    def test_returns_expected_keys(self, monkeypatch):
        from atlas_plus.brain.dispatcher import get_dispatch_stats
        monkeypatch.delenv("ATLAS_TICKET_DISPATCH", raising=False)
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "atlas_plus.tickets.store.list_tickets", return_value=[]
        ):
            stats = get_dispatch_stats()
        for key in ("enabled", "last_run_at", "last_dispatched_count", "total_dispatched", "in_flight"):
            assert key in stats, f"missing key: {key}"

    def test_enabled_reflects_env_var(self, monkeypatch):
        from atlas_plus.brain.dispatcher import get_dispatch_stats
        monkeypatch.setenv("ATLAS_TICKET_DISPATCH", "on")
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "atlas_plus.tickets.store.list_tickets", return_value=[]
        ):
            stats = get_dispatch_stats()
        assert stats["enabled"] is True

    def test_in_flight_counts_in_progress_tickets(self, monkeypatch):
        from atlas_plus.brain.dispatcher import get_dispatch_stats
        from unittest.mock import MagicMock, patch
        t = MagicMock()
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[t, t, t]):
            stats = get_dispatch_stats()
        assert stats["in_flight"] == 3

    def test_record_dispatch_run_updates_stats(self):
        import atlas_plus.brain.dispatcher as disp_mod
        from atlas_plus.brain.dispatcher import _record_dispatch_run
        # Save original to restore after test
        original = dict(disp_mod._DISPATCH_STATS)
        try:
            disp_mod._DISPATCH_STATS["total_dispatched"] = 5
            _record_dispatch_run(3)
            assert disp_mod._DISPATCH_STATS["last_dispatched_count"] == 3
            assert disp_mod._DISPATCH_STATS["total_dispatched"] == 8
            assert disp_mod._DISPATCH_STATS["last_run_at"] is not None
        finally:
            disp_mod._DISPATCH_STATS.update(original)

    def test_in_flight_returns_zero_on_ticket_store_error(self, monkeypatch):
        from atlas_plus.brain.dispatcher import get_dispatch_stats
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "atlas_plus.tickets.store.list_tickets",
            side_effect=Exception("store unavailable"),
        ):
            stats = get_dispatch_stats()
        assert stats["in_flight"] == 0


# ---------------------------------------------------------------------------
# create_verify_task / _reopen_ticket_from_verify_failure
# ---------------------------------------------------------------------------

class TestCreateVerifyTask:
    def test_returns_none_for_non_ticket_session_id(self):
        from atlas_plus.brain.dispatcher import create_verify_task
        result = create_verify_task("scheduler-curation", "do something")
        assert result is None

    def test_creates_verify_task_for_ticket_session_id(self):
        from atlas_plus.brain.dispatcher import create_verify_task
        from unittest.mock import patch, MagicMock
        fake_task = MagicMock()
        fake_task.task_id = "verify-task-001"
        with patch("atlas_plus.brain.tasks.create_task", return_value=fake_task) as mock_create:
            result = create_verify_task("ticket-ATL-050", "Implement feature X")
        assert result is fake_task
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["kind"] == "verify"
        assert call_kwargs["session_id"] == "verify-TICKET-ATL-050"
        assert "Verify:" in call_kwargs["instruction"]

    def test_verify_session_id_uppercased(self):
        from atlas_plus.brain.dispatcher import create_verify_task
        from unittest.mock import patch, MagicMock
        fake_task = MagicMock()
        with patch("atlas_plus.brain.tasks.create_task", return_value=fake_task) as mock_create:
            create_verify_task("ticket-atl-051", "do work")
        session = mock_create.call_args.kwargs["session_id"]
        assert session == "verify-TICKET-ATL-051"

    def test_create_task_exception_returns_none(self):
        from atlas_plus.brain.dispatcher import create_verify_task
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.create_task", side_effect=Exception("DB error")):
            result = create_verify_task("ticket-ATL-052", "do work")
        assert result is None


class TestReopenTicketFromVerifyFailure:
    def test_non_verify_session_id_returns_false(self):
        from atlas_plus.brain.dispatcher import _reopen_ticket_from_verify_failure
        result = _reopen_ticket_from_verify_failure("ticket-ATL-060", "tests failed")
        assert result is False

    def test_empty_session_id_returns_false(self):
        from atlas_plus.brain.dispatcher import _reopen_ticket_from_verify_failure
        result = _reopen_ticket_from_verify_failure("", "tests failed")
        assert result is False

    def test_reopens_original_ticket_on_verify_failure(self, monkeypatch):
        from atlas_plus.brain.dispatcher import _reopen_ticket_from_verify_failure
        monkeypatch.setenv("ATLAS_TICKET_QUALITY_THRESHOLD", "4.0")
        updated = []
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "atlas_plus.tickets.store.update_ticket_status",
            side_effect=lambda tid, status: updated.append((tid, status)),
        ):
            result = _reopen_ticket_from_verify_failure(
                "verify-ticket-ATL-061", "verify failed: 5 failed"
            )
        assert result is True
        assert updated == [("ATL-061", "todo")]

    def test_ticket_id_extracted_and_uppercased(self, monkeypatch):
        from atlas_plus.brain.dispatcher import _reopen_ticket_from_verify_failure
        updated = []
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "atlas_plus.tickets.store.update_ticket_status",
            side_effect=lambda tid, status: updated.append(tid),
        ):
            _reopen_ticket_from_verify_failure("verify-ticket-atl-062", "failure")
        assert updated == ["ATL-062"]
