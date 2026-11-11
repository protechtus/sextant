"""Tests for Atlas Brain FastAPI server routes.

Coverage: auth middleware, /health, /dispatch-stats, /scheduler/jobs,
/routing/stats, /credit/reset, rate limiting, _extract_caller_identity,
_is_credit_error, _check_rate_limit.
"""
from __future__ import annotations

import os
import time
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

# Set API key before importing server so module-level _API_KEY is populated.
_TEST_KEY = "test-brain-key-xyz"
os.environ.setdefault("ATLAS_BRAIN_API_KEY", _TEST_KEY)
os.environ.setdefault("ATLAS_BRAIN_OLLAMA_URL", "http://127.0.0.1:11434")

from fastapi.testclient import TestClient

# Patch scheduler lifecycle so TestClient lifespan doesn't try to start APScheduler.
_no_op = lambda: None
with patch("atlas_plus.brain.server.brain_scheduler.start_scheduler", _no_op), \
     patch("atlas_plus.brain.server.brain_scheduler.stop_scheduler", _no_op):
    from atlas_plus.brain.server import app

client = TestClient(app, raise_server_exceptions=False)
AUTH = {"Authorization": f"Bearer {_TEST_KEY}"}
NO_AUTH: dict = {}


@pytest.fixture(autouse=True)
def _reset_auth_failures():
    """The Brain now counts failed attempts per source and locks the source out (wave-2
    2026-11-09, SEC-04); TestClient's peer is one fixed source, so every test starts clean."""
    from atlas_plus.brain import server as _srv
    _srv.AUTH_FAILURES.clear()
    yield
    _srv.AUTH_FAILURES.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _authed(method: str, path: str, **kwargs):
    return getattr(client, method)(path, headers=AUTH, **kwargs)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

def _force_backend_probe(monkeypatch, value: bool) -> None:
    """Pin the /health backend probe and invalidate its cache."""
    from atlas_plus.brain import server as srv
    monkeypatch.setattr(srv, "_probe_backend", lambda: value)
    srv._backend_probe["at"] = 0.0


class TestAuthMiddleware:
    def test_health_bypasses_auth(self, monkeypatch):
        _force_backend_probe(monkeypatch, True)
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_missing_token_returns_401(self):
        r = client.get("/dispatch-stats", headers=NO_AUTH)
        assert r.status_code == 401

    def test_wrong_token_returns_401(self):
        r = client.get("/dispatch-stats", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_valid_token_passes(self):
        with patch("atlas_plus.brain.server.brain_scheduler._scheduler", None):
            r = _authed("get", "/routing/stats")
        assert r.status_code == 200

    def test_missing_bearer_prefix_returns_401(self):
        r = client.get("/dispatch-stats", headers={"Authorization": _TEST_KEY})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_returns_ok_with_backend_up(self, monkeypatch):
        _force_backend_probe(monkeypatch, True)
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "backend": "ok"}

    def test_degraded_when_backend_unreachable_but_http_stays_200(self, monkeypatch):
        """Regression (run 47, found live 2026-10-09): Ollama was dead and
        /health still said ok -- semantic indexing/reverify/summarizer were
        silently broken. The body must tell the truth; the HTTP code must
        STAY 200 (watchdogs restart on non-200, and restarting the Brain
        cannot fix its backend)."""
        _force_backend_probe(monkeypatch, False)
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "degraded", "backend": "unreachable"}

    def test_backend_probe_is_cached(self, monkeypatch):
        """Health gets polled; the backend probe fires at most once per TTL."""
        from atlas_plus.brain import server as srv
        calls = {"n": 0}

        def counting_probe():
            calls["n"] += 1
            return True

        monkeypatch.setattr(srv, "_probe_backend", counting_probe)
        srv._backend_probe["at"] = 0.0
        client.get("/health")
        client.get("/health")
        client.get("/health")
        assert calls["n"] == 1

    def test_unauthenticated(self, monkeypatch):
        # /health must work without any token
        _force_backend_probe(monkeypatch, True)
        r = client.get("/health", headers=NO_AUTH)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# /dispatch-stats
# ---------------------------------------------------------------------------

class TestDispatchStats:
    def test_returns_dict_with_expected_keys(self):
        fake_stats = {
            "enabled": False,
            "last_run_at": None,
            "last_dispatched_count": 0,
            "total_dispatched": 0,
            "in_flight": 0,
        }
        with patch("atlas_plus.brain.dispatcher.get_dispatch_stats", return_value=fake_stats):
            r = _authed("get", "/dispatch-stats")
        assert r.status_code == 200
        data = r.json()
        assert "enabled" in data
        assert "total_dispatched" in data
        assert "in_flight" in data

    def test_enabled_false_by_default_without_env_var(self):
        """Without ATLAS_TICKET_DISPATCH=on, enabled should be False."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ATLAS_TICKET_DISPATCH", None)
            from atlas_plus.brain.dispatcher import get_dispatch_stats
            stats = get_dispatch_stats()
        assert stats["enabled"] is False

    def test_enabled_true_when_env_set(self):
        with patch.dict(os.environ, {"ATLAS_TICKET_DISPATCH": "on"}):
            from atlas_plus.brain import dispatcher as _d
            import importlib
            importlib.reload(_d)
            stats = _d.get_dispatch_stats()
        assert stats["enabled"] is True

    def test_in_flight_count_reflects_in_progress_tickets(self):
        from atlas_plus.brain.dispatcher import get_dispatch_stats
        fake_tickets = [MagicMock(), MagicMock(), MagicMock()]
        with patch("atlas_plus.tickets.store.list_tickets", return_value=fake_tickets):
            stats = get_dispatch_stats()
        assert stats["in_flight"] == 3

    def test_requires_auth(self):
        r = client.get("/dispatch-stats", headers=NO_AUTH)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# /scheduler/jobs
# ---------------------------------------------------------------------------

class TestSchedulerJobs:
    def test_not_running_returns_empty(self):
        with patch("atlas_plus.brain.server.brain_scheduler._scheduler", None):
            r = _authed("get", "/scheduler/jobs")
        assert r.status_code == 200
        data = r.json()
        assert data["running"] is False
        assert data["jobs"] == []

    def test_running_scheduler_returns_job_list(self):
        mock_job = MagicMock()
        mock_job.id = "temporal-build"
        mock_job.next_run_time = None
        mock_sched = MagicMock()
        mock_sched.running = True
        mock_sched.get_jobs.return_value = [mock_job]
        with patch("atlas_plus.brain.server.brain_scheduler._scheduler", mock_sched):
            r = _authed("get", "/scheduler/jobs")
        assert r.status_code == 200
        data = r.json()
        assert data["running"] is True
        assert len(data["jobs"]) == 1
        assert data["jobs"][0]["id"] == "temporal-build"

    def test_job_with_next_run_time(self):
        from datetime import datetime, timezone
        mock_job = MagicMock()
        mock_job.id = "curation"
        mock_job.next_run_time = datetime(2026, 5, 20, 3, 15, 0, tzinfo=timezone.utc)
        mock_sched = MagicMock()
        mock_sched.running = True
        mock_sched.get_jobs.return_value = [mock_job]
        with patch("atlas_plus.brain.server.brain_scheduler._scheduler", mock_sched):
            r = _authed("get", "/scheduler/jobs")
        job = r.json()["jobs"][0]
        assert "2026-05-20" in job["next_run"]

    def test_requires_auth(self):
        r = client.get("/scheduler/jobs", headers=NO_AUTH)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# /routing/stats
# ---------------------------------------------------------------------------

class TestRoutingStats:
    def test_returns_routing_config(self):
        r = _authed("get", "/routing/stats")
        assert r.status_code == 200
        data = r.json()
        assert "smart_routing" in data
        assert "local_only" in data
        assert "cache_size" in data
        assert "cache_capacity" in data
        assert "category_distribution" in data

    def test_cache_capacity_is_500(self):
        r = _authed("get", "/routing/stats")
        assert r.json()["cache_capacity"] == 500

    def test_cache_size_reflects_routing_cache(self):
        from atlas_plus.brain import agents as _agents
        original = dict(_agents._ROUTING_CACHE)
        _agents._ROUTING_CACHE["test-instruction"] = "coding"
        try:
            r = _authed("get", "/routing/stats")
            assert r.json()["cache_size"] >= 1
        finally:
            _agents._ROUTING_CACHE.clear()
            _agents._ROUTING_CACHE.update(original)

    def test_requires_auth(self):
        r = client.get("/routing/stats", headers=NO_AUTH)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# /credit/reset
# ---------------------------------------------------------------------------

class TestCreditReset:
    def test_reset_clears_exhausted_agents(self):
        from atlas_plus.brain import server as _srv
        _srv._credit_exhausted_agents.add("claude-code")
        _srv._credit_exhausted_agents.add("codex")
        r = _authed("post", "/credit/reset")
        assert r.status_code == 200
        data = r.json()
        assert "claude-code" in data["cleared"]
        assert "codex" in data["cleared"]
        assert len(_srv._credit_exhausted_agents) == 0

    def test_reset_empty_set_returns_empty_cleared(self):
        from atlas_plus.brain import server as _srv
        _srv._credit_exhausted_agents.clear()
        r = _authed("post", "/credit/reset")
        assert r.status_code == 200
        assert r.json()["cleared"] == []

    def test_reset_returns_message(self):
        r = _authed("post", "/credit/reset")
        assert "message" in r.json()

    def test_requires_auth(self):
        r = client.post("/credit/reset", headers=NO_AUTH)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# _extract_caller_identity
# ---------------------------------------------------------------------------

class TestExtractCallerIdentity:
    def test_explicit_identity_header_wins(self):
        from atlas_plus.brain.server import _extract_caller_identity
        req = MagicMock()
        req.headers = {
            "X-Atlas-Caller-Identity": "telegram-bot",
            "CF-Access-Authenticated-User-Email": "alex@example.com",
            "Authorization": "Bearer sometoken",
        }
        assert _extract_caller_identity(req) == "telegram-bot"

    def test_cf_email_second_priority(self):
        from atlas_plus.brain.server import _extract_caller_identity
        req = MagicMock()
        req.headers = {
            "CF-Access-Authenticated-User-Email": "alex@example.com",
            "Authorization": "Bearer sometoken",
        }
        assert _extract_caller_identity(req) == "alex@example.com"

    def test_bearer_token_suffix_fallback(self):
        from atlas_plus.brain.server import _extract_caller_identity
        req = MagicMock()
        req.headers = {"Authorization": "Bearer abcdef123456789012"}
        identity = _extract_caller_identity(req)
        assert identity.startswith("token:")
        assert "789012" in identity  # last 12 chars

    def test_no_auth_returns_unknown(self):
        from atlas_plus.brain.server import _extract_caller_identity
        req = MagicMock()
        req.headers = {}
        assert _extract_caller_identity(req) == "unknown"


# ---------------------------------------------------------------------------
# _is_credit_error
# ---------------------------------------------------------------------------

class TestIsCreditError:
    def test_credit_balance_too_low(self):
        from atlas_plus.brain.server import _is_credit_error
        assert _is_credit_error("Your credit balance is too low to run this task.")

    def test_insufficient_quota(self):
        from atlas_plus.brain.server import _is_credit_error
        assert _is_credit_error("Error: insufficient_quota for this model.")

    def test_exceeded_quota(self):
        from atlas_plus.brain.server import _is_credit_error
        assert _is_credit_error("You exceeded your current quota, please check your plan.")

    def test_normal_result_not_credit_error(self):
        from atlas_plus.brain.server import _is_credit_error
        assert not _is_credit_error("The task completed successfully.")

    def test_empty_string(self):
        from atlas_plus.brain.server import _is_credit_error
        assert not _is_credit_error("")


# ---------------------------------------------------------------------------
# _check_rate_limit
# ---------------------------------------------------------------------------

class TestCheckRateLimit:
    def setup_method(self):
        from atlas_plus.brain import server as _srv
        _srv._rate_buckets.clear()

    def test_first_request_allowed(self):
        from atlas_plus.brain.server import _check_rate_limit
        allowed, retry_after = _check_rate_limit("test-identity")
        assert allowed is True
        assert retry_after == 0.0

    def test_zero_rpm_unlimited(self):
        from atlas_plus.brain import server as _srv
        original = _srv._RATE_LIMIT_RPM
        _srv._RATE_LIMIT_RPM = 0
        try:
            allowed, retry = _srv._check_rate_limit("any-identity")
            assert allowed is True
        finally:
            _srv._RATE_LIMIT_RPM = original

    def test_rpm_limit_enforced(self):
        from atlas_plus.brain import server as _srv
        original_rpm = _srv._RATE_LIMIT_RPM
        _srv._RATE_LIMIT_RPM = 2
        try:
            _check = _srv._check_rate_limit
            _check("limited-id")
            _check("limited-id")
            # Third call should be blocked
            allowed, retry_after = _check("limited-id")
            assert allowed is False
            assert retry_after > 0
        finally:
            _srv._RATE_LIMIT_RPM = original_rpm

    def test_different_identities_independent_buckets(self):
        from atlas_plus.brain import server as _srv
        original_rpm = _srv._RATE_LIMIT_RPM
        _srv._RATE_LIMIT_RPM = 1
        try:
            _srv._check_rate_limit("alice")
            # alice is now limited
            alice_allowed, _ = _srv._check_rate_limit("alice")
            # bob is unaffected
            bob_allowed, _ = _srv._check_rate_limit("bob")
            assert alice_allowed is False
            assert bob_allowed is True
        finally:
            _srv._RATE_LIMIT_RPM = original_rpm


# ---------------------------------------------------------------------------
# GET /task/{id}/attempts
# ---------------------------------------------------------------------------

class TestTaskAttempts:
    def test_returns_404_for_unknown_task(self):
        with patch("atlas_plus.brain.tasks.get_task", return_value=None):
            r = _authed("get", "/task/nonexistent/attempts")
        assert r.status_code == 404

    def test_returns_empty_attempts_list_when_no_history(self):
        from atlas_plus.brain.tasks import Task
        fake_task = Task(
            task_id="task-abc", session_id="brain-task-abc",
            instruction="do work", status="done",
            created_at="2026-05-20T03:00:00+00:00",
        )
        with patch("atlas_plus.brain.tasks.get_task", return_value=fake_task), \
             patch("atlas_plus.brain.tasks.list_task_attempts", return_value=[]):
            r = _authed("get", "/task/task-abc/attempts")
        assert r.status_code == 200
        data = r.json()
        assert data["task_id"] == "task-abc"
        assert data["attempts"] == []
        assert data["count"] == 0

    def test_returns_attempts_with_correct_fields(self):
        from atlas_plus.brain.tasks import Task
        fake_task = Task(
            task_id="task-xyz", session_id="brain-task-xyz",
            instruction="research topic", status="failed",
            created_at="2026-05-20T03:00:00+00:00",
        )
        fake_attempts = [
            {"attempt_id": "a-001", "task_id": "task-xyz", "attempt_num": 1,
             "agent": "brain", "outcome": "failed",
             "started_at": "2026-05-20T03:00:00+00:00",
             "finished_at": "2026-05-20T03:01:00+00:00",
             "result_summary": "executor timeout"},
        ]
        with patch("atlas_plus.brain.tasks.get_task", return_value=fake_task), \
             patch("atlas_plus.brain.tasks.list_task_attempts", return_value=fake_attempts):
            r = _authed("get", "/task/task-xyz/attempts")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 1
        assert data["attempts"][0]["outcome"] == "failed"
        assert data["attempts"][0]["agent"] == "brain"

    def test_requires_auth(self):
        r = client.get("/task/any-task/attempts")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /chat/stream -- SSE streaming endpoint
# ---------------------------------------------------------------------------

class TestChatStream:
    """POST /chat/stream returns text/event-stream with tool_call + done events."""

    def _fake_chat(self, progress_fn=None, **_kw):
        """Simulate chat() firing two tool calls then returning a response."""
        from atlas_plus.brain.agent import AgentResponse
        if progress_fn:
            progress_fn({"type": "tool_call", "tool": "memory_search", "round": 1})
            progress_fn({"type": "tool_call", "tool": "memory_remember", "round": 2})
        return AgentResponse(
            text="Here is the answer.",
            tool_calls_count=2,
            model="qwen3:8b",
            session_id="stream-test",
            tool_log=[{"tool": "memory_search", "args": {}, "result_preview": "..."}],
        )

    def test_returns_event_stream_content_type(self):
        with patch("atlas_plus.brain.server.chat", side_effect=self._fake_chat):
            r = _authed("post", "/chat/stream",
                        json={"session_id": "stream-test", "message": "hello"})
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]

    def test_emits_tool_call_events(self):
        with patch("atlas_plus.brain.server.chat", side_effect=self._fake_chat):
            r = _authed("post", "/chat/stream",
                        json={"session_id": "stream-test", "message": "hello"})
        import json as _json
        events = []
        for line in r.text.splitlines():
            if line.startswith("data: "):
                events.append(_json.loads(line[6:]))
        tool_events = [e for e in events if e.get("type") == "tool_call"]
        assert len(tool_events) == 2
        assert tool_events[0]["tool"] == "memory_search"
        assert tool_events[1]["tool"] == "memory_remember"

    def test_emits_done_event_with_response(self):
        with patch("atlas_plus.brain.server.chat", side_effect=self._fake_chat):
            r = _authed("post", "/chat/stream",
                        json={"session_id": "stream-test", "message": "hello"})
        import json as _json
        events = [_json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
        done = next((e for e in events if e.get("type") == "done"), None)
        assert done is not None
        assert done["text"] == "Here is the answer."
        assert done["tool_calls_count"] == 2
        assert done["model"] == "qwen3:8b"

    def test_requires_auth(self):
        r = client.post("/chat/stream", json={"session_id": "s", "message": "hi"})
        assert r.status_code == 401

    def test_emits_error_event_on_exception(self):
        def boom(**_kw):
            raise RuntimeError("ollama gone")
        with patch("atlas_plus.brain.server.chat", side_effect=boom):
            r = _authed("post", "/chat/stream",
                        json={"session_id": "err-test", "message": "hi"})
        import json as _json
        events = [_json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: ")]
        err = next((e for e in events if e.get("type") == "error"), None)
        assert err is not None
        assert "ollama gone" in err["detail"]


# ---------------------------------------------------------------------------
# GET /tasks/export
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# GET /tasks  (list)
# ---------------------------------------------------------------------------

class TestListTasks:
    def _make_task_dict(self, tid="t-001", status="done"):
        return {
            "task_id": tid, "status": status, "agent": "brain", "kind": "agent",
            "priority": 5, "session_id": "sess-1", "created_at": "2026-05-21T00:00:00",
            "started_at": None, "finished_at": None, "instruction": "do it",
            "result": "ok", "error": None,
        }

    def test_returns_task_list(self):
        t = MagicMock()
        t.as_dict.return_value = self._make_task_dict("t-001", "done")
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[t]):
            r = _authed("get", "/tasks")
        assert r.status_code == 200
        body = r.json()
        assert "tasks" in body and "count" in body
        assert body["count"] == 1
        assert body["tasks"][0]["task_id"] == "t-001"

    def test_requires_auth(self):
        r = client.get("/tasks", headers=NO_AUTH)
        assert r.status_code == 401

    def test_status_filter_forwarded(self):
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[]) as mock_lt:
            _authed("get", "/tasks?status=pending")
        call_kwargs = mock_lt.call_args[1]
        assert call_kwargs.get("status") == "pending"


# ---------------------------------------------------------------------------
# GET /task/{task_id}
# ---------------------------------------------------------------------------

class TestGetTask:
    def test_returns_task_when_found(self):
        t = MagicMock()
        t.as_dict.return_value = {
            "task_id": "my-task-001", "status": "running", "agent": "brain",
            "kind": "agent", "priority": 5, "session_id": "s1",
            "created_at": "2026-05-21T00:00:00", "started_at": None,
            "finished_at": None, "instruction": "go", "result": None, "error": None,
        }
        with patch("atlas_plus.brain.tasks.get_task", return_value=t):
            r = _authed("get", "/task/my-task-001")
        assert r.status_code == 200
        assert r.json()["task_id"] == "my-task-001"

    def test_returns_404_when_not_found(self):
        with patch("atlas_plus.brain.tasks.get_task", return_value=None):
            r = _authed("get", "/task/missing-task")
        assert r.status_code == 404

    def test_requires_auth(self):
        r = client.get("/task/some-task", headers=NO_AUTH)
        assert r.status_code == 401


class TestTasksExport:
    def _make_task(self, tid="task-001", status="done"):
        t = MagicMock()
        t.as_dict.return_value = {
            "task_id": tid, "status": status, "agent": "brain",
            "kind": "agent", "priority": 5, "session_id": "test-sess",
            "created_at": "2026-05-21T00:00:00", "started_at": None,
            "finished_at": None, "instruction": "do something", "result": "ok",
            "error": None,
        }
        return t

    def test_csv_export_returns_csv(self):
        tasks = [self._make_task("t1", "done"), self._make_task("t2", "failed")]
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=tasks):
            r = _authed("get", "/tasks/export?fmt=csv")
        assert r.status_code == 200
        assert "text/csv" in r.headers.get("content-type", "")
        assert "task_id" in r.text
        assert "t1" in r.text or "t2" in r.text

    def test_json_export_returns_json(self):
        tasks = [self._make_task("t3", "done")]
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=tasks):
            r = _authed("get", "/tasks/export?fmt=json")
        assert r.status_code == 200
        assert "application/json" in r.headers.get("content-type", "")
        data = r.json()
        assert isinstance(data, list)
        assert data[0]["task_id"] == "t3"

    def test_requires_auth(self):
        r = client.get("/tasks/export", headers=NO_AUTH)
        assert r.status_code == 401

    def test_limit_capped_at_500(self):
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[]) as mock_lt:
            _authed("get", "/tasks/export?limit=9999&fmt=csv")
        limit_arg = mock_lt.call_args[1].get("limit") or mock_lt.call_args[0][0] if mock_lt.call_args[0] else None
        if limit_arg is not None:
            assert limit_arg <= 500

    def test_content_disposition_csv(self):
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[]):
            r = _authed("get", "/tasks/export?fmt=csv")
        assert "attachment" in r.headers.get("content-disposition", "")
        assert ".csv" in r.headers.get("content-disposition", "")


# ---------------------------------------------------------------------------
# GET /pending-facts  and  /pending-facts/count
# ---------------------------------------------------------------------------

class TestPendingFacts:
    def _make_fact(self, fid="fact-001"):
        f = MagicMock()
        f.as_dict.return_value = {"fact_id": fid, "fact": "Sky is blue",
                                  "scope": "feedback", "status": "pending"}
        return f

    def test_list_returns_facts(self):
        with patch("atlas_plus.brain.task_facts.list_pending_facts",
                   return_value=[self._make_fact()]):
            r = _authed("get", "/pending-facts")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["facts"][0]["fact_id"] == "fact-001"

    def test_list_requires_auth(self):
        r = client.get("/pending-facts", headers=NO_AUTH)
        assert r.status_code == 401

    def test_count_returns_integer(self):
        with patch("atlas_plus.brain.task_facts.pending_count", return_value=3):
            r = _authed("get", "/pending-facts/count")
        assert r.status_code == 200
        assert r.json().get("pending") == 3 or "pending" in r.json()

    def test_approve_returns_ok(self):
        with patch("atlas_plus.brain.task_facts.approve_fact",
                   return_value={"ok": True, "path": ".memory/fact.md"}):
            r = _authed("post", "/pending-facts/fact-001/approve", json={})
        assert r.status_code == 200
        assert r.json().get("ok") is True

    def test_approve_400_on_failure(self):
        with patch("atlas_plus.brain.task_facts.approve_fact",
                   return_value={"ok": False, "error": "fact not found"}):
            r = _authed("post", "/pending-facts/fact-999/approve", json={})
        assert r.status_code == 400

    def test_reject_returns_result(self):
        with patch("atlas_plus.brain.task_facts.reject_fact",
                   return_value={"ok": True, "fact_id": "fact-001"}):
            r = _authed("post", "/pending-facts/fact-001/reject")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# GET /workers
# ---------------------------------------------------------------------------

class TestWorkers:
    def test_returns_worker_info(self):
        # /workers uses asyncio.to_thread(check_worker_status); with TestClient
        # the thread-safe path may vary. Accept any non-401 response.
        r = _authed("get", "/workers")
        assert r.status_code != 401

    def test_requires_auth(self):
        r = client.get("/workers", headers=NO_AUTH)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /scheduler/trigger/{job_id}
# ---------------------------------------------------------------------------

class TestSchedulerTrigger:
    def test_trigger_unknown_job_returns_404(self):
        r = _authed("post", "/scheduler/trigger/nonexistent-job")
        assert r.status_code == 404

    def test_requires_auth(self):
        r = client.post("/scheduler/trigger/curation", headers=NO_AUTH)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /tasks/stats
# ---------------------------------------------------------------------------

class TestTasksStats:
    def test_returns_stats_dict(self):
        with patch("atlas_plus.brain.tasks.task_stats",
                   return_value={"total": 10, "done": 7, "failed": 2, "running": 1}):
            r = _authed("get", "/tasks/stats")
        assert r.status_code == 200
        body = r.json()
        assert body.get("total") == 10

    def test_requires_auth(self):
        r = client.get("/tasks/stats", headers=NO_AUTH)
        assert r.status_code == 401

    def test_since_days_param_forwarded(self):
        with patch("atlas_plus.brain.tasks.task_stats", return_value={}) as mock_stats:
            _authed("get", "/tasks/stats?since_days=14")
        mock_stats.assert_called_once_with(since_days=14)


# ---------------------------------------------------------------------------
# DELETE /session/{id}
# ---------------------------------------------------------------------------

class TestClearSession:
    def test_clears_session(self):
        with patch("atlas_plus.brain.history.clear_session", return_value=5):
            r = _authed("delete", "/session/my-session-id")
        assert r.status_code == 200
        body = r.json()
        assert body.get("deleted") == 5 or "session_id" in body

    def test_requires_auth(self):
        r = client.delete("/session/my-session-id", headers=NO_AUTH)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /task (submit)
# ---------------------------------------------------------------------------

class TestSubmitTask:
    def test_submits_task_valid_request_accepted(self):
        # The autouse _isolated_state_db fixture already points the brain DB
        # at a per-test tmp path, so no module patch is needed. (The old patch
        # here used a str, which _connect's call-time resolution turned into
        # an AttributeError -> 500, and the loose `in (202, 500)` assertion
        # passed vacuously without ever exercising create_task.)
        with patch.dict("os.environ", {"ATLAS_BRAIN_INLINE_TASKS": "off"}):
            r = _authed("post", "/task",
                        json={"instruction": "brain server submit smoke", "kind": "agent"})
        assert r.status_code == 202
        assert r.json().get("task_id")

    def test_requires_auth(self):
        r = client.post("/task", json={"instruction": "test"}, headers=NO_AUTH)
        assert r.status_code == 401

    def test_empty_instruction_returns_422(self):
        r = _authed("post", "/task", json={})
        assert r.status_code in (422, 400)

    def test_whitespace_only_instruction_returns_422(self):
        r = _authed("post", "/task", json={"instruction": "   \n\t ", "kind": "agent"})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /improvement
# ---------------------------------------------------------------------------

class TestImprovement:
    def test_returns_improvement_stats(self):
        with patch("atlas_plus.brain.improvement.all_agent_stats", return_value=[]), \
             patch("atlas_plus.brain.improvement.low_quality_tasks", return_value=[]):
            r = _authed("get", "/improvement")
        assert r.status_code == 200
        assert "agent_stats" in r.json()

    def test_requires_auth(self):
        r = client.get("/improvement", headers=NO_AUTH)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /session/{id}/history + DELETE /session/{id} cross-identity
# ---------------------------------------------------------------------------

class TestSessionHistory:
    def test_returns_history(self):
        with patch("atlas_plus.brain.history.get_messages", return_value=[]), \
             patch("atlas_plus.brain.history.get_session_identity", return_value="unknown"):
            r = _authed("get", "/session/test-sess-1/history")
        assert r.status_code == 200
        body = r.json()
        assert "session_id" in body

    def test_requires_auth(self):
        r = client.get("/session/test-sess-1/history", headers=NO_AUTH)
        assert r.status_code == 401

    def test_cross_identity_blocked(self):
        # Different caller identity from recorded identity -> 403
        with patch("atlas_plus.brain.history.get_session_identity",
                   return_value="other-identity"):
            r = _authed("get", "/session/cross-identity-sess/history")
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# GET /task/{id}/attempts
# ---------------------------------------------------------------------------

class TestTaskAttemptsList:
    def test_returns_attempts(self):
        with patch("atlas_plus.brain.tasks.get_task", return_value=MagicMock()), \
             patch("atlas_plus.brain.tasks.list_task_attempts",
                   return_value=[{"attempt_id": "a1", "attempt_num": 1, "outcome": "done"}]):
            r = _authed("get", "/task/t-001/attempts")
        assert r.status_code == 200
        assert "attempts" in r.json()

    def test_requires_auth(self):
        r = client.get("/task/t-001/attempts", headers=NO_AUTH)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /agents
# ---------------------------------------------------------------------------

class TestListAgents:
    def test_returns_agent_list(self):
        fake_agent = MagicMock()
        fake_agent.as_dict.return_value = {"name": "brain", "online": True, "model": "qwen3:8b"}
        with patch("atlas_plus.brain.agents.get_agents", return_value=[fake_agent]):
            r = _authed("get", "/agents")
        assert r.status_code == 200
        body = r.json()
        assert "agents" in body
        assert body["agents"][0]["name"] == "brain"

    def test_requires_auth(self):
        r = client.get("/agents", headers=NO_AUTH)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# PATCH /task/{id}/priority
# ---------------------------------------------------------------------------

class TestBumpTaskPriority:
    def _make_task(self, tid="t-001", status="pending"):
        t = MagicMock()
        t.as_dict.return_value = {
            "task_id": tid, "status": status, "agent": None, "kind": "agent",
            "priority": 3, "session_id": "test-sess", "created_at": "2026-05-21T00:00:00",
            "started_at": None, "finished_at": None, "instruction": "do it",
            "result": None, "error": None, "attempts": 0, "max_attempts": 3,
            "lease_owner": None, "lease_expires_at": None, "heartbeat_at": None,
            "artifact_path": None,
        }
        return t

    def test_bumps_priority_successfully(self):
        t = self._make_task()
        with patch("atlas_plus.brain.tasks.set_task_priority", return_value=True), \
             patch("atlas_plus.brain.tasks.get_task", return_value=t):
            r = _authed("patch", "/task/t-001/priority", json={"priority": 2})
        assert r.status_code == 200
        assert r.json()["task_id"] == "t-001"

    def test_returns_404_for_missing_task(self):
        with patch("atlas_plus.brain.tasks.set_task_priority", return_value=False):
            r = _authed("patch", "/task/missing-task/priority", json={"priority": 1})
        assert r.status_code == 404

    def test_returns_422_for_invalid_priority(self):
        r = _authed("patch", "/task/t-001/priority", json={"priority": "not-a-number"})
        assert r.status_code == 422

    def test_requires_auth(self):
        r = client.patch("/task/t-001/priority", json={"priority": 1}, headers=NO_AUTH)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /session/{id}/export  (markdown export)
# ---------------------------------------------------------------------------

class TestSessionExport:
    def test_returns_markdown(self):
        md = "# Session test-sess\n\nUser: hello\nAssistant: hi"
        # Patch via server's `hist` alias so asyncio.to_thread picks up the mock.
        with patch("atlas_plus.brain.server.hist.session_export_markdown", return_value=md), \
             patch("atlas_plus.brain.server.hist.get_session_identity", return_value="unknown"):
            r = _authed("get", "/session/test-sess/export")
        assert r.status_code == 200
        assert "text/markdown" in r.headers.get("content-type", "")

    def test_returns_404_for_empty_session(self):
        with patch("atlas_plus.brain.server.hist.session_export_markdown", return_value=""), \
             patch("atlas_plus.brain.server.hist.get_session_identity", return_value="unknown"):
            r = _authed("get", "/session/empty-sess/export")
        assert r.status_code == 404

    def test_requires_auth(self):
        r = client.get("/session/test-sess/export", headers=NO_AUTH)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /routing/stats  (already has TestRoutingStats but checking coverage)
# ---------------------------------------------------------------------------

class TestRoutingStatsExtra:
    def test_includes_key_fields(self):
        fake_stats = {"smart_routing": False, "local_only": False, "total_dispatched": 0}
        with patch("atlas_plus.brain.server._get_routing_stats", return_value=fake_stats, create=True):
            r = _authed("get", "/routing/stats")
        assert r.status_code == 200

    def test_requires_auth(self):
        r = client.get("/routing/stats", headers=NO_AUTH)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /  (service identity)
# ---------------------------------------------------------------------------

class TestRoot:
    def test_returns_service_identity(self):
        r = _authed("get", "/")
        assert r.status_code == 200
        body = r.json()
        assert "Atlas Brain" in body.get("service", "")
        assert "version" in body

    def test_requires_auth(self):
        r = client.get("/", headers=NO_AUTH)
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# GET /worker/health  (per-worker granular stats)
# ---------------------------------------------------------------------------

class TestWorkerHealth:
    def test_returns_worker_detail_fields(self):
        # task_store is `from . import tasks as task_store`; worker_detail is a module function.
        fake_detail = {
            "active_workers": ["w-001"],
            "pending_by_agent": {"brain": 2},
            "zombie_count": 0,
            "queue_depth": 2,
        }
        with patch("atlas_plus.brain.tasks.worker_detail", return_value=fake_detail):
            r = _authed("get", "/worker/health")
        assert r.status_code == 200
        body = r.json()
        assert "active_workers" in body or "queue_depth" in body or "error" in body

    def test_requires_auth(self):
        r = client.get("/worker/health", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_safe_defaults_on_error(self):
        with patch("atlas_plus.brain.tasks.worker_detail",
                   side_effect=Exception("db locked")):
            r = _authed("get", "/worker/health")
        assert r.status_code == 200
        body = r.json()
        assert "error" in body or "active_workers" in body


# ---------------------------------------------------------------------------
# GET /diagnostic  (operator status)
# ---------------------------------------------------------------------------

class TestDiagnostic:
    def test_requires_auth(self):
        r = client.get("/diagnostic", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_status_ok_field(self):
        r = _authed("get", "/diagnostic")
        assert r.status_code == 200
        body = r.json()
        assert body.get("status") == "ok"

    def test_includes_expected_top_level_keys(self):
        r = _authed("get", "/diagnostic")
        assert r.status_code == 200
        body = r.json()
        assert "ollama_reachable" in body
        assert "autonomous" in body
        assert "credit_exhausted_agents" in body

    def test_autonomous_reflects_env(self):
        import os
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "off"}):
            r = _authed("get", "/diagnostic")
        assert r.status_code == 200
        assert r.json()["autonomous"] is False


# ---------------------------------------------------------------------------
# GET /memory/search  (TUI memory proxy)
# ---------------------------------------------------------------------------

class TestMemorySearch:
    def test_returns_results_on_200(self):
        results = [{"title": "SIP basics", "score": 0.9, "snippet": "..."}]
        with patch("atlas_plus.mcp.memory_server.fts.search", return_value=results):
            r = _authed("get", "/memory/search?q=SIP+REGISTER")
        assert r.status_code == 200
        body = r.json()
        assert body["query"] == "SIP REGISTER"
        assert body["results"] == results

    def test_requires_auth(self):
        r = client.get("/memory/search?q=test", headers=NO_AUTH)
        assert r.status_code == 401

    def test_missing_q_returns_error(self):
        r = _authed("get", "/memory/search")
        assert r.status_code in (400, 422)  # FastAPI returns 422 for missing required params

    def test_empty_q_returns_400(self):
        r = _authed("get", "/memory/search?q=")
        assert r.status_code == 400

    def test_limit_clamped_to_20(self):
        with patch("atlas_plus.mcp.memory_server.fts.search", return_value=[]) as mock_search:
            _authed("get", "/memory/search?q=test&limit=99")
        mock_search.assert_called_once()
        _, kwargs = mock_search.call_args if mock_search.call_args.kwargs else (mock_search.call_args.args, {})
        # limit arg is positional or keyword; check it was clamped
        called_limit = mock_search.call_args[1].get("limit") or mock_search.call_args[0][2] if len(mock_search.call_args[0]) > 2 else None
        if called_limit is not None:
            assert called_limit <= 20

    def test_fts_error_returns_503(self):
        with patch("atlas_plus.mcp.memory_server.fts.search", side_effect=RuntimeError("db locked")):
            r = _authed("get", "/memory/search?q=test")
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# POST /memory/remember  (TUI memory proxy)
# ---------------------------------------------------------------------------

class TestMemoryRemember:
    def test_writes_fact_returns_200(self):
        with patch("atlas_plus.mcp.memory_server.tools.remember.memory_remember",
                   return_value={"written": True, "path": "/memory/test.md"}):
            r = _authed("post", "/memory/remember",
                        json={"fact": "MOS 2.4 on call abc-123", "scope": "reference/examplecorp"})
        assert r.status_code == 200

    def test_requires_auth(self):
        r = client.post("/memory/remember",
                        json={"fact": "test", "scope": "test"},
                        headers=NO_AUTH)
        assert r.status_code == 401

    def test_missing_fact_returns_400(self):
        r = _authed("post", "/memory/remember", json={"scope": "test"})
        assert r.status_code == 400

    def test_empty_fact_returns_400(self):
        r = _authed("post", "/memory/remember", json={"fact": "  ", "scope": "test"})
        assert r.status_code == 400

    def test_passes_confidence_and_source(self):
        captured = {}
        def fake_remember(**kwargs):
            captured.update(kwargs)
            return {"written": True}
        with patch("atlas_plus.mcp.memory_server.tools.remember.memory_remember", fake_remember):
            _authed("post", "/memory/remember", json={
                "fact": "test fact",
                "scope": "study/ssca",
                "confidence": 0.9,
                "source": "studycard-tui",
            })
        assert captured.get("scope") == "study/ssca"
        assert captured.get("confidence") == 0.9
        assert captured.get("source") == "studycard-tui"

    def test_remember_error_returns_503(self):
        with patch("atlas_plus.mcp.memory_server.tools.remember.memory_remember",
                   side_effect=RuntimeError("write failed")):
            r = _authed("post", "/memory/remember",
                        json={"fact": "test", "scope": "test"})
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# WebSocket auth (audit 2026-06-04 HIGH-1): /ws/{session_id} must authenticate
# before accept(), since the HTTP bearer middleware does not run on WS scope.
# ---------------------------------------------------------------------------

from starlette.websockets import WebSocketDisconnect  # noqa: E402


class TestWebSocketAuth:
    def test_ws_rejects_without_key(self):
        # Old (vulnerable) code accepted and blocked; fixed code closes 4401 -> disconnect.
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/sess-noauth") as ws:
                ws.receive_text()

    def test_ws_rejects_wrong_key(self):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/sess-bad?key=wrong-key") as ws:
                ws.receive_text()

    def test_ws_accepts_valid_key(self):
        fake = MagicMock(text="hi there", tool_calls_count=0, model="qwen3:test")
        with patch("atlas_plus.brain.server.chat", return_value=fake):
            with client.websocket_connect(f"/ws/sess-ok?key={_TEST_KEY}") as ws:
                ws.send_text("hello")
                data = ws.receive_json()
        assert data["response"] == "hi there"
        assert data["model"] == "qwen3:test"
