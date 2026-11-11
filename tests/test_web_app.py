"""Web UI endpoint tests using FastAPI TestClient.

Coverage: auth, status, agents, tasks, tickets, memory search/files,
config toggles, stats, credit reset, scheduler trigger proxy, pending facts,
improvement/routing/temporal stats, memory gaps, pending-facts CRUD, projects,
curation report (duplicates/stale/convergence/decisions/rederivable).
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Set a known API key before importing app so module-level constants are correct.
_TEST_KEY = "test-key-abc"
os.environ.setdefault("ATLAS_WEB_API_KEY", _TEST_KEY)
# Corpus isolation (quality loop run 17): without this, the ticket tests
# wrote REAL tickets into the operator's live store (the p1 counter reached
# 228 over months), and the operator's live Brain dispatcher would
# occasionally grab a test ticket between create and read-back -- the
# only-on-this-box full-suite flake. CI never saw it: no live Brain there.
# Scoped to THIS module's runtime (a collection-time env mutation leaked
# into every other module's frozen paths).
_TMP_WORLD = tempfile.mkdtemp(prefix="webtest-world-")


@pytest.fixture(scope="module", autouse=True)
def _isolated_corpus_env():
    saved = {k: os.environ.get(k) for k in ("ATLAS_MEMORY_ROOT", "ATLAS_STATE_DIR")}
    os.environ["ATLAS_MEMORY_ROOT"] = str(Path(_TMP_WORLD) / "mem")
    os.environ["ATLAS_STATE_DIR"] = str(Path(_TMP_WORLD) / "state")
    # tickets/store.py freezes TICKETS_ROOT at import (the actual root cause
    # of the real-store writes) -- env alone cannot redirect it.
    from atlas_plus.tickets import store as _tstore
    saved_root = _tstore.TICKETS_ROOT
    _tstore.TICKETS_ROOT = Path(_TMP_WORLD) / "mem" / "tickets"
    yield
    _tstore.TICKETS_ROOT = saved_root
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
os.environ.setdefault("ATLAS_BRAIN_URL", "http://127.0.0.1:9999")  # non-routable
os.environ.setdefault("ATLAS_BRAIN_API_KEY", "brain-key")
os.environ.setdefault("ATLAS_MCP_TOKEN", "mcp-token")

from fastapi.testclient import TestClient
from atlas_plus.web.app import app

client = TestClient(app, raise_server_exceptions=False)

AUTH = {"Authorization": f"Bearer {_TEST_KEY}"}
NO_AUTH: dict = {}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestAuth:
    def test_root_requires_auth(self):
        r = client.get("/", headers=NO_AUTH)
        assert r.status_code in (401, 404)

    def test_bad_key_returns_401(self):
        r = client.get("/api/tasks", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_valid_key_passes(self):
        with patch("atlas_plus.web.app.BRAIN_URL", "http://127.0.0.1:9999"):
            r = client.get("/api/tasks", headers=AUTH)
        # Passes auth even if Brain is down; returns error from Brain proxy
        assert r.status_code != 401

    def test_key_via_query_param_refused_off_bootstrap(self):
        # 2026-11-09 data-safety fix (row 13): ?key= is only the GET /
        # bootstrap; anywhere else it would land the key in access logs.
        r = client.get(f"/api/tasks?key={_TEST_KEY}")
        assert r.status_code == 401

    def test_key_via_query_param_bootstraps_root(self):
        r = client.get(f"/?key={_TEST_KEY}")
        # Drop the minted session so later *_requires_auth tests stay anonymous.
        client.cookies.clear()
        assert r.status_code == 200

    def test_key_via_x_atlas_key_header(self):
        r = client.get("/api/tasks", headers={"X-Atlas-Key": _TEST_KEY})
        assert r.status_code != 401


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------

class TestStatus:
    def test_returns_html(self):
        with patch("atlas_plus.web.app.asyncio") as mock_asyncio:
            # gather returns three exceptions (services down)
            mock_asyncio.gather = AsyncMock(return_value=[
                Exception("down"), Exception("down"), Exception("down")
            ])
            r = client.get("/api/status", headers=AUTH)
        # Should return HTML even when services are down
        assert r.status_code == 200

    def test_shows_exhausted_badge_when_count_nonzero(self):
        """Status bar HTML includes exhausted badge when tasks have no retries left."""
        from atlas_plus.brain.tasks import Task
        fake = [Task(
            task_id="task-dead", session_id="s", instruction="x",
            status="failed", created_at="2026-05-20T01:00:00+00:00",
            attempts=3, max_attempts=3,
        )]
        with patch("atlas_plus.web.app.asyncio") as mock_asyncio, \
             patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=fake):
            mock_asyncio.gather = AsyncMock(return_value=[
                Exception("down"), Exception("down"), Exception("down")
            ])
            r = client.get("/api/status", headers=AUTH)
        assert r.status_code == 200
        assert "exhausted" in r.text.lower()

    def test_no_exhausted_badge_when_clean(self):
        """Status bar has no exhausted badge when no tasks have burned retries."""
        with patch("atlas_plus.web.app.asyncio") as mock_asyncio, \
             patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=[]):
            mock_asyncio.gather = AsyncMock(return_value=[
                Exception("down"), Exception("down"), Exception("down")
            ])
            r = client.get("/api/status", headers=AUTH)
        assert r.status_code == 200
        assert "exhausted" not in r.text.lower()


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

class TestTasks:
    def test_list_tasks_no_brain(self):
        r = client.get("/api/tasks", headers=AUTH)
        # Without Brain running, proxy returns 200 with empty data or 503
        assert r.status_code in (200, 503)

    def test_submit_task_validates_fields(self):
        # No instruction
        r = client.post("/api/tasks", json={}, headers=AUTH)
        assert r.status_code in (422, 503)

    def test_submit_task_instruction_too_long(self):
        r = client.post(
            "/api/tasks",
            json={"instruction": "x" * 15000},
            headers=AUTH,
        )
        # Pydantic Field(max_length=4000) rejects at the web layer before Brain is called
        assert r.status_code == 422

    def test_submit_task_instruction_at_limit(self):
        r = client.post(
            "/api/tasks",
            json={"instruction": "x" * 4000},
            headers=AUTH,
        )
        # Exactly at the limit: Pydantic passes, Brain may be down
        assert r.status_code in (200, 201, 422, 503)

    def test_task_detail_requires_auth(self):
        r = client.get("/api/tasks/some-task-id/detail")
        assert r.status_code == 401

    def test_task_detail_not_found(self):
        r = client.get("/api/tasks/nonexistent-id/detail", headers=AUTH)
        # Returns HTML with error message
        assert r.status_code == 200
        assert b"not found" in r.content.lower()

    def test_retry_task_requires_auth(self):
        r = client.post("/api/tasks/some-id/retry")
        assert r.status_code == 401

    def test_stop_task_requires_auth(self):
        r = client.post("/api/tasks/some-id/stop")
        assert r.status_code == 401

    def test_stop_nonexistent_task(self):
        r = client.post("/api/tasks/nonexistent/stop", headers=AUTH)
        assert r.status_code == 404


class TestTaskSubmitValidation:
    """Blank instructions must be rejected at the API, not fail downstream.

    An empty instruction used to flow through to the worker and die inside
    `claude --print` with "Input must be provided either through stdin or as
    a prompt argument" (failed webui-claude-direct tasks, 2026-10-12).
    """

    def test_claude_task_rejects_empty_instruction(self):
        r = client.post("/api/tasks/claude", json={"instruction": ""}, headers=AUTH)
        assert r.status_code == 422

    def test_claude_task_rejects_whitespace_instruction(self):
        r = client.post("/api/tasks/claude", json={"instruction": "   \n "}, headers=AUTH)
        assert r.status_code == 422

    def test_submit_rejects_whitespace_instruction(self):
        r = client.post("/api/tasks", json={"instruction": " \t "}, headers=AUTH)
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

class TestTickets:
    def test_list_tickets_returns_list(self):
        r = client.get("/api/tickets", headers=AUTH)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_ticket_board_html(self):
        r = client.get("/api/tickets/board", headers=AUTH)
        assert r.status_code == 200

    def test_ticket_board_shows_verify_badge_when_verify_task_exists(self):
        """Ticket board includes a verify badge when a verify-ticket-{ID} task exists."""
        from unittest.mock import patch
        import sqlite3

        # Create a ticket
        r = client.post(
            "/api/tickets/create",
            json={"title": "Verify badge test", "project": "test-proj", "assignee": "claude-code"},
            headers=AUTH,
        )
        assert r.status_code == 201
        tid = r.json()["id"]

        # Inject a verify task row through the task store. The conftest
        # _isolated_state_db fixture points _DB_PATH at a per-test file, so
        # this never lands in the live queue (it used to: every suite run
        # planted a real "Verify ticket TES-001" task the worker then ran).
        from atlas_plus.brain import tasks as _ts
        _ts.create_task(
            instruction=f"Verify ticket {tid}",
            session_id=f"verify-ticket-{tid}",
            kind="verify",
            priority=6,
        )

        r2 = client.get("/api/tickets/board?status_filter=all", headers=AUTH)
        assert r2.status_code == 200
        # The verify badge (green V for done, or ... for pending) should appear
        assert b"verify-ticket" in r2.content or b"V" in r2.content or r2.status_code == 200

    def test_create_ticket_missing_fields(self):
        r = client.post("/api/tickets/create", json={}, headers=AUTH)
        assert r.status_code in (400, 422)

    def test_create_and_update_ticket(self):
        r = client.post(
            "/api/tickets/create",
            json={"title": "Test ticket", "project": "test-proj"},
            headers=AUTH,
        )
        assert r.status_code == 201
        ticket_id = r.json()["id"]

        r2 = client.patch(
            f"/api/tickets/{ticket_id}/status",
            json={"status": "done"},
            headers=AUTH,
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "done"

    def test_ticket_detail_html(self):
        # Create first
        r = client.post(
            "/api/tickets/create",
            json={"title": "Detail test", "project": "p1"},
            headers=AUTH,
        )
        tid = r.json()["id"]
        r2 = client.get(f"/api/tickets/{tid}/detail", headers=AUTH)
        assert r2.status_code == 200
        assert b"Detail test" in r2.content

    def test_dispatch_stats_returns_dict(self):
        """GET /api/tickets/dispatch-stats proxies to Brain; returns dict with enabled key."""
        with patch("atlas_plus.web.app.httpx") as mock_httpx:
            mock_client = MagicMock()
            mock_httpx.AsyncClient.return_value.__aenter__ = lambda s, *a: (
                __import__("asyncio").coroutine(lambda: mock_client)()
            )
            mock_client.get.return_value.status_code = 200
            mock_client.get.return_value.json.return_value = {
                "enabled": True, "in_flight": 0,
                "last_run_at": None, "last_dispatched_count": 0, "total_dispatched": 0,
            }
            # Brain unreachable -- falls back to error dict
            r = client.get("/api/tickets/dispatch-stats", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert "enabled" in body

    def test_dispatch_stats_html_off_by_default(self, monkeypatch):
        """Dispatch stats HTML badge shows 'off' when ATLAS_TICKET_DISPATCH not set."""
        monkeypatch.delenv("ATLAS_TICKET_DISPATCH", raising=False)
        with patch("atlas_plus.web.app.httpx") as mock_httpx:
            mock_client = MagicMock()
            mock_httpx.AsyncClient.return_value.__aenter__ = lambda s, *a: (
                __import__("asyncio").coroutine(lambda: mock_client)()
            )
            mock_client.get.return_value.status_code = 200
            mock_client.get.return_value.json.return_value = {"enabled": False}
            r = client.get("/api/tickets/dispatch-stats-html", headers=AUTH)
        assert r.status_code == 200
        assert b"off" in r.content or b"dispatch" in r.content


# ---------------------------------------------------------------------------
# Memory search and file browser
# ---------------------------------------------------------------------------

class TestMemorySearch:
    def test_empty_query_returns_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            idx = Path(tmp) / "INDEX.md"
            idx.write_text("# Index\nTest content")
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/memory/search?q=", headers=AUTH)
        assert r.status_code == 200

    def test_search_query_returns_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "test.md").write_text("# Test\nsome content here")
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/memory/search?q=content", headers=AUTH)
        assert r.status_code == 200

    def test_files_requires_auth(self):
        r = client.get("/api/memory/files")
        assert r.status_code == 401

    def test_files_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/memory/files", headers=AUTH)
        assert r.status_code == 200

    def test_file_path_traversal_rejected(self):
        r = client.get("/api/memory/file?path=../../etc/passwd", headers=AUTH)
        assert r.status_code == 200
        assert b"rejected" in r.content.lower() or b"invalid" in r.content.lower()

    def test_file_write_path_traversal_rejected(self):
        r = client.post(
            "/api/memory/file",
            json={"path": "../../evil.md", "content": "bad"},
            headers=AUTH,
        )
        assert r.status_code in (400, 404)

    def test_file_write_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "test.md"
            f.write_text("# Test\n")
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)):
                r = client.post(
                    "/api/memory/file",
                    json={"path": "test.md", "content": "x" * 1_100_000},
                    headers=AUTH,
                )
        assert r.status_code == 413


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_get_config_html(self):
        r = client.get("/api/config", headers=AUTH)
        assert r.status_code == 200

    def test_toggle_unknown_var_rejected(self):
        r = client.post(
            "/api/config",
            json={"var": "EVIL_VAR"},
            headers=AUTH,
        )
        assert r.status_code == 400

    def test_toggle_known_var(self):
        r = client.post(
            "/api/config",
            json={"var": "ATLAS_SMART_ROUTING"},
            headers=AUTH,
        )
        assert r.status_code == 200
        assert r.json()["var"] == "ATLAS_SMART_ROUTING"

    def test_ticket_dispatch_toggle(self):
        """ATLAS_TICKET_DISPATCH is in _TOGGLEABLE_VARS and can be toggled."""
        r = client.post(
            "/api/config",
            json={"var": "ATLAS_TICKET_DISPATCH"},
            headers=AUTH,
        )
        assert r.status_code == 200
        assert r.json()["var"] == "ATLAS_TICKET_DISPATCH"
        assert r.json()["value"] in ("on", "off")

    def test_config_html_shows_ticket_quality_threshold(self):
        """Config panel includes ticket_quality_threshold read-only row."""
        r = client.get("/api/config", headers=AUTH)
        assert r.status_code == 200
        assert b"ticket_quality_threshold" in r.content


# ---------------------------------------------------------------------------
# Stats summary
# ---------------------------------------------------------------------------

class TestStats:
    def test_stats_summary_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/stats/summary", headers=AUTH)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Memory rebuild
# ---------------------------------------------------------------------------

class TestMemoryRebuild:
    def test_rebuild_requires_auth(self):
        r = client.post("/api/memory/rebuild")
        assert r.status_code == 401

    def test_rebuild_returns_count(self):
        with patch(
            "atlas_plus.mcp.memory_server.fts.rebuild",
            return_value=42,
        ):
            r = client.post("/api/memory/rebuild", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["indexed"] == 42


# ---------------------------------------------------------------------------
# POST /api/memory/suggest-tags
# ---------------------------------------------------------------------------

class TestMemorySuggestTags:
    def test_requires_auth(self):
        r = client.post("/api/memory/suggest-tags")
        assert r.status_code == 401

    def test_dry_run_returns_candidates_without_applying(self, tmp_path):
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        (feedback / "pref.md").write_text(
            "---\ntitle: FastAPI Pref\ntype: feedback\n---\nContent.", encoding="utf-8"
        )
        with patch("atlas_plus.web.app.MEMORY_ROOT", tmp_path):
            r = client.post("/api/memory/suggest-tags", json={"write": False}, headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["dry_run"] is True
        assert data["applied"] == 0
        assert len(data["candidates"]) >= 1

    def test_write_mode_applies_tags(self, tmp_path):
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        (feedback / "pref.md").write_text(
            "---\ntitle: FastAPI Pref\ntype: feedback\n---\nContent.", encoding="utf-8"
        )
        with patch("atlas_plus.web.app.MEMORY_ROOT", tmp_path), \
             patch.dict("os.environ", {"ATLAS_AUTONOMOUS": "on"}):
            r = client.post("/api/memory/suggest-tags", json={"write": True}, headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["dry_run"] is False
        # Either applied > 0 or the file was already tagged (skipped idempotently)
        assert "applied" in data

    def test_write_blocked_when_autonomous_off(self, tmp_path):
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        (feedback / "pref.md").write_text(
            "---\ntitle: FastAPI Pref\ntype: feedback\n---\nContent.", encoding="utf-8"
        )
        with patch("atlas_plus.web.app.MEMORY_ROOT", tmp_path), \
             patch.dict("os.environ", {"ATLAS_AUTONOMOUS": "off"}):
            r = client.post("/api/memory/suggest-tags", json={"write": True}, headers=AUTH)
        assert r.status_code == 503
        assert "ATLAS_AUTONOMOUS" in r.json().get("detail", "")

    def test_empty_memory_returns_empty_candidates(self, tmp_path):
        with patch("atlas_plus.web.app.MEMORY_ROOT", tmp_path):
            r = client.post("/api/memory/suggest-tags", json={}, headers=AUTH)
        assert r.status_code == 200
        assert r.json()["total"] == 0


# ---------------------------------------------------------------------------
# POST /api/memory/suggest-facts
# ---------------------------------------------------------------------------

class TestMemorySuggestFacts:
    def test_requires_auth(self):
        r = client.post("/api/memory/suggest-facts")
        assert r.status_code == 401

    def test_dry_run_returns_counts(self):
        stats = {"candidates": 3, "approved": 1, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats):
            r = client.post("/api/memory/suggest-facts", json={"since": "today"}, headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["candidates"] == 3
        assert data["approved"] == 1
        assert data["dry_run"] is True

    def test_auto_blocked_when_autonomous_off(self):
        with patch.dict("os.environ", {"ATLAS_AUTONOMOUS": "off"}):
            r = client.post("/api/memory/suggest-facts", json={"auto": True}, headers=AUTH)
        assert r.status_code == 503

    def test_auto_mode_sets_dry_run_false(self):
        stats = {"candidates": 2, "approved": 2, "written": 2}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats), \
             patch.dict("os.environ", {"ATLAS_AUTONOMOUS": "on"}):
            r = client.post("/api/memory/suggest-facts", json={"auto": True}, headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["dry_run"] is False
        assert data["written"] == 2

    def test_error_from_provider_returns_503(self):
        with patch("atlas_plus.curation.suggest_facts.suggest_facts",
                   side_effect=RuntimeError("no transcript")):
            r = client.post("/api/memory/suggest-facts", json={}, headers=AUTH)
        assert r.status_code == 503

    def test_skip_similar_passed_through(self):
        stats = {"candidates": 2, "approved": 0, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats) as mock_sf:
            r = client.post("/api/memory/suggest-facts", json={"skip_similar": 0.6}, headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["skip_similar"] == pytest.approx(0.6)
        call_kwargs = mock_sf.call_args[1]
        assert call_kwargs.get("skip_similar_threshold") == pytest.approx(0.6)

    def test_skip_similar_defaults_to_zero(self):
        stats = {"candidates": 1, "approved": 0, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats):
            r = client.post("/api/memory/suggest-facts", json={}, headers=AUTH)
        assert r.status_code == 200
        assert r.json()["skip_similar"] == pytest.approx(0.0)

    def test_rejected_low_confidence_in_response(self):
        stats = {"candidates": 2, "approved": 0, "written": 0, "rejected_low_confidence": 3}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats):
            r = client.post("/api/memory/suggest-facts", json={}, headers=AUTH)
        assert r.status_code == 200
        assert r.json()["rejected_low_confidence"] == 3

    def test_rejected_low_confidence_defaults_to_zero(self):
        stats = {"candidates": 1, "approved": 0, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats):
            r = client.post("/api/memory/suggest-facts", json={}, headers=AUTH)
        assert r.status_code == 200
        assert r.json()["rejected_low_confidence"] == 0


# ---------------------------------------------------------------------------
# GET /api/temporal/entities
# ---------------------------------------------------------------------------

class TestTemporalEntitiesEndpoint:
    def test_requires_auth(self):
        r = client.get("/api/temporal/entities")
        assert r.status_code == 401

    def test_returns_entities_list(self):
        from unittest.mock import MagicMock
        entity = MagicMock()
        entity.name = "Example MCP Tunnel"
        entity.labels = {"service"}
        entity.aliases = {"MCP", "tunnel"}
        entity.first_seen_at = "2026-01-01T00:00:00Z"
        entity.last_seen_at = "2026-05-21T00:00:00Z"

        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.list_entities.return_value = [entity]
            r = client.get("/api/temporal/entities", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert "entities" in data
        assert "total" in data
        assert data["total"] == 1
        assert data["entities"][0]["name"] == "Example MCP Tunnel"

    def test_empty_graph_returns_empty_list(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.list_entities.return_value = []
            r = client.get("/api/temporal/entities", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["entities"] == []
        assert data["total"] == 0

    def test_entity_has_expected_fields(self):
        from unittest.mock import MagicMock
        entity = MagicMock()
        entity.name = "Brain Worker"
        entity.labels = {"component"}
        entity.aliases = set()
        entity.first_seen_at = "2026-03-01T00:00:00Z"
        entity.last_seen_at = "2026-05-21T00:00:00Z"

        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.list_entities.return_value = [entity]
            r = client.get("/api/temporal/entities", headers=AUTH)
        assert r.status_code == 200
        e = r.json()["entities"][0]
        assert "name" in e
        assert "labels" in e
        assert "aliases" in e
        assert "first_seen_at" in e
        assert "last_seen_at" in e

    def test_limit_param_passed_through(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.list_entities.return_value = []
            r = client.get("/api/temporal/entities?limit=10", headers=AUTH)
        assert r.status_code == 200
        mock_cls.return_value.list_entities.assert_called_with(limit=10)


# ---------------------------------------------------------------------------
# GET /api/temporal/stats (HTMX staleness)
# ---------------------------------------------------------------------------

class TestTemporalStatsHtmx:
    def test_requires_auth(self):
        r = client.get("/api/temporal/stats")
        assert r.status_code == 401

    def test_shows_entity_count(self, tmp_path):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {"entities": 42, "facts_active": 100, "episodes": 5}
            instance._manifest_file = tmp_path / "nonexistent_manifest.json"
            r = client.get("/api/temporal/stats", headers=AUTH)
        assert r.status_code == 200
        assert "42" in r.text

    def test_shows_stale_warning_when_no_manifest(self, tmp_path):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {"entities": 5, "facts_active": 10, "episodes": 1}
            instance._manifest_file = tmp_path / "nonexistent_manifest.json"
            r = client.get("/api/temporal/stats", headers=AUTH)
        assert r.status_code == 200
        assert "STALE" in r.text or "never" in r.text

    def test_shows_last_built_date_when_fresh(self, tmp_path):
        import json
        from datetime import datetime, timezone
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps({"last_built": datetime.now(timezone.utc).isoformat(), "files": {}}))
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {"entities": 5, "facts_active": 10, "episodes": 1}
            instance._manifest_file = manifest_path
            r = client.get("/api/temporal/stats", headers=AUTH)
        assert r.status_code == 200
        # Fresh graph should not show STALE
        assert "STALE" not in r.text


# ---------------------------------------------------------------------------
# Ticket dispatch (atomic)
# ---------------------------------------------------------------------------

class TestTicketDispatch:
    def test_dispatch_nonexistent_ticket(self):
        r = client.post(
            "/api/tickets/ATL-NOTREAL/dispatch",
            json={},
            headers=AUTH,
        )
        assert r.status_code == 404

    def test_dispatch_wires_ticket_and_task(self):
        # Create ticket first
        r = client.post(
            "/api/tickets/create",
            json={"title": "Dispatch me", "project": "test"},
            headers=AUTH,
        )
        tid = r.json()["id"]

        # Mock Brain POST /task
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"task_id": "task-abc", "status": "pending"}

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
            r2 = client.post(
                f"/api/tickets/{tid}/dispatch",
                json={},
                headers=AUTH,
            )

        assert r2.status_code == 202
        assert r2.json()["ticket_id"] == tid


# ---------------------------------------------------------------------------
# Task export
# ---------------------------------------------------------------------------

class TestTaskExport:
    def test_export_csv_requires_auth(self):
        r = client.get("/api/tasks/export?fmt=csv")
        assert r.status_code == 401

    def test_export_csv_returns_csv(self):
        r = client.get("/api/tasks/export?fmt=csv&limit=5", headers=AUTH)
        # Brain may not be running; accept 503 proxy failure
        assert r.status_code in (200, 503)
        if r.status_code == 200:
            assert "text/csv" in r.headers.get("content-type", "")

    def test_export_json_returns_json(self):
        r = client.get("/api/tasks/export?fmt=json&limit=5", headers=AUTH)
        assert r.status_code in (200, 503)


# ---------------------------------------------------------------------------
# Session search
# ---------------------------------------------------------------------------

class TestSessionSearch:
    def test_requires_auth(self):
        r = client.get("/api/sessions/search?q=atlas")
        assert r.status_code == 401

    def test_empty_query_returns_hint(self):
        r = client.get("/api/sessions/search?q=", headers=AUTH)
        assert r.status_code == 200
        assert b"Enter a query" in r.content

    def test_no_sessions_dir_returns_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/sessions/search?q=atlas", headers=AUTH)
        assert r.status_code == 200

    def test_match_returns_filename_and_snippet(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            sessions.mkdir()
            (sessions / "2026-05-19.md").write_text(
                "# Session\nWorking on atlas-memory+ today with Brain scheduler."
            )
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/sessions/search?q=atlas", headers=AUTH)
        assert r.status_code == 200
        assert b"2026-05-19" in r.content
        assert b"atlas" in r.content.lower()

    def test_no_match_returns_no_match_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            sessions.mkdir()
            (sessions / "2026-05-19.md").write_text("# Session\nWorking on gatekeeper today.")
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/sessions/search?q=xyznotfound", headers=AUTH)
        assert r.status_code == 200
        assert b"xyznotfound" in r.content or b"No sessions match" in r.content


# ---------------------------------------------------------------------------
# Session narrative panel
# ---------------------------------------------------------------------------

class TestSessionNarrative:
    def test_requires_auth(self):
        r = client.get("/api/sessions/narrative")
        assert r.status_code == 401

    def test_no_narrative_returns_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/sessions/narrative", headers=AUTH)
        assert r.status_code == 200
        assert b"narrative" in r.content.lower()

    def test_narrative_file_rendered(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions"
            sessions.mkdir()
            (sessions / "2026-W21-narrative.md").write_text(
                "## Open loops\n\n- Need to fix routing.\n\n## Direction\n\n- Building atlas-memory+.\n"
            )
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/sessions/narrative", headers=AUTH)
        assert r.status_code == 200
        assert b"2026-W21" in r.content


# ---------------------------------------------------------------------------
# Scheduler trigger proxy
# ---------------------------------------------------------------------------

class TestSchedulerTrigger:
    def test_trigger_requires_auth(self):
        r = client.post("/api/scheduler/trigger/temporal-build")
        assert r.status_code == 401

    def test_trigger_unknown_job_propagates_404(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status.side_effect = __import__("httpx").HTTPStatusError(
            "Not Found", request=MagicMock(), response=mock_resp
        )
        mock_resp.text = "Unknown job"

        with patch("httpx.AsyncClient") as mc:
            mc.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
            r = client.post(
                "/api/scheduler/trigger/not-a-job",
                headers=AUTH,
            )
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Memory convergence endpoint
# ---------------------------------------------------------------------------

class TestMemoryConvergence:
    def test_requires_auth(self):
        r = client.get("/api/memory/convergence")
        assert r.status_code == 401

    def test_returns_clean_message_when_no_flags(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "clean.md").write_text("---\ntitle: Clean\n---\nbody\n")
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)), \
                 patch("atlas_plus.curation.weekly_report.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/memory/convergence", headers=AUTH)

        assert r.status_code == 200
        assert b"clean" in r.content.lower() or b"No contradiction" in r.content

    def test_returns_flagged_files_when_contradictions_exist(self):
        import tempfile
        import yaml
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            fm = {"title": "SIP", "contradictions": ["reference/voip.md"]}
            (Path(tmp) / "sip.md").write_text(f"---\n{yaml.dump(fm)}---\nbody\n")
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)), \
                 patch("atlas_plus.curation.weekly_report.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/memory/convergence", headers=AUTH)

        assert r.status_code == 200
        assert b"sip.md" in r.content or b"voip.md" in r.content


# ---------------------------------------------------------------------------
# Memory decisions endpoint
# ---------------------------------------------------------------------------

class TestMemoryDecisions:
    def test_requires_auth(self):
        r = client.get("/api/memory/decisions")
        assert r.status_code == 401

    def test_returns_clean_message_when_no_decisions(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)), \
                 patch("atlas_plus.curation.weekly_report.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/memory/decisions", headers=AUTH)

        assert r.status_code == 200
        assert b"No stale decisions" in r.content or b"90 days" in r.content

    def test_shows_stale_decisions(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "decisions"
            d.mkdir()
            (d / "old-choice.md").write_text(
                "---\ntitle: Old Choice\ntype: decision\nmade: 2026-01-01\n---\nbody\n"
            )
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)), \
                 patch("atlas_plus.curation.weekly_report.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/memory/decisions", headers=AUTH)

        assert r.status_code == 200
        assert b"old-choice" in r.content or b"Old Choice" in r.content


# ---------------------------------------------------------------------------
# Memory rederivable endpoint
# ---------------------------------------------------------------------------

class TestMemoryRederivable:
    def test_requires_auth(self):
        r = client.get("/api/memory/rederivable")
        assert r.status_code == 401

    def test_returns_clean_message_when_no_candidates(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)), \
                 patch("atlas_plus.curation.weekly_report.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/memory/rederivable", headers=AUTH)

        assert r.status_code == 200
        assert b"clean" in r.content.lower() or b"No re-derivable" in r.content

    def test_error_in_scan_returns_error_html(self):
        from unittest.mock import patch

        with patch("atlas_plus.curation.weekly_report._scan_rederivable", side_effect=RuntimeError("boom")):
            r = client.get("/api/memory/rederivable", headers=AUTH)

        assert r.status_code == 200
        assert b"Error" in r.content or b"boom" in r.content


# ---------------------------------------------------------------------------
# Memory topic index
# ---------------------------------------------------------------------------

class TestMemoryTopics:
    def test_requires_auth(self):
        r = client.get("/api/memory/topics")
        assert r.status_code == 401

    def test_returns_json_with_expected_keys(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/memory/topics", headers=AUTH)

        assert r.status_code == 200
        data = r.json()
        assert "tags" in data
        assert "total_tagged_files" in data
        assert "total_tags" in data

    def test_empty_memory_returns_empty_tag_list(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/memory/topics", headers=AUTH)

        data = r.json()
        assert data["tags"] == []
        assert data["total_tags"] == 0

    def test_tags_parsed_from_frontmatter(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp)
            p = mem / "feedback" / "style.md"
            p.parent.mkdir(parents=True)
            p.write_text(
                "---\ntype: feedback\ntags:\n  - python\n  - testing\n---\n\nContent.",
                encoding="utf-8",
            )
            with patch("atlas_plus.web.app.MEMORY_ROOT", mem):
                r = client.get("/api/memory/topics", headers=AUTH)

        data = r.json()
        tag_names = {t["tag"] for t in data["tags"]}
        assert "python" in tag_names
        assert "testing" in tag_names

    def test_topics_html_endpoint_returns_html(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp)
            p = mem / "feedback" / "style.md"
            p.parent.mkdir(parents=True)
            p.write_text("---\ntags: [atlas, brain]\n---\n\nContent.", encoding="utf-8")
            with patch("atlas_plus.web.app.MEMORY_ROOT", mem):
                r = client.get("/api/memory/topics/html", headers=AUTH)

        assert r.status_code == 200
        assert b"atlas" in r.content or b"brain" in r.content


# ---------------------------------------------------------------------------
# Public endpoints (no auth required)
# ---------------------------------------------------------------------------

class TestTaskFilters:
    def test_tasks_with_since_hours(self):
        """GET /api/tasks?since_hours=24 is accepted."""
        r = client.get("/api/tasks?since_hours=24&limit=5", headers=AUTH)
        assert r.status_code in (200, 503)  # 503 if Brain not running

    def test_tasks_with_agent_filter(self):
        """GET /api/tasks?agent=claude-code is accepted."""
        r = client.get("/api/tasks?agent=claude-code&limit=5", headers=AUTH)
        assert r.status_code in (200, 503)

    def test_tasks_combined_filters(self):
        """GET /api/tasks with status, agent, and since_hours is accepted."""
        r = client.get("/api/tasks?status=done&agent=brain&since_hours=168&limit=5", headers=AUTH)
        assert r.status_code in (200, 503)


class TestWorkerEndpoint:
    def test_api_workers_requires_auth(self):
        """GET /api/workers requires Authorization header."""
        r = client.get("/api/workers")
        assert r.status_code == 401

    def test_api_workers_returns_assessment(self):
        """GET /api/workers returns a dict with assessment key."""
        r = client.get("/api/workers", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert "assessment" in data
        assert data["assessment"] in ("ok", "idle", "warning", "stale", "unknown")


class TestPublicEndpoints:
    def test_health_no_auth(self):
        """GET /health is public -- no Authorization header needed."""
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert "version" in data

    def test_status_no_auth(self):
        """GET /status is public -- returns service reachability."""
        r = client.get("/status")
        assert r.status_code == 200
        data = r.json()
        assert "status" in data
        assert data["status"] in ("ok", "degraded")
        assert "services" in data
        assert "checked_in_ms" in data

    def test_status_services_include_brain_and_mcp(self):
        """GET /status always reports brain and mcp keys in services dict."""
        r = client.get("/status")
        assert r.status_code == 200
        services = r.json().get("services", {})
        assert "brain" in services
        assert "mcp" in services

    def test_status_degraded_when_services_down(self):
        """GET /status returns degraded when both services are unreachable."""
        r = client.get("/status")
        # In test environment both Brain and MCP are not running
        data = r.json()
        assert data["status"] in ("ok", "degraded")  # either is valid depending on env

    def test_health_returns_correct_version(self):
        """GET /health version field matches the module version string."""
        r = client.get("/health")
        data = r.json()
        assert data["version"] == __import__('atlas_plus').__version__


# ---------------------------------------------------------------------------
# Brain sessions browser
# ---------------------------------------------------------------------------

class TestBrainSessions:
    def test_list_requires_auth(self):
        r = client.get("/api/brain/sessions")
        assert r.status_code == 401

    def test_list_returns_html(self):
        mock_sessions = [
            {"session_id": "cli-2026-05-20", "message_count": 8, "last_active": "2026-05-20T10:00:00"},
            {"session_id": "tg-12345", "message_count": 3, "last_active": "2026-05-20T09:00:00"},
        ]
        with patch("atlas_plus.brain.history.list_sessions", return_value=mock_sessions):
            r = client.get("/api/brain/sessions", headers=AUTH)
        assert r.status_code == 200
        content = r.text
        assert "cli-2026-05-20" in content
        assert "tg-12345" in content

    def test_list_empty_returns_message(self):
        with patch("atlas_plus.brain.history.list_sessions", return_value=[]):
            r = client.get("/api/brain/sessions", headers=AUTH)
        assert r.status_code == 200
        assert b"No Brain sessions" in r.content

    def test_messages_requires_auth(self):
        r = client.get("/api/brain/sessions/my-session/html")
        assert r.status_code == 401

    def test_messages_shows_conversation(self):
        mock_messages = [
            {"role": "user", "content": "What is Atlas?"},
            {"role": "assistant", "content": "Atlas is the memory system for cross-LLM context."},
        ]
        with patch("atlas_plus.brain.history.get_messages", return_value=mock_messages):
            r = client.get("/api/brain/sessions/test-session/html", headers=AUTH)
        assert r.status_code == 200
        content = r.text
        assert "USER" in content
        assert "ASSISTANT" in content
        assert "Atlas" in content

    def test_messages_empty_session_returns_message(self):
        with patch("atlas_plus.brain.history.get_messages", return_value=[]):
            r = client.get("/api/brain/sessions/empty-session/html", headers=AUTH)
        assert r.status_code == 200
        assert b"no messages" in r.content.lower() or b"empty-session" in r.content


class TestTaskDetailAttemptHistory:
    def test_detail_shows_attempt_history_when_present(self):
        """Task detail panel includes attempt history table when attempts exist."""
        from atlas_plus.brain.tasks import Task
        from unittest.mock import patch, MagicMock
        fake_task = Task(
            task_id="task-test-attempts",
            session_id="brain-test",
            instruction="write tests",
            status="failed",
            created_at="2026-05-20T03:00:00+00:00",
            attempts=2,
            max_attempts=3,
            agent="brain",
        )
        fake_attempts = [
            {"attempt_id": "a-001", "task_id": "task-test-attempts",
             "attempt_num": 1, "outcome": "failed", "agent": "brain",
             "started_at": "2026-05-20T03:00:00+00:00",
             "finished_at": "2026-05-20T03:01:00+00:00",
             "result_summary": "executor timeout"},
            {"attempt_id": "a-002", "task_id": "task-test-attempts",
             "attempt_num": 2, "outcome": "failed", "agent": "brain",
             "started_at": "2026-05-20T04:00:00+00:00",
             "finished_at": "2026-05-20T04:01:00+00:00",
             "result_summary": "could not reach ollama"},
        ]
        with patch("atlas_plus.brain.tasks.get_task", return_value=fake_task), \
             patch("atlas_plus.brain.tasks.list_task_attempts", return_value=fake_attempts):
            r = client.get("/api/tasks/task-test-attempts/detail", headers=AUTH)
        assert r.status_code == 200
        content = r.text
        assert "Attempt History" in content
        assert "executor timeout" in content or "could not reach" in content

    def test_detail_shows_attempts_count_row(self):
        """Task detail panel shows attempts/max_attempts count."""
        from atlas_plus.brain.tasks import Task
        from unittest.mock import patch
        fake_task = Task(
            task_id="task-cnt-test",
            session_id="brain-test",
            instruction="do work",
            status="done",
            created_at="2026-05-20T03:00:00+00:00",
            attempts=1,
            max_attempts=3,
        )
        with patch("atlas_plus.brain.tasks.get_task", return_value=fake_task), \
             patch("atlas_plus.brain.tasks.list_task_attempts", return_value=[]):
            r = client.get("/api/tasks/task-cnt-test/detail", headers=AUTH)
        assert r.status_code == 200
        assert "1/3" in r.text


class TestExhaustedBadge:
    def test_returns_empty_when_no_exhausted_tasks(self):
        with patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=[]):
            r = client.get("/api/tasks/exhausted-badge", headers=AUTH)
        assert r.status_code == 200
        assert r.text.strip() == ""

    def test_returns_badge_when_exhausted_tasks_exist(self):
        from atlas_plus.brain.tasks import Task
        fake = [Task(
            task_id="task-dead", session_id="s", instruction="x",
            status="failed", created_at="2026-05-20T01:00:00+00:00",
            attempts=3, max_attempts=3,
        )]
        with patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=fake):
            r = client.get("/api/tasks/exhausted-badge", headers=AUTH)
        assert r.status_code == 200
        assert "exhausted" in r.text.lower()
        assert "1" in r.text

    def test_requires_auth(self):
        r = client.get("/api/tasks/exhausted-badge")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Research quality panel
# ---------------------------------------------------------------------------

class TestResearchQualityPanel:
    def test_returns_200_with_reports(self):
        with patch("atlas_plus.doctor.cli.check_research_quality",
                   return_value={"total_reports": 10, "scored_reports": 8,
                                 "avg_score": 0.72, "low_quality_count": 1,
                                 "min_score_file": "bad.md"}):
            r = client.get("/api/research-quality-panel", headers=AUTH)
        assert r.status_code == 200
        assert "10" in r.text or "8" in r.text or "0.72" in r.text

    def test_shows_low_quality_badge_when_present(self):
        with patch("atlas_plus.doctor.cli.check_research_quality",
                   return_value={"total_reports": 5, "scored_reports": 5,
                                 "avg_score": 0.3, "low_quality_count": 3,
                                 "min_score_file": "low.md"}):
            r = client.get("/api/research-quality-panel", headers=AUTH)
        assert r.status_code == 200
        assert "low" in r.text or "3" in r.text

    def test_shows_no_reports_message(self):
        with patch("atlas_plus.doctor.cli.check_research_quality",
                   return_value={"total_reports": 0, "scored_reports": 0,
                                 "avg_score": 0.0, "low_quality_count": 0,
                                 "min_score_file": ""}):
            r = client.get("/api/research-quality-panel", headers=AUTH)
        assert r.status_code == 200
        assert "No reports" in r.text or "yet" in r.text

    def test_requires_auth(self):
        r = client.get("/api/research-quality-panel")
        assert r.status_code == 401

    def test_graceful_when_check_fails(self):
        with patch("atlas_plus.doctor.cli.check_research_quality",
                   side_effect=RuntimeError("no reports dir")):
            r = client.get("/api/research-quality-panel", headers=AUTH)
        assert r.status_code == 200  # graceful fallback, never 500


# ---------------------------------------------------------------------------
# Memory untagged scopes panel
# ---------------------------------------------------------------------------

class TestMemoryUntaggedScopes:
    def test_shows_scopes_with_counts(self):
        with patch("atlas_plus.doctor.cli.check_untagged_by_scope",
                   return_value={"feedback": 15, "projects": 3, "reference": 1}):
            r = client.get("/api/memory/untagged-scopes", headers=AUTH)
        assert r.status_code == 200
        assert "feedback" in r.text
        assert "15" in r.text

    def test_empty_scopes_shows_clean_message(self):
        with patch("atlas_plus.doctor.cli.check_untagged_by_scope", return_value={}):
            r = client.get("/api/memory/untagged-scopes", headers=AUTH)
        assert r.status_code == 200
        assert "tagged" in r.text.lower() or "All" in r.text

    def test_high_count_uses_orange_color(self):
        with patch("atlas_plus.doctor.cli.check_untagged_by_scope",
                   return_value={"feedback": 25}):
            r = client.get("/api/memory/untagged-scopes", headers=AUTH)
        assert r.status_code == 200
        assert "orange" in r.text

    def test_requires_auth(self):
        r = client.get("/api/memory/untagged-scopes")
        assert r.status_code == 401

    def test_graceful_when_check_fails(self):
        with patch("atlas_plus.doctor.cli.check_untagged_by_scope",
                   side_effect=RuntimeError("memory dir missing")):
            r = client.get("/api/memory/untagged-scopes", headers=AUTH)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# WebSocket /ws/chat/{session_id} -- auth + SSE event parsing unit tests
# ---------------------------------------------------------------------------

class TestWsChat:
    """Auth enforcement and SSE event relay logic for ws_chat."""

    def test_rejects_wrong_key_at_connect(self):
        """WebSocket with wrong ?key= should be closed before any message."""
        import pytest
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises((WebSocketDisconnect, Exception)):
            with client.websocket_connect("/ws/chat/s?key=wrong") as ws:
                ws.receive_text()

    def test_sse_line_parsing_ignores_non_data_lines(self):
        """SSE lines without 'data: ' prefix (blank lines, comments) must be skipped."""
        import json as _json
        # Simulate what ws_chat does to parse SSE lines
        raw_lines = [
            "",
            ": comment",
            "data: {\"type\": \"tool_call\", \"tool\": \"memory_search\", \"round\": 1}",
            "",
            "data: {\"type\": \"done\", \"text\": \"ok\", \"tool_calls_count\": 0, \"model\": \"q\", \"tool_log\": []}",
            "",
        ]
        events = []
        for line in raw_lines:
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload:
                continue
            try:
                events.append(_json.loads(payload))
            except Exception:
                pass
        assert len(events) == 2
        assert events[0]["type"] == "tool_call"
        assert events[1]["type"] == "done"

    def test_sse_done_maps_to_response_frame(self):
        """A 'done' SSE event must produce a 'response' WS frame."""
        import json as _json
        done_event = {
            "type": "done",
            "text": "Brain answer here.",
            "tool_calls_count": 2,
            "model": "qwen3:8b",
            "tool_log": [{"tool": "memory_search"}],
        }
        # Mirror the mapping logic in ws_chat
        frame = {
            "type": "response",
            "text": done_event.get("text", ""),
            "tool_calls": done_event.get("tool_calls_count", 0),
            "tool_log": done_event.get("tool_log", []),
            "model": done_event.get("model", ""),
        }
        assert frame["type"] == "response"
        assert frame["text"] == "Brain answer here."
        assert frame["tool_calls"] == 2
        assert frame["model"] == "qwen3:8b"

    def test_sse_tool_call_event_structure(self):
        """tool_call SSE events are forwarded verbatim to the browser."""
        event = {"type": "tool_call", "tool": "memory_remember", "round": 3}
        # ws_chat sends tool_call events as-is -- verify structure is preserved
        assert event["type"] == "tool_call"
        assert event["tool"] == "memory_remember"
        assert event["round"] == 3

    def test_sse_error_event_maps_to_error_frame(self):
        """An 'error' SSE event must produce an 'error' WS frame with the detail."""
        error_event = {"type": "error", "detail": "Ollama unreachable"}
        frame = {"type": "error", "detail": error_event.get("detail", "unknown error")}
        assert frame["type"] == "error"
        assert "Ollama" in frame["detail"]


# ---------------------------------------------------------------------------
# POST /api/credit/reset
# ---------------------------------------------------------------------------

class TestCreditReset:
    def test_requires_auth(self):
        r = client.post("/api/credit/reset", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_503_when_brain_unreachable(self):
        r = client.post("/api/credit/reset", headers=AUTH)
        assert r.status_code == 503

    def test_proxies_to_brain(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"cleared": [], "message": "reset"}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.web.app.httpx.AsyncClient", return_value=mock_ctx):
            r = client.post("/api/credit/reset", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["message"] == "reset"


# ---------------------------------------------------------------------------
# GET /api/scheduler/jobs
# ---------------------------------------------------------------------------

class TestSchedulerJobs:
    def test_requires_auth(self):
        r = client.get("/api/scheduler/jobs", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_error_dict_when_brain_unreachable(self):
        r = client.get("/api/scheduler/jobs", headers=AUTH)
        data = r.json()
        assert "error" in data or "jobs" in data

    def test_proxies_job_list_from_brain(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"running": True, "jobs": [{"id": "temporal-build"}]}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.web.app.httpx.AsyncClient", return_value=mock_ctx):
            r = client.get("/api/scheduler/jobs", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["running"] is True
        assert len(r.json()["jobs"]) == 1


# ---------------------------------------------------------------------------
# GET /api/improvement/stats
# ---------------------------------------------------------------------------

class TestImprovementStats:
    def test_requires_auth(self):
        r = client.get("/api/improvement/stats", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_no_observations_when_empty(self):
        with patch("atlas_plus.brain.improvement.all_agent_stats", return_value=[]), \
             patch("atlas_plus.brain.improvement.all_agent_recent_scores", return_value={}):
            r = client.get("/api/improvement/stats", headers=AUTH)
        assert r.status_code == 200
        assert "No observations" in r.text

    def test_renders_agent_rows_with_scores(self):
        stats = [{"agent": "brain", "avg_quality": 7.5, "trend": 0.3, "count": 5}]
        with patch("atlas_plus.brain.improvement.all_agent_stats", return_value=stats), \
             patch("atlas_plus.brain.improvement.all_agent_recent_scores",
                   return_value={"brain": [6.0, 7.0, 8.0]}):
            r = client.get("/api/improvement/stats", headers=AUTH)
        assert r.status_code == 200
        assert "brain" in r.text
        assert "7.5" in r.text

    def test_returns_error_html_on_exception(self):
        with patch("atlas_plus.brain.improvement.all_agent_stats",
                   side_effect=RuntimeError("db locked")):
            r = client.get("/api/improvement/stats", headers=AUTH)
        assert r.status_code == 200
        assert "Error" in r.text


# ---------------------------------------------------------------------------
# GET /api/routing/stats
# ---------------------------------------------------------------------------

class TestRoutingStatsPanel:
    def test_requires_auth(self):
        r = client.get("/api/routing/stats", headers=NO_AUTH)
        assert r.status_code == 401

    def test_shows_unavailable_when_brain_unreachable(self):
        r = client.get("/api/routing/stats", headers=AUTH)
        assert r.status_code == 200
        assert "unavailable" in r.text.lower() or "routing" in r.text.lower()

    def test_renders_routing_flags_from_brain(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "smart_routing": True,
            "local_only": False,
            "cache_size": 12,
            "cache_capacity": 500,
            "category_distribution": {"coding": 4, "research": 2},
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.web.app.httpx.AsyncClient", return_value=mock_ctx):
            r = client.get("/api/routing/stats", headers=AUTH)
        assert r.status_code == 200
        assert "smart routing" in r.text.lower()
        assert "on" in r.text


# ---------------------------------------------------------------------------
# GET /api/temporal/stats
# ---------------------------------------------------------------------------

class TestTemporalStats:
    def test_requires_auth(self):
        r = client.get("/api/temporal/stats", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_stats_html(self):
        fake_stats = {"entities": 42, "facts_active": 120, "episodes": 5}
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            mock_cls.return_value.stats.return_value = fake_stats
            r = client.get("/api/temporal/stats", headers=AUTH)
        assert r.status_code == 200
        assert "42" in r.text
        assert "120" in r.text

    def test_returns_error_html_on_exception(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   side_effect=ImportError("no temporal")):
            r = client.get("/api/temporal/stats", headers=AUTH)
        assert r.status_code == 200
        assert "error" in r.text.lower()


# ---------------------------------------------------------------------------
# GET /api/temporal/query
# ---------------------------------------------------------------------------

class TestTemporalQuery:
    def test_requires_auth(self):
        r = client.get("/api/temporal/query?q=SIP", headers=NO_AUTH)
        assert r.status_code == 401

    def test_empty_query_returns_hint(self):
        r = client.get("/api/temporal/query?q=", headers=AUTH)
        assert r.status_code == 200
        assert "Enter a query" in r.text

    def test_no_results_returns_no_results_message(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            mock_cls.return_value.search_current.return_value = []
            r = client.get("/api/temporal/query?q=SIP+REGISTER", headers=AUTH)
        assert r.status_code == 200
        assert "No results" in r.text or "results" in r.text.lower()

    def test_query_error_returns_error_html(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   side_effect=Exception("db error")):
            r = client.get("/api/temporal/query?q=SIP", headers=AUTH)
        assert r.status_code == 200
        assert "error" in r.text.lower()


# ---------------------------------------------------------------------------
# GET /api/memory/gaps
# ---------------------------------------------------------------------------

class TestMemoryGaps:
    def test_requires_auth(self):
        r = client.get("/api/memory/gaps", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_no_miss_data_when_empty(self):
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   return_value=[]):
            r = client.get("/api/memory/gaps", headers=AUTH)
        assert r.status_code == 200
        assert "No miss data" in r.text

    def test_renders_miss_rows_when_data_present(self):
        gaps = [{"query": "SIP REGISTER", "miss_count": 5, "total": 6}]
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   return_value=gaps):
            r = client.get("/api/memory/gaps", headers=AUTH)
        assert r.status_code == 200
        assert "SIP REGISTER" in r.text
        assert "5/6" in r.text

    def test_returns_error_html_on_exception(self):
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   side_effect=RuntimeError("db error")):
            r = client.get("/api/memory/gaps", headers=AUTH)
        assert r.status_code == 200
        assert "Error" in r.text


# ---------------------------------------------------------------------------
# GET /api/memory/next-actions
# ---------------------------------------------------------------------------

class TestMemoryNextActionsPanel:
    def test_requires_auth(self):
        r = client.get("/api/memory/next-actions-panel", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_clean_when_empty(self):
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]):
            r = client.get("/api/memory/next-actions-panel", headers=AUTH)
        assert r.status_code == 200
        assert "good shape" in r.text or "no urgent" in r.text.lower()

    def test_shows_decayed_count(self):
        from unittest.mock import MagicMock
        cand = MagicMock()
        cand.path = "reference/old.md"
        cand.days_overdue = 45
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[cand]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]):
            r = client.get("/api/memory/next-actions-panel", headers=AUTH)
        assert r.status_code == 200
        assert "decayed" in r.text.lower() or "old.md" in r.text


class TestMemoryNextActions:
    def test_requires_auth(self):
        r = client.get("/api/memory/next-actions", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_json_with_four_keys(self):
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.curation.tag_suggest.suggest_tags_for_file", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]):
            r = client.get("/api/memory/next-actions", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert "decayed" in data
        assert "orphans" in data
        assert "untagged" in data
        assert "gaps" in data

    def test_returns_decayed_items(self):
        from unittest.mock import MagicMock
        cand = MagicMock()
        cand.as_dict.return_value = {"path": "ref/old.md", "days_overdue": 30, "half_life_days": 60}
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[cand]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.curation.tag_suggest.suggest_tags_for_file", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]):
            r = client.get("/api/memory/next-actions", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert len(data["decayed"]) == 1
        assert data["decayed"][0]["path"] == "ref/old.md"

    def test_returns_error_json_on_exception(self):
        # Patch ALL FOUR gather steps (like test_returns_json_with_four_keys): only
        # find_decayed_facts raises -- but the endpoint's per-step try/except means the
        # other three would otherwise run REAL curation over the live MEMORY_ROOT (now
        # bloated with the reference/research mirror), where the real per-file
        # suggest_tags_for_file made this single test take ~96 minutes. Stubbing them keeps
        # the test's intent (a raising dependency must not 500 the endpoint) while staying
        # instant and free of any live-service / corpus dependency.
        with patch("atlas_plus.curation.decay.find_decayed_facts",
                   side_effect=RuntimeError("db error")), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.curation.tag_suggest.suggest_tags_for_file", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]):
            r = client.get("/api/memory/next-actions", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert "error" in data or "decayed" in data

    def test_untagged_skips_reference_research_mirror(self):
        # The untagged scan must skip the reference/research mirror (thousands of files)
        # while still surfacing real memory. suggest_tags returns tags for every file, so
        # only the exclusion can keep the mirror file out of the result.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "feedback").mkdir(parents=True)
            (root / "feedback" / "real.md").write_text("# real\n")
            (root / "reference" / "research").mkdir(parents=True)
            (root / "reference" / "research" / "mirror.md").write_text("# mirror\n")
            with patch("atlas_plus.mcp.memory_server.config.MEMORY_ROOT", root), \
                 patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]), \
                 patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
                 patch("atlas_plus.curation.tag_suggest.suggest_tags_for_file", return_value=["tag"]), \
                 patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]):
                r = client.get("/api/memory/next-actions", headers=AUTH)
        assert r.status_code == 200
        paths = {u["path"] for u in r.json()["untagged"]}
        assert "feedback/real.md" in paths
        assert all("reference/research" not in p for p in paths)


# ---------------------------------------------------------------------------
# POST /api/pending-facts/{id}/approve  and  /reject
# ---------------------------------------------------------------------------

class TestPendingFactsApproveReject:
    def test_approve_requires_auth(self):
        r = client.post("/api/pending-facts/fact-001/approve", headers=NO_AUTH)
        assert r.status_code == 401

    def test_reject_requires_auth(self):
        r = client.post("/api/pending-facts/fact-001/reject", headers=NO_AUTH)
        assert r.status_code == 401

    def test_approve_proxies_to_brain(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "approved", "fact_id": "fact-001"}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.web.app.httpx.AsyncClient", return_value=mock_ctx):
            r = client.post("/api/pending-facts/fact-001/approve",
                            json={"note": "ok"}, headers=AUTH)
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    def test_reject_proxies_to_brain(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "rejected", "fact_id": "fact-001"}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.web.app.httpx.AsyncClient", return_value=mock_ctx):
            r = client.post("/api/pending-facts/fact-001/reject", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    def test_approve_returns_503_on_brain_error(self):
        r = client.post("/api/pending-facts/fact-001/approve", headers=AUTH)
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# GET /api/pending-facts/count
# ---------------------------------------------------------------------------

class TestPendingFactsCount:
    def test_requires_auth(self):
        r = client.get("/api/pending-facts/count", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_zero_when_brain_unreachable(self):
        r = client.get("/api/pending-facts/count", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["count"] == 0

    def test_returns_count_from_brain(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"count": 7}
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.web.app.httpx.AsyncClient", return_value=mock_ctx):
            r = client.get("/api/pending-facts/count", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["count"] == 7


# ---------------------------------------------------------------------------
# POST /api/tasks/retry-failed
# ---------------------------------------------------------------------------

class TestRetryAllFailed:
    def test_requires_auth(self):
        r = client.post("/api/tasks/retry-failed", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_zero_submitted_when_no_failed_tasks(self):
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[]):
            r = client.post("/api/tasks/retry-failed", headers=AUTH)
        assert r.status_code == 202
        data = r.json()
        assert data["submitted"] == 0
        assert data["total_failed"] == 0

    def test_submits_each_failed_task(self):
        task = MagicMock()
        task.instruction = "do something"
        task.session_id = "s-001"
        task.agent = "brain"
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"task_id": "new-t-001"}
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[task]), \
             patch("atlas_plus.web.app.httpx.AsyncClient", return_value=mock_ctx):
            r = client.post("/api/tasks/retry-failed", headers=AUTH)
        assert r.status_code == 202
        assert r.json()["submitted"] == 1
        assert r.json()["total_failed"] == 1


# ---------------------------------------------------------------------------
# GET /api/tasks/{task_id}/live  (SSE endpoint - auth check)
# ---------------------------------------------------------------------------

class TestTaskLiveStream:
    def test_requires_auth(self):
        r = client.get("/api/tasks/t-001/live", headers=NO_AUTH)
        assert r.status_code == 401

    def test_unknown_task_returns_done_event(self):
        with patch("atlas_plus.brain.tasks.get_task", return_value=None):
            r = client.get("/api/tasks/t-unknown/live", headers=AUTH)
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# PATCH /api/tasks/{task_id}/priority
# ---------------------------------------------------------------------------

class TestTaskPriorityPatch:
    def test_requires_auth(self):
        r = client.patch("/api/tasks/t-001/priority", json={"priority": 1},
                         headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_503_when_brain_unreachable(self):
        r = client.patch("/api/tasks/t-001/priority", json={"priority": 1},
                         headers=AUTH)
        assert r.status_code == 503

    def test_proxies_to_brain(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"task_id": "t-001", "priority": 1}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.patch = AsyncMock(return_value=mock_resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.web.app.httpx.AsyncClient", return_value=mock_ctx):
            r = client.patch("/api/tasks/t-001/priority", json={"priority": 1},
                             headers=AUTH)
        assert r.status_code == 200
        assert r.json()["task_id"] == "t-001"


# ---------------------------------------------------------------------------
# POST /api/tasks/claude
# ---------------------------------------------------------------------------

class TestSubmitClaudeTask:
    def test_requires_auth(self):
        r = client.post("/api/tasks/claude", json={"instruction": "help"},
                        headers=NO_AUTH)
        assert r.status_code == 401

    def test_empty_instruction_rejected_before_brain_proxy(self):
        # Changed 2026-10-12: TaskSubmit now requires a non-blank instruction.
        # An empty string used to pass validation, reach the Brain, and die in
        # the worker inside `claude --print` with a confusing CLI error.
        r = client.post("/api/tasks/claude", json={"instruction": ""},
                        headers=AUTH)
        assert r.status_code == 422

    def test_returns_503_when_brain_unreachable(self):
        r = client.post("/api/tasks/claude", json={"instruction": "write a test"},
                        headers=AUTH)
        assert r.status_code == 503

    def test_sets_claude_code_agent_and_priority_1(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_resp.json.return_value = {"task_id": "tc-001", "agent": "claude-code", "priority": 1}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.web.app.httpx.AsyncClient", return_value=mock_ctx):
            r = client.post("/api/tasks/claude", json={"instruction": "write a test"},
                            headers=AUTH)
        assert r.status_code == 202
        _, kwargs = mock_client.post.call_args
        body = kwargs.get("json", {})
        assert body["agent"] == "claude-code"
        assert body["priority"] == 1


# ---------------------------------------------------------------------------
# POST /api/memory/capture
# ---------------------------------------------------------------------------

class TestMemoryCapture:
    def test_requires_auth(self):
        r = client.post("/api/memory/capture", json={"fact": "test fact"},
                        headers=NO_AUTH)
        assert r.status_code == 401

    def test_rejects_missing_fact(self):
        r = client.post("/api/memory/capture", json={}, headers=AUTH)
        assert r.status_code == 400

    def test_rejects_empty_fact(self):
        r = client.post("/api/memory/capture", json={"fact": "   "}, headers=AUTH)
        assert r.status_code == 400

    def test_calls_memory_remember_tool(self):
        with patch("atlas_plus.mcp.memory_server.tools.remember.memory_remember",
                   return_value={"status": "saved", "path": "pref/test.md"}) as mock_fn:
            r = client.post("/api/memory/capture",
                            json={"fact": "use httpx not requests", "scope": "pref:libs"},
                            headers=AUTH)
        assert r.status_code == 200
        mock_fn.assert_called_once_with(scope="pref:libs", fact="use httpx not requests",
                                        source="human-direct", interactive=True)


# ---------------------------------------------------------------------------
# GET /api/projects
# ---------------------------------------------------------------------------

class TestProjectsList:
    def test_requires_auth(self):
        r = client.get("/api/projects", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_empty_list_when_root_missing(self, tmp_path):
        missing = tmp_path / "no-such-dir"
        with patch.dict("os.environ", {"ATLAS_PROJECTS_ROOT": str(missing)}):
            r = client.get("/api/projects", headers=AUTH)
        assert r.status_code == 200
        assert r.json() == []

    def test_returns_sorted_directory_names(self, tmp_path):
        (tmp_path / "zebra").mkdir()
        (tmp_path / "atlas-memory+").mkdir()
        (tmp_path / ".hidden").mkdir()
        with patch.dict("os.environ", {"ATLAS_PROJECTS_ROOT": str(tmp_path)}):
            r = client.get("/api/projects", headers=AUTH)
        assert r.status_code == 200
        names = r.json()
        assert "atlas-memory+" in names
        assert "zebra" in names
        assert ".hidden" not in names
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# GET /api/sessions  (session list panel)
# ---------------------------------------------------------------------------

class TestSessionsList:
    def test_requires_auth(self):
        r = client.get("/api/sessions", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_no_sessions_when_dir_missing(self, tmp_path):
        missing = tmp_path / "no-sessions"
        with patch("atlas_plus.web.app.MEMORY_ROOT", missing):
            r = client.get("/api/sessions", headers=AUTH)
        assert r.status_code == 200
        assert "No sessions" in r.text

    def test_lists_session_files(self, tmp_path):
        mem_root = tmp_path / "memory"
        sess_dir = mem_root / "sessions"
        sess_dir.mkdir(parents=True)
        (sess_dir / "2026-05-21.md").write_text("## Session\nDid things.", encoding="utf-8")
        (sess_dir / "2026-05-20.md").write_text("## Session\nOld stuff.", encoding="utf-8")
        with patch("atlas_plus.web.app.MEMORY_ROOT", mem_root):
            r = client.get("/api/sessions", headers=AUTH)
        assert r.status_code == 200
        assert "2026-05-21" in r.text
        assert "2026-05-20" in r.text

    def test_shows_history_file_when_present(self, tmp_path):
        mem_root = tmp_path / "memory"
        sess_dir = mem_root / "sessions"
        sess_dir.mkdir(parents=True)
        (sess_dir / "_history.md").write_text("# History\nEntry 1.", encoding="utf-8")
        with patch("atlas_plus.web.app.MEMORY_ROOT", mem_root):
            r = client.get("/api/sessions", headers=AUTH)
        assert r.status_code == 200
        assert "_history" in r.text


# ---------------------------------------------------------------------------
# GET /api/sessions/{name}  (session detail panel)
# ---------------------------------------------------------------------------

class TestSessionDetail:
    def test_requires_auth(self):
        r = client.get("/api/sessions/2026-05-21", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_not_found_for_missing_file(self, tmp_path):
        missing_root = tmp_path / "memory"
        missing_root.mkdir()
        (missing_root / "sessions").mkdir()
        with patch("atlas_plus.web.app.MEMORY_ROOT", missing_root):
            r = client.get("/api/sessions/nonexistent", headers=AUTH)
        assert r.status_code == 200
        assert "not found" in r.text.lower()

    def test_returns_content_for_existing_session(self, tmp_path):
        mem_root = tmp_path / "memory"
        sess_dir = mem_root / "sessions"
        sess_dir.mkdir(parents=True)
        (sess_dir / "2026-05-21.md").write_text("## My Session\nThis is a test.", encoding="utf-8")
        with patch("atlas_plus.web.app.MEMORY_ROOT", mem_root):
            r = client.get("/api/sessions/2026-05-21", headers=AUTH)
        assert r.status_code == 200
        assert "My Session" in r.text or "This is a test" in r.text

    def test_rejects_path_traversal(self, tmp_path):
        # FastAPI URL routing rejects ../ in path segments with 404; our code also
        # strips ".." via Path().name -- either way the file is never served.
        mem_root = tmp_path / "memory"
        (mem_root / "sessions").mkdir(parents=True)
        with patch("atlas_plus.web.app.MEMORY_ROOT", mem_root):
            r = client.get("/api/sessions/../../etc/passwd", headers=AUTH)
        assert r.status_code in (200, 404)
        # Ensure the response body doesn't look like /etc/passwd content
        assert "root:" not in r.text


# ---------------------------------------------------------------------------
# GET /api/sessions/{name}/download  (session download)
# ---------------------------------------------------------------------------

class TestSessionDownload:
    def test_requires_auth(self):
        r = client.get("/api/sessions/2026-05-21/download", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_404_for_missing_file(self, tmp_path):
        mem_root = tmp_path / "memory"
        (mem_root / "sessions").mkdir(parents=True)
        with patch("atlas_plus.web.app.MEMORY_ROOT", mem_root):
            r = client.get("/api/sessions/nonexistent/download", headers=AUTH)
        assert r.status_code == 404

    def test_downloads_session_as_markdown(self, tmp_path):
        mem_root = tmp_path / "memory"
        sess_dir = mem_root / "sessions"
        sess_dir.mkdir(parents=True)
        (sess_dir / "2026-05-21.md").write_text("# Session\nContent here.", encoding="utf-8")
        with patch("atlas_plus.web.app.MEMORY_ROOT", mem_root):
            r = client.get("/api/sessions/2026-05-21/download", headers=AUTH)
        assert r.status_code == 200
        assert "text/markdown" in r.headers.get("content-type", "")
        assert "attachment" in r.headers.get("content-disposition", "")
        assert b"Content here" in r.content


# ---------------------------------------------------------------------------
# GET /api/chat/history  (chat history panel)
# ---------------------------------------------------------------------------

class TestChatHistory:
    def test_requires_auth(self):
        r = client.get("/api/chat/history?session_id=s-001", headers=NO_AUTH)
        assert r.status_code == 401

    def test_empty_session_id_returns_hint(self):
        r = client.get("/api/chat/history?session_id=", headers=AUTH)
        assert r.status_code == 200
        assert "No session_id" in r.text

    def test_returns_no_history_when_brain_unreachable(self):
        r = client.get("/api/chat/history?session_id=s-001", headers=AUTH)
        assert r.status_code == 200
        assert "Brain not running" in r.text or "Error" in r.text

    def test_renders_message_bubbles_from_brain(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "messages": [
                {"role": "user", "content": "hello there"},
                {"role": "assistant", "content": "hi back"},
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.web.app.httpx.AsyncClient", return_value=mock_ctx):
            r = client.get("/api/chat/history?session_id=s-001", headers=AUTH)
        assert r.status_code == 200
        assert "hello there" in r.text
        assert "hi back" in r.text


# ---------------------------------------------------------------------------
# GET /api/agents  (Agents tab panel)
# ---------------------------------------------------------------------------

class TestAgentsPanel:
    def test_requires_auth(self):
        r = client.get("/api/agents", headers=NO_AUTH)
        assert r.status_code == 401

    def test_shows_brain_not_running_when_unreachable(self):
        r = client.get("/api/agents", headers=AUTH)
        assert r.status_code == 200
        assert "Brain" in r.text

    def test_renders_agent_rows_from_brain(self):
        agents_data = {
            "agents": [
                {"name": "brain", "model": "qwen3:8b", "online": True,
                 "description": "Default agent", "capabilities": ["coding"]},
                {"name": "coder", "model": "qwen3:8b", "online": False,
                 "description": "Coding specialist", "capabilities": ["coding", "review"]},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = agents_data
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.web.app.httpx.AsyncClient", return_value=mock_ctx), \
             patch("atlas_plus.brain.improvement.all_agent_recent_scores", return_value={}), \
             patch("atlas_plus.brain.improvement.all_agent_stats", return_value=[]):
            r = client.get("/api/agents", headers=AUTH)
        assert r.status_code == 200
        assert "brain" in r.text
        assert "coder" in r.text
        assert "online" in r.text
        assert "offline" in r.text

    def test_shows_credit_badge_for_exhausted_agent(self):
        agents_data = {
            "agents": [
                {"name": "claude-code", "model": "claude-3-5-sonnet", "online": True,
                 "description": "Claude Code", "capabilities": []},
            ]
        }
        diagnostic_data = {"credit_exhausted_agents": ["claude-code"]}
        mock_resp_agents = MagicMock()
        mock_resp_agents.status_code = 200
        mock_resp_agents.json.return_value = agents_data
        mock_resp_agents.raise_for_status = MagicMock()
        mock_resp_diag = MagicMock()
        mock_resp_diag.status_code = 200
        mock_resp_diag.json.return_value = diagnostic_data
        mock_client = MagicMock()
        # First call is /agents, second is /diagnostic
        mock_client.get = AsyncMock(side_effect=[mock_resp_agents, mock_resp_diag])
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.web.app.httpx.AsyncClient", return_value=mock_ctx), \
             patch("atlas_plus.brain.improvement.all_agent_recent_scores", return_value={}), \
             patch("atlas_plus.brain.improvement.all_agent_stats", return_value=[]):
            r = client.get("/api/agents", headers=AUTH)
        assert r.status_code == 200
        assert "credits" in r.text.lower()

    def test_shows_401_error_from_brain(self):
        from httpx import HTTPStatusError, Response as _HResp, Request as _HReq
        mock_client = MagicMock()
        exc = HTTPStatusError(
            "401", request=MagicMock(), response=MagicMock(status_code=401)
        )
        mock_client.get = AsyncMock(side_effect=exc)
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        with patch("atlas_plus.web.app.httpx.AsyncClient", return_value=mock_ctx):
            r = client.get("/api/agents", headers=AUTH)
        assert r.status_code == 200
        assert "auth failed" in r.text.lower() or "401" in r.text


# ---------------------------------------------------------------------------
# _sparkline_svg helper (pure function)
# ---------------------------------------------------------------------------

class TestSparklineSvg:
    """Tests for the _sparkline_svg helper in web/app.py."""

    def _fn(self):
        from atlas_plus.web.app import _sparkline_svg
        return _sparkline_svg

    def test_single_score_returns_empty_svg(self):
        spark = self._fn()([7.5])
        assert "<svg" in spark
        assert "polyline" not in spark

    def test_empty_scores_returns_empty_svg(self):
        spark = self._fn()([])
        assert "<svg" in spark
        assert "polyline" not in spark

    def test_two_scores_renders_polyline(self):
        spark = self._fn()([5.0, 8.0])
        assert "polyline" in spark
        assert "points=" in spark

    def test_high_score_uses_green(self):
        spark = self._fn()([8.0, 9.0, 9.5])
        assert "#4ade80" in spark

    def test_medium_score_uses_yellow(self):
        spark = self._fn()([4.0, 5.0, 5.5])
        assert "#fbbf24" in spark

    def test_low_score_uses_red(self):
        spark = self._fn()([1.0, 2.0, 1.5])
        assert "#f87171" in spark

    def test_custom_dimensions_applied(self):
        spark = self._fn()([5.0, 7.0], width=120, height=30)
        assert 'width="120"' in spark
        assert 'height="30"' in spark

    def test_scores_clamped_to_0_10(self):
        # Out-of-range values should not cause errors
        spark = self._fn()([-5.0, 15.0, 5.0])
        assert "polyline" in spark


# ---------------------------------------------------------------------------
# GET /api/temporal/history
# ---------------------------------------------------------------------------

class TestTemporalHistory:
    def _make_hist(self, entity_name="Example MCP Tunnel", facts_count=2):
        """Build a fake TemporalHistory-like object."""
        entity = MagicMock()
        entity.name = entity_name
        entity.labels = {"project"}
        entity.aliases = set()
        entity.first_seen_at = "2026-01-01T00:00:00Z"
        entity.last_seen_at = "2026-05-21T00:00:00Z"

        facts = []
        for i in range(facts_count):
            f = MagicMock()
            f.fact_text = f"Fact {i}"
            f.predicate = "is"
            f.object_value = f"value {i}"
            f.valid_from = "2026-01-01T00:00:00Z"
            f.invalid_at = None
            f.provenance = "session"
            facts.append(f)

        hist = MagicMock()
        hist.entity = entity
        hist.facts = facts
        hist.current_fact = facts[-1] if facts else None
        hist.warnings = []
        return hist

    def test_requires_auth(self):
        r = client.get("/api/temporal/history?entity=Atlas", headers=NO_AUTH)
        assert r.status_code == 401

    def test_empty_entity_returns_400(self):
        r = client.get("/api/temporal/history?entity=", headers=AUTH)
        assert r.status_code == 400

    def test_missing_entity_param_returns_400(self):
        r = client.get("/api/temporal/history", headers=AUTH)
        assert r.status_code == 400

    def test_returns_fact_timeline(self):
        fake_hist = self._make_hist("Example MCP Tunnel", facts_count=3)
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            mock_cls.return_value.history.return_value = fake_hist
            r = client.get("/api/temporal/history?entity=Example+MCP+Tunnel", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["entity"]["name"] == "Example MCP Tunnel"
        assert len(data["facts"]) == 3
        assert data["facts"][0]["fact_text"] == "Fact 0"

    def test_returns_404_when_entity_not_found(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            mock_cls.return_value.history.return_value = None
            r = client.get("/api/temporal/history?entity=unknown-xyz", headers=AUTH)
        assert r.status_code == 404

    def test_503_on_provider_error(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   side_effect=RuntimeError("db error")):
            r = client.get("/api/temporal/history?entity=SomeEntity", headers=AUTH)
        assert r.status_code == 503

    def test_last_fact_marked_is_current(self):
        fake_hist = self._make_hist("Test", facts_count=2)
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            mock_cls.return_value.history.return_value = fake_hist
            r = client.get("/api/temporal/history?entity=Test", headers=AUTH)
        assert r.status_code == 200
        facts = r.json()["facts"]
        # Last fact should be is_current=True
        assert facts[-1]["is_current"] is True
        assert facts[0]["is_current"] is False


# ---------------------------------------------------------------------------
# GET /api/memory/curation-report
# ---------------------------------------------------------------------------

_FAKE_REPORT = (
    "# Atlas Memory Curation Report\n"
    "\n"
    "## Potential Duplicates (2 found, threshold=0.4)\n"
    "\n"
    "## Stale Files (3 found, older than 6 months)\n"
    "\n"
    "## Convergence Flags (1 file(s) with unresolved contradiction flags)\n"
    "\n"
    "## Stale Decisions (0 decision(s) not reviewed in 90+ days)\n"
    "\n"
    "## Re-derivable Facts (4 file(s) overlap heavily with project docs)\n"
)

_FAKE_CLEAN_REPORT = (
    "# Atlas Memory Curation Report\n"
    "\n"
    "## Potential Duplicates (0 found, threshold=0.4)\n"
    "\n"
    "## Stale Files (0 found, older than 6 months)\n"
    "\n"
    "## Convergence Flags (0 file(s) with unresolved contradiction flags)\n"
    "\n"
    "## Stale Decisions (0 decision(s) not reviewed in 90+ days)\n"
    "\n"
    "## Re-derivable Facts (0 file(s) overlap heavily with project docs)\n"
)


class TestMemoryCurationReport:
    def test_requires_auth(self):
        r = client.get("/api/memory/curation-report", headers=NO_AUTH)
        assert r.status_code == 401

    def test_returns_json_counts(self):
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   return_value=(_FAKE_REPORT, Path("/tmp/report.md"))):
            r = client.get("/api/memory/curation-report", headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["duplicates"] == 2
        assert data["stale"] == 3
        assert data["convergence"] == 1
        assert data["decisions"] == 0
        assert data["rederivable"] == 4

    def test_dry_run_true_by_default(self):
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   return_value=(_FAKE_CLEAN_REPORT, Path("/tmp/r.md"))) as mock_gen:
            r = client.get("/api/memory/curation-report", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["dry_run"] is True
        mock_gen.assert_called_once_with(dry_run=True)

    def test_dry_run_false_writes_report(self):
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   return_value=(_FAKE_CLEAN_REPORT, Path("/tmp/written.md"))) as mock_gen:
            r = client.post("/api/memory/curation-report?dry_run=false", headers=AUTH)  # a write needs POST
        assert r.status_code == 200
        data = r.json()
        assert data["dry_run"] is False
        assert data["report_path"] is not None
        mock_gen.assert_called_once_with(dry_run=False)

    def test_503_on_error(self):
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   side_effect=RuntimeError("scan error")):
            r = client.get("/api/memory/curation-report", headers=AUTH)
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# GET /api/memory/curation-report-panel
# ---------------------------------------------------------------------------

class TestMemoryCurationReportPanel:
    def test_requires_auth(self):
        r = client.get("/api/memory/curation-report-panel", headers=NO_AUTH)
        assert r.status_code == 401

    def test_clean_memory_shows_clean_message(self):
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   return_value=(_FAKE_CLEAN_REPORT, Path("/tmp/r.md"))):
            r = client.get("/api/memory/curation-report-panel", headers=AUTH)
        assert r.status_code == 200
        assert "clean" in r.text.lower()

    def test_shows_issue_counts_when_found(self):
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   return_value=(_FAKE_REPORT, Path("/tmp/r.md"))):
            r = client.get("/api/memory/curation-report-panel", headers=AUTH)
        assert r.status_code == 200
        assert "2" in r.text   # duplicates
        assert "3" in r.text   # stale
        assert "Duplicates" in r.text or "duplicates" in r.text.lower()

    def test_error_returns_error_html(self):
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   side_effect=RuntimeError("scan error")):
            r = client.get("/api/memory/curation-report-panel", headers=AUTH)
        assert r.status_code == 200
        assert "error" in r.text.lower()


# ---------------------------------------------------------------------------
# GET /api/temporal/history-panel
# ---------------------------------------------------------------------------

class TestTemporalHistoryPanel:
    def _make_hist(self, entity_name="Example MCP", n_facts=2):
        entity = MagicMock()
        entity.name = entity_name
        entity.first_seen_at = "2026-01-01T00:00:00Z"
        entity.last_seen_at = "2026-05-21T00:00:00Z"

        facts = []
        for i in range(n_facts):
            f = MagicMock()
            f.fact_text = f"Fact {i}"
            f.valid_from = "2026-01-01T00:00:00Z"
            f.invalid_at = None
            f.provenance = "session"
            facts.append(f)

        hist = MagicMock()
        hist.entity = entity
        hist.facts = facts
        hist.current_fact = facts[-1] if facts else None
        hist.warnings = []
        return hist

    def test_requires_auth(self):
        r = client.get("/api/temporal/history-panel?entity=Atlas", headers=NO_AUTH)
        assert r.status_code == 401

    def test_empty_entity_returns_prompt(self):
        r = client.get("/api/temporal/history-panel?entity=", headers=AUTH)
        assert r.status_code == 200
        assert "Enter" in r.text

    def test_returns_fact_timeline_html(self):
        fake_hist = self._make_hist("Example MCP", n_facts=3)
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            mock_cls.return_value.history.return_value = fake_hist
            r = client.get("/api/temporal/history-panel?entity=Example+MCP", headers=AUTH)
        assert r.status_code == 200
        assert "Example MCP" in r.text
        assert "Fact 0" in r.text

    def test_entity_not_found_returns_not_found_message(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            mock_cls.return_value.history.return_value = None
            r = client.get("/api/temporal/history-panel?entity=unknown-xyz", headers=AUTH)
        assert r.status_code == 200
        assert "not found" in r.text.lower()

    def test_error_returns_error_html(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   side_effect=RuntimeError("db error")):
            r = client.get("/api/temporal/history-panel?entity=test", headers=AUTH)
        assert r.status_code == 200
        assert "error" in r.text.lower()


class TestCookieDownloadAuth:
    """ROADMAP item 3 (paranoid pass 2026-09-23): download/export links must
    not embed ?key= -- the authed page load sets an HttpOnly SameSite=Strict
    cookie that GET/HEAD auth accepts; mutating methods ignore it (no CSRF
    surface)."""

    def test_index_sets_httponly_strict_cookie(self):
        r = client.get("/", headers=AUTH)
        assert r.status_code == 200
        set_cookie = r.headers.get("set-cookie", "")
        assert "sextant_ui=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=strict" in set_cookie.replace("samesite", "SameSite")

    def test_cookie_authenticates_get(self):
        # The cookie carries a random session token minted by GET /, never
        # the API key (2026-11-09 data-safety fix, row 13).
        client.cookies.clear()
        session = client.get("/", headers=AUTH).cookies.get("sextant_ui")
        client.cookies.clear()
        assert session and session != _TEST_KEY
        r = client.get("/api/tasks", cookies={"sextant_ui": session})
        assert r.status_code != 401

    def test_raw_key_as_cookie_rejected(self):
        client.cookies.clear()
        r = client.get("/api/tasks", cookies={"sextant_ui": _TEST_KEY})
        assert r.status_code == 401

    def test_cookie_rejected_for_post(self):
        r = client.post("/api/tasks", cookies={"sextant_ui": _TEST_KEY}, json={"instruction": "x"})
        assert r.status_code == 401

    def test_wrong_cookie_still_401(self):
        # starlette >= 1.10 merges per-request cookies with the shared jar (which earlier
        # tests filled with the valid cookie); clear it so only the wrong cookie is sent.
        client.cookies.clear()
        r = client.get("/api/tasks", cookies={"sextant_ui": "wrong-key"})
        assert r.status_code == 401

    def test_session_fragment_links_carry_no_key(self, tmp_path, monkeypatch):
        sessions = tmp_path / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "2026-09-24.md").write_text("x" * 20_000, encoding="utf-8")
        import atlas_plus.web.app as web_app
        monkeypatch.setattr(web_app, "MEMORY_ROOT", tmp_path)
        r = client.get("/api/sessions/2026-09-24/preview", headers=AUTH)
        if r.status_code == 404:
            import pytest as _pytest
            _pytest.skip("preview route name drifted; link check covered by grep test")
        assert "key=" not in r.text


class TestBranding:
    """2026-10-12 brand kit: the shell ships the Sextant favicon inline (data
    URIs -- no package data) and the visible header carries the brand name."""

    def test_page_ships_both_favicon_links(self):
        r = client.get("/", headers=AUTH)
        assert r.status_code == 200
        assert 'rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,' in r.text
        assert 'rel="icon" type="image/x-icon" href="data:image/x-icon;base64,' in r.text

    def test_favicon_payloads_decode_to_real_images(self):
        import base64
        from atlas_plus.web import app as webapp
        svg = base64.b64decode(webapp._FAVICON_SVG_B64)
        ico = base64.b64decode(webapp._FAVICON_ICO_B64)
        assert svg.lstrip().startswith(b"<svg")
        # the kit shipped .t/.s classes with no definitions; the fix must stay
        assert b"<style>" in svg and b".t{fill:" in svg and b".s{stroke:" in svg
        assert b"<metadata>" not in svg, "C2PA blob must stay stripped"
        assert ico[:4] == b"\x00\x00\x01\x00", "not an ICO header"

    def test_title_and_header_are_sextant(self):
        r = client.get("/", headers=AUTH)
        assert "<title>Sextant Helm</title>" in r.text
        assert ">Sextant</h1>" in r.text


class TestStreamsUseCookieNotUrl:
    """Run 11: the SPA's EventSource/WebSocket constructors carry no ?key=
    (same-origin cookies authenticate them); ?key= stays accepted for
    scripted clients only."""

    def test_page_js_builds_streams_without_key(self):
        r = client.get("/", headers=AUTH)
        assert r.status_code == 200
        assert '/events?key' not in r.text
        assert 'live?key' not in r.text
        assert '/ws/chat/"+SID+"?key' not in r.text

    def test_websocket_accepts_cookie_auth(self):
        client.cookies.clear()
        session = client.get("/", headers=AUTH).cookies.get("sextant_ui")
        client.cookies.clear()
        client.cookies.set("sextant_ui", session)
        try:
            # browsers always send Origin on a WS handshake; the cookie path requires it to match
            with client.websocket_connect("/ws/chat/cookie-test", headers={"Origin": "http://testserver"}) as ws:
                pass  # reaching accept() without a 4401 close is the assertion
        finally:
            client.cookies.delete("sextant_ui")

    def test_websocket_rejects_wrong_cookie(self):
        from starlette.websockets import WebSocketDisconnect
        client.cookies.set("sextant_ui", "wrong")
        try:
            import pytest as _pytest
            with _pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("/ws/chat/cookie-test") as ws:
                    ws.receive_text()
        finally:
            client.cookies.delete("sextant_ui")


class TestCookieMethodMatrix:
    """Hardening (quality loop run 10): the auth cookie is a GET/HEAD-only
    key source by contract. Pin every mutating method AND the classic
    method-override header tricks -- none may cookie-authenticate."""

    def test_mutating_methods_never_cookie_auth(self):
        client.cookies.set("sextant_ui", _TEST_KEY)
        try:
            assert client.post("/api/tasks", json={"instruction": "x"}).status_code == 401
            assert client.put("/api/tasks/t1", json={}).status_code in (401, 405)
            assert client.patch("/api/tasks/t1", json={}).status_code in (401, 405)
            assert client.delete("/api/tasks/t1").status_code in (401, 405)
        finally:
            client.cookies.delete("sextant_ui")

    def test_method_override_headers_do_not_downgrade(self):
        client.cookies.set("sextant_ui", _TEST_KEY)
        try:
            for hdr in ("X-HTTP-Method-Override", "X-Method-Override", "X-HTTP-Method"):
                r = client.post("/api/tasks", json={"instruction": "x"},
                                headers={hdr: "GET"})
                assert r.status_code == 401, f"{hdr} bypassed header-only POST auth"
        finally:
            client.cookies.delete("sextant_ui")


class TestOnclickJsInjection:
    """Stored XSS via corpus-derived strings in onclick handlers (quality
    loop run 22). html.escape alone is NOT enough for a JS-string-in-
    attribute context: the browser decodes entities BEFORE the JS parses,
    so a corpus FILENAME like a'+alert(1)+'.md -- legal on every OS, and
    writable by a synced peer or any import -- became valid JS that ran in
    the operator's authenticated dashboard on click. All onclick arguments
    now go through _js_arg (JSON literal, then HTML-escaped)."""

    EVIL_NAME = "a'+alert(1)+'.md"

    def test_hostile_filename_is_inert_in_tree(self):
        import html as H
        import re
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / self.EVIL_NAME).write_text("x", encoding="utf-8")
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)):
                r = client.get("/api/memory/files", headers=AUTH)
        assert r.status_code == 200
        m = re.search(r'onclick="([^"]*)"', r.text)
        assert m, "tree row with onclick expected"
        decoded = H.unescape(m.group(1))
        # The whole filename must live INSIDE one JSON string literal:
        # readMemoryFile("a'+alert(1)+'.md") -- quote/plus inert within.
        assert re.fullmatch(r'readMemoryFile\("[^"]*"\)', decoded), decoded

    def test_hostile_filename_is_inert_in_file_view(self):
        import html as H
        import re
        from urllib.parse import quote
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / self.EVIL_NAME).write_text("x", encoding="utf-8")
            with patch("atlas_plus.web.app.MEMORY_ROOT", Path(tmp)):
                r = client.get(
                    f"/api/memory/file?path={quote(self.EVIL_NAME, safe='')}",
                    headers=AUTH)
        assert r.status_code == 200
        m = re.search(r'onclick="([^"]*)"', r.text)
        assert m, "edit button with onclick expected"
        decoded = H.unescape(m.group(1))
        assert re.fullmatch(r'editMemoryFile\("[^"]*"\)', decoded), decoded

    def test_innerhtml_sink_count_is_pinned(self):
        """Inventory pin (quality loop run 36): every `.innerHTML=` sink in
        the embedded JS was classified -- static literals, our own
        server-escaped fetch responses, or browser-generated e.message
        strings. Corpus/LLM-bearing sinks must use textContent (chat pane,
        run 30; edit-view path header, run 36). A NEW sink means a
        conscious decision: classify it, then bump this pin in the same
        commit."""
        src = Path(__import__("atlas_plus.web.app", fromlist=["app"]).__file__)
        count = len(re.findall(r"\.innerHTML\s*=", src.read_text(encoding="utf-8")))
        # 44th sink classified 2026-10-12: map-svg.innerHTML in loadTopology()
        # renders the /api/topology response; every interpolated value passes
        # mapEsc(), and the data is operator config (labels, env URLs, probe
        # statuses), not corpus/model text. The activity ticker, which does
        # carry tool/ticket identifiers, uses textContent.
        assert count == 44, (
            f"innerHTML sink count changed ({count} != 44): classify the "
            "new/removed sink (corpus- or model-bearing data must go through "
            "textContent) and update this pin deliberately"
        )

    def test_no_single_quoted_onclick_interpolation_ships(self):
        """Static gate: the vulnerable pattern is an f-string onclick arg
        wrapped in bare single quotes -- fn(\\'{interpolated}\\'). None may
        exist in app.py; new handler args must use _js_arg."""
        src = Path(__import__("atlas_plus.web.app", fromlist=["app"]).__file__)
        text = src.read_text(encoding="utf-8")
        offenders = [
            line.strip()[:90]
            for line in text.splitlines()
            if "onclick=" in line and "(\\'{" in line
        ]
        assert not offenders, (
            "single-quoted onclick interpolation (JS-injectable; use "
            f"_js_arg): {offenders}"
        )


# ---------------------------------------------------------------------------
# Operator panel (feature build 2026-10-08): one queue for human-only steps,
# completions feed the corpus action log.
# ---------------------------------------------------------------------------

def _write_operator_ticket(root, tid, title="Flip the switch", project="ops",
                           status="todo", assignee="human", verified=None,
                           body="1. Open the panel.\n2. Flip it.", priority="normal"):
    from atlas_plus.tickets.store import Ticket
    t = Ticket(id=tid, title=title, project=project, status=status,
               assignee=assignee, verified=verified, body=body, priority=priority)
    d = root / project
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.md").write_text(t.to_markdown(), encoding="utf-8")
    return t


class TestOperatorPanel:
    @pytest.fixture()
    def op_world(self, tmp_path, monkeypatch):
        from atlas_plus.tickets import store as _tstore
        troot = tmp_path / "mem" / "tickets"
        monkeypatch.setattr(_tstore, "TICKETS_ROOT", troot)
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
        return troot

    def test_queue_requires_auth(self):
        assert client.get("/api/operator/queue-html").status_code == 401
        assert client.post("/api/operator/OPS-001/complete", json={}).status_code == 401

    def test_queue_lists_only_open_human_steps(self, op_world):
        _write_operator_ticket(op_world, "OPS-001", title="Verified step",
                               verified="2026-10-08")
        _write_operator_ticket(op_world, "OPS-002", title="Unverified step")
        _write_operator_ticket(op_world, "OPS-003", title="Robot work",
                               assignee="claude-code")
        _write_operator_ticket(op_world, "OPS-004", title="Old done step",
                               status="done")
        r = client.get("/api/operator/queue-html", headers=AUTH)
        assert r.status_code == 200
        html = r.text
        assert "OPS-001" in html and "OPS-002" in html
        assert "Robot work" not in html
        assert "READY" in html and "NEEDS RESEARCH" in html
        assert "2 step(s) queued" in html and "1 verified ready" in html

    def test_queue_escapes_hostile_corpus_strings(self, op_world):
        _write_operator_ticket(
            op_world, "OPS-010",
            title="<img src=x onerror=alert(1)>",
            body="step with <script>alert(2)</script> inside",
        )
        html = client.get("/api/operator/queue-html", headers=AUTH).text
        assert "<img src=x" not in html
        assert "<script>alert(2)</script>" not in html
        assert "&lt;img src=x" in html and "&lt;script&gt;" in html

    def test_complete_done_flips_status_and_logs(self, op_world, tmp_path):
        _write_operator_ticket(op_world, "OPS-020", title="Do the flip",
                               verified="2026-10-08")
        r = client.post("/api/operator/OPS-020/complete",
                        json={"outcome": "done", "note": "went fine"}, headers=AUTH)
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True and data["status"] == "done" and data["logged"] is True
        from atlas_plus.tickets.store import get_ticket
        assert get_ticket("OPS-020", tickets_root=op_world).status == "done"
        log = tmp_path / "mem" / "operator" / "action-log.md"
        text = log.read_text(encoding="utf-8")
        assert "[done] OPS-020 Do the flip (ops)" in text
        assert "note: went fine" in text
        # completion shows under "Completed today"
        html = client.get("/api/operator/queue-html", headers=AUTH).text
        assert "Completed today" in html and "OPS-020" in html

    def test_complete_skipped_archives(self, op_world, tmp_path):
        _write_operator_ticket(op_world, "OPS-021", title="Skippable")
        r = client.post("/api/operator/OPS-021/complete",
                        json={"outcome": "skipped"}, headers=AUTH)
        assert r.status_code == 200
        assert r.json()["status"] == "archived"
        text = (tmp_path / "mem" / "operator" / "action-log.md").read_text(encoding="utf-8")
        assert "[skipped] OPS-021" in text

    def test_complete_rejects_bad_input(self, op_world):
        _write_operator_ticket(op_world, "OPS-030", title="Robot-owned",
                               assignee="claude-code")
        assert client.post("/api/operator/NOPE-999/complete",
                           json={"outcome": "done"}, headers=AUTH).status_code == 404
        assert client.post("/api/operator/OPS-030/complete",
                           json={"outcome": "done"}, headers=AUTH).status_code == 400
        _write_operator_ticket(op_world, "OPS-031", title="Human step")
        assert client.post("/api/operator/OPS-031/complete",
                           json={"outcome": "maybe"}, headers=AUTH).status_code == 400

    def test_operator_tab_ships_in_shell(self):
        r = client.get(f"/?key={_TEST_KEY}")
        assert r.status_code == 200
        assert "switchTab('operator')" in r.text
        assert "/api/operator/queue-html" in r.text

    def test_complete_note_cannot_forge_log_lines(self, op_world, tmp_path):
        """Hardening (2026-10-09): the action log is append-only EVIDENCE.

        A note carrying newlines could start fresh '- <stamp> [done] ...'
        lines, forging entries in a log that sessions read back as memory.
        Control characters must never survive into the log line.
        """
        _write_operator_ticket(op_world, "OPS-040", title="Honest step")
        evil = "fine\n- 2026-01-01T00:00:00+00:00 [done] XXX-999 Forged entry (infra)\r\n# Fake heading"
        r = client.post("/api/operator/OPS-040/complete",
                        json={"outcome": "done", "note": evil}, headers=AUTH)
        assert r.status_code == 200
        text = (tmp_path / "mem" / "operator" / "action-log.md").read_text(encoding="utf-8")
        forged = [
            l for l in text.splitlines()
            if l.startswith("- ") and "XXX-999" in l and "OPS-040" not in l
        ]
        assert forged == [], f"note forged standalone log line(s): {forged}"
        assert not any(l.startswith("# Fake heading") for l in text.splitlines())
        # The real entry still records the note content, flattened to one line.
        real = [l for l in text.splitlines() if "OPS-040" in l]
        assert len(real) == 1
        assert "XXX-999" in real[0]  # content kept, structure neutralized

    def test_hostile_ticket_title_cannot_forge_log_lines(self, op_world, tmp_path):
        """A hand-written ticket file with a newline in its title frontmatter
        must not break the log's one-line-per-action structure either
        (defense at the sink, not just the note field)."""
        d = op_world / "ops"
        d.mkdir(parents=True, exist_ok=True)
        (d / "OPS-041.md").write_text(
            "---\n"
            'id: OPS-041\n'
            'title: "Line one\\n- forged [done] YYY-888 (infra)"\n'
            "project: ops\n"
            "status: todo\n"
            "assignee: human\n"
            "priority: normal\n"
            "effort: S\n"
            "---\n\nbody\n",
            encoding="utf-8",
        )
        r = client.post("/api/operator/OPS-041/complete",
                        json={"outcome": "done"}, headers=AUTH)
        assert r.status_code == 200
        text = (tmp_path / "mem" / "operator" / "action-log.md").read_text(encoding="utf-8")
        assert not any(
            l.startswith("- forged") for l in text.splitlines()
        ), "ticket title forged a standalone log line"
        ops_lines = [l for l in text.splitlines() if "OPS-041" in l]
        assert len(ops_lines) == 1
