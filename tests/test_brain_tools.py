"""Tests for Brain tool schema and dispatch wrappers."""

from __future__ import annotations

import json
import os

import pytest
from unittest.mock import MagicMock, patch

from atlas_plus.brain import tools


# ---------------------------------------------------------------------------
# TOOL_SCHEMAS structure
# ---------------------------------------------------------------------------

class TestToolSchemas:
    def test_all_schemas_have_type_function(self):
        for schema in tools.TOOL_SCHEMAS:
            assert schema["type"] == "function", f"bad type in {schema}"

    def test_all_schemas_have_name(self):
        names = [s["function"]["name"] for s in tools.TOOL_SCHEMAS]
        assert len(names) == len(set(names)), "duplicate tool names"
        for name in names:
            assert isinstance(name, str) and len(name) > 0

    def test_all_schemas_have_parameters(self):
        for schema in tools.TOOL_SCHEMAS:
            fn = schema["function"]
            assert "parameters" in fn
            assert fn["parameters"]["type"] == "object"
            assert "properties" in fn["parameters"]

    def test_expected_tool_names_present(self):
        names = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
        for expected in (
            "memory_search", "memory_read", "memory_list",
            "temporal_query", "temporal_history", "temporal_build",
            "tickets_list", "project_state",
            "memory_remember", "tickets_create", "tickets_status",
        ):
            assert expected in names, f"missing tool: {expected}"

    def test_read_tools_have_required_fields(self):
        read_names = {"memory_search", "memory_read", "temporal_query", "temporal_history"}
        for schema in tools.TOOL_SCHEMAS:
            fn = schema["function"]
            if fn["name"] in read_names:
                assert "required" in fn["parameters"], f"{fn['name']} missing required"

    def test_write_tools_present(self):
        names = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
        assert "memory_remember" in names
        assert "tickets_create" in names
        assert "tickets_status" in names


# ---------------------------------------------------------------------------
# _is_autonomous
# ---------------------------------------------------------------------------

class TestIsAutonomous:
    def test_off_by_default(self, monkeypatch):
        # ATL-013: unset ATLAS_AUTONOMOUS is fail-CLOSED (no autonomous writes).
        monkeypatch.delenv("ATLAS_AUTONOMOUS", raising=False)
        assert tools._is_autonomous() is False

    def test_off_when_set_to_off(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        assert tools._is_autonomous() is False

    def test_off_when_set_to_0(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "0")
        assert tools._is_autonomous() is False

    def test_on_when_set_to_on(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        assert tools._is_autonomous() is True


# ---------------------------------------------------------------------------
# dispatch -- write gate
# ---------------------------------------------------------------------------

class TestDispatchWriteGate:
    def test_memory_remember_blocked_when_not_autonomous(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        result = json.loads(tools.dispatch("memory_remember", {"scope": "feedback", "fact": "test"}))
        assert "error" in result
        assert "ATLAS_AUTONOMOUS" in result["error"]

    def test_tickets_create_blocked_when_not_autonomous(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        result = json.loads(tools.dispatch("tickets_create", {"title": "X", "project": "Y"}))
        assert "error" in result

    def test_tickets_status_blocked_when_not_autonomous(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        result = json.loads(tools.dispatch("tickets_status", {"ticket_id": "ATL-001", "status": "done"}))
        assert "error" in result

    def test_write_tool_allowed_when_autonomous(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        fake_result = {"ok": True, "file": "feedback/x.md"}
        with patch("atlas_plus.mcp.memory_server.tools.remember.memory_remember",
                   return_value=fake_result):
            result = json.loads(tools.dispatch("memory_remember", {"scope": "feedback", "fact": "x"}))
        assert "error" not in result
        assert result.get("ok") is True


# ---------------------------------------------------------------------------
# dispatch -- read tools
# ---------------------------------------------------------------------------

class TestDispatchReadTools:
    def test_memory_search_dispatches(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        fake = [{"path": "feedback/x.md", "score": 0.9, "snippet": "test"}]
        with patch("atlas_plus.mcp.memory_server.fts.search", return_value=fake):
            result = json.loads(tools.dispatch("memory_search", {"query": "test"}))
        assert isinstance(result, list)
        assert result[0]["path"] == "feedback/x.md"

    def test_memory_list_dispatches(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        fake = [{"path": "feedback/x.md"}]
        with patch("atlas_plus.mcp.memory_server.tools.list.memory_list", return_value=fake):
            result = json.loads(tools.dispatch("memory_list", {}))
        assert isinstance(result, list)

    def test_memory_read_dispatches(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        fake = {"path": "feedback/x.md", "content": "hello"}
        with patch("atlas_plus.mcp.memory_server.tools.read.memory_read", return_value=fake):
            result = json.loads(tools.dispatch("memory_read", {"path": "feedback/x.md"}))
        assert result["path"] == "feedback/x.md"

    def test_tickets_list_dispatches(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        mock_ticket = MagicMock()
        mock_ticket.as_dict.return_value = {"id": "ATL-001", "status": "todo"}
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[mock_ticket]):
            result = json.loads(tools.dispatch("tickets_list", {}))
        assert isinstance(result, list)
        assert result[0]["id"] == "ATL-001"

    def test_project_state_dispatches(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        fake = {"name": "atlas-memory+", "git": {"branch": "main"}}
        with patch("atlas_plus.mcp.memory_server.tools.project_state.project_state",
                   return_value=fake):
            result = json.loads(tools.dispatch("project_state", {"name": "atlas-memory+"}))
        assert result["name"] == "atlas-memory+"

    def test_temporal_build_dispatches(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        fake = {"source": "atlas", "facts_added": 3, "index": {"facts_active": 7}}
        monkeypatch.setattr(tools, "build_temporal_graph", lambda source="atlas": fake)
        result = json.loads(tools.dispatch("temporal_build", {"source": "atlas"}))
        assert result["facts_added"] == 3


# ---------------------------------------------------------------------------
# dispatch -- error handling
# ---------------------------------------------------------------------------

class TestDispatchErrorHandling:
    def test_unknown_tool_returns_error(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        result = json.loads(tools.dispatch("totally_unknown_tool", {}))
        assert "error" in result

    def test_exception_in_call_returns_error_json(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        with patch("atlas_plus.mcp.memory_server.fts.search", side_effect=RuntimeError("db locked")):
            result = json.loads(tools.dispatch("memory_search", {"query": "test"}))
        assert "error" in result
        assert "db locked" in result["error"]

    def test_dispatch_result_is_always_valid_json(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        for tool_name in ("memory_search", "memory_list", "unknown_xyz"):
            with patch("atlas_plus.mcp.memory_server.fts.search", return_value=[]), \
                 patch("atlas_plus.mcp.memory_server.tools.list.memory_list", return_value=[]):
                result_str = tools.dispatch(tool_name, {"query": "x"})
            json.loads(result_str)  # must not raise


# ---------------------------------------------------------------------------
# Legacy test (kept for regression)
# ---------------------------------------------------------------------------

def test_temporal_build_tool_schema_and_dispatch(monkeypatch):
    def fake_build_temporal_graph(source="atlas"):
        return {
            "source": source,
            "files_indexed": 1,
            "files_skipped": 2,
            "facts_added": 3,
            "warnings": [],
            "index": {
                "episodes": 4,
                "entities": 5,
                "facts_total": 6,
                "facts_active": 7,
            },
        }

    monkeypatch.setattr(tools, "build_temporal_graph", fake_build_temporal_graph, raising=False)

    names = {schema["function"]["name"] for schema in tools.TOOL_SCHEMAS}
    assert "temporal_build" in names

    payload = json.loads(tools.dispatch("temporal_build", {"source": "sessions"}))
    assert payload["source"] == "sessions"
    assert payload["index"]["facts_active"] == 7
