"""Helm service-control panel tests -- /api/services list + start/stop/restart actions.

The PowerShell runner is monkeypatched, so these never touch real scheduled tasks.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


def _client(monkeypatch, ps_impl):
    from atlas_plus.web import app as webapp

    monkeypatch.setattr(webapp, "_ALLOW_NO_AUTH", True, raising=False)
    monkeypatch.setattr(webapp, "API_KEY", "", raising=False)
    monkeypatch.setattr(webapp, "_run_powershell", ps_impl, raising=True)
    return TestClient(webapp.app)


class TestServicesPanel:
    def test_list_renders_states(self, monkeypatch):
        out = "Atlas-Brain\tRunning\t0\nAtlas-Web-UI\tReady\t0\n"
        client = _client(monkeypatch, lambda cmd, timeout=20.0: (0, out))
        r = client.get("/api/services")
        assert r.status_code == 200
        assert "Brain" in r.text and "Running" in r.text
        # Task name embeds as a JSON literal (then HTML-escaped) since the
        # run-22 onclick hardening; the action verb stays a plain literal.
        assert "svcAction(&quot;Atlas-Brain&quot;,'restart')" in r.text

    def test_restart_builds_stop_then_start(self, monkeypatch):
        calls = {}

        def fake(cmd, timeout=25.0):
            calls["cmd"] = cmd
            return (0, "ok")

        client = _client(monkeypatch, fake)
        r = client.post("/api/services/Atlas-Brain/restart")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True and d["task"] == "Atlas-Brain"
        assert "Stop-ScheduledTask" in calls["cmd"] and "Start-ScheduledTask" in calls["cmd"]

    def test_unknown_service_rejected(self, monkeypatch):
        client = _client(monkeypatch, lambda cmd, timeout=25.0: (0, ""))
        r = client.post("/api/services/EvilTask/restart")
        assert r.status_code == 400

    def test_unknown_action_rejected(self, monkeypatch):
        client = _client(monkeypatch, lambda cmd, timeout=25.0: (0, ""))
        r = client.post("/api/services/Atlas-Brain/nuke")
        assert r.status_code == 400

    def test_start_and_stop_commands(self, monkeypatch):
        seen = {"cmds": []}

        def fake(cmd, timeout=25.0):
            seen["cmds"].append(cmd)
            return (0, "")

        client = _client(monkeypatch, fake)
        client.post("/api/services/Atlas-MCP/start")
        client.post("/api/services/Atlas-MCP/stop")
        assert any("Start-ScheduledTask" in c for c in seen["cmds"])
        assert any("Stop-ScheduledTask" in c for c in seen["cmds"])
