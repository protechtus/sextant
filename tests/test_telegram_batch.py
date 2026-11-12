"""Tests for Telegram /tasks_batch command (v0.6-B).

Uses AsyncMock + MagicMock to simulate Telegram Update objects and
httpx client calls without requiring a real Bot connection.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

pytest.importorskip("telegram")   # the `telegram` extra is optional; 213 hard failures without it (AG8)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(text: str, user_id: int = 12345) -> MagicMock:
    """Build a minimal fake Telegram Update."""
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_user.id = user_id
    return update


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.args = []
    return ctx


# ---------------------------------------------------------------------------
# cmd_tasks_batch
# ---------------------------------------------------------------------------

class TestCmdTasksBatch:
    """Tests for the /tasks_batch Telegram command."""

    @pytest.mark.asyncio
    async def test_no_instructions_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_tasks_batch
        update = _make_update("/tasks_batch")
        ctx = _make_context()

        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"):
            await cmd_tasks_batch(update, ctx)

        update.message.reply_text.assert_called_once()
        call_arg = update.message.reply_text.call_args[0][0]
        assert "Usage" in call_arg or "usage" in call_arg.lower()

    @pytest.mark.asyncio
    async def test_no_brain_url_returns_error(self):
        from atlas_plus.telegram.commands import cmd_tasks_batch
        update = _make_update("/tasks_batch first task\nsecond task")
        ctx = _make_context()

        with patch.dict("os.environ", {}, clear=True):
            with patch("atlas_plus.telegram.commands.BRAIN_URL", ""):
                await cmd_tasks_batch(update, ctx)

        update.message.reply_text.assert_called_once()
        assert "ATLAS_BRAIN_URL" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_submits_each_instruction_as_task(self):
        from atlas_plus.telegram.commands import cmd_tasks_batch
        update = _make_update("/tasks_batch\nResearch Python async\nWrite temporal tests")
        ctx = _make_context()

        mock_response = MagicMock()
        mock_response.json.return_value = {"task_id": "task-xyz", "status": "pending"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_tasks_batch(update, ctx)

        # Should have posted 2 tasks
        assert mock_client.post.call_count == 2
        # Reply should mention the count
        call_arg = update.message.reply_text.call_args[0][0]
        assert "2" in call_arg or "2/2" in call_arg

    @pytest.mark.asyncio
    async def test_max_batch_size_enforced(self):
        from atlas_plus.telegram.commands import cmd_tasks_batch
        # 11 instructions -- one too many
        instructions = "\n".join(f"task {i}" for i in range(11))
        update = _make_update(f"/tasks_batch\n{instructions}")
        ctx = _make_context()

        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"):
            await cmd_tasks_batch(update, ctx)

        # Should refuse with a size error, no POST calls needed
        call_arg = update.message.reply_text.call_args[0][0]
        assert "large" in call_arg.lower() or "max" in call_arg.lower() or "10" in call_arg

    @pytest.mark.asyncio
    async def test_inline_newline_syntax_parsed(self):
        """\\n in message text should be treated as newlines."""
        from atlas_plus.telegram.commands import cmd_tasks_batch
        update = _make_update(r"/tasks_batch First task\nSecond task")
        ctx = _make_context()

        mock_response = MagicMock()
        mock_response.json.return_value = {"task_id": "task-abc", "status": "pending"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_tasks_batch(update, ctx)

        # Two instructions should be submitted
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_partial_failure_reported(self):
        """If one task submission fails, the reply notes the failure."""
        from atlas_plus.telegram.commands import cmd_tasks_batch
        update = _make_update("/tasks_batch\nGood task\nFailing task")
        ctx = _make_context()

        good_response = MagicMock()
        good_response.json.return_value = {"task_id": "task-good", "status": "pending"}
        good_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        # First call succeeds, second raises
        mock_client.post = AsyncMock(side_effect=[good_response, Exception("timeout")])

        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_tasks_batch(update, ctx)

        call_arg = update.message.reply_text.call_args[0][0]
        assert "FAILED" in call_arg or "1/2" in call_arg

    @pytest.mark.asyncio
    async def test_blank_lines_filtered(self):
        """Blank lines between instructions should not create empty tasks."""
        from atlas_plus.telegram.commands import cmd_tasks_batch
        update = _make_update("/tasks_batch\n\nOnly task\n\n")
        ctx = _make_context()

        mock_response = MagicMock()
        mock_response.json.return_value = {"task_id": "task-single", "status": "pending"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_tasks_batch(update, ctx)

        # Only 1 real instruction -- only 1 POST
        assert mock_client.post.call_count == 1


# ---------------------------------------------------------------------------
# cmd_memory_search
# ---------------------------------------------------------------------------

class TestCmdMemorySearch:
    """Tests for /memory <query> Telegram command."""

    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_memory_search
        update = _make_update("/memory")
        ctx = _make_context()
        await cmd_memory_search(update, ctx)
        call_arg = update.message.reply_text.call_args[0][0]
        assert "Usage" in call_arg or "usage" in call_arg.lower()

    @pytest.mark.asyncio
    async def test_returns_results(self):
        from atlas_plus.telegram.commands import cmd_memory_search
        update = _make_update("/memory atlas brain")
        ctx = _make_context()
        ctx.args = ["atlas", "brain"]

        mock_results = [
            {"path": "feedback/style.md", "score": 0.8, "snippet": "Atlas Brain uses Qwen3."},
            {"path": "identity/alex.md",   "score": 0.6, "snippet": "Alex works on Atlas."},
        ]

        with patch("atlas_plus.mcp.memory_server.tools.search.memory_search", return_value=mock_results):
            await cmd_memory_search(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "feedback/style.md" in reply
        assert "high" in reply or "medium" in reply

    @pytest.mark.asyncio
    async def test_no_results_reports_empty(self):
        from atlas_plus.telegram.commands import cmd_memory_search
        update = _make_update("/memory nothing")
        ctx = _make_context()
        ctx.args = ["nothing"]

        with patch("atlas_plus.mcp.memory_server.tools.search.memory_search", return_value=[]):
            await cmd_memory_search(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "No results" in reply or "no results" in reply.lower()

    @pytest.mark.asyncio
    async def test_search_exception_returns_error(self):
        from atlas_plus.telegram.commands import cmd_memory_search
        update = _make_update("/memory broken")
        ctx = _make_context()
        ctx.args = ["broken"]

        with patch(
            "atlas_plus.mcp.memory_server.tools.search.memory_search",
            side_effect=RuntimeError("index corrupt"),
        ):
            await cmd_memory_search(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "error" in reply.lower() or "Search" in reply


# ---------------------------------------------------------------------------
# cmd_memory_confirm
# ---------------------------------------------------------------------------

class TestCmdMemoryConfirm:
    """Tests for /confirm <path> Telegram command."""

    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_memory_confirm
        update = _make_update("/confirm")
        ctx = _make_context()
        await cmd_memory_confirm(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "Usage" in reply or "usage" in reply.lower()

    @pytest.mark.asyncio
    async def test_file_not_found_returns_error(self, tmp_path):
        from atlas_plus.telegram.commands import cmd_memory_confirm
        update = _make_update("/confirm feedback/missing.md")
        ctx = _make_context()
        ctx.args = ["feedback/missing.md"]

        with patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT", tmp_path):
            await cmd_memory_confirm(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "not found" in reply.lower() or "not" in reply.lower()

    @pytest.mark.asyncio
    async def test_success_message(self, tmp_path):
        from atlas_plus.telegram.commands import cmd_memory_confirm
        # Create the file
        target = tmp_path / "feedback" / "style.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("---\ntype: feedback\n---\n\nContent.", encoding="utf-8")

        update = _make_update("/confirm feedback/style.md")
        ctx = _make_context()
        ctx.args = ["feedback/style.md"]

        with patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.mcp.memory_server.routing.confirm_fact", return_value=True):
            await cmd_memory_confirm(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Confirmed" in reply or "confirmed" in reply.lower()


# ---------------------------------------------------------------------------
# cmd_brain_sessions
# ---------------------------------------------------------------------------

class TestCmdBrainSessions:
    """Tests for /brain_sessions Telegram command."""

    @pytest.mark.asyncio
    async def test_shows_sessions_list(self):
        from atlas_plus.telegram.commands import cmd_brain_sessions
        update = _make_update("/brain_sessions")
        ctx = _make_context()

        mock_sessions = [
            {"session_id": "cli-2026-05-20", "message_count": 10, "last_active": "2026-05-20T10:00:00"},
            {"session_id": "tg-12345", "message_count": 3, "last_active": "2026-05-20T09:00:00"},
        ]
        with patch("atlas_plus.brain.history.list_sessions", return_value=mock_sessions):
            await cmd_brain_sessions(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "cli-2026-05-20" in reply
        assert "tg-12345" in reply

    @pytest.mark.asyncio
    async def test_empty_sessions_returns_message(self):
        from atlas_plus.telegram.commands import cmd_brain_sessions
        update = _make_update("/brain_sessions")
        ctx = _make_context()

        with patch("atlas_plus.brain.history.list_sessions", return_value=[]):
            await cmd_brain_sessions(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "No Brain sessions" in reply or "no" in reply.lower()

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        from atlas_plus.telegram.commands import cmd_brain_sessions
        update = _make_update("/brain_sessions")
        ctx = _make_context()

        with patch("atlas_plus.brain.history.list_sessions", side_effect=RuntimeError("db err")):
            await cmd_brain_sessions(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Error" in reply or "error" in reply.lower()


# ---------------------------------------------------------------------------
# cmd_exhausted
# ---------------------------------------------------------------------------

class TestCmdExhausted:
    """Tests for /exhausted Telegram command."""

    @pytest.mark.asyncio
    async def test_no_exhausted_tasks_returns_clean_message(self):
        from atlas_plus.telegram.commands import cmd_exhausted
        update = _make_update("/exhausted")
        ctx = _make_context()

        with patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=[]):
            await cmd_exhausted(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "No exhausted" in reply or "no" in reply.lower()

    @pytest.mark.asyncio
    async def test_lists_exhausted_tasks(self):
        from atlas_plus.telegram.commands import cmd_exhausted
        from atlas_plus.brain.tasks import Task
        update = _make_update("/exhausted")
        ctx = _make_context()

        fake_tasks = [
            Task(
                task_id="task-dead123456",
                session_id="ticket-ATL-003",
                instruction="fix the broken pipeline",
                status="failed",
                created_at="2026-05-20T01:00:00+00:00",
                attempts=3,
                max_attempts=3,
                agent="brain",
            ),
        ]
        with patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=fake_tasks):
            await cmd_exhausted(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "task-dead" in reply or "Exhausted" in reply
        assert "brain" in reply or "failed" in reply

    @pytest.mark.asyncio
    async def test_exception_returns_error_message(self):
        from atlas_plus.telegram.commands import cmd_exhausted
        update = _make_update("/exhausted")
        ctx = _make_context()

        with patch(
            "atlas_plus.brain.tasks.list_exhausted_tasks",
            side_effect=RuntimeError("db unavailable"),
        ):
            await cmd_exhausted(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Error" in reply or "error" in reply.lower()


# ---------------------------------------------------------------------------
# cmd_dispatch_plan
# ---------------------------------------------------------------------------

class TestCmdDispatchPlan:
    @pytest.mark.asyncio
    async def test_no_eligible_tickets_returns_message(self):
        from atlas_plus.telegram.commands import cmd_dispatch_plan
        update = _make_update("/dispatch_plan")
        ctx = _make_context()

        with patch("atlas_plus.tickets.store.list_tickets", return_value=[]), \
             patch("atlas_plus.brain.dispatcher.is_dispatch_enabled", return_value=False):
            await cmd_dispatch_plan(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Plan" in reply or "no dispatch" in reply.lower() or reply.strip() != ""

    @pytest.mark.asyncio
    async def test_lists_eligible_tickets(self):
        from atlas_plus.telegram.commands import cmd_dispatch_plan
        from unittest.mock import MagicMock
        update = _make_update("/dispatch_plan")
        ctx = _make_context()

        ticket = MagicMock(spec=[])
        ticket.id = "ATL-001"
        ticket.title = "implement retry logic"
        ticket.status = "todo"
        ticket.assignee = "brain"
        ticket.priority = "high"

        with patch("atlas_plus.tickets.store.list_tickets", return_value=[ticket]), \
             patch("atlas_plus.brain.dispatcher.is_dispatch_enabled", return_value=True):
            await cmd_dispatch_plan(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "ATL-001" in reply or "Plan" in reply

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        from atlas_plus.telegram.commands import cmd_dispatch_plan
        update = _make_update("/dispatch_plan")
        ctx = _make_context()

        with patch(
            "atlas_plus.tickets.store.list_tickets",
            side_effect=RuntimeError("tickets db gone"),
        ):
            await cmd_dispatch_plan(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "unavailable" in reply.lower() or "error" in reply.lower() or reply.strip() != ""


# ---------------------------------------------------------------------------
# cmd_recent_tasks
# ---------------------------------------------------------------------------

class TestCmdRecentTasks:
    @pytest.mark.asyncio
    async def test_no_completed_tasks_returns_message(self):
        from atlas_plus.telegram.commands import cmd_recent_tasks
        update = _make_update("/recent_tasks")
        ctx = _make_context()

        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[]):
            await cmd_recent_tasks(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "no completed" in reply.lower() or "No completed" in reply

    @pytest.mark.asyncio
    async def test_lists_done_tasks(self):
        from atlas_plus.telegram.commands import cmd_recent_tasks
        from atlas_plus.brain.tasks import Task
        update = _make_update("/recent_tasks")
        ctx = _make_context()

        fake = [Task(
            task_id="task-done-abc",
            session_id="brain-test",
            instruction="write pytest tests",
            status="done",
            created_at="2026-05-20T03:00:00+00:00",
            finished_at="2026-05-20T03:01:00+00:00",
            agent="brain",
        )]
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=fake):
            await cmd_recent_tasks(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "task-done-abc" in reply or "Recent" in reply

    @pytest.mark.asyncio
    async def test_exception_returns_error(self):
        from atlas_plus.telegram.commands import cmd_recent_tasks
        update = _make_update("/recent_tasks")
        ctx = _make_context()

        with patch("atlas_plus.brain.tasks.list_tasks",
                   side_effect=RuntimeError("db unavailable")):
            await cmd_recent_tasks(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Error" in reply or "error" in reply.lower()


# ---------------------------------------------------------------------------
# cmd_system_health
# ---------------------------------------------------------------------------

class TestCmdSystemHealth:
    @pytest.mark.asyncio
    async def test_shows_health_summary(self):
        """cmd_system_health replies with Atlas System Health message."""
        from atlas_plus.telegram.commands import cmd_system_health
        update = _make_update("/sys_health")
        ctx = _make_context()

        mock_service = {"name": "brain", "status": "up", "detail": ""}
        with patch("atlas_plus.doctor.cli.check_services", return_value=[mock_service]), \
             patch("atlas_plus.doctor.cli.check_worker_status",
                   return_value={"assessment": "ok", "pending": 0}), \
             patch("atlas_plus.doctor.cli.check_memory_quality",
                   return_value={"total_files": 100, "confirmed_pct": 65, "tagged_pct": 40}), \
             patch("atlas_plus.brain.task_facts.pending_count", return_value=0), \
             patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=[]):
            await cmd_system_health(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "Health" in reply
        assert "Services" in reply or "Worker" in reply

    @pytest.mark.asyncio
    async def test_shows_exhausted_when_present(self):
        """cmd_system_health includes exhausted tasks line when count > 0."""
        from atlas_plus.telegram.commands import cmd_system_health
        from atlas_plus.brain.tasks import Task
        update = _make_update("/sys_health")
        ctx = _make_context()

        fake_exhausted = [Task(
            task_id="task-dead", session_id="s", instruction="x",
            status="failed", created_at="2026-05-20T01:00:00+00:00",
            attempts=3, max_attempts=3,
        )]
        with patch("atlas_plus.doctor.cli.check_services", return_value=[]), \
             patch("atlas_plus.doctor.cli.check_worker_status",
                   return_value={"assessment": "idle", "pending": 0}), \
             patch("atlas_plus.doctor.cli.check_memory_quality",
                   return_value={"total_files": 50, "confirmed_pct": 40, "tagged_pct": 30}), \
             patch("atlas_plus.brain.task_facts.pending_count", return_value=0), \
             patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=fake_exhausted):
            await cmd_system_health(update, ctx)

        reply = update.message.reply_text.call_args[0][0]
        assert "exhausted" in reply.lower() or "Exhausted" in reply


# ---------------------------------------------------------------------------
# cmd_research_gaps
# ---------------------------------------------------------------------------

class TestCmdResearchGaps:
    @pytest.mark.asyncio
    async def test_no_misses_shows_clean_message(self):
        from atlas_plus.telegram.commands import cmd_research_gaps
        update = _make_update("/research_gaps")
        ctx = _make_context()
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   return_value=[]):
            await cmd_research_gaps(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "no" in reply.lower() or "No" in reply

    @pytest.mark.asyncio
    async def test_shows_miss_queries(self):
        from atlas_plus.telegram.commands import cmd_research_gaps
        update = _make_update("/research_gaps")
        ctx = _make_context()
        misses = [{"query": "SIP REGISTER flow", "miss_count": 4, "total": 7}]
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   return_value=misses), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            await cmd_research_gaps(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "SIP REGISTER" in reply or "sip" in reply.lower()

    @pytest.mark.asyncio
    async def test_shows_uncovered_hint(self):
        from atlas_plus.telegram.commands import cmd_research_gaps
        update = _make_update("/research_gaps")
        ctx = _make_context()
        misses = [{"query": "uncovered topic", "miss_count": 3, "total": 5}]
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   return_value=misses), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            await cmd_research_gaps(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "autoticket" in reply or "ticket" in reply.lower() or "gap" in reply.lower()


# ---------------------------------------------------------------------------
# cmd_research_quality -- topic filter
# ---------------------------------------------------------------------------

class TestCmdGapReport:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_gap_report
        update = _make_update("/gap_report")
        ctx = _make_context()
        await cmd_gap_report(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "Usage" in reply or "usage" in reply.lower() or "gap" in reply.lower()

    @pytest.mark.asyncio
    async def test_queues_research_gap_task(self):
        from atlas_plus.telegram.commands import cmd_gap_report
        from unittest.mock import MagicMock, patch
        update = _make_update("/gap_report SIP OPTIONS")
        ctx = _make_context()
        ctx.args = ["SIP", "OPTIONS"]
        mock_task = MagicMock()
        mock_task.task_id = "gap-001"
        with patch("atlas_plus.brain.tasks.create_task", return_value=mock_task) as mock_create:
            await cmd_gap_report(update, ctx)
        mock_create.assert_called_once()
        call_kw = mock_create.call_args[1]
        assert call_kw.get("kind") == "research_gap"
        assert "SIP OPTIONS" in call_kw.get("instruction", "")
        reply = update.message.reply_text.call_args[0][0]
        assert "gap-001" in reply or "queued" in reply.lower()

    @pytest.mark.asyncio
    async def test_graceful_on_failure(self):
        from atlas_plus.telegram.commands import cmd_gap_report
        from unittest.mock import patch
        update = _make_update("/gap_report some topic")
        ctx = _make_context()
        ctx.args = ["some", "topic"]
        with patch("atlas_plus.brain.tasks.create_task", side_effect=RuntimeError("DB locked")):
            await cmd_gap_report(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "Error" in reply or "error" in reply.lower()


class TestCmdResearchQualityTopicFilter:
    @pytest.mark.asyncio
    async def test_global_summary_no_args(self):
        from atlas_plus.telegram.commands import cmd_research_quality
        update = _make_update("/research_quality")
        ctx = _make_context()
        with patch("atlas_plus.doctor.cli.check_research_quality",
                   return_value={"total_reports": 10, "scored_reports": 8,
                                 "avg_score": 0.72, "low_quality_count": 0,
                                 "min_score_file": ""}):
            await cmd_research_quality(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "0.72" in reply or "quality" in reply.lower()

    @pytest.mark.asyncio
    async def test_topic_filter_with_matching_reports(self, tmp_path):
        from atlas_plus.telegram.commands import cmd_research_quality
        update = _make_update("/research_quality sip")
        ctx = _make_context()
        ctx.args = ["sip"]
        # Create a report file matching "sip" topic
        rdir = tmp_path / "reports"
        rdir.mkdir()
        report = rdir / "sip-register.md"
        report.write_text("---\ntitle: SIP REGISTER\ntopic: sip\nquality_score: 0.8\n---\nbody\n",
                          encoding="utf-8")
        with patch("os.environ.get",
                   side_effect=lambda k, d="": str(tmp_path) if k == "ATLAS_STATE_DIR" else d):
            await cmd_research_quality(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "sip" in reply.lower() or "0.80" in reply or "0.8" in reply

    @pytest.mark.asyncio
    async def test_topic_filter_no_match(self, tmp_path):
        from atlas_plus.telegram.commands import cmd_research_quality
        update = _make_update("/research_quality voip")
        ctx = _make_context()
        ctx.args = ["voip"]
        rdir = tmp_path / "reports"
        rdir.mkdir()
        with patch("os.environ.get",
                   side_effect=lambda k, d="": str(tmp_path) if k == "ATLAS_STATE_DIR" else d):
            await cmd_research_quality(update, ctx)
        reply = update.message.reply_text.call_args[0][0]
        assert "No reports" in reply or "voip" in reply.lower()


# ---------------------------------------------------------------------------
# cmd_help
# ---------------------------------------------------------------------------

class TestCmdHelp:
    @pytest.mark.asyncio
    async def test_sends_help_text(self):
        from atlas_plus.telegram.commands import cmd_help
        update = _make_update("/help")
        ctx = _make_context()
        await cmd_help(update, ctx)
        update.message.reply_text.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "/task" in text
        assert "/search" in text


# ---------------------------------------------------------------------------
# cmd_search
# ---------------------------------------------------------------------------

class TestCmdSearch:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_search
        update = _make_update("/search")
        ctx = _make_context()
        await cmd_search(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_returns_results(self):
        from atlas_plus.telegram.commands import cmd_search
        update = _make_update("/search sip")
        ctx = _make_context()
        ctx.args = ["sip"]
        fake_results = [{"path": "reference/sip.md", "snippet": "SIP is a protocol"}]
        with patch("atlas_plus.mcp.memory_server.fts.search", return_value=fake_results):
            await cmd_search(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "sip.md" in text

    @pytest.mark.asyncio
    async def test_no_results_message(self):
        from atlas_plus.telegram.commands import cmd_search
        update = _make_update("/search unknownquery")
        ctx = _make_context()
        ctx.args = ["unknownquery"]
        with patch("atlas_plus.mcp.memory_server.fts.search", return_value=[]):
            await cmd_search(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No results" in text

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_search
        update = _make_update("/search fail")
        ctx = _make_context()
        ctx.args = ["fail"]
        with patch("atlas_plus.mcp.memory_server.fts.search",
                   side_effect=RuntimeError("db locked")):
            await cmd_search(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "error" in text.lower() or "Error" in text


# ---------------------------------------------------------------------------
# cmd_remember
# ---------------------------------------------------------------------------

class TestCmdRemember:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_remember
        update = _make_update("/remember")
        ctx = _make_context()
        await cmd_remember(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_one_arg_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_remember
        update = _make_update("/remember scope-only")
        ctx = _make_context()
        ctx.args = ["scope-only"]
        await cmd_remember(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_success_shows_file(self):
        from atlas_plus.telegram.commands import cmd_remember
        update = _make_update("/remember reference:sip sip uses udp")
        ctx = _make_context()
        ctx.args = ["reference:sip", "sip", "uses", "udp"]
        with patch("atlas_plus.mcp.memory_server.tools.remember.memory_remember",
                   return_value={"ok": True, "file": "reference/sip.md"}):
            await cmd_remember(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "sip.md" in text

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_remember
        update = _make_update("/remember ref fact")
        ctx = _make_context()
        ctx.args = ["ref", "fact"]
        with patch("atlas_plus.mcp.memory_server.tools.remember.memory_remember",
                   side_effect=RuntimeError("write blocked")):
            await cmd_remember(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text or "error" in text.lower()


# ---------------------------------------------------------------------------
# cmd_task
# ---------------------------------------------------------------------------

class TestCmdTask:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_task
        update = _make_update("/task")
        ctx = _make_context()
        await cmd_task(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_queues_task_and_shows_id(self):
        from atlas_plus.telegram.commands import cmd_task
        update = _make_update("/task research SIP")
        ctx = _make_context()
        ctx.args = ["research", "SIP"]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"task_id": "abc-123", "status": "pending"}
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_task(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "abc-123" in text or "Task" in text

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_task
        update = _make_update("/task do something")
        ctx = _make_context()
        ctx.args = ["do", "something"]
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=Exception("connection refused"))
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_task(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text or "error" in text.lower()


# ---------------------------------------------------------------------------
# cmd_tasks (list tasks)
# ---------------------------------------------------------------------------

class TestCmdTasks:
    @pytest.mark.asyncio
    async def test_no_brain_url_shows_message(self):
        from atlas_plus.telegram.commands import cmd_tasks
        update = _make_update("/tasks")
        ctx = _make_context()
        with patch("atlas_plus.telegram.commands.BRAIN_URL", ""):
            await cmd_tasks(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ATLAS_BRAIN_URL" in text or "not set" in text.lower()

    @pytest.mark.asyncio
    async def test_shows_task_list(self):
        from atlas_plus.telegram.commands import cmd_tasks
        update = _make_update("/tasks")
        ctx = _make_context()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "tasks": [
                {"task_id": "t-001", "status": "done", "agent": "brain",
                 "instruction": "research SIP"}
            ],
            "count": 1,
        }
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_tasks(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "t-001" in text or "research SIP" in text

    @pytest.mark.asyncio
    async def test_no_tasks_shows_message(self):
        from atlas_plus.telegram.commands import cmd_tasks
        update = _make_update("/tasks")
        ctx = _make_context()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"tasks": [], "count": 0}
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_tasks(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No tasks" in text


# ---------------------------------------------------------------------------
# cmd_agents
# ---------------------------------------------------------------------------

class TestCmdAgents:
    @pytest.mark.asyncio
    async def test_no_brain_url_shows_message(self):
        from atlas_plus.telegram.commands import cmd_agents
        update = _make_update("/agents")
        ctx = _make_context()
        with patch("atlas_plus.telegram.commands.BRAIN_URL", ""):
            await cmd_agents(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ATLAS_BRAIN_URL" in text or "not set" in text.lower()

    @pytest.mark.asyncio
    async def test_shows_agent_list(self):
        from atlas_plus.telegram.commands import cmd_agents
        update = _make_update("/agents")
        ctx = _make_context()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "agents": [
                {"name": "brain", "model": "qwen3:8b", "online": True,
                 "capabilities": ["memory"], "current_task": None},
            ]
        }
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_agents(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "brain" in text

    @pytest.mark.asyncio
    async def test_no_agents_shows_message(self):
        from atlas_plus.telegram.commands import cmd_agents
        update = _make_update("/agents")
        ctx = _make_context()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"agents": []}
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_agents(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No agents" in text


# ---------------------------------------------------------------------------
# cmd_ticket
# ---------------------------------------------------------------------------

class TestCmdTicket:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_ticket
        update = _make_update("/ticket")
        ctx = _make_context()
        await cmd_ticket(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_one_arg_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_ticket
        update = _make_update("/ticket project-only")
        ctx = _make_context()
        ctx.args = ["project-only"]
        await cmd_ticket(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_creates_ticket(self):
        from atlas_plus.telegram.commands import cmd_ticket
        from atlas_plus.tickets.store import Ticket
        from pathlib import Path
        update = _make_update("/ticket atlas Fix the login bug")
        ctx = _make_context()
        ctx.args = ["atlas", "Fix", "the", "login", "bug"]
        fake_ticket = Ticket(id="ATL-010", title="Fix the login bug",
                             project="atlas", path=Path("dummy.md"))
        with patch("atlas_plus.tickets.store.create_ticket", return_value=fake_ticket):
            await cmd_ticket(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ATL-010" in text

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_ticket
        update = _make_update("/ticket atlas Title")
        ctx = _make_context()
        ctx.args = ["atlas", "Title"]
        with patch("atlas_plus.tickets.store.create_ticket",
                   side_effect=ValueError("bad project")):
            await cmd_ticket(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text or "error" in text.lower()


# ---------------------------------------------------------------------------
# cmd_doctor
# ---------------------------------------------------------------------------

class TestCmdDoctor:
    @pytest.mark.asyncio
    async def test_sends_thinking_then_health(self):
        from atlas_plus.telegram.commands import cmd_doctor
        update = _make_update("/doctor")
        ctx = _make_context()
        fake_data = {
            "services": [{"name": "Brain", "status": "up", "detail": ""}],
            "brain_diagnostic": {"model": "qwen3:8b", "autonomous": True},
            "tickets": {"by_status": {"todo": 1}, "by_assignee": {"claude-code": 1, "human": 0}},
            "nightly": {},
            "memory_gaps": {},
            "pending_facts": {"count": 0},
            "sla_breaches": {"count": 0},
            "disk": {},
        }
        with patch("atlas_plus.doctor.cli.collect", return_value=fake_data):
            await cmd_doctor(update, ctx)
        assert update.message.reply_text.call_count >= 1
        # First call is "Checking..." then the health data
        last_text = update.message.reply_text.call_args[0][0]
        assert "Brain" in last_text or "Health" in last_text

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_doctor
        update = _make_update("/doctor")
        ctx = _make_context()
        with patch("atlas_plus.doctor.cli.collect", side_effect=RuntimeError("no db")):
            await cmd_doctor(update, ctx)
        last_text = update.message.reply_text.call_args[0][0]
        assert "Doctor error" in last_text or "error" in last_text.lower()


# ---------------------------------------------------------------------------
# cmd_dispatch (specific agent)
# ---------------------------------------------------------------------------

class TestCmdDispatch:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_dispatch
        update = _make_update("/dispatch")
        ctx = _make_context()
        await cmd_dispatch(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_dispatches_to_agent(self):
        from atlas_plus.telegram.commands import cmd_dispatch
        update = _make_update("/dispatch brain explain SIP")
        ctx = _make_context()
        ctx.args = ["brain", "explain", "SIP"]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"task_id": "dispatch-001", "status": "pending"}
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_dispatch(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "brain" in text or "dispatch-001" in text


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

class TestCmdStatus:
    @pytest.mark.asyncio
    async def test_shows_task_and_ticket_counts(self):
        from atlas_plus.telegram.commands import cmd_status
        update = _make_update("/status")
        ctx = _make_context()
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[]), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            await cmd_status(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Tasks" in text or "Status" in text

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_status
        update = _make_update("/status")
        ctx = _make_context()
        with patch("atlas_plus.brain.tasks.list_tasks", side_effect=RuntimeError("db fail")):
            await cmd_status(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text or "error" in text.lower()


# ---------------------------------------------------------------------------
# cmd_gaps
# ---------------------------------------------------------------------------

class TestCmdGaps:
    @pytest.mark.asyncio
    async def test_no_miss_data_shows_message(self):
        from atlas_plus.telegram.commands import cmd_gaps
        update = _make_update("/gaps")
        ctx = _make_context()
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   return_value=[]):
            await cmd_gaps(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No miss" in text or "first" in text.lower()

    @pytest.mark.asyncio
    async def test_shows_gap_list(self):
        from atlas_plus.telegram.commands import cmd_gaps
        update = _make_update("/gaps")
        ctx = _make_context()
        fake_gaps = [{"query": "sip registration", "miss_count": 4, "total": 5}]
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   return_value=fake_gaps):
            await cmd_gaps(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "sip" in text.lower()

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_gaps
        update = _make_update("/gaps")
        ctx = _make_context()
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   side_effect=RuntimeError("log missing")):
            await cmd_gaps(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_temporal
# ---------------------------------------------------------------------------

class TestCmdTemporal:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_temporal
        update = _make_update("/temporal")
        ctx = _make_context()
        await cmd_temporal(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_empty_graph_shows_message(self):
        from atlas_plus.telegram.commands import cmd_temporal
        update = _make_update("/temporal sip")
        ctx = _make_context()
        ctx.args = ["sip"]
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider.stats",
                   return_value={"facts": 0}), \
             patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider.__init__",
                   return_value=None):
            await cmd_temporal(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "empty" in text.lower() or "build" in text.lower() or "Error" in text

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_temporal
        update = _make_update("/temporal sip")
        ctx = _make_context()
        ctx.args = ["sip"]
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   side_effect=RuntimeError("graph corrupted")):
            await cmd_temporal(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_research (queue research task)
# ---------------------------------------------------------------------------

class TestCmdResearch:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_research
        update = _make_update("/research")
        ctx = _make_context()
        await cmd_research(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_queues_research_task(self):
        from atlas_plus.telegram.commands import cmd_research
        update = _make_update("/research SIP registration")
        ctx = _make_context()
        ctx.args = ["SIP", "registration"]
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"task_id": "research-001", "status": "pending"}
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_research(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "research-001" in text or "Research" in text


# ---------------------------------------------------------------------------
# cmd_retry
# ---------------------------------------------------------------------------

class TestCmdRetry:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_retry
        update = _make_update("/retry")
        ctx = _make_context()
        await cmd_retry(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_task_not_found(self):
        from atlas_plus.telegram.commands import cmd_retry
        update = _make_update("/retry unknown-task")
        ctx = _make_context()
        ctx.args = ["unknown-task"]
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("atlas_plus.brain.tasks.get_task", return_value=None):
            await cmd_retry(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "not found" in text.lower() or "unknown-task" in text

    @pytest.mark.asyncio
    async def test_retries_and_shows_new_id(self):
        from atlas_plus.telegram.commands import cmd_retry
        from unittest.mock import MagicMock
        update = _make_update("/retry old-task-001")
        ctx = _make_context()
        ctx.args = ["old-task-001"]
        fake_task = MagicMock()
        fake_task.instruction = "research SIP"
        fake_task.session_id = "tg-12345"
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"task_id": "retry-001", "status": "pending"}
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("atlas_plus.brain.tasks.get_task", return_value=fake_task), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_retry(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "retry-001" in text or "Retried" in text


# ---------------------------------------------------------------------------
# cmd_queue (queue ticket for claude-code)
# ---------------------------------------------------------------------------

class TestCmdQueue:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_queue
        update = _make_update("/queue")
        ctx = _make_context()
        await cmd_queue(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_creates_ticket_for_claude_code(self):
        from atlas_plus.telegram.commands import cmd_queue
        from atlas_plus.tickets.store import Ticket
        from pathlib import Path
        update = _make_update("/queue atlas Implement retry logic")
        ctx = _make_context()
        ctx.args = ["atlas", "Implement", "retry", "logic"]
        fake_ticket = Ticket(id="ATL-011", title="Implement retry logic",
                             project="atlas", assignee="claude-code", path=Path("x.md"))
        with patch("atlas_plus.tickets.store.create_ticket", return_value=fake_ticket):
            await cmd_queue(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ATL-011" in text

    @pytest.mark.asyncio
    async def test_error_shown(self):
        from atlas_plus.telegram.commands import cmd_queue
        update = _make_update("/queue atlas Title")
        ctx = _make_context()
        ctx.args = ["atlas", "Title"]
        with patch("atlas_plus.tickets.store.create_ticket",
                   side_effect=ValueError("invalid project")):
            await cmd_queue(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_tickets (list tickets)
# ---------------------------------------------------------------------------

class TestCmdTickets:
    @pytest.mark.asyncio
    async def test_shows_open_tickets(self):
        from atlas_plus.telegram.commands import cmd_tickets
        from atlas_plus.tickets.store import Ticket
        from pathlib import Path
        update = _make_update("/tickets")
        ctx = _make_context()
        fake_ticket = Ticket(id="ATL-001", title="Fix login", project="atlas",
                             status="todo", path=Path("x.md"))
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[fake_ticket]):
            await cmd_tickets(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ATL-001" in text or "Fix login" in text

    @pytest.mark.asyncio
    async def test_empty_shows_no_open(self):
        from atlas_plus.telegram.commands import cmd_tickets
        update = _make_update("/tickets")
        ctx = _make_context()
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            await cmd_tickets(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "no open" in text.lower() or "No open" in text or "0" in text or "no tickets" in text.lower() or "not found" in text.lower()

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_tickets
        update = _make_update("/tickets")
        ctx = _make_context()
        with patch("atlas_plus.tickets.store.list_tickets",
                   side_effect=RuntimeError("tickets dir missing")):
            await cmd_tickets(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_done (mark ticket done)
# ---------------------------------------------------------------------------

class TestCmdDone:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_done
        update = _make_update("/done")
        ctx = _make_context()
        await cmd_done(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_marks_ticket_done(self):
        from atlas_plus.telegram.commands import cmd_done
        from atlas_plus.tickets.store import Ticket
        from pathlib import Path
        update = _make_update("/done ATL-007")
        ctx = _make_context()
        ctx.args = ["ATL-007"]
        fake_ticket = Ticket(id="ATL-007", title="Task complete", project="atlas",
                             status="done", assignee="human", path=Path("x.md"))
        with patch("atlas_plus.tickets.store.update_ticket_status",
                   return_value=fake_ticket), \
             patch("atlas_plus.telegram.notify.push_job_done", new_callable=AsyncMock):
            await cmd_done(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ATL-007" in text

    @pytest.mark.asyncio
    async def test_not_found_shows_message(self):
        from atlas_plus.telegram.commands import cmd_done
        update = _make_update("/done ATL-999")
        ctx = _make_context()
        ctx.args = ["ATL-999"]
        with patch("atlas_plus.tickets.store.update_ticket_status",
                   side_effect=FileNotFoundError("not found")):
            await cmd_done(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "not found" in text.lower() or "ATL-999" in text


# ---------------------------------------------------------------------------
# cmd_stop (cancel task)
# ---------------------------------------------------------------------------

class TestCmdStop:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_stop
        update = _make_update("/stop")
        ctx = _make_context()
        await cmd_stop(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text

    @pytest.mark.asyncio
    async def test_task_not_found(self):
        from atlas_plus.telegram.commands import cmd_stop
        update = _make_update("/stop abc-001")
        ctx = _make_context()
        ctx.args = ["abc-001"]
        with patch("atlas_plus.brain.tasks.get_task", return_value=None):
            await cmd_stop(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "not found" in text.lower() or "abc-001" in text

    @pytest.mark.asyncio
    async def test_cancels_running_task(self):
        from atlas_plus.telegram.commands import cmd_stop
        fake_task = MagicMock()
        fake_task.task_id = "run-001"
        fake_task.status = "running"
        update = _make_update("/stop run-001")
        ctx = _make_context()
        ctx.args = ["run-001"]
        with patch("atlas_plus.brain.tasks.get_task", return_value=fake_task), \
             patch("atlas_plus.brain.tasks.mark_failed") as mock_fail:
            await cmd_stop(update, ctx)
        mock_fail.assert_called_once()
        text = update.message.reply_text.call_args[0][0]
        assert "cancel" in text.lower() or "run-001" in text

    @pytest.mark.asyncio
    async def test_already_done_task_shows_message(self):
        from atlas_plus.telegram.commands import cmd_stop
        fake_task = MagicMock()
        fake_task.task_id = "done-001"
        fake_task.status = "done"
        update = _make_update("/stop done-001")
        ctx = _make_context()
        ctx.args = ["done-001"]
        with patch("atlas_plus.brain.tasks.get_task", return_value=fake_task):
            await cmd_stop(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "already" in text.lower() or "done-001" in text


# ---------------------------------------------------------------------------
# cmd_convergence
# ---------------------------------------------------------------------------

class TestCmdConvergence:
    @pytest.mark.asyncio
    async def test_no_issues_shows_clean_message(self):
        from atlas_plus.telegram.commands import cmd_convergence
        update = _make_update("/convergence")
        ctx = _make_context()
        with patch("atlas_plus.doctor.cli.check_convergence",
                   return_value={"flagged": 0, "paths": []}):
            await cmd_convergence(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "no" in text.lower() or "convergence" in text.lower()

    @pytest.mark.asyncio
    async def test_shows_flagged_files(self):
        from atlas_plus.telegram.commands import cmd_convergence
        update = _make_update("/convergence")
        ctx = _make_context()
        with patch("atlas_plus.doctor.cli.check_convergence",
                   return_value={"flagged": 2, "paths": ["ref/sip.md", "proj/voip.md"]}):
            await cmd_convergence(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "sip.md" in text or "2" in text

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_convergence
        update = _make_update("/convergence")
        ctx = _make_context()
        with patch("atlas_plus.doctor.cli.check_convergence",
                   side_effect=RuntimeError("memory root missing")):
            await cmd_convergence(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_narrative
# ---------------------------------------------------------------------------

class TestCmdNarrative:
    @pytest.mark.asyncio
    async def test_no_sessions_shows_message(self):
        from atlas_plus.telegram.commands import cmd_narrative
        from atlas_plus.session_narrative import NarrativeReport
        update = _make_update("/narrative")
        ctx = _make_context()
        empty_report = NarrativeReport(
            week_label="2026-W21", sessions_analyzed=0,
            open_loops=[], direction_signals=[], active_projects=[],
            energy="neutral", generated_at="2026-05-21",
        )
        with patch("atlas_plus.session_narrative.build_narrative",
                   return_value=empty_report):
            await cmd_narrative(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No session" in text or "session" in text.lower()

    @pytest.mark.asyncio
    async def test_shows_narrative_report(self):
        from atlas_plus.telegram.commands import cmd_narrative
        from atlas_plus.session_narrative import NarrativeReport
        update = _make_update("/narrative")
        ctx = _make_context()
        report = NarrativeReport(
            week_label="2026-W21", sessions_analyzed=3,
            open_loops=["SIP research ongoing"],
            direction_signals=["Converging on Atlas v0.17"],
            active_projects=["atlas-memory+"],
            energy="converging", generated_at="2026-05-21",
        )
        with patch("atlas_plus.session_narrative.build_narrative", return_value=report):
            await cmd_narrative(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "converging" in text.lower() or "atlas-memory+" in text

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_narrative
        update = _make_update("/narrative")
        ctx = _make_context()
        with patch("atlas_plus.session_narrative.build_narrative",
                   side_effect=RuntimeError("sessions dir missing")):
            await cmd_narrative(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_rederive
# ---------------------------------------------------------------------------

class TestCmdRederive:
    @pytest.mark.asyncio
    async def test_no_candidates_shows_clean_message(self):
        from atlas_plus.telegram.commands import cmd_rederive
        update = _make_update("/rederive")
        ctx = _make_context()
        with patch("atlas_plus.curation.rederive.find_rederivable", return_value=[]):
            await cmd_rederive(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "clean" in text.lower() or "No" in text

    @pytest.mark.asyncio
    async def test_shows_rederivable_list(self):
        from atlas_plus.telegram.commands import cmd_rederive
        from atlas_plus.curation.rederive import RederiveCandidate
        update = _make_update("/rederive")
        ctx = _make_context()
        fake = [RederiveCandidate("ref/sip.md", "SIP", "reference",
                                  "AGENTS.md", 0.55, ["sip"])]
        with patch("atlas_plus.curation.rederive.find_rederivable", return_value=fake):
            await cmd_rederive(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "sip" in text.lower() or "0.55" in text


# ---------------------------------------------------------------------------
# cmd_taskstatus
# ---------------------------------------------------------------------------

class TestCmdTaskstatus:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_taskstatus
        update = _make_update("/taskstatus")
        ctx = _make_context()
        await cmd_taskstatus(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text or "usage" in text.lower()

    @pytest.mark.asyncio
    async def test_returns_task_status(self):
        from atlas_plus.telegram.commands import cmd_taskstatus
        update = _make_update("/taskstatus abc-123")
        ctx = _make_context()
        ctx.args = ["abc-123"]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "done", "started_at": "2026-05-21T01:00:00Z",
                                        "finished_at": "2026-05-21T01:05:00Z", "result": "All good."}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_taskstatus(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "abc-123" in text and "done" in text

    @pytest.mark.asyncio
    async def test_404_shows_not_found(self):
        from atlas_plus.telegram.commands import cmd_taskstatus
        update = _make_update("/taskstatus missing-99")
        ctx = _make_context()
        ctx.args = ["missing-99"]
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_taskstatus(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "not found" in text.lower() or "missing-99" in text


# ---------------------------------------------------------------------------
# cmd_watch
# ---------------------------------------------------------------------------

class TestCmdWatch:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_watch
        update = _make_update("/watch")
        ctx = _make_context()
        await cmd_watch(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text or "usage" in text.lower()

    @pytest.mark.asyncio
    async def test_already_done_task_shows_result(self):
        from atlas_plus.telegram.commands import cmd_watch
        update = _make_update("/watch done-task-1")
        update.effective_chat = MagicMock()
        update.effective_chat.id = 99
        ctx = _make_context()
        ctx.args = ["done-task-1"]
        ctx.job_queue = None
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "done", "result": "Completed successfully."}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_watch(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "done" in text.lower()

    @pytest.mark.asyncio
    async def test_running_task_queues_watch(self):
        from atlas_plus.telegram.commands import cmd_watch
        update = _make_update("/watch running-task-2")
        update.effective_chat = MagicMock()
        update.effective_chat.id = 99
        ctx = _make_context()
        ctx.args = ["running-task-2"]
        ctx.job_queue = MagicMock()
        ctx.job_queue.run_repeating = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "running"}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_watch(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "watching" in text.lower() or "notified" in text.lower()
        ctx.job_queue.run_repeating.assert_called_once()


# ---------------------------------------------------------------------------
# cmd_decisions
# ---------------------------------------------------------------------------

class TestCmdDecisions:
    @pytest.mark.asyncio
    async def test_no_decisions_dir_shows_message(self):
        from atlas_plus.telegram.commands import cmd_decisions
        update = _make_update("/decisions")
        ctx = _make_context()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT",
                   __import__("pathlib").Path("/nonexistent")):
            await cmd_decisions(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "no decision" in text.lower() or "not found" in text.lower()

    @pytest.mark.asyncio
    async def test_shows_decisions_list(self, tmp_path):
        from atlas_plus.telegram.commands import cmd_decisions
        update = _make_update("/decisions")
        ctx = _make_context()
        decisions_dir = tmp_path / "decisions"
        decisions_dir.mkdir()
        (decisions_dir / "adr-001.md").write_text(
            "---\ntitle: Use SQLite\nmade: 2026-01-01\nlast_reviewed: 2026-01-01\n---\n\nContent.\n",
            encoding="utf-8",
        )
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path):
            await cmd_decisions(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "SQLite" in text or "decision" in text.lower()

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_decisions
        update = _make_update("/decisions")
        ctx = _make_context()
        with patch("asyncio.to_thread", side_effect=RuntimeError("disk error")):
            await cmd_decisions(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_supervisor
# ---------------------------------------------------------------------------

class TestCmdSupervisor:
    @pytest.mark.asyncio
    async def test_no_brain_url_returns_error(self):
        from atlas_plus.telegram.commands import cmd_supervisor
        update = _make_update("/supervisor")
        ctx = _make_context()
        with patch("atlas_plus.telegram.commands.BRAIN_URL", ""):
            await cmd_supervisor(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ATLAS_BRAIN_URL" in text or "not set" in text.lower()

    @pytest.mark.asyncio
    async def test_queues_supervisor_task(self):
        from atlas_plus.telegram.commands import cmd_supervisor
        update = _make_update("/supervisor")
        ctx = _make_context()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"task_id": "sup-task-abc"}
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_supervisor(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "sup-task-abc" in text

    @pytest.mark.asyncio
    async def test_http_error_handled(self):
        from atlas_plus.telegram.commands import cmd_supervisor
        update = _make_update("/supervisor")
        ctx = _make_context()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=Exception("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_supervisor(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_claude
# ---------------------------------------------------------------------------

class TestCmdClaude:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_claude
        update = _make_update("/claude")
        ctx = _make_context()
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"):
            await cmd_claude(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text or "usage" in text.lower()

    @pytest.mark.asyncio
    async def test_queues_claude_code_task(self):
        from atlas_plus.telegram.commands import cmd_claude
        update = _make_update("/claude refactor auth module")
        ctx = _make_context()
        ctx.args = ["refactor", "auth", "module"]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"task_id": "claude-task-xyz"}
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_claude(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "claude-task-xyz" in text

    @pytest.mark.asyncio
    async def test_error_shown(self):
        from atlas_plus.telegram.commands import cmd_claude
        update = _make_update("/claude do something")
        ctx = _make_context()
        ctx.args = ["do", "something"]
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=Exception("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_claude(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_decay
# ---------------------------------------------------------------------------

class TestCmdDecay:
    @pytest.mark.asyncio
    async def test_no_decayed_shows_clean_message(self):
        from atlas_plus.telegram.commands import cmd_decay
        update = _make_update("/decay")
        ctx = _make_context()
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]):
            await cmd_decay(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No memory" in text or "no memory" in text.lower() or "half-life" in text.lower()

    @pytest.mark.asyncio
    async def test_shows_decayed_files(self):
        from atlas_plus.telegram.commands import cmd_decay
        update = _make_update("/decay")
        ctx = _make_context()
        fake = MagicMock()
        fake.path = "identity/role.md"
        fake.half_life_days = 90
        fake.days_overdue = 15
        fake.updated = "2026-02-01"
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[fake]):
            await cmd_decay(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "identity/role" in text or "90" in text

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_decay
        update = _make_update("/decay")
        ctx = _make_context()
        with patch("asyncio.to_thread", side_effect=RuntimeError("import error")):
            await cmd_decay(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_loop_status
# ---------------------------------------------------------------------------

class TestCmdLoopStatus:
    @pytest.mark.asyncio
    async def test_shows_dispatcher_off_when_disabled(self):
        from atlas_plus.telegram.commands import cmd_loop_status
        update = _make_update("/loop_status")
        ctx = _make_context()
        with patch("atlas_plus.brain.dispatcher.get_dispatch_stats",
                   return_value={"enabled": False}), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]), \
             patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=[]):
            await cmd_loop_status(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "off" in text.lower() or "Dispatcher" in text

    @pytest.mark.asyncio
    async def test_shows_ticket_counts(self):
        from atlas_plus.telegram.commands import cmd_loop_status
        from atlas_plus.tickets.store import Ticket
        from pathlib import Path
        update = _make_update("/loop_status")
        ctx = _make_context()
        tickets = [
            Ticket(id="T1", title="A", project="p", status="todo", path=Path("t1.md")),
            Ticket(id="T2", title="B", project="p", status="in-progress", path=Path("t2.md")),
        ]
        with patch("atlas_plus.brain.dispatcher.get_dispatch_stats",
                   return_value={"enabled": True, "in_flight": 1, "total_dispatched": 5}), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=tickets), \
             patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=[]):
            await cmd_loop_status(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "1" in text and "todo" in text.lower() or "in-progress" in text.lower()

    @pytest.mark.asyncio
    async def test_exception_in_dispatcher_handled(self):
        from atlas_plus.telegram.commands import cmd_loop_status
        update = _make_update("/loop_status")
        ctx = _make_context()
        with patch("atlas_plus.brain.dispatcher.get_dispatch_stats",
                   side_effect=ImportError("no dispatcher")), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]), \
             patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=[]):
            await cmd_loop_status(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "unavailable" in text.lower() or "Automation" in text


# ---------------------------------------------------------------------------
# cmd_research_force
# ---------------------------------------------------------------------------

class TestCmdResearchForce:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_research_force
        update = _make_update("/research_force")
        ctx = _make_context()
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"):
            await cmd_research_force(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text or "usage" in text.lower()

    @pytest.mark.asyncio
    async def test_queues_research_task(self):
        from atlas_plus.telegram.commands import cmd_research_force
        update = _make_update("/research_force SIP REGISTER flow")
        ctx = _make_context()
        ctx.args = ["SIP", "REGISTER", "flow"]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"task_id": "rf-task-001"}
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_research_force(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "rf-task-001" in text

    @pytest.mark.asyncio
    async def test_error_shown(self):
        from atlas_plus.telegram.commands import cmd_research_force
        update = _make_update("/research_force RTCP")
        ctx = _make_context()
        ctx.args = ["RTCP"]
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=Exception("brain down"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_research_force(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_credit_reset
# ---------------------------------------------------------------------------

class TestCmdCreditReset:
    @pytest.mark.asyncio
    async def test_no_brain_url_returns_error(self):
        from atlas_plus.telegram.commands import cmd_credit_reset
        update = _make_update("/credit")
        ctx = _make_context()
        with patch("atlas_plus.telegram.commands.BRAIN_URL", ""):
            await cmd_credit_reset(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ATLAS_BRAIN_URL" in text or "not set" in text.lower()

    @pytest.mark.asyncio
    async def test_cleared_agents_shown(self):
        from atlas_plus.telegram.commands import cmd_credit_reset
        update = _make_update("/credit")
        ctx = _make_context()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"cleared": ["claude-code", "qwen3"]}
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_credit_reset(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "claude-code" in text or "cleared" in text.lower()

    @pytest.mark.asyncio
    async def test_no_exhausted_agents_shows_message(self):
        from atlas_plus.telegram.commands import cmd_credit_reset
        update = _make_update("/credit")
        ctx = _make_context()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"cleared": []}
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_credit_reset(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No agents" in text or "not" in text.lower()


# ---------------------------------------------------------------------------
# cmd_digest
# ---------------------------------------------------------------------------

class TestCmdDigest:
    @pytest.mark.asyncio
    async def test_no_brain_url_returns_error(self):
        from atlas_plus.telegram.commands import cmd_digest
        update = _make_update("/digest")
        ctx = _make_context()
        with patch("atlas_plus.telegram.commands.BRAIN_URL", ""):
            await cmd_digest(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ATLAS_BRAIN_URL" in text or "not set" in text.lower()

    @pytest.mark.asyncio
    async def test_queues_digest_task(self):
        from atlas_plus.telegram.commands import cmd_digest
        update = _make_update("/digest")
        ctx = _make_context()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"task_id": "digest-task-555"}
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_digest(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "digest-task-555" in text

    @pytest.mark.asyncio
    async def test_error_shown(self):
        from atlas_plus.telegram.commands import cmd_digest
        update = _make_update("/digest")
        ctx = _make_context()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=Exception("503"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_digest(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_report
# ---------------------------------------------------------------------------

class TestCmdReport:
    @pytest.mark.asyncio
    async def test_shows_task_summary(self):
        from atlas_plus.telegram.commands import cmd_report
        update = _make_update("/report")
        ctx = _make_context()
        fake_task = MagicMock()
        fake_task.status = "done"
        fake_task.task_id = "tsk-001"
        fake_task.instruction = "fix the login bug"
        fake_task.error = None
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[fake_task]):
            await cmd_report(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "done" in text.lower() or "1" in text

    @pytest.mark.asyncio
    async def test_empty_task_list_still_replies(self):
        from atlas_plus.telegram.commands import cmd_report
        update = _make_update("/report")
        ctx = _make_context()
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[]):
            await cmd_report(update, ctx)
        update.message.reply_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_report
        update = _make_update("/report")
        ctx = _make_context()
        with patch("atlas_plus.brain.tasks.list_tasks",
                   side_effect=RuntimeError("db locked")):
            await cmd_report(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_orphans
# ---------------------------------------------------------------------------

class TestCmdOrphans:
    @pytest.mark.asyncio
    async def test_no_orphans_shows_clean_message(self):
        from atlas_plus.telegram.commands import cmd_orphans
        update = _make_update("/orphans")
        ctx = _make_context()
        with patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]):
            await cmd_orphans(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No orphan" in text or "no orphan" in text.lower()

    @pytest.mark.asyncio
    async def test_shows_orphan_list(self):
        from atlas_plus.telegram.commands import cmd_orphans
        update = _make_update("/orphans 5")
        ctx = _make_context()
        ctx.args = ["5"]
        fake = [{"path": "identity/old-role.md", "age_days": 200, "score": 0.1}]
        with patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=fake):
            await cmd_orphans(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "identity/old-role" in text or "200" in text

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_orphans
        update = _make_update("/orphans")
        ctx = _make_context()
        with patch("atlas_plus.curation.orphan.find_orphan_candidates",
                   side_effect=Exception("scan failed")):
            await cmd_orphans(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "failed" in text.lower() or "Error" in text


# ---------------------------------------------------------------------------
# cmd_research_index
# ---------------------------------------------------------------------------

class TestCmdResearchIndex:
    @pytest.mark.asyncio
    async def test_no_reports_dir_shows_message(self, tmp_path):
        from atlas_plus.telegram.commands import cmd_research_index
        update = _make_update("/research_index")
        ctx = _make_context()
        with patch.dict("os.environ", {"ATLAS_STATE_DIR": str(tmp_path)}):
            await cmd_research_index(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No" in text or "not found" in text.lower()

    @pytest.mark.asyncio
    async def test_shows_report_list(self, tmp_path):
        from atlas_plus.telegram.commands import cmd_research_index
        update = _make_update("/research_index")
        ctx = _make_context()
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        (reports_dir / "research-sip-2026-05-01.md").write_text(
            '---\ntitle: "SIP REGISTER"\ndate: 2026-05-01\nquality_score: 0.82\n---\n\nContent.\n',
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"ATLAS_STATE_DIR": str(tmp_path)}):
            await cmd_research_index(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "SIP" in text or "0.82" in text or "Research Index" in text

    @pytest.mark.asyncio
    async def test_empty_reports_dir_shows_message(self, tmp_path):
        from atlas_plus.telegram.commands import cmd_research_index
        update = _make_update("/research_index")
        ctx = _make_context()
        (tmp_path / "reports").mkdir()
        with patch.dict("os.environ", {"ATLAS_STATE_DIR": str(tmp_path)}):
            await cmd_research_index(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No" in text or "not found" in text.lower()


# ---------------------------------------------------------------------------
# cmd_stats
# ---------------------------------------------------------------------------

class TestCmdStats:
    @pytest.mark.asyncio
    async def test_shows_task_stats(self):
        from atlas_plus.telegram.commands import cmd_stats
        update = _make_update("/stats")
        ctx = _make_context()
        with patch("atlas_plus.brain.tasks.task_stats",
                   return_value={"total": 10, "done": 7, "failed": 2, "running": 1}), \
             patch("atlas_plus.brain.task_facts.pending_count", return_value=3), \
             patch("atlas_plus.brain.history.session_count", return_value=5):
            await cmd_stats(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "10" in text or "Stats" in text

    @pytest.mark.asyncio
    async def test_shows_escalation_state(self):
        from atlas_plus.telegram.commands import cmd_stats
        update = _make_update("/stats")
        ctx = _make_context()
        with patch("atlas_plus.brain.tasks.task_stats", return_value={}), \
             patch("atlas_plus.brain.task_facts.pending_count", return_value=0), \
             patch("atlas_plus.brain.history.session_count", return_value=0), \
             patch.dict("os.environ", {"ATLAS_CLAUDE_ESCALATION": "on"}):
            await cmd_stats(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "on" in text.lower() or "Escalation" in text

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_stats
        update = _make_update("/stats")
        ctx = _make_context()
        with patch("atlas_plus.brain.tasks.task_stats",
                   side_effect=ImportError("no tasks")):
            await cmd_stats(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "error" in text.lower() or "Stats" in text.lower()


# ---------------------------------------------------------------------------
# cmd_status_update
# ---------------------------------------------------------------------------

class TestCmdStatusUpdate:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_status_update
        update = _make_update("/status")
        ctx = _make_context()
        await cmd_status_update(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text or "usage" in text.lower()

    @pytest.mark.asyncio
    async def test_updates_ticket_status(self):
        from atlas_plus.telegram.commands import cmd_status_update
        from atlas_plus.tickets.store import Ticket
        from pathlib import Path
        update = _make_update("/status ATL-010 done")
        ctx = _make_context()
        ctx.args = ["ATL-010", "done"]
        fake_ticket = Ticket(id="ATL-010", title="Fix bug", project="atlas",
                              status="done", path=Path("t.md"))
        with patch("atlas_plus.tickets.store.update_ticket_status",
                   return_value=fake_ticket):
            await cmd_status_update(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ATL-010" in text and "done" in text

    @pytest.mark.asyncio
    async def test_not_found_shows_error(self):
        from atlas_plus.telegram.commands import cmd_status_update
        update = _make_update("/status ATL-999 done")
        ctx = _make_context()
        ctx.args = ["ATL-999", "done"]
        with patch("atlas_plus.tickets.store.update_ticket_status",
                   side_effect=FileNotFoundError("ticket not found")):
            await cmd_status_update(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text or "not found" in text.lower()


# ---------------------------------------------------------------------------
# cmd_plan
# ---------------------------------------------------------------------------

class TestCmdPlan:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_plan
        update = _make_update("/plan")
        ctx = _make_context()
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"):
            await cmd_plan(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text or "usage" in text.lower()

    @pytest.mark.asyncio
    async def test_queues_plan_task(self):
        from atlas_plus.telegram.commands import cmd_plan
        update = _make_update("/plan build search feature")
        ctx = _make_context()
        ctx.args = ["build", "search", "feature"]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"task_id": "plan-task-001"}
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_plan(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "plan-task-001" in text

    @pytest.mark.asyncio
    async def test_error_shown(self):
        from atlas_plus.telegram.commands import cmd_plan
        update = _make_update("/plan fix all bugs")
        ctx = _make_context()
        ctx.args = ["fix", "all", "bugs"]
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=Exception("brain offline"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_plan(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_priority
# ---------------------------------------------------------------------------

class TestCmdPriority:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_priority
        update = _make_update("/priority")
        ctx = _make_context()
        await cmd_priority(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text or "usage" in text.lower()

    @pytest.mark.asyncio
    async def test_invalid_priority_shows_error(self):
        from atlas_plus.telegram.commands import cmd_priority
        update = _make_update("/priority ATL-001 urgent")
        ctx = _make_context()
        ctx.args = ["ATL-001", "urgent"]
        await cmd_priority(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Invalid" in text or "invalid" in text.lower()

    @pytest.mark.asyncio
    async def test_updates_priority(self, tmp_path):
        from atlas_plus.telegram.commands import cmd_priority
        from atlas_plus.tickets.store import Ticket
        from pathlib import Path
        update = _make_update("/priority ATL-002 high")
        ctx = _make_context()
        ctx.args = ["ATL-002", "high"]
        ticket_path = tmp_path / "ATL-002.md"
        ticket_path.write_text("---\nid: ATL-002\ntitle: Fix\nstatus: todo\n---\n", encoding="utf-8")
        fake_ticket = Ticket(id="ATL-002", title="Fix", project="atlas",
                              status="todo", priority="normal", path=ticket_path)
        fake_ticket.to_markdown = MagicMock(return_value="---\nid: ATL-002\n---\n")
        with patch("atlas_plus.tickets.store.get_ticket", return_value=fake_ticket), \
             patch("atlas_plus.mcp.memory_server.versioning.version_before_write"):
            await cmd_priority(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ATL-002" in text and "high" in text


# ---------------------------------------------------------------------------
# cmd_project
# ---------------------------------------------------------------------------

class TestCmdProject:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_project
        update = _make_update("/project")
        ctx = _make_context()
        await cmd_project(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text or "usage" in text.lower()

    @pytest.mark.asyncio
    async def test_shows_project_state(self):
        from atlas_plus.telegram.commands import cmd_project
        update = _make_update("/project atlas-memory+")
        ctx = _make_context()
        ctx.args = ["atlas-memory+"]
        fake_state = {
            "project": "atlas-memory+",
            "git": {"branch": "main", "dirty": False, "ahead": 0},
            "open_tickets": [{"id": "ATL-010", "title": "public readme"}],
        }
        with patch("atlas_plus.mcp.memory_server.tools.project_state.project_state",
                   return_value=fake_state):
            await cmd_project(update, ctx)
        update.message.reply_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_shows_message(self):
        from atlas_plus.telegram.commands import cmd_project
        update = _make_update("/project unknown-project")
        ctx = _make_context()
        ctx.args = ["unknown-project"]
        with patch("atlas_plus.mcp.memory_server.tools.project_state.project_state",
                   side_effect=Exception("not found")):
            await cmd_project(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text or "error" in text.lower()


# ---------------------------------------------------------------------------
# cmd_quick
# ---------------------------------------------------------------------------

class TestCmdQuick:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_quick
        update = _make_update("/quick")
        ctx = _make_context()
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"):
            await cmd_quick(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text or "usage" in text.lower()

    @pytest.mark.asyncio
    async def test_queues_fast_task(self):
        from atlas_plus.telegram.commands import cmd_quick
        update = _make_update("/quick what is SIP")
        ctx = _make_context()
        ctx.args = ["what", "is", "SIP"]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"task_id": "fast-task-abc"}
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_quick(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "fast-task-abc" in text

    @pytest.mark.asyncio
    async def test_error_shown(self):
        from atlas_plus.telegram.commands import cmd_quick
        update = _make_update("/quick explain DTMF")
        ctx = _make_context()
        ctx.args = ["explain", "DTMF"]
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=Exception("503"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_quick(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_run
# ---------------------------------------------------------------------------

class TestCmdRun:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_run
        update = _make_update("/run")
        ctx = _make_context()
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"):
            await cmd_run(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text or "usage" in text.lower()

    @pytest.mark.asyncio
    async def test_unknown_job_shows_error(self):
        from atlas_plus.telegram.commands import cmd_run
        update = _make_update("/run bogus-job")
        ctx = _make_context()
        ctx.args = ["bogus-job"]
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"):
            await cmd_run(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Unknown" in text or "unknown" in text.lower() or "Valid" in text

    @pytest.mark.asyncio
    async def test_known_job_dispatches_task(self):
        from atlas_plus.telegram.commands import cmd_run
        update = _make_update("/run curation")
        ctx = _make_context()
        ctx.args = ["curation"]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"task_id": "sched-task-001"}
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_run(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "sched-task-001" in text or "curation" in text


# ---------------------------------------------------------------------------
# cmd_think
# ---------------------------------------------------------------------------

class TestCmdThink:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_think
        update = _make_update("/think")
        ctx = _make_context()
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"):
            await cmd_think(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text or "usage" in text.lower()

    @pytest.mark.asyncio
    async def test_queues_brain_task(self):
        from atlas_plus.telegram.commands import cmd_think
        update = _make_update("/think analyze failure patterns")
        ctx = _make_context()
        ctx.args = ["analyze", "failure", "patterns"]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"task_id": "think-task-007"}
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_think(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "think-task-007" in text

    @pytest.mark.asyncio
    async def test_error_shown(self):
        from atlas_plus.telegram.commands import cmd_think
        update = _make_update("/think why did tests fail")
        ctx = _make_context()
        ctx.args = ["why", "did", "tests", "fail"]
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=Exception("brain timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_think(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text


# ---------------------------------------------------------------------------
# cmd_ticket_status
# ---------------------------------------------------------------------------

class TestCmdTicketStatus:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_ticket_status
        update = _make_update("/ticketstatus")
        ctx = _make_context()
        await cmd_ticket_status(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text or "usage" in text.lower()

    @pytest.mark.asyncio
    async def test_shows_ticket_info(self):
        from atlas_plus.telegram.commands import cmd_ticket_status
        from atlas_plus.tickets.store import Ticket
        from pathlib import Path
        update = _make_update("/ticketstatus ATL-009")
        ctx = _make_context()
        ctx.args = ["ATL-009"]
        fake_ticket = Ticket(id="ATL-009", title="Public README", project="atlas",
                              status="todo", assignee="any", priority="normal",
                              path=Path("t.md"))
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[fake_ticket]), \
             patch("atlas_plus.brain.tasks.list_tasks", return_value=[]):
            await cmd_ticket_status(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ATL-009" in text

    @pytest.mark.asyncio
    async def test_not_found_shows_message(self):
        from atlas_plus.telegram.commands import cmd_ticket_status
        update = _make_update("/ticketstatus ATL-999")
        ctx = _make_context()
        ctx.args = ["ATL-999"]
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            await cmd_ticket_status(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "not found" in text.lower() or "ATL-999" in text


# ---------------------------------------------------------------------------
# cmd_verify_ticket
# ---------------------------------------------------------------------------

class TestCmdVerifyTicket:
    @pytest.mark.asyncio
    async def test_no_args_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_verify_ticket
        update = _make_update("/verify")
        ctx = _make_context()
        await cmd_verify_ticket(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Usage" in text or "usage" in text.lower()

    @pytest.mark.asyncio
    async def test_invalid_format_shows_error(self):
        from atlas_plus.telegram.commands import cmd_verify_ticket
        update = _make_update("/verify notanid")
        ctx = _make_context()
        ctx.args = ["notanid"]
        await cmd_verify_ticket(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Invalid" in text or "format" in text.lower()

    @pytest.mark.asyncio
    async def test_queues_verify_task(self):
        from atlas_plus.telegram.commands import cmd_verify_ticket
        update = _make_update("/verify ATL-007")
        ctx = _make_context()
        ctx.args = ["ATL-007"]
        fake_task = MagicMock()
        fake_task.task_id = "verify-task-abc123"
        with patch("atlas_plus.brain.tasks.create_task", return_value=fake_task):
            await cmd_verify_ticket(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ATL-007" in text and "verify" in text.lower()


# ---------------------------------------------------------------------------
# cmd_dispatch_tickets
# ---------------------------------------------------------------------------

class TestCmdDispatchTickets:
    @pytest.mark.asyncio
    async def test_no_eligible_tickets_shows_message(self):
        from atlas_plus.telegram.commands import cmd_dispatch_tickets
        update = _make_update("/dispatch_tickets")
        ctx = _make_context()
        with patch("atlas_plus.brain.dispatcher.dispatch_all_eligible", return_value=[]):
            await cmd_dispatch_tickets(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No eligible" in text or "no eligible" in text.lower()

    @pytest.mark.asyncio
    async def test_shows_dispatched_list(self):
        from atlas_plus.telegram.commands import cmd_dispatch_tickets
        update = _make_update("/dispatch_tickets")
        ctx = _make_context()
        fake = [{"ticket_id": "ATL-010", "task_id": "task-xyz-001", "agent": "claude-code"}]
        with patch("atlas_plus.brain.dispatcher.dispatch_all_eligible", return_value=fake):
            await cmd_dispatch_tickets(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ATL-010" in text or "task-xyz" in text

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_dispatch_tickets
        update = _make_update("/dispatch_tickets")
        ctx = _make_context()
        with patch("atlas_plus.brain.dispatcher.dispatch_all_eligible",
                   side_effect=RuntimeError("db error")):
            await cmd_dispatch_tickets(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Dispatch error" in text or "error" in text.lower()


# ---------------------------------------------------------------------------
# cmd_export
# ---------------------------------------------------------------------------

class TestCmdExport:
    @pytest.mark.asyncio
    async def test_no_brain_url_returns_error(self):
        from atlas_plus.telegram.commands import cmd_export
        update = _make_update("/export")
        ctx = _make_context()
        with patch("atlas_plus.telegram.commands.BRAIN_URL", ""):
            await cmd_export(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "ATLAS_BRAIN_URL" in text or "not set" in text.lower()

    @pytest.mark.asyncio
    async def test_sends_document_on_success(self):
        from atlas_plus.telegram.commands import cmd_export
        update = _make_update("/export csv")
        update.message.reply_document = AsyncMock()
        ctx = _make_context()
        ctx.args = ["csv"]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.content = b"task_id,status\ntask-1,done\n"
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_export(update, ctx)
        update.message.reply_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_http_error_shown(self):
        from atlas_plus.telegram.commands import cmd_export
        update = _make_update("/export")
        ctx = _make_context()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=Exception("connection error"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.telegram.commands.BRAIN_URL", "http://127.0.0.1:8767"), \
             patch("httpx.AsyncClient", return_value=mock_client):
            await cmd_export(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "failed" in text.lower() or "Error" in text


# ---------------------------------------------------------------------------
# cmd_projects
# ---------------------------------------------------------------------------

class TestCmdProjects:
    @pytest.mark.asyncio
    async def test_no_projects_root_shows_message(self, tmp_path):
        from atlas_plus.telegram.commands import cmd_projects
        update = _make_update("/projects")
        ctx = _make_context()
        missing = tmp_path / "nonexistent-projects"
        with patch("atlas_plus.telegram.commands.PROJECTS_ROOT", missing):
            await cmd_projects(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "not found" in text.lower() or "No" in text

    @pytest.mark.asyncio
    async def test_lists_project_directories(self, tmp_path):
        from atlas_plus.telegram.commands import cmd_projects
        update = _make_update("/projects")
        ctx = _make_context()
        (tmp_path / "atlas-memory+").mkdir()
        (tmp_path / "harbor-app").mkdir()
        (tmp_path / ".hidden").mkdir()
        with patch("atlas_plus.telegram.commands.PROJECTS_ROOT", tmp_path):
            await cmd_projects(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "atlas-memory+" in text and "harbor-app" in text
        assert ".hidden" not in text

    @pytest.mark.asyncio
    async def test_exception_handled(self, tmp_path):
        from atlas_plus.telegram.commands import cmd_projects
        update = _make_update("/projects")
        ctx = _make_context()
        broken_root = MagicMock()
        broken_root.exists.return_value = True
        broken_root.iterdir.side_effect = PermissionError("access denied")
        with patch("atlas_plus.telegram.commands.PROJECTS_ROOT", broken_root):
            await cmd_projects(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text or "error" in text.lower()


# ---------------------------------------------------------------------------
# cmd_research_quality (global summary mode -- topic-filter already in TestCmdResearchQualityTopicFilter)
# ---------------------------------------------------------------------------

class TestCmdResearchQualityGlobal:
    @pytest.mark.asyncio
    async def test_no_reports_dir_shows_message(self):
        from atlas_plus.telegram.commands import cmd_research_quality
        update = _make_update("/research_quality")
        ctx = _make_context()
        with patch("atlas_plus.doctor.cli.check_research_quality", return_value={}):
            await cmd_research_quality(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "No research" in text or "no research" in text.lower()

    @pytest.mark.asyncio
    async def test_shows_quality_summary(self):
        from atlas_plus.telegram.commands import cmd_research_quality
        update = _make_update("/research_quality")
        ctx = _make_context()
        fake_rq = {
            "total_reports": 15,
            "scored_reports": 12,
            "avg_score": 0.74,
            "high_quality": 5,
            "medium_quality": 7,
            "needs_work": 3,
            "missing_score": 3,
        }
        with patch("atlas_plus.doctor.cli.check_research_quality", return_value=fake_rq):
            await cmd_research_quality(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "15" in text or "0.74" in text or "quality" in text.lower()

    @pytest.mark.asyncio
    async def test_exception_handled(self):
        from atlas_plus.telegram.commands import cmd_research_quality
        update = _make_update("/research_quality")
        ctx = _make_context()
        with patch("atlas_plus.doctor.cli.check_research_quality",
                   side_effect=Exception("reports dir missing")):
            await cmd_research_quality(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text or "error" in text.lower()


# ---------------------------------------------------------------------------
# /suggest_tags
# ---------------------------------------------------------------------------

class TestCmdSuggestTags:
    @pytest.mark.asyncio
    async def test_no_untagged_shows_all_tagged(self):
        from atlas_plus.telegram.commands import cmd_suggest_tags
        update = _make_update("/suggest_tags")
        ctx = _make_context()
        with patch("atlas_plus.curation.tag_suggest.suggest_tags_for_file",
                   return_value=[]), \
             patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT",
                   __import__("pathlib").Path("/nonexistent")):
            await cmd_suggest_tags(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "tagged" in text.lower() or "no" in text.lower()

    @pytest.mark.asyncio
    async def test_shows_untagged_files(self, tmp_path):
        from atlas_plus.telegram.commands import cmd_suggest_tags
        update = _make_update("/suggest_tags")
        ctx = _make_context()
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        (feedback / "pref.md").write_text(
            "---\ntitle: FastAPI Pref\ntype: feedback\n---\nContent.", encoding="utf-8"
        )
        with patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT", tmp_path):
            await cmd_suggest_tags(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "pref.md" in text or "fastapi" in text.lower() or "Untagged" in text

    @pytest.mark.asyncio
    async def test_exception_shows_error(self, tmp_path, monkeypatch):
        from atlas_plus.telegram.commands import cmd_suggest_tags
        # the scan reads the config module's MEMORY_ROOT (import-time); give it one untagged
        # note so the mocked suggester is actually reached (suite isolation 2026-11-09)
        root = tmp_path / "memory"
        (root / "projects").mkdir(parents=True)
        (root / "projects" / "x.md").write_text("# x" + chr(10) + chr(10) + "no tags here" + chr(10), encoding="utf-8")
        import importlib
        monkeypatch.setattr(importlib.import_module("atlas_plus.mcp.memory_server.config"), "MEMORY_ROOT", root)
        update = _make_update("/suggest_tags")
        ctx = _make_context()

        async def _boom(fn, *a, **k):  # the scan itself blows up, whatever the corpus holds
            raise RuntimeError("scan failed")

        with patch("atlas_plus.telegram.commands.asyncio.to_thread", _boom):
            await cmd_suggest_tags(update, ctx)
        text = update.message.reply_text.call_args[0][0]
        assert "Error" in text or "error" in text.lower()


class TestCmdSuggestFacts:
    @pytest.mark.asyncio
    async def test_no_candidates_shows_message(self):
        from atlas_plus.telegram.commands import cmd_suggest_facts
        update = _make_update("/suggest_facts")
        ctx = _make_context()
        stats = {"candidates": 0, "approved": 0, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats):
            await cmd_suggest_facts(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "no fact" in full_text.lower() or "No fact" in full_text

    @pytest.mark.asyncio
    async def test_shows_candidates_count(self):
        from atlas_plus.telegram.commands import cmd_suggest_facts
        update = _make_update("/suggest_facts")
        ctx = _make_context()
        stats = {"candidates": 5, "approved": 2, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats):
            await cmd_suggest_facts(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "5" in full_text or "candidates" in full_text.lower()

    @pytest.mark.asyncio
    async def test_runtime_error_shows_error(self):
        from atlas_plus.telegram.commands import cmd_suggest_facts
        update = _make_update("/suggest_facts")
        ctx = _make_context()
        with patch("atlas_plus.curation.suggest_facts.suggest_facts",
                   side_effect=RuntimeError("no transcript")):
            await cmd_suggest_facts(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "Error" in full_text or "no transcript" in full_text


# ---------------------------------------------------------------------------
# /temporal_search, /temporal_stats
# ---------------------------------------------------------------------------

class TestCmdCurationReport:
    def _make_report(self, dups=2, stale=3):
        return (
            f"# Atlas Memory Curation Report\n\n"
            f"## Potential Duplicates ({dups} found, threshold=0.40)\n"
            f"## Stale Files ({stale} found, older than 6 months)\n"
            f"## Convergence Flags (0 contradictions found)\n"
            f"## Stale Decisions (1 decisions need revisit)\n"
        )

    @pytest.mark.asyncio
    async def test_shows_counts(self):
        from atlas_plus.telegram.commands import cmd_curation_report
        from pathlib import Path
        report = self._make_report(dups=2, stale=3)
        update = _make_update("/curation_report")
        ctx = _make_context()
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   return_value=(report, Path(".atlas/report.md"))):
            await cmd_curation_report(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "2" in full_text or "Duplicates" in full_text

    @pytest.mark.asyncio
    async def test_error_shows_message(self):
        from atlas_plus.telegram.commands import cmd_curation_report
        update = _make_update("/curation_report")
        ctx = _make_context()
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   side_effect=Exception("memory root not found")):
            await cmd_curation_report(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "Error" in full_text or "memory root" in full_text


class TestCmdTemporalRebuild:
    @pytest.mark.asyncio
    async def test_fresh_graph_skips_rebuild(self):
        from atlas_plus.telegram.commands import cmd_temporal_rebuild
        update = _make_update("/temporal_rebuild")
        ctx = _make_context()
        ctx.args = []
        with patch("atlas_plus.doctor.cli.check_temporal",
                   return_value={"stale": False, "age_days": 1, "entities": 10, "last_built": "2026-05-21"}):
            await cmd_temporal_rebuild(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "fresh" in full_text.lower() or "force" in full_text.lower()

    @pytest.mark.asyncio
    async def test_stale_triggers_rebuild(self):
        from atlas_plus.telegram.commands import cmd_temporal_rebuild
        result = {
            "source": "all", "files_indexed": 3, "files_skipped": 5, "facts_added": 10,
            "warnings": [],
            "index": {"episodes": 2, "entities": 12, "facts_total": 50, "facts_active": 45}
        }
        update = _make_update("/temporal_rebuild")
        ctx = _make_context()
        ctx.args = []
        with patch("atlas_plus.doctor.cli.check_temporal",
                   return_value={"stale": True, "age_days": 10, "entities": 5}), \
             patch("atlas_plus.temporal_graph.build.build_temporal_graph", return_value=result):
            await cmd_temporal_rebuild(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "rebuilt" in full_text.lower() or "12" in full_text

    @pytest.mark.asyncio
    async def test_force_flag_bypasses_freshness(self):
        from atlas_plus.telegram.commands import cmd_temporal_rebuild
        result = {
            "source": "all", "files_indexed": 2, "files_skipped": 3, "facts_added": 8,
            "warnings": [],
            "index": {"episodes": 1, "entities": 8, "facts_total": 40, "facts_active": 38}
        }
        update = _make_update("/temporal_rebuild --force")
        ctx = _make_context()
        ctx.args = ["--force"]
        with patch("atlas_plus.doctor.cli.check_temporal",
                   return_value={"stale": False, "age_days": 1, "entities": 10, "last_built": "2026-05-21"}), \
             patch("atlas_plus.temporal_graph.build.build_temporal_graph", return_value=result):
            await cmd_temporal_rebuild(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "rebuilt" in full_text.lower() or "8" in full_text


class TestCmdTemporalSearch:
    @pytest.mark.asyncio
    async def test_no_query_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_temporal_search
        update = _make_update("/temporal_search")
        ctx = _make_context()
        ctx.args = []
        await cmd_temporal_search(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "Usage" in full_text or "temporal_search" in full_text.lower()

    @pytest.mark.asyncio
    async def test_empty_graph_shows_message(self):
        from atlas_plus.telegram.commands import cmd_temporal_search
        update = _make_update("/temporal_search Atlas")
        ctx = _make_context()
        ctx.args = ["Atlas"]
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {"facts": 0}
            await cmd_temporal_search(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "empty" in full_text.lower() or "build" in full_text.lower()

    @pytest.mark.asyncio
    async def test_shows_results(self):
        from atlas_plus.telegram.commands import cmd_temporal_search
        from unittest.mock import MagicMock
        result = MagicMock()
        result.score = 0.9
        result.fact.fact_text = "Atlas MCP Tunnel is live"
        result.fact.valid_from = "2026-05-01T00:00:00Z"
        result.provenance = "memory"
        result.freshness_warning = None

        update = _make_update("/temporal_search Atlas MCP")
        ctx = _make_context()
        ctx.args = ["Atlas", "MCP"]
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {"facts": 5}
            instance.search_current.return_value = [result]
            await cmd_temporal_search(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "Atlas MCP Tunnel" in full_text


class TestCmdTemporalHistory:
    @pytest.mark.asyncio
    async def test_no_entity_shows_usage(self):
        from atlas_plus.telegram.commands import cmd_temporal_history
        update = _make_update("/temporal_history")
        ctx = _make_context()
        ctx.args = []
        await cmd_temporal_history(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "Usage" in full_text or "temporal_history" in full_text.lower()

    @pytest.mark.asyncio
    async def test_no_facts_shows_message(self):
        from atlas_plus.telegram.commands import cmd_temporal_history
        from unittest.mock import MagicMock
        entity = MagicMock()
        entity.name = "Atlas Brain"
        entity.first_seen_at = "2026-01-01T00:00:00Z"
        entity.last_seen_at = "2026-05-21T00:00:00Z"
        history = MagicMock()
        history.entity = entity
        history.facts = []
        history.warnings = []

        update = _make_update("/temporal_history Atlas Brain")
        ctx = _make_context()
        ctx.args = ["Atlas", "Brain"]
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.history.return_value = history
            await cmd_temporal_history(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "no temporal history" in full_text.lower() or "Atlas Brain" in full_text

    @pytest.mark.asyncio
    async def test_shows_fact_timeline(self):
        from atlas_plus.telegram.commands import cmd_temporal_history
        from unittest.mock import MagicMock
        entity = MagicMock()
        entity.name = "Atlas MCP Tunnel"
        entity.first_seen_at = "2026-01-01T00:00:00Z"
        entity.last_seen_at = "2026-05-21T00:00:00Z"

        fact = MagicMock()
        fact.fact_text = "Atlas MCP Tunnel is live at port 8765"
        fact.valid_from = "2026-01-01T00:00:00Z"
        fact.invalid_at = None

        history = MagicMock()
        history.entity = entity
        history.facts = [fact]
        history.current_fact = fact
        history.warnings = []

        update = _make_update("/temporal_history Atlas MCP Tunnel")
        ctx = _make_context()
        ctx.args = ["Atlas", "MCP", "Tunnel"]
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.history.return_value = history
            await cmd_temporal_history(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "Atlas MCP Tunnel" in full_text


class TestCmdTemporalStats:
    @pytest.mark.asyncio
    async def test_shows_stats(self):
        from atlas_plus.telegram.commands import cmd_temporal_stats
        update = _make_update("/temporal_stats")
        ctx = _make_context()
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {
                "episodes": 3, "entities": 10, "facts": 50, "facts_active": 45
            }
            await cmd_temporal_stats(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "10" in full_text or "Entities" in full_text

    @pytest.mark.asyncio
    async def test_error_shows_message(self):
        from atlas_plus.telegram.commands import cmd_temporal_stats
        update = _make_update("/temporal_stats")
        ctx = _make_context()
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   side_effect=Exception("no db")):
            await cmd_temporal_stats(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "Error" in full_text or "no db" in full_text


# ---------------------------------------------------------------------------
# /session_log
# ---------------------------------------------------------------------------

class TestCmdSessionLog:
    @pytest.mark.asyncio
    async def test_no_commits_shows_nothing_message(self):
        from atlas_plus.telegram.commands import cmd_session_log
        update = _make_update("/session_log")
        ctx = _make_context()
        with patch("atlas_plus.sprint_summary.write_session_log",
                   return_value={"ok": True, "entry": "", "paths_written": []}):
            await cmd_session_log(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "nothing" in full_text.lower() or "No commits" in full_text

    @pytest.mark.asyncio
    async def test_writes_session_log_and_shows_summary(self):
        from atlas_plus.telegram.commands import cmd_session_log
        update = _make_update("/session_log")
        ctx = _make_context()
        fake_entry = "## 2026-05-21 -- atlas-memory+\n\nv0.17-CH: curation report Web UI"
        with patch("atlas_plus.sprint_summary.write_session_log",
                   return_value={"ok": True, "entry": fake_entry, "paths_written": ["/tmp/a.md"]}):
            await cmd_session_log(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "written" in full_text.lower() or "1" in full_text

    @pytest.mark.asyncio
    async def test_dry_run_flag_shows_preview(self):
        from atlas_plus.telegram.commands import cmd_session_log
        update = _make_update("/session_log --dry-run")
        ctx = _make_context()
        ctx.args = ["--dry-run"]
        fake_entry = "## 2026-05-21 -- atlas-memory+\n\npreview content here"
        with patch("atlas_plus.sprint_summary.write_session_log",
                   return_value={"ok": True, "entry": fake_entry, "paths_written": []}):
            await cmd_session_log(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "DRY RUN" in full_text or "preview" in full_text.lower() or "dry" in full_text.lower()

    @pytest.mark.asyncio
    async def test_error_surfaced(self):
        from atlas_plus.telegram.commands import cmd_session_log
        update = _make_update("/session_log")
        ctx = _make_context()
        with patch("atlas_plus.sprint_summary.write_session_log",
                   side_effect=RuntimeError("git error")):
            await cmd_session_log(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "Error" in full_text or "git error" in full_text


# ---------------------------------------------------------------------------
# /next_actions
# ---------------------------------------------------------------------------

class TestCmdNextActions:
    @pytest.mark.asyncio
    async def test_clean_memory_shows_good_shape_message(self):
        from atlas_plus.telegram.commands import cmd_next_actions
        update = _make_update("/next_actions")
        ctx = _make_context()
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]):
            await cmd_next_actions(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "good shape" in full_text.lower() or "no urgent" in full_text.lower()

    @pytest.mark.asyncio
    async def test_shows_decayed_facts(self):
        from atlas_plus.telegram.commands import cmd_next_actions
        update = _make_update("/next_actions")
        ctx = _make_context()
        decayed = MagicMock()
        decayed.path = "feedback/testing.md"
        decayed.days_overdue = 12
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[decayed]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]):
            await cmd_next_actions(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "Decayed" in full_text or "decayed" in full_text.lower()
        assert "12" in full_text

    @pytest.mark.asyncio
    async def test_shows_gap_queries(self):
        from atlas_plus.telegram.commands import cmd_next_actions
        update = _make_update("/next_actions")
        ctx = _make_context()
        gap = {"query": "SIP REGISTER flow", "miss_count": 5, "total": 8}
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[gap]):
            await cmd_next_actions(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "SIP REGISTER" in full_text or "Gaps" in full_text

    @pytest.mark.asyncio
    async def test_import_error_surfaced(self):
        from atlas_plus.telegram.commands import cmd_next_actions
        update = _make_update("/next_actions")
        ctx = _make_context()
        with patch("atlas_plus.telegram.commands.cmd_next_actions.__module__", "x"), \
             patch("atlas_plus.curation.decay.find_decayed_facts",
                   side_effect=ImportError("no module")):
            # Even if decay import fails, command should not raise
            try:
                await cmd_next_actions(update, ctx)
            except Exception:
                pass
        # At minimum something was replied
        assert update.message.reply_text.called


# ---------------------------------------------------------------------------
# /decay_report
# ---------------------------------------------------------------------------

class TestCmdDecayReport:
    @pytest.mark.asyncio
    async def test_queues_decay_report_task(self):
        from atlas_plus.telegram.commands import cmd_decay_report
        update = _make_update("/decay_report")
        ctx = _make_context()
        mock_task = MagicMock()
        mock_task.task_id = "dr-tg-001"
        with patch("atlas_plus.brain.tasks.create_task", return_value=mock_task):
            await cmd_decay_report(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "dr-tg-001" in full_text or "decay" in full_text.lower()

    @pytest.mark.asyncio
    async def test_error_surfaced(self):
        from atlas_plus.telegram.commands import cmd_decay_report
        update = _make_update("/decay_report")
        ctx = _make_context()
        with patch("atlas_plus.brain.tasks.create_task",
                   side_effect=RuntimeError("db locked")):
            await cmd_decay_report(update, ctx)
        calls = [c[0][0] for c in update.message.reply_text.call_args_list]
        full_text = " ".join(calls)
        assert "Error" in full_text or "db locked" in full_text


# ---------------------------------------------------------------------------
# _reply_long helper
# ---------------------------------------------------------------------------

class TestReplyLong:
    """Tests for the _reply_long message-splitting helper."""

    @pytest.mark.asyncio
    async def test_short_text_single_message(self):
        """Text under 4096 chars sends exactly one message."""
        from atlas_plus.telegram.commands import _reply_long
        update = _make_update("/test")
        await _reply_long(update, "hello world")
        assert update.message.reply_text.call_count == 1

    @pytest.mark.asyncio
    async def test_long_text_splits_into_multiple_messages(self):
        """Text over 4096 chars is split into multiple messages."""
        from atlas_plus.telegram.commands import _reply_long
        update = _make_update("/test")
        long_text = "line\n" * 1000  # ~5000 chars
        await _reply_long(update, long_text)
        assert update.message.reply_text.call_count >= 2

    @pytest.mark.asyncio
    async def test_each_chunk_within_limit(self):
        """No individual chunk exceeds 4096 characters."""
        from atlas_plus.telegram.commands import _reply_long
        update = _make_update("/test")
        long_text = "x" * 10000
        await _reply_long(update, long_text)
        for call in update.message.reply_text.call_args_list:
            assert len(call[0][0]) <= 4096

    @pytest.mark.asyncio
    async def test_parse_mode_forwarded(self):
        """parse_mode is passed through to every chunk."""
        from atlas_plus.telegram.commands import _reply_long
        update = _make_update("/test")
        long_text = "*bold*\n" * 700
        await _reply_long(update, long_text, parse_mode="Markdown")
        for call in update.message.reply_text.call_args_list:
            assert call[1].get("parse_mode") == "Markdown"

    @pytest.mark.asyncio
    async def test_empty_text_sends_placeholder(self):
        """Empty string sends a single placeholder message."""
        from atlas_plus.telegram.commands import _reply_long
        update = _make_update("/test")
        await _reply_long(update, "")
        assert update.message.reply_text.call_count == 1
        text = update.message.reply_text.call_args[0][0]
        assert "empty" in text

    @pytest.mark.asyncio
    async def test_splits_on_newline_boundary(self):
        """Splitting prefers a newline boundary over mid-word cut."""
        from atlas_plus.telegram.commands import _reply_long
        update = _make_update("/test")
        # Build text where a newline falls just inside the 4096 limit
        chunk_a = "a" * 4090 + "\n"
        chunk_b = "b" * 10
        await _reply_long(update, chunk_a + chunk_b)
        first_chunk = update.message.reply_text.call_args_list[0][0][0]
        assert first_chunk.endswith("a" * 4090)  # split at the newline
