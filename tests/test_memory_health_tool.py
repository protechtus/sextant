"""memory_health must not report healthy when the memory root is missing.

Persona finding (2026-10-16 batch 3): a user who skips sextant-init, or whose
ATLAS_MEMORY_ROOT never reached the MCP server process (set only in .zshrc
while the client was GUI-launched), got {"status": "ok"} and stopped
investigating. The docs' own verify step then reads as a false pass.
"""
from __future__ import annotations

from unittest.mock import patch

from atlas_plus.mcp.memory_server import server


class TestMemoryHealth:
    def test_ok_when_root_exists(self, tmp_path):
        with patch.object(server, "MEMORY_ROOT", tmp_path):
            out = server.memory_health()
        assert out == {"status": "ok", "memory_root_exists": True, "memory_root": str(tmp_path)}

    def test_degraded_with_hint_when_root_missing(self, tmp_path):
        missing = tmp_path / "no-such-root"
        with patch.object(server, "MEMORY_ROOT", missing):
            out = server.memory_health()
        assert out["status"] == "degraded"
        assert out["memory_root_exists"] is False
        assert "sextant-init" in out["hint"]
        assert "ATLAS_MEMORY_ROOT" in out["hint"]
        assert out["memory_root"] == str(missing)
