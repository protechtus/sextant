"""Unit tests for the MCP server hard-lockout (audit finding R3).

The MCP server previously only ALERTED on repeated auth failures; these cover the
added lockout that blocks a brute-forcing IP with 429 after a threshold, while a
legitimate token holder (no failures) is never affected.
"""
from __future__ import annotations

import time

import pytest

from atlas_plus.mcp.memory_server import server


@pytest.fixture(autouse=True)
def _clean_state():
    server._AUTH_FAILURES.clear()
    server._LOCKED_OUT.clear()
    server._AUTH_ALERTED.clear()
    yield
    server._AUTH_FAILURES.clear()
    server._LOCKED_OUT.clear()
    server._AUTH_ALERTED.clear()


def test_below_threshold_is_not_locked(monkeypatch):
    monkeypatch.setattr(server, "_LOCKOUT_THRESHOLD", 5)
    for _ in range(4):
        server._record_auth_failure("9.9.9.9", "/x")
    assert server._lockout_remaining("9.9.9.9") == 0.0


def test_reaching_threshold_locks_out(monkeypatch):
    monkeypatch.setattr(server, "_LOCKOUT_THRESHOLD", 3)
    monkeypatch.setattr(server, "_LOCKOUT_SECONDS", 300.0)
    for _ in range(3):
        server._record_auth_failure("6.6.6.6", "/x")
    assert server._lockout_remaining("6.6.6.6") > 0.0


def test_unrelated_ip_not_locked(monkeypatch):
    monkeypatch.setattr(server, "_LOCKOUT_THRESHOLD", 3)
    for _ in range(3):
        server._record_auth_failure("6.6.6.6", "/x")
    # A different IP (e.g. the legitimate client) is unaffected.
    assert server._lockout_remaining("1.2.3.4") == 0.0


def test_expired_lockout_clears(monkeypatch):
    monkeypatch.setattr(server, "_LOCKOUT_THRESHOLD", 3)
    # Simulate a lockout that already expired.
    server._LOCKED_OUT["7.7.7.7"] = time.monotonic() - 1.0
    assert server._lockout_remaining("7.7.7.7") == 0.0
    assert "7.7.7.7" not in server._LOCKED_OUT  # cleaned up on read


def test_alert_fires_before_lockout(monkeypatch):
    # Alert threshold below lockout threshold: operator is warned first.
    monkeypatch.setattr(server, "_AUTH_ALERT_THRESHOLD", 2)
    monkeypatch.setattr(server, "_LOCKOUT_THRESHOLD", 4)
    alerts = []
    monkeypatch.setattr(server, "_alert_auth_spike", lambda ip, n, path: alerts.append((ip, n)))
    for _ in range(2):
        server._record_auth_failure("5.5.5.5", "/x")
    assert alerts and server._lockout_remaining("5.5.5.5") == 0.0  # alerted, not yet locked
    for _ in range(2):
        server._record_auth_failure("5.5.5.5", "/x")
    assert server._lockout_remaining("5.5.5.5") > 0.0  # now locked
