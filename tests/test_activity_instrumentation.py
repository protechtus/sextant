"""Each instrumented component must pulse the activity feed -- and never
let the pulse break the operation itself."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas_plus import activity


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))
    return tmp_path / ".atlas"


def _events() -> list[dict]:
    return activity.tail(limit=1000)


class TestBrainTaskEvents:
    def test_mark_done_emits_task_done(self, state, tmp_path, monkeypatch):
        from atlas_plus.brain import tasks
        db = tmp_path / "state.sqlite"
        t = tasks.create_task("test instruction", db_path=db)
        tasks.mark_running(t.task_id, db_path=db, lease_owner="w1")
        tasks.mark_done(t.task_id, "ok", db_path=db)
        events = [e for e in _events() if e["source"] == "brain"]
        kinds = [e["event"] for e in events]
        assert "task_running" in kinds
        assert "task_done" in kinds
        done = [e for e in events if e["event"] == "task_done"][0]
        assert done["task_id"] == t.task_id

    def test_activity_failure_never_breaks_the_task(self, state, tmp_path, monkeypatch):
        from atlas_plus.brain import tasks
        monkeypatch.setattr(
            tasks, "_record_activity",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        db = tmp_path / "state.sqlite"
        t = tasks.create_task("test instruction", db_path=db)
        with pytest.raises(RuntimeError):
            tasks.mark_running(t.task_id, db_path=db)
        # the helper itself must swallow, so patch the inner recorder instead
        monkeypatch.undo()
        monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
        monkeypatch.setattr(
            activity, "record",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        tasks.mark_done(t.task_id, "ok", db_path=db)  # must not raise


class TestTicketEvents:
    def test_create_and_status_emit(self, state, tmp_path, monkeypatch):
        from atlas_plus.tickets import store
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "1")
        root = tmp_path / "tickets"
        t = store.create_ticket("Test ticket", "testproj", tickets_root=root)
        monkeypatch.setattr(store, "TICKETS_ROOT", root)
        store.update_ticket_status(t.id, "done", tickets_root=root)
        events = [e for e in _events() if e["source"] == "tickets"]
        assert [e["event"] for e in events] == ["ticket_created", "ticket_status"]
        assert events[1]["status"] == "done"
        # every ticket event points at the backing markdown file
        assert events[0]["ref"] == f"tickets/testproj/{t.id}.md"
        assert events[1]["ref"] == f"tickets/testproj/{t.id}.md"


class TestSyncEvents:
    def test_audit_mirrors_to_activity(self, state, tmp_path):
        from atlas_plus.sync import store as sync_store
        sync_store.audit("checkin", root=tmp_path / "sync", device="dev-1")
        events = [e for e in _events() if e["source"] == "sync"]
        assert events and events[-1]["event"] == "checkin"
        assert events[-1]["device"] == "dev-1"


class TestTemporalEvents:
    def test_fixture_build_emits_graph_built(self, state, tmp_path):
        from atlas_plus.temporal_graph.build import build_temporal_graph
        result = build_temporal_graph(source="fixtures", graph_root=tmp_path / "tg")
        assert "error" not in result
        events = [e for e in _events() if e["source"] == "temporal"]
        assert events and events[-1]["event"] == "graph_built"
        assert events[-1]["facts_active"] == result["index"]["facts_active"]


class TestMcpToolEvents:
    def test_wrapped_tool_records_name_and_duration(self, state):
        from atlas_plus.mcp.memory_server import server
        result = server.now_state_tool()
        assert isinstance(result, dict)
        events = [e for e in _events() if e["source"] == "mcp"]
        assert events, "a tool call must pulse the feed"
        last = events[-1]
        assert last["event"] == "tool_called"
        assert last["tool"] == "now_state_tool"
        assert last["ok"] is True
        assert isinstance(last["ms"], (int, float))

    def test_reference_args_are_recorded_but_content_args_are_not(self, state):
        """Operator request 2026-10-12: the Map must show WHAT is being
        communicated with a reference to WHERE the data lives. References
        only -- path/prefix/project/ids/truncated query -- never bodies."""
        from atlas_plus.mcp.memory_server import server
        server.memory_list_tool(prefix="identity", limit=5)
        events = [e for e in _events() if e["source"] == "mcp"]
        assert events[-1]["ref"] == "prefix=identity"
        # the allowlist must never grow a content-bearing field
        for forbidden in ("body", "content", "text", "fact", "instruction", "note"):
            assert forbidden not in server._REF_ARGS

    def test_ref_values_are_truncated(self):
        from atlas_plus.mcp.memory_server import server
        fields = server._ref_fields({"path": "x" * 500, "body": "secret " * 50})
        assert len(fields["ref"]) <= 200
        assert "secret" not in fields["ref"]

    def test_wrapper_preserves_tool_registration(self):
        """Every registered tool must still carry its real name and doc --
        FastMCP builds the public schema from these."""
        import anyio
        from atlas_plus.mcp.memory_server import server

        tools = anyio.run(server.mcp.list_tools)
        names = {t.name for t in tools}
        assert "memory_search_tool" in names
        assert "now_state_tool" in names
        by_name = {t.name: t for t in tools}
        assert "query" in json.dumps(by_name["memory_search_tool"].inputSchema), (
            "wrapped tool lost its parameter schema"
        )
