"""Tests for the control-panel auth-failure log + throttle (ATL-014).

Two layers:
  - Pure unit tests of AuthFailureLog (no FastAPI, time injected).
  - In-process integration tests via FastAPI TestClient that exercise the real
    _check_auth wiring: a 401 is recorded, repeated failures trip a 429, and the
    /api/security/auth-failures endpoint surfaces them (and requires auth itself).
"""
from __future__ import annotations

import pytest

from atlas_plus.web.auth_failures import AuthFailureLog


# --- pure unit tests --------------------------------------------------------

class TestAuthFailureLog:
    def test_record_and_recent_newest_first(self):
        log = AuthFailureLog()
        log.record(path="/a", client_ip="1.1.1.1", reason="bad_key", now=1.0)
        log.record(path="/b", client_ip="2.2.2.2", reason="missing_key", now=2.0)
        recent = log.recent()
        assert [r.path for r in recent] == ["/b", "/a"]  # newest first

    def test_ring_buffer_is_bounded(self):
        log = AuthFailureLog(max_records=3)
        for i in range(10):
            log.record(path=f"/{i}", client_ip="x", now=float(i))
        recent = log.recent(limit=100)
        assert len(recent) == 3
        assert [r.path for r in recent] == ["/9", "/8", "/7"]

    def test_failures_from_respects_window(self):
        log = AuthFailureLog(window_seconds=60.0)
        log.record(path="/x", client_ip="9.9.9.9", now=100.0)
        log.record(path="/x", client_ip="9.9.9.9", now=150.0)
        # at now=170, both are within the last 60s -> wait, 100 is 70s ago
        assert log.failures_from("9.9.9.9", now=170.0) == 1   # only the 150.0 one
        assert log.failures_from("9.9.9.9", now=155.0) == 2   # both within window

    def test_failures_from_matches_cf_ip(self):
        log = AuthFailureLog()
        log.record(path="/x", client_ip="127.0.0.1", cf_ip="8.8.8.8", now=1.0)
        assert log.failures_from("8.8.8.8", now=2.0) == 1
        assert log.failures_from("127.0.0.1", now=2.0) == 0      # a forwarded failure belongs to its client, not the proxy (AG47)
        assert log.failures_via_peer("127.0.0.1", now=2.0) == 1  # but the proxy's own flood is visible (AG44)

    def test_empty_ip_never_counts_or_limits(self):
        log = AuthFailureLog()
        log.record(path="/x", client_ip="", cf_ip="", now=1.0)
        assert log.failures_from("", now=2.0) == 0
        assert log.is_rate_limited("", now=2.0) is False

    def test_is_rate_limited_threshold(self):
        log = AuthFailureLog(max_failures_per_window=3, window_seconds=60.0)
        for i in range(2):
            log.record(path="/x", client_ip="5.5.5.5", now=float(i))
        assert log.is_rate_limited("5.5.5.5", now=10.0) is False  # 2 < 3
        log.record(path="/x", client_ip="5.5.5.5", now=3.0)
        assert log.is_rate_limited("5.5.5.5", now=10.0) is True   # 3 >= 3

    def test_clear(self):
        log = AuthFailureLog()
        log.record(path="/x", client_ip="1.1.1.1", now=1.0)
        log.clear()
        assert log.recent() == []
        assert log.stats()["total"] == 0

    def test_as_dict_shape_prefers_cf_ip(self):
        log = AuthFailureLog()
        rec = log.record(path="/x", client_ip="127.0.0.1", cf_ip="3.3.3.3",
                         reason="bad_key", now=0.0)
        d = rec.as_dict()
        assert d["source_ip"] == "3.3.3.3"          # real source preferred
        assert d["reason"] == "bad_key"
        assert "iso" in d and d["iso"].startswith("1970-01-01")

    def test_stats_counts_window_and_sources(self):
        log = AuthFailureLog(window_seconds=60.0)
        log.record(path="/x", client_ip="a", now=100.0)
        log.record(path="/x", client_ip="b", now=150.0)
        s = log.stats(now=155.0)
        assert s["total"] == 2
        assert s["in_window"] == 2
        assert s["distinct_sources_in_window"] == 2


# --- integration tests via TestClient ---------------------------------------

@pytest.fixture
def web(monkeypatch):
    """Wire a known API key into the live app module + reset the shared log."""
    from fastapi.testclient import TestClient

    from atlas_plus.web import app as webapp

    key = "integration-test-key"
    monkeypatch.setattr(webapp, "API_KEY", key)
    monkeypatch.setattr(webapp, "_ALLOW_NO_AUTH", False)
    webapp.AUTH_FAILURES.clear()
    client = TestClient(webapp.app, raise_server_exceptions=False)
    return client, webapp, key


def test_unauthenticated_request_records_failure(web):
    client, webapp, _key = web
    r = client.get("/api/security/auth-failures")  # no key
    assert r.status_code == 401
    assert webapp.AUTH_FAILURES.stats()["total"] == 1
    assert webapp.AUTH_FAILURES.recent()[0].reason == "missing_key"


def test_bad_key_recorded_with_reason(web):
    client, webapp, _key = web
    r = client.get("/api/security/auth-failures", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    assert webapp.AUTH_FAILURES.recent()[0].reason == "bad_key"


def test_repeated_failures_trip_429(web):
    client, webapp, _key = web
    limit = webapp.AUTH_FAILURES.max_failures_per_window
    for _ in range(limit):
        assert client.get("/api/security/auth-failures").status_code == 401
    # next attempt is throttled even though it is the same (missing) credential
    r = client.get("/api/security/auth-failures")
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_endpoint_returns_records_with_valid_key(web):
    client, webapp, key = web
    client.get("/api/tasks", headers={"Authorization": "Bearer wrong"})  # 1 failure
    r = client.get("/api/security/auth-failures", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    data = r.json()
    assert data["stats"]["total"] >= 1
    assert data["failures"][0]["reason"] == "bad_key"
    assert data["failures"][0]["path"] == "/api/tasks"


def test_html_panel_renders(web):
    client, webapp, key = web
    auth = {"Authorization": f"Bearer {key}"}
    # empty state
    r = client.get("/api/security/auth-failures.html", headers=auth)
    assert r.status_code == 200
    assert "No auth failures recorded" in r.text
    # with a failure present
    client.get("/api/tasks", headers={"Authorization": "Bearer wrong"})
    r = client.get("/api/security/auth-failures.html", headers=auth)
    assert r.status_code == 200
    assert "/api/tasks" in r.text and "bad_key" in r.text


def test_status_bar_includes_web_and_autonomous(web):
    # ATL-014 #4: status bar surfaces the Web UI itself + autonomous-write mode.
    # (conftest's autouse fixture sets ATLAS_AUTONOMOUS=on for the suite.)
    client, webapp, key = web
    r = client.get("/api/status", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert "web: up" in r.text
    assert "autonomous: on" in r.text
