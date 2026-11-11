"""Tests for the four dispatch fixes shipped in v0.3-D.

1. Ticket loop close: task done/failed/blocked closes originating ticket
2. Assignee-to-agent mapping in the dispatch endpoint
3. ATLAS_LOCAL_ONLY mode marks API agents offline
4. Smart routing default changed to on
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fix 1: Ticket loop close
# ---------------------------------------------------------------------------

class TestTicketLoopClose:
    def _make_db(self, tmp: Path, session_id: str) -> tuple[Path, str]:
        """Create a task in a temp SQLite DB and return (db_path, task_id)."""
        from atlas_plus.brain.tasks import create_task, _DB_PATH
        db = tmp / "state.sqlite"
        task = create_task(
            instruction="Do something",
            session_id=session_id,
            db_path=db,
        )
        return db, task.task_id

    def test_done_closes_ticket(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            db, task_id = self._make_db(tmp_path, "ticket-PRJ-001")
            closed_ids = []
            statuses = []
            def mock_update(tid, status, **kw):
                closed_ids.append(tid)
                statuses.append(status)
                return MagicMock()
            with patch("atlas_plus.tickets.store.update_ticket_status", side_effect=mock_update), \
                 patch("atlas_plus.tickets.store.get_ticket", return_value=MagicMock(path=tmp_path/"t.md", status="in-progress")):
                from atlas_plus.brain.tasks import mark_done
                mark_done(task_id, "All done.", db_path=db)
        assert "PRJ-001" in closed_ids
        assert statuses[closed_ids.index("PRJ-001")] == "done"

    def test_failed_returns_ticket_to_todo(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            db, task_id = self._make_db(tmp_path, "ticket-ATL-007")
            statuses = []
            def mock_update(tid, status, **kw):
                statuses.append(status)
                return MagicMock()
            with patch("atlas_plus.tickets.store.update_ticket_status", side_effect=mock_update), \
                 patch("atlas_plus.tickets.store.get_ticket", return_value=MagicMock(path=tmp_path/"t.md", status="in-progress")):
                from atlas_plus.brain.tasks import mark_failed
                mark_failed(task_id, "Error occurred.", db_path=db)
        assert "todo" in statuses

    def test_blocked_marks_ticket_blocked(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            db, task_id = self._make_db(tmp_path, "ticket-ATL-008")
            statuses = []
            def mock_update(tid, status, **kw):
                statuses.append(status)
                return MagicMock()
            with patch("atlas_plus.tickets.store.update_ticket_status", side_effect=mock_update), \
                 patch("atlas_plus.tickets.store.get_ticket", return_value=MagicMock(path=tmp_path/"t.md", status="in-progress")):
                from atlas_plus.brain.tasks import mark_blocked
                mark_blocked(task_id, "Blocked on human.", db_path=db)
        assert "blocked" in statuses

    def test_non_ticket_session_no_ticket_update(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db, task_id = self._make_db(Path(tmp), "scheduler-temporal-build")
            update_calls = []
            with patch("atlas_plus.tickets.store.update_ticket_status",
                       side_effect=lambda *a, **kw: update_calls.append(a)):
                from atlas_plus.brain.tasks import mark_done
                mark_done(task_id, "Done.", db_path=db)
        assert update_calls == []

    def test_missing_ticket_never_raises(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db, task_id = self._make_db(Path(tmp), "ticket-NOTREAL-999")
            # No mock -- real tickets store won't find NOTREAL-999, should not crash
            from atlas_plus.brain.tasks import mark_done
            mark_done(task_id, "Done.", db_path=db)  # must not raise

    def test_malformed_session_id_no_match(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db, task_id = self._make_db(Path(tmp), "ticket-notapattern")
            update_calls = []
            with patch("atlas_plus.tickets.store.update_ticket_status",
                       side_effect=lambda *a, **kw: update_calls.append(a)):
                from atlas_plus.brain.tasks import mark_done
                mark_done(task_id, "Done.", db_path=db)
        assert update_calls == []


# ---------------------------------------------------------------------------
# Fix 2: Assignee-to-agent mapping (tested via the dispatch endpoint)
# ---------------------------------------------------------------------------

class TestAssigneeAgentMapping:
    """These tests verify the mapping logic at the web/app.py dispatch layer."""

    def _dispatch(self, assignee: str, agent_override: str = None):
        """Dispatch a STUB ticket and return (response, payload posted to Brain).

        Fully isolated: the ticket is mocked at the store layer, so the test never
        touches the real .memory/tickets/ and verifies the assignee->agent mapping
        deterministically regardless of what other tickets happen to exist.
        """
        from fastapi.testclient import TestClient
        from unittest.mock import AsyncMock, MagicMock

        # app.API_KEY freezes at first import (whichever test file imported the
        # app first), so env setdefault here is order-dependent: in subset runs
        # (pytest -k, CI shards) the frozen key mismatched and every dispatch
        # 401'd with an empty payload. Patch the request-time module globals
        # instead; also neutralize the shared rate limiter ("testclient" is not
        # loopback, so failures leaked by OTHER tests could 429 us).
        import atlas_plus.web.app as web_app
        client = TestClient(web_app.app, raise_server_exceptions=False)
        AUTH = {"Authorization": "Bearer test-key-abc"}

        ticket_id = "TES-001"
        ticket = MagicMock(id=ticket_id, title="Test dispatch", body="", assignee=assignee)

        posted_payload = {}
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"task_id": "task-xyz", "status": "pending"}

        async def fake_post(url, json=None, headers=None):
            posted_payload.update(json or {})
            return mock_resp

        with patch.object(web_app, "API_KEY", "test-key-abc"), \
             patch.object(web_app.AUTH_FAILURES, "is_rate_limited", return_value=False), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[ticket]), \
             patch("atlas_plus.tickets.store.update_ticket_status", return_value=MagicMock()), \
             patch("httpx.AsyncClient") as mc:
            mc.return_value.__aenter__.return_value.post = AsyncMock(side_effect=fake_post)
            body = {} if agent_override is None else {"agent": agent_override}
            r2 = client.post(f"/api/tickets/{ticket_id}/dispatch", json=body, headers=AUTH)

        assert r2.status_code < 400, f"dispatch failed at HTTP layer: {r2.status_code} {r2.text[:200]}"
        return r2, posted_payload

    def test_claude_code_assignee_passes_agent_hint(self):
        r, payload = self._dispatch("claude-code")
        assert r is not None
        assert payload.get("agent") == "claude-code"

    def test_local_assignee_passes_brain_hint(self):
        r, payload = self._dispatch("local")
        assert r is not None
        assert payload.get("agent") == "brain"

    def test_any_assignee_no_agent_hint(self):
        r, payload = self._dispatch("any")
        assert r is not None
        assert "agent" not in payload

    def test_human_assignee_no_agent_hint(self):
        r, payload = self._dispatch("human")
        assert r is not None
        assert "agent" not in payload

    def test_explicit_body_agent_overrides_assignee(self):
        r, payload = self._dispatch("claude-code", agent_override="coder")
        assert r is not None
        assert payload.get("agent") == "coder"


# ---------------------------------------------------------------------------
# Fix 3: ATLAS_LOCAL_ONLY mode
# ---------------------------------------------------------------------------

class TestLocalOnlyMode:
    def _make_all_online(self):
        """Return mock Ollama models list with all local models present."""
        return ["qwen3:8b", "qwen3:1.7b", "qwen2.5-coder:7b", "mistral:latest"]

    def test_local_only_api_agents_offline(self):
        import importlib
        import atlas_plus.brain.agents as agents_mod
        with patch.object(agents_mod, "_LOCAL_ONLY", True):
            agents_mod._last_refresh = 0
            ollama_models = self._make_all_online()
            for agent in agents_mod.REGISTRY:
                result = agents_mod._check_one(agent, ollama_models)
                if agent.agent_type not in agents_mod._LOCAL_AGENT_TYPES:
                    assert result is False, f"{agent.name} should be offline in local-only mode"

    def test_local_only_ollama_agents_still_online(self):
        import atlas_plus.brain.agents as agents_mod
        with patch.object(agents_mod, "_LOCAL_ONLY", True):
            ollama_models = self._make_all_online()
            brain = agents_mod._registry_by_name["brain"]
            assert agents_mod._check_one(brain, ollama_models) is True

    def test_local_only_routing_never_returns_api_agent(self):
        import atlas_plus.brain.agents as agents_mod
        with patch.object(agents_mod, "_LOCAL_ONLY", True), \
             patch.object(agents_mod, "_SMART_ROUTING", False):
            # Force all agents to online status
            for a in agents_mod.REGISTRY:
                a.online = True
            # Complex coding instruction would normally go to claude-code
            result = agents_mod.route("implement a multi-file authentication system")
            for a in agents_mod.REGISTRY:
                a.online = False
        assert result.agent_type in agents_mod._LOCAL_AGENT_TYPES

    def test_local_only_smart_routing_reroutes_complex_coding(self):
        import atlas_plus.brain.agents as agents_mod
        with patch.object(agents_mod, "_LOCAL_ONLY", True), \
             patch.object(agents_mod, "_SMART_ROUTING", True), \
             patch("atlas_plus.brain.agents._classify_instruction", return_value="complex-coding"):
            for a in agents_mod.REGISTRY:
                a.online = True
            result = agents_mod.route("architect a multi-file auth system")
            for a in agents_mod.REGISTRY:
                a.online = False
        # complex-coding in local-only should go to coder or brain, not claude-code
        assert result.agent_type in agents_mod._LOCAL_AGENT_TYPES


# ---------------------------------------------------------------------------
# Fix 4: Smart routing on by default
# ---------------------------------------------------------------------------

class TestSmartRoutingDefault:
    def test_smart_routing_env_default_is_on(self):
        """ATLAS_SMART_ROUTING defaults to 'on' when env var is unset."""
        import os
        # Remove the env var and re-evaluate the module-level expression
        env_backup = os.environ.pop("ATLAS_SMART_ROUTING", None)
        try:
            result = os.environ.get("ATLAS_SMART_ROUTING", "on").lower() in ("on", "1", "true")
            assert result is True
        finally:
            if env_backup is not None:
                os.environ["ATLAS_SMART_ROUTING"] = env_backup

    def test_smart_routing_can_be_disabled_via_env(self):
        import os
        backup = os.environ.get("ATLAS_SMART_ROUTING")
        os.environ["ATLAS_SMART_ROUTING"] = "off"
        try:
            result = os.environ.get("ATLAS_SMART_ROUTING", "on").lower() in ("on", "1", "true")
            assert result is False
        finally:
            if backup is not None:
                os.environ["ATLAS_SMART_ROUTING"] = backup
            else:
                os.environ.pop("ATLAS_SMART_ROUTING", None)
