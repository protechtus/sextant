"""Tests for atlas_plus.autonomous -- ATLAS_AUTONOMOUS kill switch."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest


class TestRequireAutonomousWrites:
    """require_autonomous_writes() raises when autonomous writes are disabled."""

    def test_raises_when_env_unset(self):
        # FAIL-CLOSED (ATL-013): unset must now BLOCK writes, not allow them.
        from atlas_plus.autonomous import require_autonomous_writes
        import pytest
        env = {k: v for k, v in os.environ.items() if k != "ATLAS_AUTONOMOUS"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="blocked"):
                require_autonomous_writes()

    def test_passes_when_on(self):
        from atlas_plus.autonomous import require_autonomous_writes
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "on"}):
            require_autonomous_writes()  # must not raise

    def test_raises_when_off(self):
        from atlas_plus.autonomous import require_autonomous_writes
        import pytest
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "off"}):
            with pytest.raises(RuntimeError, match="blocked"):
                require_autonomous_writes()

    def test_raises_when_zero(self):
        from atlas_plus.autonomous import require_autonomous_writes
        import pytest
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "0"}):
            with pytest.raises(RuntimeError):
                require_autonomous_writes()

    def test_raises_when_false(self):
        from atlas_plus.autonomous import require_autonomous_writes
        import pytest
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "false"}):
            with pytest.raises(RuntimeError):
                require_autonomous_writes()

    def test_error_message_includes_operation(self):
        from atlas_plus.autonomous import require_autonomous_writes
        import pytest
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "off"}):
            with pytest.raises(RuntimeError, match="backup"):
                require_autonomous_writes("backup")


class TestIsSessionAutonomous:
    """is_session_autonomous() resolution order: SESSION > AUTONOMOUS > default."""

    def test_session_on_overrides_autonomous_off(self):
        from atlas_plus.autonomous import is_session_autonomous
        with patch.dict(os.environ,
                        {"ATLAS_SESSION_AUTONOMOUS": "on", "ATLAS_AUTONOMOUS": "off"}):
            assert is_session_autonomous() is True

    def test_session_off_overrides_autonomous_on(self):
        from atlas_plus.autonomous import is_session_autonomous
        with patch.dict(os.environ,
                        {"ATLAS_SESSION_AUTONOMOUS": "off", "ATLAS_AUTONOMOUS": "on"}):
            assert is_session_autonomous() is False

    def test_session_not_set_falls_back_to_autonomous_on(self):
        from atlas_plus.autonomous import is_session_autonomous
        env = {k: v for k, v in os.environ.items()
               if k not in ("ATLAS_SESSION_AUTONOMOUS", "ATLAS_AUTONOMOUS")}
        env["ATLAS_AUTONOMOUS"] = "on"
        with patch.dict(os.environ, env, clear=True):
            assert is_session_autonomous() is True

    def test_session_not_set_falls_back_to_autonomous_off(self):
        from atlas_plus.autonomous import is_session_autonomous
        env = {k: v for k, v in os.environ.items()
               if k not in ("ATLAS_SESSION_AUTONOMOUS", "ATLAS_AUTONOMOUS")}
        env["ATLAS_AUTONOMOUS"] = "off"
        with patch.dict(os.environ, env, clear=True):
            assert is_session_autonomous() is False

    def test_both_unset_defaults_to_false(self):
        # FAIL-CLOSED (ATL-013): both unset must now resolve to False.
        from atlas_plus.autonomous import is_session_autonomous
        env = {k: v for k, v in os.environ.items()
               if k not in ("ATLAS_SESSION_AUTONOMOUS", "ATLAS_AUTONOMOUS")}
        with patch.dict(os.environ, env, clear=True):
            assert is_session_autonomous() is False

    def test_session_one_is_true(self):
        from atlas_plus.autonomous import is_session_autonomous
        with patch.dict(os.environ, {"ATLAS_SESSION_AUTONOMOUS": "1"}):
            assert is_session_autonomous() is True

    def test_session_zero_is_false(self):
        from atlas_plus.autonomous import is_session_autonomous
        with patch.dict(os.environ, {"ATLAS_SESSION_AUTONOMOUS": "0"}):
            assert is_session_autonomous() is False


class TestPrimitiveGating:
    """Audit HIGH-3 / ATL-013: the gate must be enforced at the write PRIMITIVE, so
    every entrypoint (CLI, curation, suggest-facts) is covered, not just MCP wrappers."""

    def test_append_fact_blocked_when_off(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import append_fact
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "off"}):
            with pytest.raises(RuntimeError, match="blocked"):
                append_fact(tmp_path / "f.md", "a fact", {})
        assert not (tmp_path / "f.md").exists()  # nothing written

    def test_append_fact_allowed_when_on(self, tmp_path):
        from atlas_plus.mcp.memory_server.routing import append_fact
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "on"}):
            append_fact(tmp_path / "f.md", "a fact", {})
        assert (tmp_path / "f.md").exists()

    def test_create_ticket_blocked_when_off(self, tmp_path):
        from atlas_plus.tickets.store import create_ticket
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "off"}):
            with pytest.raises(RuntimeError, match="blocked"):
                create_ticket("a title", "testproj", tickets_root=tmp_path)

    def test_update_status_NOT_gated_lifecycle(self, tmp_path):
        # Status transitions are a lifecycle op (task close / reopen) -- must work even when off.
        from atlas_plus.tickets.store import create_ticket, update_ticket_status
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "on"}):
            t = create_ticket("a title", "testproj", tickets_root=tmp_path)
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "off"}):
            updated = update_ticket_status(t.id, "in-progress", tickets_root=tmp_path)
        assert updated.status == "in-progress"


class TestFailClosedAllowlist:
    """The gate must be an ALLOWLIST (fixed 2026-07-02): the original denylist check
    (off/0/false) meant any garbage value -- a typo like "of", a stray "yes", a corrupted
    env -- silently failed OPEN, inverting the ATL-013 fail-closed hardening."""

    def test_garbage_values_block_writes(self, monkeypatch):
        import pytest
        from atlas_plus.autonomous import require_autonomous_writes
        for garbage in ("of", "onn", "yes", "enabled", "ON_", " on"):
            monkeypatch.setenv("ATLAS_AUTONOMOUS", garbage)
            with pytest.raises(RuntimeError):
                require_autonomous_writes("test")

    def test_all_allowlist_values_pass(self, monkeypatch):
        from atlas_plus.autonomous import require_autonomous_writes
        for ok in ("on", "1", "true", "ON", "True"):
            monkeypatch.setenv("ATLAS_AUTONOMOUS", ok)
            require_autonomous_writes("test")  # must not raise

    def test_session_fallback_is_fail_closed_on_garbage(self, monkeypatch):
        from atlas_plus.autonomous import is_session_autonomous
        monkeypatch.delenv("ATLAS_SESSION_AUTONOMOUS", raising=False)
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "yes")   # garbage -> closed
        assert is_session_autonomous() is False

    def test_error_message_shows_actual_value(self, monkeypatch):
        import pytest
        from atlas_plus.autonomous import require_autonomous_writes
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "onn")
        with pytest.raises(RuntimeError, match="'onn'"):
            require_autonomous_writes("test")
