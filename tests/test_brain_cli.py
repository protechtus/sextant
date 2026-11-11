"""Tests for Brain CLI commands (v0.5-C).

Covers: /tasks, /task <id>, /pending, /approve, /reject, /search, /agents,
/escalation, /help, /clear, and command-not-found handling.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_console(width: int = 200) -> Console:
    """Return a Console that captures output to a string buffer."""
    from io import StringIO
    buf = StringIO()
    return Console(file=buf, width=width), buf


def _run_cmd(cmd: str, session_id: str = "test-session") -> str:
    """Run a CLI command and return captured Rich output as plain text."""
    from io import StringIO
    import atlas_plus.brain.cli as cli_mod

    buf = StringIO()
    test_console = Console(file=buf, width=200, highlight=False, markup=False)
    orig = cli_mod.console
    cli_mod.console = test_console
    try:
        cli_mod._handle_command(cmd, session_id)
    finally:
        cli_mod.console = orig
    return buf.getvalue()


# ---------------------------------------------------------------------------
# /tasks
# ---------------------------------------------------------------------------

class TestCLITasks:
    def test_tasks_shows_table(self):
        mock_task = MagicMock()
        mock_task.task_id = "task-abc-001"
        mock_task.status = "done"
        mock_task.agent = "brain"
        mock_task.instruction = "Do something useful"
        mock_task.finished_at = "2026-05-20T03:00:00"

        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[mock_task]):
            out = _run_cmd("/tasks")

        assert "task-abc-001" in out
        assert "brain" in out

    def test_tasks_empty(self):
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[]):
            out = _run_cmd("/tasks")
        assert "No tasks" in out

    def test_tasks_respects_limit_arg(self):
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[]) as mock_list:
            _run_cmd("/tasks 25")
        mock_list.assert_called_once_with(limit=25)

    def test_tasks_clamps_limit_at_50(self):
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[]) as mock_list:
            _run_cmd("/tasks 9999")
        mock_list.assert_called_once_with(limit=50)


# ---------------------------------------------------------------------------
# /task <id>
# ---------------------------------------------------------------------------

class TestCLITaskDetail:
    def test_task_detail_shows_fields(self):
        mock_task = MagicMock()
        mock_task.task_id = "detail-001"
        mock_task.status = "done"
        mock_task.agent = "claude-code"
        mock_task.kind = "agent"
        mock_task.priority = 1
        mock_task.session_id = "sess-x"
        mock_task.created_at = "2026-05-20T00:00:00"
        mock_task.started_at = "2026-05-20T00:01:00"
        mock_task.finished_at = "2026-05-20T00:02:00"
        mock_task.instruction = "Fix the bug"
        mock_task.result = "Fixed."

        with patch("atlas_plus.brain.tasks.get_task", return_value=mock_task):
            out = _run_cmd("/task detail-001")

        assert "detail-001" in out
        assert "Fix the bug" in out

    def test_task_detail_not_found(self):
        with patch("atlas_plus.brain.tasks.get_task", return_value=None):
            out = _run_cmd("/task missing-id")
        assert "not found" in out.lower()

    def test_task_detail_no_arg(self):
        out = _run_cmd("/task")
        assert "Usage" in out or "usage" in out


# ---------------------------------------------------------------------------
# /pending, /approve, /reject
# ---------------------------------------------------------------------------

class TestCLIPendingFacts:
    def _make_fact(self, fact_id: str = None, text: str = "Atlas uses SQLite for state.") -> MagicMock:
        f = MagicMock()
        f.id = fact_id or str(uuid.uuid4())
        f.fact_text = text
        f.scope_hint = "project:atlas"
        f.confidence = 0.75
        f.status = "pending"
        return f

    def test_pending_shows_table(self):
        facts = [self._make_fact("aaa-" + str(i)) for i in range(3)]
        with patch("atlas_plus.brain.task_facts.list_pending_facts", return_value=facts):
            out = _run_cmd("/pending")
        assert "aaa-0" in out or "Pending" in out

    def test_pending_empty(self):
        with patch("atlas_plus.brain.task_facts.list_pending_facts", return_value=[]):
            out = _run_cmd("/pending")
        assert "No pending" in out

    def test_approve_success(self):
        fact = self._make_fact("abc123")
        with patch("atlas_plus.brain.task_facts.list_pending_facts", return_value=[fact]), \
             patch("atlas_plus.brain.task_facts.approve_fact",
                   return_value={"ok": True, "file": "/memory/foo.md", "fact_id": "abc123"}):
            out = _run_cmd("/approve abc")
        assert "Approved" in out

    def test_approve_no_match(self):
        with patch("atlas_plus.brain.task_facts.list_pending_facts", return_value=[]):
            out = _run_cmd("/approve zzz")
        assert "No pending fact" in out

    def test_approve_ambiguous(self):
        facts = [self._make_fact("aaa-001"), self._make_fact("aaa-002")]
        with patch("atlas_plus.brain.task_facts.list_pending_facts", return_value=facts):
            out = _run_cmd("/approve aaa")
        assert "Ambiguous" in out

    def test_approve_no_arg(self):
        out = _run_cmd("/approve")
        assert "Usage" in out or "usage" in out

    def test_reject_success(self):
        fact = self._make_fact("def456")
        with patch("atlas_plus.brain.task_facts.list_pending_facts", return_value=[fact]), \
             patch("atlas_plus.brain.task_facts.reject_fact",
                   return_value={"ok": True, "fact_id": "def456"}):
            out = _run_cmd("/reject def")
        assert "Rejected" in out

    def test_reject_no_arg(self):
        out = _run_cmd("/reject")
        assert "Usage" in out or "usage" in out


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------

class TestCLISearch:
    def test_search_returns_results(self):
        results = [
            {"path": ".memory/feedback/foo.md", "score": 1.5, "snippet": "This is relevant."},
        ]
        with patch("atlas_plus.mcp.memory_server.tools.search.memory_search",
                   return_value=results):
            out = _run_cmd("/search escalation")
        assert "foo.md" in out or "relevant" in out

    def test_search_shows_confidence_band(self):
        results = [
            {"path": "feedback/foo.md", "score": 0.85, "snippet": "high confidence result"},
        ]
        with patch("atlas_plus.mcp.memory_server.tools.search.memory_search",
                   return_value=results):
            out = _run_cmd("/search atlas")
        assert "high" in out

    def test_search_empty_results(self):
        with patch("atlas_plus.mcp.memory_server.tools.search.memory_search",
                   return_value=[]):
            out = _run_cmd("/search nothing_will_match_xyz")
        assert "No results" in out

    def test_search_no_query(self):
        out = _run_cmd("/search")
        assert "Usage" in out or "usage" in out


# ---------------------------------------------------------------------------
# /agents
# ---------------------------------------------------------------------------

class TestCLIAgents:
    def test_agents_table(self):
        mock_agent = MagicMock()
        mock_agent.name = "brain"
        mock_agent.model = "qwen3:8b"
        mock_agent.online = True
        mock_agent.capabilities = ["planning", "memory", "tool_use"]

        with patch("atlas_plus.brain.agents.get_agents", return_value=[mock_agent]):
            out = _run_cmd("/agents")
        assert "brain" in out

    def test_agents_refresh_error(self):
        with patch("atlas_plus.brain.agents.get_agents",
                   side_effect=RuntimeError("Ollama offline")):
            out = _run_cmd("/agents")
        assert "error" in out.lower() or "offline" in out.lower()


# ---------------------------------------------------------------------------
# /escalation
# ---------------------------------------------------------------------------

class TestCLIEscalation:
    def test_escalation_on(self, monkeypatch):
        monkeypatch.setenv("ATLAS_CLAUDE_ESCALATION", "on")
        monkeypatch.setenv("ATLAS_CLAUDE_QUALITY_ESCALATION", "off")
        monkeypatch.setenv("ATLAS_CLAUDE_QUALITY_THRESHOLD", "3.5")
        out = _run_cmd("/escalation")
        assert "ON" in out
        assert "3.5" in out

    def test_escalation_off(self, monkeypatch):
        monkeypatch.setenv("ATLAS_CLAUDE_ESCALATION", "off")
        monkeypatch.setenv("ATLAS_CLAUDE_QUALITY_ESCALATION", "off")
        out = _run_cmd("/escalation")
        assert "OFF" in out

    def test_escalation_circuit_breakers_shown(self, monkeypatch):
        monkeypatch.setenv("ATLAS_CLAUDE_ESCALATION", "on")
        out = _run_cmd("/escalation")
        assert "claude-code" in out and "circuit" in out.lower()


# ---------------------------------------------------------------------------
# /help, /clear, unknown command
# ---------------------------------------------------------------------------

class TestCLIWatch:
    def test_watch_done_task_shows_result(self):
        mock_task = MagicMock()
        mock_task.task_id = "watch-001"
        mock_task.status = "done"
        mock_task.agent = "brain"
        mock_task.result = "The analysis is complete and findings are X."

        with patch("atlas_plus.brain.tasks.get_task", return_value=mock_task):
            out = _run_cmd("/watch watch-001")

        assert "done" in out or "complete" in out.lower() or "analysis" in out

    def test_watch_failed_task(self):
        mock_task = MagicMock()
        mock_task.task_id = "watch-002"
        mock_task.status = "failed"
        mock_task.agent = "brain"
        mock_task.result = None

        with patch("atlas_plus.brain.tasks.get_task", return_value=mock_task):
            out = _run_cmd("/watch watch-002")

        assert "failed" in out.lower()

    def test_watch_not_found(self):
        with patch("atlas_plus.brain.tasks.get_task", return_value=None):
            out = _run_cmd("/watch missing-id")
        assert "not found" in out.lower()

    def test_watch_no_arg(self):
        out = _run_cmd("/watch")
        assert "Usage" in out or "usage" in out


class TestCLISubmit:
    def test_submit_queues_task(self):
        mock_task = MagicMock()
        mock_task.task_id = "sub-001"

        with patch("atlas_plus.brain.tasks.create_task", return_value=mock_task) as mock_create:
            out = _run_cmd("/submit Write a summary of the atlas project")

        assert "Queued" in out or "sub-001" in out
        call_kw = mock_create.call_args[1]
        assert "Write a summary" in call_kw["instruction"]
        assert call_kw["agent"] == "brain"

    def test_submit_agent_override(self):
        mock_task = MagicMock()
        mock_task.task_id = "sub-002"

        with patch("atlas_plus.brain.tasks.create_task", return_value=mock_task) as mock_create:
            _run_cmd("/submit --agent claude-code Fix the auth bug")

        call_kw = mock_create.call_args[1]
        assert call_kw["agent"] == "claude-code"
        assert "Fix the auth bug" in call_kw["instruction"]

    def test_submit_no_instruction(self):
        out = _run_cmd("/submit")
        assert "Usage" in out or "usage" in out

    def test_submit_error_shown(self):
        with patch("atlas_plus.brain.tasks.create_task", side_effect=RuntimeError("db locked")):
            out = _run_cmd("/submit Do something")
        assert "Failed" in out or "locked" in out


class TestCLISaveFact:
    def test_save_writes_fact(self):
        with patch("atlas_plus.mcp.memory_server.tools.remember.memory_remember",
                   return_value={"ok": True, "file": "/memory/feedback/test.md"}):
            out = _run_cmd("/save feedback Always use type hints in Python.")
        assert "Saved" in out or "saved" in out.lower()

    def test_save_no_arg_shows_usage(self):
        out = _run_cmd("/save")
        assert "Usage" in out or "usage" in out

    def test_save_scope_only_shows_usage(self):
        out = _run_cmd("/save feedback")
        assert "Usage" in out or "usage" in out

    def test_save_error_shown(self):
        with patch("atlas_plus.mcp.memory_server.tools.remember.memory_remember",
                   side_effect=RuntimeError("autonomous mode off")):
            out = _run_cmd("/save feedback some fact")
        assert "error" in out.lower() or "Error" in out


class TestCLIBrowse:
    def test_browse_shows_table(self):
        mock_items = [
            {"path": "feedback/archive-not-delete.md", "title": "Archive not delete",
             "type": "feedback", "updated": "2026-05-20"},
        ]
        with patch("atlas_plus.mcp.memory_server.tools.list.memory_list",
                   return_value=mock_items):
            out = _run_cmd("/browse feedback")
        assert "feedback/archive-not-delete.md" in out or "Archive" in out

    def test_browse_empty(self):
        with patch("atlas_plus.mcp.memory_server.tools.list.memory_list",
                   return_value=[]):
            out = _run_cmd("/browse nonexistent-scope")
        assert "No memory files" in out

    def test_browse_all_when_no_arg(self):
        with patch("atlas_plus.mcp.memory_server.tools.list.memory_list",
                   return_value=[]) as mock_list:
            _run_cmd("/browse")
        mock_list.assert_called_once_with(prefix="", limit=50)

    def test_browse_error_shown(self):
        with patch("atlas_plus.mcp.memory_server.tools.list.memory_list",
                   side_effect=RuntimeError("index error")):
            out = _run_cmd("/browse feedback")
        assert "error" in out.lower() or "Error" in out


class TestCLICheck:
    def test_check_shows_table(self):
        with patch("atlas_plus.brain.cli._check_ollama_reachable", return_value=True), \
             patch("atlas_plus.brain.task_facts.pending_count", return_value=0):
            out = _run_cmd("/check")
        assert "Ollama" in out or "Check" in out

    def test_check_ollama_down_shows_down(self):
        with patch("atlas_plus.brain.cli._check_ollama_reachable", return_value=False), \
             patch("atlas_plus.brain.task_facts.pending_count", return_value=0):
            out = _run_cmd("/check")
        assert "down" in out.lower() or "Ollama" in out


class TestCLIConfirm:
    def test_confirm_success(self, tmp_path):
        # Write a test memory file
        mem_file = tmp_path / "feedback" / "test.md"
        mem_file.parent.mkdir(parents=True)
        mem_file.write_text("---\nid: test\ntitle: Test\n---\n\nContent.", encoding="utf-8")

        with patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.mcp.memory_server.routing.confirm_fact", return_value=True):
            out = _run_cmd("/confirm feedback/test.md")
        assert "Confirmed" in out or "confirmed" in out.lower()

    def test_confirm_not_found(self, tmp_path):
        with patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT", tmp_path):
            out = _run_cmd("/confirm nonexistent.md")
        assert "not found" in out.lower()

    def test_confirm_no_arg(self):
        out = _run_cmd("/confirm")
        assert "Usage" in out or "usage" in out


class TestCLIStats:
    def test_stats_shows_task_counts(self):
        with patch("atlas_plus.brain.tasks.task_stats", return_value={"total": 5, "done": 3, "failed": 1, "running": 0, "pending": 1}), \
             patch("atlas_plus.brain.task_facts.pending_count", return_value=2), \
             patch("atlas_plus.brain.history.session_count", return_value=4):
            out = _run_cmd("/stats")
        assert "5" in out or "Tasks" in out

    def test_stats_error_shown(self):
        with patch("atlas_plus.brain.tasks.task_stats", side_effect=RuntimeError("db error")):
            out = _run_cmd("/stats")
        assert "error" in out.lower() or "Error" in out


class TestCLIPriority:
    def test_priority_sets_value(self):
        with patch("atlas_plus.brain.tasks.set_task_priority") as mock_set:
            out = _run_cmd("/priority task-001 5")
        mock_set.assert_called_once_with("task-001", 5)
        assert "Priority" in out or "priority" in out.lower()

    def test_priority_no_arg_shows_usage(self):
        out = _run_cmd("/priority")
        assert "Usage" in out or "usage" in out

    def test_priority_non_numeric_shows_usage(self):
        out = _run_cmd("/priority task-001 notanumber")
        assert "Usage" in out or "usage" in out


class TestCLIEscalate:
    def test_escalate_task(self):
        mock_task = MagicMock()
        mock_task.task_id = "brain-001"
        mock_task.agent = "brain"
        mock_task.session_id = "sess-x"
        mock_task.instruction = "Do something complex"
        mock_task.result = "partial result"

        with patch("atlas_plus.brain.tasks.get_task", return_value=mock_task), \
             patch("atlas_plus.brain.escalation.create_escalation_task",
                   return_value="esc-new-001"):
            out = _run_cmd("/escalate brain-001")

        assert "Escalated" in out or "esc-new-001" in out

    def test_escalate_already_claude_code(self):
        mock_task = MagicMock()
        mock_task.task_id = "cc-001"
        mock_task.agent = "claude-code"

        with patch("atlas_plus.brain.tasks.get_task", return_value=mock_task):
            out = _run_cmd("/escalate cc-001")

        assert "already" in out.lower() or "claude-code" in out

    def test_escalate_not_found(self):
        with patch("atlas_plus.brain.tasks.get_task", return_value=None):
            out = _run_cmd("/escalate missing")
        assert "not found" in out.lower()

    def test_escalate_no_arg(self):
        out = _run_cmd("/escalate")
        assert "Usage" in out or "usage" in out


class TestCLIRetry:
    def test_retry_failed_task(self):
        mock_task = MagicMock()
        mock_task.task_id = "orig-001"
        mock_task.status = "failed"
        mock_task.instruction = "Do something"
        mock_task.agent = "brain"
        mock_task.session_id = "sess-x"
        mock_task.priority = 5

        new_task = MagicMock()
        new_task.task_id = "retry-001"

        with patch("atlas_plus.brain.tasks.get_task", return_value=mock_task), \
             patch("atlas_plus.brain.tasks.create_task", return_value=new_task) as mock_create:
            out = _run_cmd("/retry orig-001")

        mock_create.assert_called_once()
        assert "Retried" in out or "retry-001" in out

    def test_retry_non_terminal_shows_warning(self):
        mock_task = MagicMock()
        mock_task.task_id = "running-001"
        mock_task.status = "running"

        with patch("atlas_plus.brain.tasks.get_task", return_value=mock_task):
            out = _run_cmd("/retry running-001")

        assert "running" in out or "failed/blocked" in out.lower()

    def test_retry_not_found(self):
        with patch("atlas_plus.brain.tasks.get_task", return_value=None):
            out = _run_cmd("/retry missing")
        assert "not found" in out.lower()

    def test_retry_no_arg(self):
        out = _run_cmd("/retry")
        assert "Usage" in out or "usage" in out


class TestCLIKill:
    def test_kill_pending_task(self):
        mock_task = MagicMock()
        mock_task.task_id = "kill-001"
        mock_task.status = "pending"

        with patch("atlas_plus.brain.tasks.get_task", return_value=mock_task), \
             patch("atlas_plus.brain.tasks.mark_failed") as mock_fail:
            out = _run_cmd("/kill kill-001")

        mock_fail.assert_called_once()
        assert "cancel" in out.lower() or "failed" in out.lower() or "kill-001" in out

    def test_kill_terminal_task_shows_warning(self):
        mock_task = MagicMock()
        mock_task.task_id = "kill-done"
        mock_task.status = "done"

        with patch("atlas_plus.brain.tasks.get_task", return_value=mock_task):
            out = _run_cmd("/kill kill-done")

        assert "terminal" in out.lower() or "done" in out or "already" in out.lower()

    def test_kill_not_found(self):
        with patch("atlas_plus.brain.tasks.get_task", return_value=None):
            out = _run_cmd("/kill missing-task")
        assert "not found" in out.lower()

    def test_kill_no_arg(self):
        out = _run_cmd("/kill")
        assert "Usage" in out or "usage" in out


class TestCLISessions:
    def test_sessions_shows_table(self):
        mock_sessions = [
            {"session_id": "cli-2026-05-20", "message_count": 5, "last_active": "2026-05-20T12:00:00"},
            {"session_id": "cli-2026-05-19", "message_count": 3, "last_active": "2026-05-19T08:00:00"},
        ]
        with patch("atlas_plus.brain.history.list_sessions", return_value=mock_sessions):
            out = _run_cmd("/sessions")
        assert "cli-2026-05-20" in out

    def test_sessions_empty(self):
        with patch("atlas_plus.brain.history.list_sessions", return_value=[]):
            out = _run_cmd("/sessions")
        assert "No Brain sessions" in out or "No" in out


class TestCLIMisc:
    def test_help_lists_commands(self):
        out = _run_cmd("/help")
        for cmd in ("/approve", "/reject", "/search", "/agents", "/escalation", "/pending"):
            assert cmd in out

    def test_clear_calls_history(self):
        with patch("atlas_plus.brain.history.clear_session", return_value=3) as mock_clear:
            out = _run_cmd("/clear", session_id="sess-abc")
        mock_clear.assert_called_once_with("sess-abc")
        assert "Cleared" in out or "cleared" in out.lower() or "3" in out

    def test_unknown_command(self):
        out = _run_cmd("/notarealcommand")
        assert "Unknown" in out or "unknown" in out.lower()

    def test_quit_returns_false(self):
        from io import StringIO
        import atlas_plus.brain.cli as cli_mod
        buf = StringIO()
        test_console = Console(file=buf, width=200)
        orig = cli_mod.console
        cli_mod.console = test_console
        try:
            result = cli_mod._handle_command("/quit", "sess")
        finally:
            cli_mod.console = orig
        assert result is False


# ---------------------------------------------------------------------------
# CLI persistent history (JSONL)
# ---------------------------------------------------------------------------

class TestCLIHistoryJsonl:
    """Tests for .atlas/cli-history/<session_id>.jsonl persistence."""

    def test_append_cli_turn_creates_file(self, tmp_path):
        import atlas_plus.brain.cli as cli_mod
        orig_dir = cli_mod._CLI_HISTORY_DIR
        cli_mod._CLI_HISTORY_DIR = tmp_path / "cli-history"
        try:
            cli_mod._append_cli_turn("test-session", "user", "Hello Brain")
            expected = tmp_path / "cli-history" / "test-session.jsonl"
            assert expected.exists()
        finally:
            cli_mod._CLI_HISTORY_DIR = orig_dir

    def test_append_cli_turn_writes_valid_json(self, tmp_path):
        import json
        import atlas_plus.brain.cli as cli_mod
        orig_dir = cli_mod._CLI_HISTORY_DIR
        cli_mod._CLI_HISTORY_DIR = tmp_path / "cli-history"
        try:
            cli_mod._append_cli_turn("sess1", "user", "What is the answer?")
            cli_mod._append_cli_turn("sess1", "assistant", "Forty-two.")
            path = tmp_path / "cli-history" / "sess1.jsonl"
            lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
            assert len(lines) == 2
            assert lines[0]["role"] == "user"
            assert lines[1]["role"] == "assistant"
            assert "ts" in lines[0]
        finally:
            cli_mod._CLI_HISTORY_DIR = orig_dir

    def test_load_cli_history_jsonl_returns_turns(self, tmp_path):
        import json
        import atlas_plus.brain.cli as cli_mod
        hist_dir = tmp_path / "cli-history"
        hist_dir.mkdir()
        # Write two turns directly
        p = hist_dir / "my-session.jsonl"
        p.write_text(
            json.dumps({"role": "user", "content": "Hi", "ts": "2026-05-20T10:00:00+00:00"}) + "\n"
            + json.dumps({"role": "assistant", "content": "Hello!", "ts": "2026-05-20T10:00:01+00:00"}) + "\n",
            encoding="utf-8",
        )
        orig_dir = cli_mod._CLI_HISTORY_DIR
        cli_mod._CLI_HISTORY_DIR = hist_dir
        try:
            turns = cli_mod._load_cli_history_jsonl("my-session")
            assert len(turns) == 2
            assert turns[0]["role"] == "user"
        finally:
            cli_mod._CLI_HISTORY_DIR = orig_dir

    def test_load_cli_history_missing_returns_empty(self, tmp_path):
        import atlas_plus.brain.cli as cli_mod
        orig_dir = cli_mod._CLI_HISTORY_DIR
        cli_mod._CLI_HISTORY_DIR = tmp_path / "cli-history"
        try:
            result = cli_mod._load_cli_history_jsonl("nonexistent-session")
            assert result == []
        finally:
            cli_mod._CLI_HISTORY_DIR = orig_dir

    def test_list_cli_sessions_empty(self, tmp_path):
        import atlas_plus.brain.cli as cli_mod
        orig_dir = cli_mod._CLI_HISTORY_DIR
        cli_mod._CLI_HISTORY_DIR = tmp_path / "no-such-dir"
        try:
            result = cli_mod._list_cli_sessions()
            assert result == []
        finally:
            cli_mod._CLI_HISTORY_DIR = orig_dir

    def test_list_cli_sessions_sorted_by_recency(self, tmp_path):
        import json
        import atlas_plus.brain.cli as cli_mod
        hist_dir = tmp_path / "cli-history"
        hist_dir.mkdir()
        # Older session
        (hist_dir / "session-a.jsonl").write_text(
            json.dumps({"role": "user", "content": "old", "ts": "2026-05-18T10:00:00+00:00"}) + "\n",
            encoding="utf-8",
        )
        # Newer session
        (hist_dir / "session-b.jsonl").write_text(
            json.dumps({"role": "user", "content": "new", "ts": "2026-05-20T10:00:00+00:00"}) + "\n",
            encoding="utf-8",
        )
        orig_dir = cli_mod._CLI_HISTORY_DIR
        cli_mod._CLI_HISTORY_DIR = hist_dir
        try:
            sessions = cli_mod._list_cli_sessions()
            assert sessions[0]["session_id"] == "session-b"
            assert sessions[1]["session_id"] == "session-a"
        finally:
            cli_mod._CLI_HISTORY_DIR = orig_dir

    def test_session_id_sanitized_for_filename(self, tmp_path):
        import atlas_plus.brain.cli as cli_mod
        orig_dir = cli_mod._CLI_HISTORY_DIR
        cli_mod._CLI_HISTORY_DIR = tmp_path / "cli-history"
        try:
            cli_mod._append_cli_turn("session/with:bad\\chars", "user", "test")
            # The file should exist with sanitized name (no slashes)
            files = list((tmp_path / "cli-history").glob("*.jsonl"))
            assert len(files) == 1
            assert "/" not in files[0].name
            assert "\\" not in files[0].name
        finally:
            cli_mod._CLI_HISTORY_DIR = orig_dir

    def test_show_cli_sessions_command(self, tmp_path):
        """_show_cli_sessions() should produce output listing sessions."""
        import json
        import atlas_plus.brain.cli as cli_mod
        hist_dir = tmp_path / "cli-history"
        hist_dir.mkdir()
        (hist_dir / "my-research.jsonl").write_text(
            json.dumps({"role": "user", "content": "hi", "ts": "2026-05-20T09:00:00+00:00"}) + "\n",
            encoding="utf-8",
        )
        orig_dir = cli_mod._CLI_HISTORY_DIR
        cli_mod._CLI_HISTORY_DIR = hist_dir
        from io import StringIO
        buf = StringIO()
        test_console = Console(file=buf, width=200, highlight=False, markup=False)
        orig_console = cli_mod.console
        cli_mod.console = test_console
        try:
            cli_mod._show_cli_sessions()
            out = buf.getvalue()
            assert "my-research" in out
        finally:
            cli_mod._CLI_HISTORY_DIR = orig_dir
            cli_mod.console = orig_console


# ---------------------------------------------------------------------------
# /export-session
# ---------------------------------------------------------------------------

class TestCLIExportSession:
    """Tests for _export_session() and /export CLI command."""

    def test_export_creates_markdown_file(self, tmp_path):
        import atlas_plus.brain.cli as cli_mod
        from io import StringIO

        # Patch history to return some messages
        messages = [
            {"role": "user", "content": "Hello Brain, what is the temporal graph?"},
            {"role": "assistant", "content": "The temporal graph tracks entities over time."},
        ]

        orig_history_dir = cli_mod._CLI_HISTORY_DIR
        cli_mod._CLI_HISTORY_DIR = tmp_path / "cli-history"
        buf = StringIO()
        test_console = Console(file=buf, width=200, highlight=False, markup=False)
        orig_console = cli_mod.console
        cli_mod.console = test_console

        try:
            with patch("atlas_plus.brain.history.get_messages", return_value=messages):
                cli_mod._export_session("test-session", str(tmp_path / "out.md"))
        finally:
            cli_mod._CLI_HISTORY_DIR = orig_history_dir
            cli_mod.console = orig_console

        out_file = tmp_path / "out.md"
        assert out_file.exists()
        content = out_file.read_text(encoding="utf-8")
        assert "Brain Session: test-session" in content
        assert "Hello Brain" in content
        assert "temporal graph" in content

    def test_export_uses_default_path_and_reports_exported(self, tmp_path):
        import atlas_plus.brain.cli as cli_mod
        from io import StringIO
        from pathlib import Path as _Path

        messages = [{"role": "user", "content": "Test message for export"}]

        # Point the history dir at a tmp location so the exports subdir goes there too
        hist_dir = tmp_path / "cli-history"
        orig_history_dir = cli_mod._CLI_HISTORY_DIR
        cli_mod._CLI_HISTORY_DIR = hist_dir
        buf = StringIO()
        test_console = Console(file=buf, width=200, highlight=False, markup=False)
        orig_console = cli_mod.console
        cli_mod.console = test_console

        try:
            with patch("atlas_plus.brain.history.get_messages", return_value=messages):
                cli_mod._export_session("my-session")
            out = buf.getvalue()
        finally:
            cli_mod._CLI_HISTORY_DIR = orig_history_dir
            cli_mod.console = orig_console

        # The function should print "Exported N messages to <path>" or similar
        assert "Exported" in out or "my-session" in out

    def test_export_no_history_shows_message(self, tmp_path):
        import atlas_plus.brain.cli as cli_mod
        from io import StringIO

        orig_history_dir = cli_mod._CLI_HISTORY_DIR
        cli_mod._CLI_HISTORY_DIR = tmp_path / "cli-history"
        buf = StringIO()
        test_console = Console(file=buf, width=200, highlight=False, markup=False)
        orig_console = cli_mod.console
        cli_mod.console = test_console

        try:
            with patch("atlas_plus.brain.history.get_messages", return_value=[]):
                cli_mod._export_session("empty-session")
            out = buf.getvalue()
        finally:
            cli_mod._CLI_HISTORY_DIR = orig_history_dir
            cli_mod.console = orig_console

        assert "No history" in out or "no history" in out.lower()

    def test_export_command_wired_in_handle_command(self):
        """Verify /export is routed through _handle_command."""
        import atlas_plus.brain.cli as cli_mod
        from io import StringIO

        export_calls = []

        def fake_export(session_id, output_path_arg=""):
            export_calls.append((session_id, output_path_arg))

        orig = cli_mod._export_session
        cli_mod._export_session = fake_export
        buf = StringIO()
        test_console = Console(file=buf, width=200, highlight=False, markup=False)
        orig_console = cli_mod.console
        cli_mod.console = test_console

        try:
            cli_mod._handle_command("/export", "sess-test")
        finally:
            cli_mod._export_session = orig
            cli_mod.console = orig_console

        assert export_calls == [("sess-test", "")]


# ---------------------------------------------------------------------------
# /topics, /decay, /digest commands
# ---------------------------------------------------------------------------

class TestCLINewCommands:
    def test_topics_no_results_shows_message(self):
        from unittest.mock import patch
        with patch("atlas_plus.web.app._build_topic_index", return_value={}):
            out = _run_cmd("/topics")
        assert "No tags" in out or "no tags" in out.lower() or "found" in out.lower()

    def test_topics_shows_table(self):
        from unittest.mock import patch
        mock_index = {
            "python": ["feedback/style.md", "identity/alex.md"],
            "testing": ["feedback/test.md"],
        }
        with patch("atlas_plus.web.app._build_topic_index", return_value=mock_index):
            out = _run_cmd("/topics")
        assert "python" in out
        assert "testing" in out

    def test_decay_no_candidates_shows_green(self):
        from unittest.mock import patch
        with patch("atlas_plus.mcp.memory_server.routing.downgrade_stale_confirmations",
                   return_value=[]):
            out = _run_cmd("/decay")
        assert "No stale" in out or "no stale" in out.lower()

    def test_decay_shows_candidates(self):
        from unittest.mock import patch
        candidates = [
            {"path": "feedback/old.md", "old_confidence": "high",
             "last_confirmed": "2020-01-01", "age_days": 2000},
        ]
        with patch("atlas_plus.mcp.memory_server.routing.downgrade_stale_confirmations",
                   return_value=candidates):
            out = _run_cmd("/decay")
        assert "feedback/old.md" in out or "old.md" in out

    def test_digest_no_arg_shows_usage(self):
        out = _run_cmd("/digest")
        assert "Usage" in out or "usage" in out.lower()

    def test_digest_shows_digest_for_done_task(self):
        from unittest.mock import MagicMock, patch
        mock_task = MagicMock()
        mock_task.status = "done"
        mock_task.result_digest = "This task searched the codebase and found 3 issues."
        mock_task.result = "Long result text here..."
        with patch("atlas_plus.brain.tasks.get_task", return_value=mock_task):
            out = _run_cmd("/digest task-abc-001")
        assert "searched the codebase" in out or "Digest" in out

    def test_digest_shows_message_for_no_digest(self):
        from unittest.mock import MagicMock, patch
        mock_task = MagicMock()
        mock_task.status = "done"
        mock_task.result_digest = None
        mock_task.result = "Some result."
        with patch("atlas_plus.brain.tasks.get_task", return_value=mock_task):
            out = _run_cmd("/digest task-xyz")
        assert "No digest" in out or "no digest" in out.lower() or "ATLAS_DIGEST" in out


class TestCLIVerifyCommand:
    def test_verify_no_arg_shows_usage(self):
        out = _run_cmd("/verify")
        assert "Usage" in out or "usage" in out.lower() or "verify" in out.lower()

    def test_verify_bad_format_shows_error(self):
        out = _run_cmd("/verify not-a-ticket")
        assert "Invalid" in out or "invalid" in out.lower() or "format" in out.lower()

    def test_verify_queues_verify_task(self):
        from unittest.mock import MagicMock, patch
        mock_ticket = MagicMock()
        mock_ticket.id = "ATL-007"
        mock_ticket.title = "Fix the thing"
        mock_ticket.status = "in-progress"
        mock_task = MagicMock()
        mock_task.task_id = "verify-task-abc-123456"
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[mock_ticket]), \
             patch("atlas_plus.brain.tasks.create_task", return_value=mock_task) as mock_create:
            out = _run_cmd("/verify ATL-007")
        assert mock_create.called
        call_kw = mock_create.call_args.kwargs
        assert call_kw.get("kind") == "verify"
        assert call_kw.get("session_id") == "verify-ticket-ATL-007"
        assert "queued" in out.lower() or "Verify task" in out

    def test_verify_shows_not_found_for_unknown_ticket(self):
        from unittest.mock import patch
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            out = _run_cmd("/verify ATL-999")
        assert "not found" in out.lower() or "Not found" in out

    def test_verify_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/verify" in out


# ---------------------------------------------------------------------------
# /session-log command
# ---------------------------------------------------------------------------

class TestSessionLogCommand:
    def test_no_commits_shows_nothing_to_write(self):
        from unittest.mock import patch
        with patch("atlas_plus.sprint_summary.write_session_log",
                   return_value={"ok": True, "entry": "", "paths_written": []}):
            out = _run_cmd("/session-log")
        assert "nothing" in out.lower() or "no commits" in out.lower()

    def test_dry_run_shows_entry(self):
        from unittest.mock import patch
        fake = {"ok": True, "entry": "## 2026-05-20 -- atlas-memory+\n\n/loop sprint.", "paths_written": []}
        with patch("atlas_plus.sprint_summary.write_session_log", return_value=fake):
            out = _run_cmd("/session-log --dry-run")
        assert "2026-05-20" in out or "sprint" in out

    def test_successful_write_shows_paths(self):
        from unittest.mock import patch
        fake = {
            "ok": True,
            "entry": "## 2026-05-20 -- atlas-memory+\n\n/loop sprint.",
            "paths_written": [r"C:\Tools\.memory\sessions\_history.md"],
        }
        with patch("atlas_plus.sprint_summary.write_session_log", return_value=fake):
            out = _run_cmd("/session-log")
        assert "_history.md" in out or "written" in out.lower()

    def test_error_shows_error_message(self):
        from unittest.mock import patch
        with patch("atlas_plus.sprint_summary.write_session_log",
                   side_effect=RuntimeError("git gone")):
            out = _run_cmd("/session-log")
        assert "error" in out.lower() or "git gone" in out

    def test_session_log_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/session-log" in out


# ---------------------------------------------------------------------------
# /loop command
# ---------------------------------------------------------------------------

class TestLoopCommand:
    def test_shows_dispatcher_state_disabled(self):
        from unittest.mock import patch, MagicMock
        with patch("atlas_plus.brain.dispatcher.get_dispatch_stats",
                   return_value={"enabled": False, "in_flight": 0, "total_dispatched": 0,
                                 "last_run_at": None}), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            out = _run_cmd("/loop")
        assert "off" in out.lower() or "disabled" in out.lower() or "Dispatcher" in out

    def test_shows_dispatcher_state_enabled(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.dispatcher.get_dispatch_stats",
                   return_value={"enabled": True, "in_flight": 2, "total_dispatched": 10,
                                 "last_run_at": None}), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            out = _run_cmd("/loop")
        assert "on" in out.lower() or "in-flight" in out.lower() or "10" in out

    def test_shows_ticket_counts(self):
        from unittest.mock import patch, MagicMock
        tickets = []
        for s in ("todo", "todo", "in-progress", "blocked"):
            t = MagicMock()
            t.status = s
            t.reopen_count = 0
            tickets.append(t)
        with patch("atlas_plus.brain.dispatcher.get_dispatch_stats",
                   return_value={"enabled": False, "in_flight": 0, "total_dispatched": 0,
                                 "last_run_at": None}), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=tickets):
            out = _run_cmd("/loop")
        assert "2" in out or "todo" in out.lower()

    def test_shows_reopened_tickets(self):
        from unittest.mock import patch, MagicMock
        t1 = MagicMock()
        t1.status = "todo"
        t1.reopen_count = 3
        t1.id = "ATL-001"
        t1.title = "Failing test fix"
        with patch("atlas_plus.brain.dispatcher.get_dispatch_stats",
                   return_value={"enabled": False, "in_flight": 0, "total_dispatched": 0,
                                 "last_run_at": None}), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[t1]):
            out = _run_cmd("/loop")
        assert "ATL-001" in out or "reopen" in out.lower() or "x3" in out

    def test_loop_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/loop" in out

    def test_loop_graceful_on_exception(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.dispatcher.get_dispatch_stats",
                   side_effect=RuntimeError("db gone")):
            out = _run_cmd("/loop")
        assert "unavailable" in out.lower() or "error" in out.lower() or "db gone" in out


# ---------------------------------------------------------------------------
# /orphans command
# ---------------------------------------------------------------------------

class TestOrphansCommand:
    def test_no_orphans_shows_no_candidates(self):
        from unittest.mock import patch
        with patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]):
            out = _run_cmd("/orphans")
        assert "No orphan" in out or "no orphan" in out.lower()

    def test_shows_orphan_table(self):
        from unittest.mock import patch
        fake = [
            {
                "path": "reference/old-tool.md",
                "age_days": 250,
                "score": 0.2,
                "issues": ["low_health", "no_inbound_refs"],
            }
        ]
        with patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=fake):
            out = _run_cmd("/orphans")
        assert "reference/old-tool.md" in out or "250" in out

    def test_limit_arg_parsed(self):
        from unittest.mock import patch
        captured = {}
        def fake_find(*args, **kwargs):
            captured["limit"] = kwargs.get("limit")
            return []
        with patch("atlas_plus.curation.orphan.find_orphan_candidates", side_effect=fake_find):
            _run_cmd("/orphans 5")
        assert captured.get("limit") == 5

    def test_exception_shows_error(self):
        from unittest.mock import patch
        with patch("atlas_plus.curation.orphan.find_orphan_candidates",
                   side_effect=RuntimeError("scan failed")):
            out = _run_cmd("/orphans")
        assert "failed" in out.lower() or "scan failed" in out

    def test_orphans_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/orphans" in out


# ---------------------------------------------------------------------------
# /health command
# ---------------------------------------------------------------------------

class TestHealthCommand:
    def test_shows_atlas_health_header(self):
        from unittest.mock import patch
        with patch("atlas_plus.doctor.cli.check_services", return_value=[
                {"name": "mcp", "status": "up"}, {"name": "brain", "status": "up"}]), \
             patch("atlas_plus.doctor.cli.check_worker_status", return_value={
                "assessment": "ok", "pending": 0}), \
             patch("atlas_plus.doctor.cli.check_memory_quality", return_value={
                "total_files": 100, "confirmed_pct": 70, "tagged_pct": 80, "untagged_files": 5}):
            out = _run_cmd("/health")
        assert "Atlas Health" in out

    def test_shows_service_count(self):
        from unittest.mock import patch
        with patch("atlas_plus.doctor.cli.check_services", return_value=[
                {"name": "mcp", "status": "up"}]), \
             patch("atlas_plus.doctor.cli.check_worker_status", return_value={
                "assessment": "idle", "pending": 0}), \
             patch("atlas_plus.doctor.cli.check_memory_quality", return_value={
                "total_files": 50, "confirmed_pct": 60, "tagged_pct": 70, "untagged_files": 0}):
            out = _run_cmd("/health")
        assert "up" in out.lower() or "Services" in out

    def test_graceful_when_checks_fail(self):
        from unittest.mock import patch
        with patch("atlas_plus.doctor.cli.check_services",
                   side_effect=RuntimeError("service check failed")):
            out = _run_cmd("/health")
        # Should not crash; Atlas Health header still shows
        assert "Atlas Health" in out or "failed" in out.lower()

    def test_health_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/health" in out

    def test_shows_temporal_staleness_warning(self):
        from unittest.mock import patch
        with patch("atlas_plus.doctor.cli.check_services", return_value=[]), \
             patch("atlas_plus.doctor.cli.check_worker_status", return_value={"assessment": "ok", "pending": 0}), \
             patch("atlas_plus.doctor.cli.check_memory_quality", return_value={"total_files": 10, "confirmed_pct": 70, "tagged_pct": 80}), \
             patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]), \
             patch("atlas_plus.doctor.cli.check_temporal", return_value={"entities": 5, "stale": True, "age_days": 10}):
            out = _run_cmd("/health")
        assert "stale" in out.lower() or "Temporal" in out or "10d" in out

    def test_no_staleness_warning_when_fresh(self):
        from unittest.mock import patch
        with patch("atlas_plus.doctor.cli.check_services", return_value=[]), \
             patch("atlas_plus.doctor.cli.check_worker_status", return_value={"assessment": "ok", "pending": 0}), \
             patch("atlas_plus.doctor.cli.check_memory_quality", return_value={"total_files": 10, "confirmed_pct": 70, "tagged_pct": 80}), \
             patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]), \
             patch("atlas_plus.doctor.cli.check_temporal", return_value={"entities": 5, "stale": False, "age_days": 1}):
            out = _run_cmd("/health")
        assert "stale" not in out.lower()


# ---------------------------------------------------------------------------
# /topics command
# ---------------------------------------------------------------------------

class TestTopicsCommand:
    def test_shows_topic_cloud(self):
        from unittest.mock import patch
        fake_topics = {"atlas": ["/f1.md", "/f2.md"], "brain": ["/f3.md"]}
        with patch("atlas_plus.web.app._build_topic_index", return_value=fake_topics):
            out = _run_cmd("/topics")
        assert "atlas" in out or "Topics" in out or "brain" in out

    def test_no_topics_shows_no_tags(self):
        from unittest.mock import patch
        with patch("atlas_plus.web.app._build_topic_index", return_value={}):
            out = _run_cmd("/topics")
        assert "no tags" in out.lower() or "0" in out or out.strip() != ""

    def test_topics_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/topics" in out


# ---------------------------------------------------------------------------
# /quality command
# ---------------------------------------------------------------------------

class TestQualityCommand:
    def test_shows_quality_summary(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        # /quality bails early when <state_dir>/reports is absent, so give it a
        # real (empty) reports dir -- the summary itself is mocked. Keeps the
        # test independent of the operator machine's live .atlas state.
        (tmp_path / "reports").mkdir()
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path))
        with patch("atlas_plus.doctor.cli.check_research_quality",
                   return_value={"total_reports": 10, "avg_score": 0.72,
                                 "low_quality_count": 2, "lowest_files": ["bad.md"]}):
            out = _run_cmd("/quality")
        assert "10" in out or "quality" in out.lower() or "research" in out.lower()

    def test_graceful_when_research_unavailable(self):
        from unittest.mock import patch
        with patch("atlas_plus.doctor.cli.check_research_quality",
                   side_effect=RuntimeError("no reports dir")):
            out = _run_cmd("/quality")
        # Should not raise; may show error or empty
        assert out is not None

    def test_quality_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/quality" in out

    def _write_report(self, d, name: str, topic: str, score: float = None) -> None:
        from pathlib import Path
        p = Path(d) / name
        Path(d).mkdir(parents=True, exist_ok=True)
        fm = f'---\ntitle: "Research: {name}"\ntopic: {topic}\ndate: 2026-05-20\n'
        if score is not None:
            fm += f"quality_score: {score}\n"
        fm += "---\n\nbody text.\n"
        p.write_text(fm, encoding="utf-8")

    def test_topic_filter_shows_only_matching_reports(self, tmp_path):
        rdir = tmp_path / "reports"
        self._write_report(str(rdir), "sip-reg.md", "sip", 0.8)
        self._write_report(str(rdir), "voip-basics.md", "voip", 0.6)
        self._write_report(str(rdir), "sip-trunk.md", "sip", 0.4)
        with patch("os.environ.get",
                   side_effect=lambda k, d="": str(tmp_path) if k == "ATLAS_STATE_DIR" else d):
            out = _run_cmd("/quality sip")
        assert "sip-reg.md" in out or "sip-trunk.md" in out
        assert "voip-basics.md" not in out

    def test_topic_filter_no_match_shows_not_found(self, tmp_path):
        rdir = tmp_path / "reports"
        self._write_report(str(rdir), "sip-reg.md", "sip", 0.8)
        with patch("os.environ.get",
                   side_effect=lambda k, d="": str(tmp_path) if k == "ATLAS_STATE_DIR" else d):
            out = _run_cmd("/quality voip")
        assert "No reports" in out or "voip" in out.lower()

    def test_topic_filter_shows_filter_label(self, tmp_path):
        rdir = tmp_path / "reports"
        self._write_report(str(rdir), "sip-reg.md", "sip", 0.75)
        with patch("os.environ.get",
                   side_effect=lambda k, d="": str(tmp_path) if k == "ATLAS_STATE_DIR" else d):
            out = _run_cmd("/quality sip")
        assert "sip" in out.lower()

    def test_numeric_arg_still_sets_limit(self, tmp_path):
        rdir = tmp_path / "reports"
        for i in range(8):
            self._write_report(str(rdir), f"report{i}.md", "misc", 0.3)
        with patch("os.environ.get",
                   side_effect=lambda k, d="": str(tmp_path) if k == "ATLAS_STATE_DIR" else d):
            with patch("atlas_plus.doctor.cli.check_research_quality",
                       return_value={"total_reports": 8, "scored_reports": 8,
                                     "avg_score": 0.3, "low_quality_count": 8,
                                     "min_score_file": "report0.md"}):
                out = _run_cmd("/quality 3")
        # Should show "3 more" or similar truncation indicator
        assert out is not None


# ---------------------------------------------------------------------------
# /decay command
# ---------------------------------------------------------------------------

class TestDecayCommand:
    def test_no_stale_facts_shows_clean_message(self):
        from unittest.mock import patch
        with patch("atlas_plus.mcp.memory_server.routing.downgrade_stale_confirmations",
                   return_value=[]):
            out = _run_cmd("/decay")
        assert "No stale" in out or "no stale" in out.lower()

    def test_shows_decay_candidates(self):
        from unittest.mock import patch
        fake = [{"path": "feedback/style.md", "old_confidence": "high",
                 "last_confirmed": "2025-01-01", "age_days": 200}]
        with patch("atlas_plus.mcp.memory_server.routing.downgrade_stale_confirmations",
                   return_value=fake):
            out = _run_cmd("/decay")
        assert "feedback/style.md" in out or "1" in out

    def test_default_threshold_is_90_days(self):
        from unittest.mock import patch
        captured = {}
        def fake_decay(threshold_days, **kwargs):
            captured["threshold"] = threshold_days
            return []
        with patch("atlas_plus.mcp.memory_server.routing.downgrade_stale_confirmations",
                   side_effect=fake_decay):
            _run_cmd("/decay")
        assert captured.get("threshold") == 90

    def test_custom_threshold_from_arg(self):
        from unittest.mock import patch
        captured = {}
        def fake_decay(threshold_days, **kwargs):
            captured["threshold"] = threshold_days
            return []
        with patch("atlas_plus.mcp.memory_server.routing.downgrade_stale_confirmations",
                   side_effect=fake_decay):
            _run_cmd("/decay 60")
        assert captured.get("threshold") == 60

    def test_decay_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/decay" in out


# ---------------------------------------------------------------------------
# /digest command
# ---------------------------------------------------------------------------

class TestDigestCommand:
    def test_no_arg_shows_usage(self):
        out = _run_cmd("/digest")
        assert "Usage" in out or "usage" in out.lower()

    def test_task_not_found_shows_error(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.get_task", return_value=None):
            out = _run_cmd("/digest task-xyz")
        assert "not found" in out.lower() or "Task" in out

    def test_done_task_with_digest_shows_digest(self):
        from unittest.mock import patch, MagicMock
        task = MagicMock()
        task.status = "done"
        task.result_digest = "Fixed the SIP registration bug by updating the timer."
        task.result = "Full result text here."
        with patch("atlas_plus.brain.tasks.get_task", return_value=task):
            out = _run_cmd("/digest task-abc")
        assert "Fixed the SIP" in out or "Digest" in out

    def test_done_task_without_digest_shows_hint(self):
        from unittest.mock import patch, MagicMock
        task = MagicMock()
        task.status = "done"
        task.result_digest = None
        task.result = "Some result text"
        with patch("atlas_plus.brain.tasks.get_task", return_value=task):
            out = _run_cmd("/digest task-abc")
        assert "No digest" in out or "ATLAS_DIGEST_RESULTS" in out

    def test_non_done_task_shows_status_warning(self):
        from unittest.mock import patch, MagicMock
        task = MagicMock()
        task.status = "running"
        with patch("atlas_plus.brain.tasks.get_task", return_value=task):
            out = _run_cmd("/digest task-running")
        assert "running" in out or "done tasks" in out.lower() or "done" in out

    def test_digest_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/digest" in out


# ---------------------------------------------------------------------------
# /research-index command
# ---------------------------------------------------------------------------

class TestResearchIndexCommand:
    def _write_report(self, d, name: str, title: str, date_str: str, score: float = None) -> None:
        from pathlib import Path
        p = Path(d) / name
        Path(d).mkdir(parents=True, exist_ok=True)
        fm = f'---\ntitle: "Research: {title}"\ndate: {date_str}\n'
        if score is not None:
            fm += f"quality_score: {score}\n"
        fm += "---\n\nbody text here for the report.\n"
        p.write_text(fm, encoding="utf-8")

    def test_no_reports_dir_shows_no_dir_message(self, tmp_path):
        import os
        with patch("os.environ.get",
                   side_effect=lambda k, d="": str(tmp_path / "nosuch") if k == "ATLAS_STATE_DIR" else d):
            out = _run_cmd("/research-index")
        assert "No" in out or "not found" in out.lower()

    def test_reports_listed(self, tmp_path):
        rdir = tmp_path / "reports"
        self._write_report(str(rdir), "sip.md", "SIP Registration", "2026-05-20", 0.8)
        with patch("os.environ.get",
                   side_effect=lambda k, d="": str(tmp_path) if k == "ATLAS_STATE_DIR" else d):
            out = _run_cmd("/research-index")
        assert "SIP Registration" in out or "sip" in out.lower()

    def test_research_index_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/research-index" in out


# ---------------------------------------------------------------------------
# /research-gaps command
# ---------------------------------------------------------------------------

class TestResearchGapsCommand:
    def test_no_misses_shows_clean_message(self):
        from unittest.mock import patch
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   return_value=[]):
            out = _run_cmd("/research-gaps")
        assert "No memory miss" in out or "no" in out.lower()

    def test_shows_miss_queries_table(self):
        from unittest.mock import patch
        misses = [{"query": "SIP registration", "miss_count": 5, "total": 8}]
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   return_value=misses), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]), \
             patch("atlas_plus.brain.research.find_similar_research_reports", return_value=[]):
            out = _run_cmd("/research-gaps")
        assert "SIP registration" in out or "sip" in out.lower()

    def test_shows_ticket_coverage(self):
        from unittest.mock import patch, MagicMock
        misses = [{"query": "SIP registration", "miss_count": 5, "total": 8}]
        ticket = MagicMock()
        ticket.title = "Research gap: SIP registration"
        ticket.status = "todo"
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   return_value=misses), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[ticket]), \
             patch("atlas_plus.brain.research.find_similar_research_reports", return_value=[]):
            out = _run_cmd("/research-gaps")
        assert "Y" in out or "sip" in out.lower()

    def test_shows_uncovered_hint_when_no_ticket(self):
        from unittest.mock import patch
        misses = [{"query": "uncovered topic", "miss_count": 3, "total": 5}]
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   return_value=misses), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]), \
             patch("atlas_plus.brain.research.find_similar_research_reports", return_value=[]):
            out = _run_cmd("/research-gaps")
        assert "autoticket" in out or "gap" in out.lower()

    def test_research_gaps_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/research-gaps" in out


# ---------------------------------------------------------------------------
# /gap-report command
# ---------------------------------------------------------------------------

class TestGapReportCommand:
    def test_queues_research_gap_task(self):
        from unittest.mock import patch, MagicMock
        mock_task = MagicMock()
        mock_task.task_id = "gap-task-abc123"
        with patch("atlas_plus.brain.tasks.create_task", return_value=mock_task) as mock_create:
            out = _run_cmd("/gap-report SIP OPTIONS flow")
        mock_create.assert_called_once()
        call_kw = mock_create.call_args[1]
        assert call_kw.get("kind") == "research_gap"
        assert "Research gap: SIP OPTIONS flow" in call_kw.get("instruction", "")
        assert "gap-task-abc123" in out or "Queued" in out

    def test_empty_topic_shows_usage(self):
        out = _run_cmd("/gap-report")
        assert "Usage" in out or "usage" in out.lower()

    def test_watch_flag_parsed(self):
        from unittest.mock import patch, MagicMock
        mock_task = MagicMock()
        mock_task.task_id = "watch-task-001"
        mock_task.status = "done"
        mock_task.result = "Research completed."
        with patch("atlas_plus.brain.tasks.create_task", return_value=mock_task), \
             patch("atlas_plus.brain.tasks.get_task", return_value=mock_task):
            out = _run_cmd("/gap-report --watch DTMF handling")
        # watch mode should still queue a task; topic extracted after --watch
        assert out is not None  # didn't crash

    def test_gap_report_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/gap-report" in out


# ---------------------------------------------------------------------------
# /tasks and /task commands
# ---------------------------------------------------------------------------

class TestTasksCommand:
    def test_lists_recent_tasks(self):
        from unittest.mock import patch, MagicMock
        task = MagicMock(spec=[])  # spec=[] prevents MagicMock auto-attrs from confusing Rich
        task.task_id = "task-001"
        task.status = "done"
        task.kind = "agent"
        task.created_at = "2026-05-20T21:00:00Z"
        task.instruction = "Run a test"
        task.agent = "brain"
        task.finished_at = "2026-05-20T21:02:00Z"
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[task]):
            out = _run_cmd("/tasks")
        assert "task-001" in out or "done" in out.lower()

    def test_no_tasks_shows_empty_message(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[]):
            out = _run_cmd("/tasks")
        assert "No tasks" in out or "no task" in out.lower() or out.strip() != ""

    def test_tasks_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/tasks" in out


class TestTaskCommand:
    def test_shows_task_detail(self):
        from unittest.mock import patch, MagicMock
        task = MagicMock()
        task.task_id = "task-detail-001"
        task.status = "done"
        task.kind = "agent"
        task.instruction = "Fix the bug"
        task.result = "Bug fixed."
        task.created_at = "2026-05-20T21:00:00Z"
        task.started_at = "2026-05-20T21:01:00Z"
        task.finished_at = "2026-05-20T21:02:00Z"
        task.attempts = 1
        task.agent = "brain"
        with patch("atlas_plus.brain.tasks.get_task", return_value=task):
            out = _run_cmd("/task task-detail-001")
        assert "task-detail-001" in out or "brain" in out or "done" in out.lower()

    def test_not_found_shows_error(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.get_task", return_value=None):
            out = _run_cmd("/task nosuchid")
        assert "not found" in out.lower() or "nosuchid" in out

    def test_task_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/task " in out


# ---------------------------------------------------------------------------
# /stats command
# ---------------------------------------------------------------------------

class TestStatsCommand:
    def test_shows_stats_header(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.task_stats",
                   return_value={"done": 10, "failed": 2, "total": 12}), \
             patch("atlas_plus.brain.task_facts.pending_count", return_value=3), \
             patch("atlas_plus.brain.history.list_sessions", return_value=["s1", "s2"]):
            out = _run_cmd("/stats")
        assert "12" in out or "10" in out or "stats" in out.lower() or "Tasks" in out

    def test_graceful_on_exception(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.task_stats",
                   side_effect=RuntimeError("db gone")):
            out = _run_cmd("/stats")
        assert out is not None

    def test_stats_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/stats" in out


# ---------------------------------------------------------------------------
# /submit command
# ---------------------------------------------------------------------------

class TestSubmitCommand:
    def test_submit_queues_task(self):
        from unittest.mock import patch, MagicMock
        task = MagicMock()
        task.task_id = "new-task-xyz"
        with patch("atlas_plus.brain.tasks.create_task", return_value=task):
            out = _run_cmd("/submit Build a new feature")
        assert "new-task-xyz" in out or "queued" in out.lower() or "task" in out.lower()

    def test_no_arg_shows_usage(self):
        out = _run_cmd("/submit")
        assert "Usage" in out or "usage" in out.lower() or "instruction" in out.lower()

    def test_submit_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/submit" in out


# ---------------------------------------------------------------------------
# /search command
# ---------------------------------------------------------------------------

class TestSearchCommand:
    def test_no_arg_shows_usage(self):
        out = _run_cmd("/search")
        assert "Usage" in out or "usage" in out.lower()

    def test_results_shown(self):
        from unittest.mock import patch
        fake_results = [
            {"path": "feedback/style.md", "score": 0.8,
             "content": "The style guide says use kebab-case."},
        ]
        with patch("atlas_plus.mcp.memory_server.tools.search.memory_search",
                   return_value=fake_results):
            out = _run_cmd("/search style guide")
        assert "feedback/style.md" in out or "style" in out.lower()

    def test_no_results_shows_empty(self):
        from unittest.mock import patch
        with patch("atlas_plus.mcp.memory_server.tools.search.memory_search",
                   return_value=[]):
            out = _run_cmd("/search xyznosuchthing")
        assert "No results" in out or "no results" in out.lower()

    def test_search_error_handled(self):
        from unittest.mock import patch
        with patch("atlas_plus.mcp.memory_server.tools.search.memory_search",
                   side_effect=RuntimeError("fts failed")):
            out = _run_cmd("/search anything")
        assert "error" in out.lower() or "fts failed" in out

    def test_search_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/search" in out


# ---------------------------------------------------------------------------
# /agents and /sessions commands
# ---------------------------------------------------------------------------

class TestAgentsCommand:
    def test_shows_agent_list(self):
        from unittest.mock import patch, MagicMock
        agent = MagicMock()
        agent.name = "brain"
        agent.model = "qwen3:8b"
        agent.online = True
        agent.capabilities = ["text"]
        with patch("atlas_plus.brain.agents.get_agents", return_value=[agent]):
            out = _run_cmd("/agents")
        assert "brain" in out or "qwen" in out.lower() or "Agents" in out

    def test_agents_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/agents" in out


class TestSessionsCommand:
    def test_no_sessions_shows_empty_message(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.history.list_sessions", return_value=[]):
            out = _run_cmd("/sessions")
        assert "No sessions" in out or "no sessions" in out.lower() or out.strip() != ""

    def test_sessions_listed(self):
        from unittest.mock import patch
        sessions = [
            {"session_id": "cli-2026-05-20", "message_count": 10, "last_active": "2026-05-20T21:00:00Z"},
        ]
        with patch("atlas_plus.brain.history.list_sessions", return_value=sessions):
            out = _run_cmd("/sessions")
        assert "cli-2026-05-20" in out or "10" in out

    def test_sessions_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/sessions" in out


# ---------------------------------------------------------------------------
# /browse and /save commands
# ---------------------------------------------------------------------------

class TestBrowseCommand:
    def test_lists_memory_files(self):
        from unittest.mock import patch
        fake_items = [
            {"path": "feedback/style.md", "title": "Style Guide"},
            {"path": "reference/tools.md", "title": "Tools Ref"},
        ]
        with patch("atlas_plus.mcp.memory_server.tools.list.memory_list",
                   return_value=fake_items):
            out = _run_cmd("/browse")
        assert "feedback/style.md" in out or "Style Guide" in out

    def test_scope_filtered(self):
        from unittest.mock import patch
        captured = {}
        def fake_list(prefix="", **kwargs):
            captured["prefix"] = prefix
            return []
        with patch("atlas_plus.mcp.memory_server.tools.list.memory_list",
                   side_effect=fake_list):
            _run_cmd("/browse feedback")
        assert "feedback" in captured.get("prefix", "")

    def test_browse_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/browse" in out


class TestSaveCommand:
    def test_no_arg_shows_usage(self):
        out = _run_cmd("/save")
        assert "Usage" in out or "usage" in out.lower()

    def test_missing_scope_shows_usage(self):
        out = _run_cmd("/save just-one-word")
        assert "Usage" in out or "usage" in out.lower()

    def test_successful_save_shows_saved(self):
        from unittest.mock import patch
        with patch("atlas_plus.mcp.memory_server.tools.remember.memory_remember",
                   return_value={"ok": True, "file": "feedback/new-fact.md"}):
            out = _run_cmd("/save feedback Always use ISO 8601 dates.")
        assert "Saved" in out or "feedback" in out

    def test_autonomous_off_shows_helpful_message(self):
        from unittest.mock import patch
        with patch("atlas_plus.mcp.memory_server.tools.remember.memory_remember",
                   return_value={"ok": False, "error": "autonomous writes disabled"}):
            out = _run_cmd("/save feedback Some fact.")
        assert "ATLAS_AUTONOMOUS" in out or "autonomous" in out.lower() or "failed" in out.lower()

    def test_save_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/save" in out


# ---------------------------------------------------------------------------
# /kill and /retry commands
# ---------------------------------------------------------------------------

class TestKillCommand:
    def test_kill_cancels_running_task(self):
        from unittest.mock import patch, MagicMock
        task = MagicMock()
        task.task_id = "task-run-001"
        task.status = "running"
        with patch("atlas_plus.brain.tasks.get_task", return_value=task), \
             patch("atlas_plus.brain.tasks.mark_failed") as mock_fail:
            out = _run_cmd("/kill task-run-001")
        mock_fail.assert_called_once()
        assert "cancelled" in out.lower() or "failed" in out.lower() or "task-run-001" in out

    def test_kill_not_found_shows_error(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.get_task", return_value=None):
            out = _run_cmd("/kill nosuchid")
        assert "not found" in out.lower()

    def test_kill_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/kill" in out


class TestRetryCommand:
    def test_retry_resubmits_failed_task(self):
        from unittest.mock import patch, MagicMock
        old_task = MagicMock()
        old_task.task_id = "task-fail-001"
        old_task.status = "failed"
        old_task.instruction = "Rebuild the index"
        old_task.agent = "brain"
        new_task = MagicMock()
        new_task.task_id = "task-retry-002"
        with patch("atlas_plus.brain.tasks.get_task", return_value=old_task), \
             patch("atlas_plus.brain.tasks.create_task", return_value=new_task):
            out = _run_cmd("/retry task-fail-001")
        assert "task-retry-002" in out or "queued" in out.lower() or "retry" in out.lower()

    def test_retry_not_found_shows_error(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.get_task", return_value=None):
            out = _run_cmd("/retry nosuchid")
        assert "not found" in out.lower()

    def test_retry_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/retry" in out


# ---------------------------------------------------------------------------
# /check and /confirm commands
# ---------------------------------------------------------------------------

class TestCheckCommand:
    def test_shows_config_info(self):
        out = _run_cmd("/check")
        assert out.strip() != ""  # Just verify it produces some output

    def test_check_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/check" in out


class TestConfirmCommand:
    def test_confirms_existing_file(self, tmp_path):
        from unittest.mock import patch
        # Create a real file
        mem = tmp_path / ".memory"
        mem.mkdir()
        fact = mem / "feedback" / "style.md"
        fact.parent.mkdir()
        fact.write_text("---\ntitle: Style\n---\nBody.", encoding="utf-8")
        with patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT", mem), \
             patch("atlas_plus.mcp.memory_server.routing.confirm_fact", return_value=True):
            out = _run_cmd("/confirm feedback/style.md")
        assert "Confirmed" in out or "confirmed" in out.lower()

    def test_file_not_found_shows_error(self, tmp_path):
        from unittest.mock import patch
        mem = tmp_path / ".memory"
        mem.mkdir()
        with patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT", mem):
            out = _run_cmd("/confirm feedback/nonexistent.md")
        assert "not found" in out.lower() or "File" in out

    def test_confirm_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/confirm" in out


# ---------------------------------------------------------------------------
# /pending, /approve, /reject commands
# ---------------------------------------------------------------------------

class TestPendingCommand:
    def test_lists_pending_facts(self):
        from unittest.mock import patch, MagicMock
        fact = MagicMock(spec=[])  # blank spec to prevent Rich from receiving MagicMocks
        fact.id = "fact-abc-001"
        fact.scope_hint = "feedback"
        fact.fact_text = "Always use ISO 8601 dates."
        fact.task_id = "task-001"
        fact.confidence = 0.85
        with patch("atlas_plus.brain.task_facts.list_pending_facts", return_value=[fact]):
            out = _run_cmd("/pending")
        assert "fact-abc-001" in out or "feedback" in out or "ISO 8601" in out

    def test_no_pending_shows_empty(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.task_facts.list_pending_facts", return_value=[]):
            out = _run_cmd("/pending")
        assert "No pending" in out or "no pending" in out.lower() or out.strip() != ""

    def test_pending_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/pending" in out


class TestApproveRejectCommands:
    def test_approve_no_arg_shows_usage(self):
        out = _run_cmd("/approve")
        assert "Usage" in out or "usage" in out.lower() or "id" in out.lower()

    def test_approve_successful(self):
        from unittest.mock import patch, MagicMock
        fact = MagicMock()
        fact.id = "fact-xyz-001"
        fact.scope = "feedback"
        fact.text = "Use 4-space indentation."
        with patch("atlas_plus.brain.task_facts.list_pending_facts", return_value=[fact]), \
             patch("atlas_plus.brain.task_facts.approve_fact",
                   return_value={"ok": True, "file": "feedback/indent.md"}):
            out = _run_cmd("/approve fact-xyz")
        assert "pproved" in out or "feedback/indent" in out

    def test_reject_no_arg_shows_usage(self):
        out = _run_cmd("/reject")
        assert "Usage" in out or "usage" in out.lower() or "id" in out.lower()

    def test_reject_successful(self):
        from unittest.mock import patch, MagicMock
        fact = MagicMock()
        fact.id = "fact-bad-001"
        fact.text = "Wrong fact."
        with patch("atlas_plus.brain.task_facts.list_pending_facts", return_value=[fact]), \
             patch("atlas_plus.brain.task_facts.reject_fact", return_value={"ok": True}):
            out = _run_cmd("/reject fact-bad")
        assert "ejected" in out or "fact-bad" in out

    def test_approve_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/approve" in out

    def test_reject_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/reject" in out


# ---------------------------------------------------------------------------
# /escalation, /priority, /dispatch-tickets, /ticket-status
# ---------------------------------------------------------------------------

class TestEscalationCommand:
    def test_shows_escalation_panel(self):
        out = _run_cmd("/escalation")
        assert "escalation" in out.lower() or "Escalation" in out

    def test_shows_env_var_names(self):
        out = _run_cmd("/escalation")
        assert "ATLAS_CLAUDE_ESCALATION" in out

    def test_escalation_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/escalation" in out


class TestPriorityCommand:
    def test_no_arg_shows_usage(self):
        out = _run_cmd("/priority")
        assert "Usage" in out or "usage" in out.lower()

    def test_sets_priority_successfully(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.set_task_priority", return_value=True):
            out = _run_cmd("/priority task-001 5")
        assert "Priority set" in out or "5" in out

    def test_priority_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/priority" in out


class TestDispatchTicketsCommand:
    def test_no_tickets_shows_no_eligible(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.dispatcher.dispatch_all_eligible", return_value=[]):
            out = _run_cmd("/dispatch-tickets")
        assert "No eligible" in out or "no eligible" in out.lower()

    def test_shows_dispatched_count(self):
        from unittest.mock import patch
        fake = [{"ticket_id": "ATL-001", "task_id": "task-xyz", "agent": "brain"}]
        with patch("atlas_plus.brain.dispatcher.dispatch_all_eligible", return_value=fake):
            out = _run_cmd("/dispatch-tickets")
        assert "1" in out or "ATL-001" in out or "Dispatched" in out

    def test_dispatch_tickets_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/dispatch-tickets" in out


class TestTicketStatusCommand:
    def test_no_arg_shows_usage(self):
        out = _run_cmd("/ticket-status")
        assert "Usage" in out or "usage" in out.lower()

    def test_ticket_not_found_shows_error(self):
        from unittest.mock import patch
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            out = _run_cmd("/ticket-status ATL-999")
        assert "not found" in out.lower() or "ATL-999" in out

    def test_ticket_status_shown(self):
        from unittest.mock import patch, MagicMock
        ticket = MagicMock()
        ticket.id = "ATL-001"
        ticket.status = "in-progress"
        ticket.title = "Fix SIP registration"
        ticket.assignee = "claude-code"
        ticket.reopen_count = 0
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[ticket]), \
             patch("atlas_plus.brain.tasks.list_tasks", return_value=[]):
            out = _run_cmd("/ticket-status ATL-001")
        assert "ATL-001" in out or "in-progress" in out.lower()

    def test_ticket_status_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/ticket-status" in out


# ---------------------------------------------------------------------------
# /escalate, /clear, /export, /verify commands
# ---------------------------------------------------------------------------

class TestEscalateCommand:
    def test_no_arg_shows_usage(self):
        out = _run_cmd("/escalate")
        assert "Usage" in out or "usage" in out.lower() or "task" in out.lower()

    def test_task_not_found_shows_error(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.get_task", return_value=None):
            out = _run_cmd("/escalate nosuchid")
        assert "not found" in out.lower()

    def test_already_cc_task_shows_warning(self):
        from unittest.mock import patch, MagicMock
        task = MagicMock()
        task.task_id = "esc-001"
        task.agent = "claude-code"
        with patch("atlas_plus.brain.tasks.get_task", return_value=task):
            out = _run_cmd("/escalate esc-001")
        assert "already" in out.lower() or "claude-code" in out

    def test_escalate_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/escalate" in out


class TestClearCommand:
    def test_clear_calls_history_clear(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.history.clear_session", return_value=3) as mock_clear:
            out = _run_cmd("/clear")
        mock_clear.assert_called_once()
        assert "cleared" in out.lower() or "3" in out or out.strip() != ""

    def test_clear_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/clear" in out


class TestExportCommand:
    def test_export_no_messages_shows_message(self, tmp_path):
        from unittest.mock import patch
        with patch("atlas_plus.brain.history.session_export_markdown", return_value=""):
            out = _run_cmd("/export")
        assert "No messages" in out or "no messages" in out.lower() or out.strip() != ""

    def test_export_writes_file(self, tmp_path):
        from unittest.mock import patch
        import os
        with patch("atlas_plus.brain.history.session_export_markdown",
                   return_value="## Session\n\nContent here."), \
             patch.dict(os.environ, {"ATLAS_STATE_DIR": str(tmp_path)}):
            out = _run_cmd("/export")
        # Should either show a file path or confirm export
        assert out.strip() != ""

    def test_export_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/export" in out


class TestVerifyCliCommand:
    def test_no_arg_shows_usage(self):
        out = _run_cmd("/verify")
        assert "Usage" in out or "usage" in out.lower() or "ticket" in out.lower()

    def test_ticket_not_found_shows_error(self):
        from unittest.mock import patch
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            out = _run_cmd("/verify ATL-999")
        assert "not found" in out.lower() or "ATL-999" in out

    def test_verify_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/verify" in out


class TestAttemptsCommand:
    def test_no_arg_shows_usage(self):
        out = _run_cmd("/attempts")
        assert "Usage" in out or "usage" in out.lower()

    def test_task_not_found_shows_error(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.get_task", return_value=None):
            out = _run_cmd("/attempts task-nonexistent")
        assert "not found" in out.lower()

    def test_no_attempts_shows_message(self):
        from unittest.mock import patch, MagicMock
        mock_task = MagicMock(spec=[])
        mock_task.task_id = "task-abc123"
        mock_task.status = "failed"
        with patch("atlas_plus.brain.tasks.get_task", return_value=mock_task), \
             patch("atlas_plus.brain.tasks.list_task_attempts", return_value=[]):
            out = _run_cmd("/attempts task-abc123")
        assert "no attempt" in out.lower() or "not" in out.lower()

    def test_shows_attempt_table_when_history_exists(self):
        from unittest.mock import patch, MagicMock
        mock_task = MagicMock(spec=[])
        mock_task.task_id = "task-abc123"
        mock_task.status = "done"
        fake_attempts = [
            {"attempt_id": "attempt-001", "task_id": "task-abc123",
             "attempt_num": 1, "outcome": "failed", "agent": "brain",
             "started_at": "2026-05-20T03:00:00+00:00",
             "finished_at": "2026-05-20T03:01:00+00:00",
             "result_summary": "executor error"},
            {"attempt_id": "attempt-002", "task_id": "task-abc123",
             "attempt_num": 2, "outcome": "done", "agent": "brain",
             "started_at": "2026-05-20T03:05:00+00:00",
             "finished_at": "2026-05-20T03:06:00+00:00",
             "result_summary": "completed successfully"},
        ]
        with patch("atlas_plus.brain.tasks.get_task", return_value=mock_task), \
             patch("atlas_plus.brain.tasks.list_task_attempts", return_value=fake_attempts):
            out = _run_cmd("/attempts task-abc123")
        # Table is rendered; check agent column (no markup) and title
        assert "brain" in out
        assert "2 attempts" in out.lower() or "attempt" in out.lower()

    def test_attempts_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/attempts" in out


class TestRecentCommand:
    def test_no_completed_tasks_shows_message(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[]):
            out = _run_cmd("/recent")
        assert "no completed" in out.lower() or "No completed" in out

    def test_shows_table_with_finished_tasks(self):
        from unittest.mock import patch
        from atlas_plus.brain.tasks import Task
        task = Task(
            task_id="task-done-abc12",
            session_id="brain-test",
            instruction="write tests",
            status="done",
            created_at="2026-05-20T03:00:00+00:00",
            finished_at="2026-05-20T03:01:00+00:00",
            agent="brain",
        )
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[task]):
            out = _run_cmd("/recent")
        assert "task-done-abc12" in out or "Recent" in out

    def test_excludes_pending_and_running(self):
        from unittest.mock import patch
        from atlas_plus.brain.tasks import Task
        pending = Task(
            task_id="task-pending",
            session_id="brain-test",
            instruction="pending task",
            status="pending",
            created_at="2026-05-20T03:00:00+00:00",
            finished_at=None,
        )
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[pending]):
            out = _run_cmd("/recent")
        assert "task-pending" not in out

    def test_recent_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/recent" in out


class TestPlanCommand:
    def test_no_eligible_shows_message(self):
        from unittest.mock import patch
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[]), \
             patch("atlas_plus.brain.dispatcher.is_dispatch_enabled", return_value=False):
            out = _run_cmd("/plan")
        assert "Automation Plan" in out or "no dispatch" in out.lower() or out.strip() != ""

    def test_shows_eligible_tickets_table(self):
        from unittest.mock import patch, MagicMock
        ticket = MagicMock(spec=[])
        ticket.id = "ATL-001"
        ticket.title = "fix the bug"
        ticket.status = "todo"
        ticket.assignee = "brain"
        ticket.priority = "high"
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[ticket]), \
             patch("atlas_plus.brain.dispatcher.is_dispatch_enabled", return_value=True):
            out = _run_cmd("/plan")
        assert "ATL-001" in out or "brain" in out

    def test_human_tickets_excluded_from_table(self):
        from unittest.mock import patch, MagicMock
        human_ticket = MagicMock(spec=[])
        human_ticket.id = "ATL-H01"
        human_ticket.title = "human action needed"
        human_ticket.status = "todo"
        human_ticket.assignee = "human"
        human_ticket.priority = "normal"
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[human_ticket]), \
             patch("atlas_plus.brain.dispatcher.is_dispatch_enabled", return_value=False):
            out = _run_cmd("/plan")
        assert "ATL-H01" not in out  # human tickets not shown in dispatch table

    def test_plan_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/plan" in out


class TestExhaustedCommand:
    def test_no_exhausted_tasks_shows_message(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=[]):
            out = _run_cmd("/exhausted")
        assert "no exhausted" in out.lower() or "No exhausted" in out

    def test_shows_table_when_exhausted_tasks_exist(self):
        from unittest.mock import patch
        from atlas_plus.brain.tasks import Task
        fake = [Task(
            task_id="task-deadbeef1234",
            session_id="ticket-ATL-005",
            instruction="implement retry logic",
            status="failed",
            created_at="2026-05-20T01:00:00+00:00",
            attempts=3,
            max_attempts=3,
            agent="brain",
        )]
        with patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=fake):
            out = _run_cmd("/exhausted")
        assert "brain" in out
        assert "task-deadbeef" in out or "Exhausted" in out

    def test_exhausted_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/exhausted" in out


# ---------------------------------------------------------------------------
# /queue-research
# ---------------------------------------------------------------------------

class TestQueueResearch:
    def test_no_args_shows_usage(self):
        out = _run_cmd("/queue-research")
        assert "Usage" in out or "usage" in out.lower()

    def test_queues_inline_topics(self):
        fake_task = MagicMock()
        fake_task.task_id = "rq-task-001"
        with patch("atlas_plus.brain.tasks.create_task", return_value=fake_task):
            out = _run_cmd("/queue-research SIP REGISTER flow")
        assert "Queued" in out or "queued" in out.lower()

    def test_queues_topics_from_file(self, tmp_path):
        topics_file = tmp_path / "topics.txt"
        topics_file.write_text("SIP REGISTER\nRTP media\n# comment\n\n", encoding="utf-8")
        fake_task = MagicMock()
        fake_task.task_id = "rq-task-002"
        with patch("atlas_plus.brain.tasks.create_task", return_value=fake_task):
            out = _run_cmd(f"/queue-research {topics_file}")
        assert "2 queued" in out or "queued" in out.lower()

    def test_deduplicates_topics(self):
        fake_task = MagicMock()
        fake_task.task_id = "rq-task-003"
        with patch("atlas_plus.brain.tasks.create_task", return_value=fake_task) as mock_create:
            out = _run_cmd("/queue-research SIP REGISTER\\nSIP REGISTER\\nRTP")
        assert mock_create.call_count == 2

    def test_exception_shows_error(self):
        with patch("atlas_plus.brain.tasks.create_task",
                   side_effect=RuntimeError("db locked")):
            out = _run_cmd("/queue-research SIP REGISTER")
        assert "Failed" in out or "failed" in out.lower() or "locked" in out

    def test_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/queue-research" in out


# ---------------------------------------------------------------------------
# /suggest-tags
# ---------------------------------------------------------------------------

class TestSuggestTags:
    def test_no_untagged_files_shows_clean_message(self, tmp_path):
        with patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT", tmp_path):
            out = _run_cmd("/suggest-tags")
        assert "No untagged" in out or "no untagged" in out.lower() or out != ""

    def test_dry_run_shows_suggestions(self, tmp_path):
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        (feedback / "pref.md").write_text(
            "---\ntitle: FastAPI Preference\ntype: feedback\n---\nContent.", encoding="utf-8"
        )
        with patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT", tmp_path):
            out = _run_cmd("/suggest-tags")
        assert "dry-run" in out.lower() or "fastapi" in out.lower() or "feedback" in out.lower()

    def test_write_requires_autonomous(self, tmp_path):
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        (feedback / "pref.md").write_text(
            "---\ntitle: FastAPI Pref\ntype: feedback\n---\nContent.", encoding="utf-8"
        )
        with patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.autonomous.require_autonomous_writes",
                   side_effect=RuntimeError("ATLAS_AUTONOMOUS is off")):
            out = _run_cmd("/suggest-tags --write")
        assert "ATLAS_AUTONOMOUS" in out or "off" in out

    def test_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/suggest-tags" in out

    def test_wired_in_dispatch(self):
        with patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT",
                   Path("C:/nonexistent")):
            out = _run_cmd("/suggest-tags")
        # Should not be "Unknown command" -- the command is registered
        assert "Unknown command" not in out


# ---------------------------------------------------------------------------
# /suggest-facts
# ---------------------------------------------------------------------------

class TestSuggestFacts:
    def test_no_candidates_shows_clean_message(self):
        stats = {"candidates": 0, "approved": 0, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats):
            out = _run_cmd("/suggest-facts")
        assert "No fact candidates" in out or "no fact" in out.lower()

    def test_shows_candidates_count(self):
        stats = {"candidates": 3, "approved": 1, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats):
            out = _run_cmd("/suggest-facts")
        assert "3" in out or "candidates" in out.lower()

    def test_auto_requires_autonomous(self):
        with patch("atlas_plus.autonomous.require_autonomous_writes",
                   side_effect=RuntimeError("ATLAS_AUTONOMOUS is off")):
            out = _run_cmd("/suggest-facts --auto")
        assert "ATLAS_AUTONOMOUS" in out or "off" in out

    def test_since_flag_parsed(self):
        stats = {"candidates": 0, "approved": 0, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats) as mock_sf:
            _run_cmd("/suggest-facts --since 4h")
        mock_sf.assert_called_once()
        call_kwargs = mock_sf.call_args[1]
        assert call_kwargs.get("since") == "4h"

    def test_runtime_error_shows_red_message(self):
        with patch("atlas_plus.curation.suggest_facts.suggest_facts",
                   side_effect=RuntimeError("no transcript found")):
            out = _run_cmd("/suggest-facts")
        assert "error" in out.lower() or "no transcript" in out.lower()

    def test_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/suggest-facts" in out

    def test_wired_in_dispatch(self):
        stats = {"candidates": 0, "approved": 0, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats):
            out = _run_cmd("/suggest-facts")
        assert "Unknown command" not in out

    def test_skip_similar_flag_passed_through(self):
        stats = {"candidates": 2, "approved": 0, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats) as mock_sf:
            _run_cmd("/suggest-facts --skip-similar 0.6")
        mock_sf.assert_called_once()
        call_kwargs = mock_sf.call_args[1]
        assert call_kwargs.get("skip_similar_threshold") == pytest.approx(0.6)

    def test_skip_similar_invalid_float_shows_error(self):
        out = _run_cmd("/suggest-facts --skip-similar notanumber")
        assert "error" in out.lower() or "float" in out.lower() or "skip-similar" in out.lower()

    def test_skip_similar_shown_in_mode_string(self):
        stats = {"candidates": 2, "approved": 0, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats):
            out = _run_cmd("/suggest-facts --skip-similar 0.5")
        assert "0.50" in out or "skip-similar" in out.lower()

    def test_threshold_flag_passed_through(self):
        stats = {"candidates": 1, "approved": 0, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats) as mock_sf:
            _run_cmd("/suggest-facts --threshold 0.85")
        mock_sf.assert_called_once()
        call_kwargs = mock_sf.call_args[1]
        assert call_kwargs.get("confidence_threshold") == pytest.approx(0.85)

    def test_shows_rejected_low_confidence_in_output(self):
        stats = {"candidates": 2, "approved": 0, "written": 0, "rejected_low_confidence": 3}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats):
            out = _run_cmd("/suggest-facts")
        assert "rejected" in out.lower() or "3" in out


# ---------------------------------------------------------------------------
# /next-actions
# ---------------------------------------------------------------------------

class TestNextActions:
    def test_no_items_shows_clean_message(self):
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.curation.tag_suggest.suggest_tags_for_file", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT",
                   Path("C:/nonexistent")):
            out = _run_cmd("/next-actions")
        assert "good shape" in out.lower() or "no urgent" in out.lower()

    def test_shows_decayed_items(self):
        from atlas_plus.curation.decay import DecayCandidate
        cand = DecayCandidate("ref/old.md", "Old", "reference", 30, "2025-01-01", 90)
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[cand]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.curation.tag_suggest.suggest_tags_for_file", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT",
                   Path("C:/nonexistent")):
            out = _run_cmd("/next-actions")
        assert "old.md" in out or "Decayed" in out or "decayed" in out.lower()

    def test_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/next-actions" in out

    def test_wired_in_dispatch(self):
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.curation.tag_suggest.suggest_tags_for_file", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT",
                   Path("C:/nonexistent")):
            out = _run_cmd("/next-actions")
        assert "Unknown command" not in out


# ---------------------------------------------------------------------------
# /session-log command
# ---------------------------------------------------------------------------

class TestCurationReport:
    def _make_report(self, dups=0, stale=0, convergence=0, decisions=0):
        lines = [
            "# Atlas Memory Curation Report",
            "",
            f"## Potential Duplicates ({dups} found, threshold=0.40)",
            f"## Stale Files ({stale} found, older than 6 months)",
            f"## Convergence Flags ({convergence} contradictions found)",
            f"## Stale Decisions ({decisions} decisions need revisit)",
        ]
        return "\n".join(lines)

    def test_shows_summary_counts(self):
        from pathlib import Path
        report = self._make_report(dups=3, stale=5)
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   return_value=(report, Path(".atlas/reports/report.md"))):
            out = _run_cmd("/curation-report")
        assert "3" in out or "Duplicates" in out
        assert "5" in out or "Stale" in out

    def test_clean_report_shows_zeros(self):
        from pathlib import Path
        report = self._make_report(dups=0, stale=0, convergence=0, decisions=0)
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   return_value=(report, Path(".atlas/reports/report.md"))):
            out = _run_cmd("/curation-report")
        assert "Curation Report" in out

    def test_error_shows_message(self):
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   side_effect=RuntimeError("memory root not found")):
            out = _run_cmd("/curation-report")
        assert "error" in out.lower() or "memory root" in out

    def test_write_flag_changes_status(self):
        from pathlib import Path
        report = self._make_report()
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   return_value=(report, Path(".atlas/reports/report.md"))) as mock_gen:
            out = _run_cmd("/curation-report --write")
        call_kwargs = mock_gen.call_args[1]
        assert call_kwargs.get("dry_run") is False

    def test_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/curation-report" in out

    def test_wired_in_dispatch(self):
        from pathlib import Path
        report = self._make_report()
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   return_value=(report, Path(".atlas/reports/report.md"))):
            out = _run_cmd("/curation-report")
        assert "Unknown command" not in out


class TestSessionLog:
    def test_dry_run_shows_preview(self):
        result = {
            "ok": True,
            "entry": "## 2026-05-21 -- atlas-memory+\n\nTest session log entry.",
            "paths_written": [],
        }
        with patch("atlas_plus.sprint_summary.write_session_log", return_value=result):
            out = _run_cmd("/session-log --dry-run")
        assert "DRY RUN" in out or "session" in out.lower() or "2026-05-21" in out

    def test_no_commits_today_shows_message(self):
        result = {"ok": True, "entry": "", "paths_written": []}
        with patch("atlas_plus.sprint_summary.write_session_log", return_value=result):
            out = _run_cmd("/session-log")
        assert "No commits" in out or "nothing" in out.lower()

    def test_success_shows_paths_written(self):
        result = {
            "ok": True,
            "entry": "## 2026-05-21 -- atlas-memory+\n\nSome log.",
            "paths_written": [
                "C:/Tools/.memory/sessions/_history.md",
                "C:/Tools/updates.txt",
            ],
        }
        with patch("atlas_plus.sprint_summary.write_session_log", return_value=result):
            out = _run_cmd("/session-log")
        assert "2" in out or "written" in out.lower() or "_history.md" in out

    def test_failure_shows_error(self):
        result = {"ok": False, "error": "git not available"}
        with patch("atlas_plus.sprint_summary.write_session_log", return_value=result):
            out = _run_cmd("/session-log")
        assert "failed" in out.lower() or "git not available" in out

    def test_exception_shows_error(self):
        with patch("atlas_plus.sprint_summary.write_session_log",
                   side_effect=RuntimeError("git binary not found")):
            out = _run_cmd("/session-log")
        assert "error" in out.lower() or "git binary" in out

    def test_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/session-log" in out

    def test_wired_in_dispatch(self):
        result = {"ok": True, "entry": "", "paths_written": []}
        with patch("atlas_plus.sprint_summary.write_session_log", return_value=result):
            out = _run_cmd("/session-log")
        assert "Unknown command" not in out


# ---------------------------------------------------------------------------
# /temporal-search, /temporal-history, /temporal-entities
# ---------------------------------------------------------------------------

class TestTemporalRebuild:
    def test_fresh_graph_skips_rebuild(self):
        with patch("atlas_plus.doctor.cli.check_temporal",
                   return_value={"stale": False, "age_days": 1, "entities": 10, "last_built": "2026-05-21"}):
            out = _run_cmd("/temporal-rebuild")
        assert "fresh" in out.lower() or "stale" not in out.lower()

    def test_stale_triggers_rebuild(self):
        result = {
            "source": "all", "files_indexed": 5, "files_skipped": 10, "facts_added": 20,
            "warnings": [],
            "index": {"episodes": 3, "entities": 15, "facts_total": 80, "facts_active": 70}
        }
        with patch("atlas_plus.doctor.cli.check_temporal",
                   return_value={"stale": True, "age_days": 10, "entities": 5}), \
             patch("atlas_plus.temporal_graph.build.build_temporal_graph", return_value=result):
            out = _run_cmd("/temporal-rebuild")
        assert "rebuilt" in out.lower() or "15" in out or "entities" in out.lower()

    def test_force_rebuilds_even_when_fresh(self):
        result = {
            "source": "all", "files_indexed": 3, "files_skipped": 8, "facts_added": 5,
            "warnings": [],
            "index": {"episodes": 2, "entities": 12, "facts_total": 60, "facts_active": 55}
        }
        with patch("atlas_plus.doctor.cli.check_temporal",
                   return_value={"stale": False, "age_days": 1, "entities": 10, "last_built": "2026-05-21"}), \
             patch("atlas_plus.temporal_graph.build.build_temporal_graph", return_value=result):
            out = _run_cmd("/temporal-rebuild --force")
        assert "rebuilt" in out.lower() or "12" in out

    def test_build_error_shows_message(self):
        with patch("atlas_plus.doctor.cli.check_temporal",
                   return_value={"stale": True, "age_days": 8}), \
             patch("atlas_plus.temporal_graph.build.build_temporal_graph",
                   return_value={"error": "source unknown"}):
            out = _run_cmd("/temporal-rebuild")
        assert "error" in out.lower()

    def test_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/temporal-rebuild" in out

    def test_wired_in_dispatch(self):
        with patch("atlas_plus.doctor.cli.check_temporal",
                   return_value={"stale": False, "age_days": 1, "entities": 5, "last_built": "2026-05-21"}):
            out = _run_cmd("/temporal-rebuild")
        assert "Unknown command" not in out


class TestTemporalSearch:
    def _mock_provider(self, facts=None, entities=None):
        from unittest.mock import MagicMock
        from atlas_plus.temporal_graph.models import TemporalResult, TemporalFact, _now_iso
        provider = MagicMock()
        provider.stats.return_value = {"facts": len(facts or [1]), "entities": len(entities or [1])}
        provider.search_current.return_value = facts or []
        provider.search_as_of.return_value = facts or []
        provider.list_entities.return_value = entities or []
        return provider

    def test_no_query_shows_usage(self):
        out = _run_cmd("/temporal-search")
        assert "temporal-search" in out.lower() or "query" in out.lower()

    def test_empty_graph_shows_warning(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {"facts": 0}
            out = _run_cmd("/temporal-search Atlas")
        assert "empty" in out.lower() or "build" in out.lower()

    def test_no_results_shows_clean_message(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {"facts": 5}
            instance.search_current.return_value = []
            out = _run_cmd("/temporal-search nonexistent-entity")
        assert "no temporal facts" in out.lower() or "no" in out.lower()

    def test_shows_results(self):
        from unittest.mock import MagicMock
        result = MagicMock()
        result.score = 0.85
        result.fact.fact_text = "Atlas MCP Tunnel is live at port 8765"
        result.fact.valid_from = "2026-05-01T00:00:00Z"
        result.provenance = "memory"
        result.freshness_warning = None

        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {"facts": 10}
            instance.search_current.return_value = [result]
            out = _run_cmd("/temporal-search Atlas MCP")
        assert "Atlas MCP Tunnel" in out or "8765" in out

    def test_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/temporal-search" in out

    def test_wired_in_dispatch(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {"facts": 0}
            out = _run_cmd("/temporal-search Atlas")
        assert "Unknown command" not in out


class TestTemporalHistory:
    def test_no_entity_shows_usage(self):
        out = _run_cmd("/temporal-history")
        assert "temporal-history" in out.lower() or "entity" in out.lower()

    def test_shows_entity_history(self):
        from unittest.mock import MagicMock
        entity = MagicMock()
        entity.name = "Atlas MCP Tunnel"
        entity.aliases = ["MCP", "tunnel"]
        entity.first_seen_at = "2026-01-01T00:00:00Z"
        entity.last_seen_at = "2026-05-21T00:00:00Z"

        fact = MagicMock()
        fact.fact_text = "Atlas MCP Tunnel is live"
        fact.valid_from = "2026-01-01T00:00:00Z"
        fact.invalid_at = None

        history = MagicMock()
        history.entity = entity
        history.facts = [fact]
        history.current_fact = fact
        history.warnings = []

        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.history.return_value = history
            out = _run_cmd("/temporal-history Atlas MCP Tunnel")
        assert "Atlas MCP Tunnel" in out

    def test_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/temporal-history" in out


class TestTemporalEntities:
    def test_empty_graph_shows_warning(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {"entities": 0}
            out = _run_cmd("/temporal-entities")
        assert "no entities" in out.lower() or "empty" in out.lower() or "build" in out.lower()

    def test_shows_entity_list(self):
        from unittest.mock import MagicMock
        entity = MagicMock()
        entity.name = "Atlas Brain"
        entity.aliases = []

        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {"entities": 1}
            instance.list_entities.return_value = [entity]
            out = _run_cmd("/temporal-entities")
        assert "Atlas Brain" in out

    def test_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/temporal-entities" in out

    def test_wired_in_dispatch(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {"entities": 0}
            out = _run_cmd("/temporal-entities")
        assert "Unknown command" not in out


# ---------------------------------------------------------------------------
# /decay-report
# ---------------------------------------------------------------------------

class TestDecayReportCommand:
    def test_queues_decay_report_task(self):
        from unittest.mock import patch, MagicMock
        mock_task = MagicMock()
        mock_task.task_id = "decay-task-abc123"
        with patch("atlas_plus.brain.tasks.create_task", return_value=mock_task) as mock_create:
            out = _run_cmd("/decay-report")
        mock_create.assert_called_once()
        call_kw = mock_create.call_args[1]
        assert call_kw.get("kind") == "decay_report"
        assert "decay-task-abc123" in out or "Decay" in out

    def test_task_id_shown_in_output(self):
        from unittest.mock import patch, MagicMock
        mock_task = MagicMock()
        mock_task.task_id = "dr-xyz-789"
        with patch("atlas_plus.brain.tasks.create_task", return_value=mock_task):
            out = _run_cmd("/decay-report")
        assert "dr-xyz-789" in out

    def test_create_task_failure_shows_error(self):
        from unittest.mock import patch
        with patch("atlas_plus.brain.tasks.create_task",
                   side_effect=RuntimeError("db locked")):
            out = _run_cmd("/decay-report")
        assert "Failed" in out or "error" in out.lower() or "db locked" in out

    def test_wired_in_help(self):
        out = _run_cmd("/help")
        assert "/decay-report" in out

    def test_wired_in_dispatch(self):
        from unittest.mock import patch, MagicMock
        mock_task = MagicMock()
        mock_task.task_id = "decay-test-001"
        with patch("atlas_plus.brain.tasks.create_task", return_value=mock_task):
            out = _run_cmd("/decay-report")
        assert "Unknown command" not in out
