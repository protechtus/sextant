"""AG125: a dashboard panel that fails says which panel and what to do next, never the exception's text.

Every HTML fragment route below used to answer a failure with `... unavailable: {exc}` or `Error: {exc}`:
a host path, a module name or an SQL message on the page, and no next step. Each route is driven in
process with its reader made to raise an exception that carries two paths and a sentinel.
"""
from __future__ import annotations

import asyncio
import logging
import os

import pytest

_KEY = "test-key-abc"
os.environ.setdefault("ATLAS_WEB_API_KEY", _KEY)
os.environ.setdefault("ATLAS_BRAIN_URL", "http://127.0.0.1:9")

from fastapi.testclient import TestClient  # noqa: E402

from atlas_plus import _next_step  # noqa: E402
from atlas_plus.web import app as web  # noqa: E402

SENTINEL = "SENTINEL-7421"


def _boom():
    return RuntimeError("boom at C:\\secret\\place /home/user/.secret " + SENTINEL)


def _raise(*_a, **_k):
    raise _boom()


async def _araise(*_a, **_k):
    raise _boom()


class _BoomClient:
    def __init__(self, *_a, **_k):
        pass

    async def __aenter__(self):
        raise _boom()

    async def __aexit__(self, *_a):
        return False


def _patch_thread(mp):
    mp.setattr(asyncio, "to_thread", _araise)


def _patch_peer(mp):
    mp.setattr(web, "_PeerClient", _BoomClient)


def _patch(target, attr):
    def apply(mp):
        import importlib
        mp.setattr(importlib.import_module(target), attr, _raise)
    return apply


ROUTES = [
    ("/api/maintenance", _patch("atlas_plus.brain.maintenance", "summarize_maintenance")),
    ("/api/security/audit", _patch("atlas_plus.web.security_status", "read_audit_index")),
    ("/api/loop/status", _patch("atlas_plus.web.loop_status", "read_loop_state")),
    ("/api/agents", _patch_peer),
    ("/api/tasks", _patch("atlas_plus.brain.tasks", "list_tasks")),
    ("/api/chat/history?session_id=s1", _patch_peer),
    ("/api/improvement/stats", _patch_thread),
    ("/api/pending-facts", _patch_peer),
    ("/api/proposals", _patch_peer),
    ("/api/memory/gaps", _patch_thread),
    ("/api/memory/next-actions-panel", _patch_thread),
    ("/api/memory/convergence", _patch_thread),
    ("/api/memory/decisions", _patch_thread),
    ("/api/memory/rederivable", _patch_thread),
    ("/api/memory/curation-report-panel", _patch_thread),
    ("/api/memory/topics/html", _patch_thread),
    ("/api/temporal/stats", _patch_thread),
    ("/api/temporal/query?q=x", _patch_thread),
    ("/api/temporal/history-panel?entity=x", _patch_thread),
    ("/api/inbox", _patch("atlas_plus.inbox.store", "list_items")),
    ("/api/tickets/board", _patch("atlas_plus.tickets.store", "list_tickets")),
    ("/api/operator/queue-html", _patch("atlas_plus.tickets.store", "list_tickets")),
    ("/api/brain/sessions", _patch_thread),
    ("/api/brain/sessions/s1/html", _patch_thread),
]


@pytest.mark.parametrize("route,apply", ROUTES, ids=[r for r, _ in ROUTES])
def test_failed_panel_names_next_step_not_the_exception(route, apply, monkeypatch, caplog):
    monkeypatch.setattr(web, "BRAIN_URL", "http://127.0.0.1:9")
    apply(monkeypatch)
    client = TestClient(web.app, raise_server_exceptions=False)
    with caplog.at_level(logging.WARNING, logger="atlas.web"):
        r = client.get(route, headers={"Authorization": "Bearer " + os.environ["ATLAS_WEB_API_KEY"]})
    body = r.text
    assert r.status_code == 200, (r.status_code, body[:300])
    assert SENTINEL not in body and "secret" not in body, body
    assert "Traceback" not in body
    assert len(body) < 2000
    assert "Next:" in body and _next_step.command("doctor").strip("`") in body, body
    assert "RuntimeError" in body  # the class name says what kind of failure it was
    assert SENTINEL in caplog.text  # the detail went to the log, not nowhere
