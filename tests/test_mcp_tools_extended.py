"""Extended tests for MCP memory server tools: read, search_log, remember helpers, tickets."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# memory_read tool (tools/read.py)
# ---------------------------------------------------------------------------

class TestSafeResolve:
    def test_traversal_rejected(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.read as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod._safe_resolve("../etc/passwd")
        assert result is None

    def test_absolute_path_rejected(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.read as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod._safe_resolve("/absolute/path")
        assert result is None

    def test_null_byte_rejected(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.read as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod._safe_resolve("feed\x00back/file.md")
        assert result is None

    def test_valid_path_resolves(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.read as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod._safe_resolve("feedback/pref.md")
        assert result is not None

    def test_excluded_dir_rejected(self, tmp_path):
        # `archive` is in the default EXCLUDED_DIRS; _safe_resolve now delegates to the
        # shared confiner (audit HIGH-2), so no _EXCLUDED patch is needed.
        import atlas_plus.mcp.memory_server.tools.read as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod._safe_resolve("archive/old.md")
        assert result is None

    def test_dot_dir_rejected(self, tmp_path):
        # Regression for audit HIGH-2: .git/config (and any dotfile) must be blocked.
        import atlas_plus.mcp.memory_server.tools.read as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            assert mod._safe_resolve(".git/config") is None


class TestMemoryRead:
    def test_invalid_path_returns_error(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.read as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.memory_read("../etc/passwd")
        assert result["error"] == "invalid path"
        assert result["frontmatter"] == {}
        assert result["body"] == ""

    def test_not_found_returns_error(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.read as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.memory_read("feedback/missing.md")
        assert result["error"] == "not found"

    def test_reads_file_with_frontmatter(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.read as mod
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        (feedback / "pref.md").write_text(
            "---\ntitle: Preference\ntype: feedback\n---\n\nThe body text.",
            encoding="utf-8",
        )
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.memory_read("feedback/pref.md")
        assert result["error"] is None
        assert result["frontmatter"]["title"] == "Preference"
        assert "body text" in result["body"]
        assert result["truncated"] is False

    def test_reads_file_without_frontmatter(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.read as mod
        ref = tmp_path / "reference"
        ref.mkdir()
        (ref / "note.md").write_text("Just plain text.", encoding="utf-8")
        with patch.object(mod, "MEMORY_ROOT", tmp_path):
            result = mod.memory_read("reference/note.md")
        assert result["error"] is None
        assert result["frontmatter"] == {}
        assert "plain text" in result["body"]

    def test_truncated_flag_set_for_large_file(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.read as mod
        ref = tmp_path / "reference"
        ref.mkdir()
        big_content = "x" * 200_000
        (ref / "big.md").write_bytes(big_content.encode())
        with patch.object(mod, "MEMORY_ROOT", tmp_path), \
             patch.object(mod, "MAX_READ_BYTES", 100):
            result = mod.memory_read("reference/big.md")
        assert result["truncated"] is True


# ---------------------------------------------------------------------------
# search_log tool (tools/search_log.py)
# ---------------------------------------------------------------------------

class TestLogSearch:
    def test_creates_log_file(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.search_log as mod
        log_file = tmp_path / "logs" / "mcp-search.ndjson"
        with patch.object(mod, "LOG_FILE", log_file):
            mod.log_search("SIP registration", "reference", 0.8, 3)
        assert log_file.exists()

    def test_is_miss_when_zero_results(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.search_log as mod
        log_file = tmp_path / "logs" / "mcp-search.ndjson"
        with patch.object(mod, "LOG_FILE", log_file):
            mod.log_search("obscure query", "", 0.0, 0)
        record = json.loads(log_file.read_text())
        assert record["miss"] is True

    def test_is_miss_when_low_score(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.search_log as mod
        log_file = tmp_path / "logs" / "mcp-search.ndjson"
        with patch.object(mod, "LOG_FILE", log_file):
            mod.log_search("another query", "", 0.1, 2)
        record = json.loads(log_file.read_text())
        assert record["miss"] is True

    def test_not_miss_when_good_score(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.search_log as mod
        log_file = tmp_path / "logs" / "mcp-search.ndjson"
        with patch.object(mod, "LOG_FILE", log_file):
            mod.log_search("SIP", "reference", 0.9, 5)
        record = json.loads(log_file.read_text())
        assert record["miss"] is False

    def test_silently_swallows_io_error(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.search_log as mod
        # Put a FILE where a parent directory is required: mkdir(parents=True) then raises on
        # EVERY OS, so this genuinely exercises the swallow path AND leaks nothing outside tmp_path.
        # The old path "/dev/null/this/cannot/exist/..." only raised on Unix; on Windows it
        # resolved to C:\dev\null\... (an ordinary, creatable folder), so log_search SUCCEEDED --
        # the error branch was never tested and the test created real junk dirs at the drive root.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        bad_path = blocker / "sub" / "mcp-search.ndjson"
        with patch.object(mod, "LOG_FILE", bad_path):
            mod.log_search("query", "", 0.5, 1)  # must NOT raise
        assert not bad_path.exists()


class TestLoadLog:
    def test_returns_empty_when_missing(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.search_log as mod
        log_file = tmp_path / "logs" / "mcp-search.ndjson"
        with patch.object(mod, "LOG_FILE", log_file):
            result = mod.load_log()
        assert result == []

    def test_parses_recent_records(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.search_log as mod
        from datetime import datetime, timezone
        log_file = tmp_path / "logs" / "mcp-search.ndjson"
        log_file.parent.mkdir(parents=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "query": "SIP", "scope": "", "top_score": 0.9,
            "result_count": 3, "miss": False, "caller_identity": "test",
        }
        log_file.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with patch.object(mod, "LOG_FILE", log_file):
            result = mod.load_log(since_hours=24)
        assert len(result) == 1
        assert result[0]["query"] == "SIP"

    def test_skips_old_records(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.search_log as mod
        log_file = tmp_path / "logs" / "mcp-search.ndjson"
        log_file.parent.mkdir(parents=True)
        old_record = {
            "ts": "2020-01-01T00:00:00+00:00",
            "query": "old", "scope": "", "top_score": 0.5,
            "result_count": 1, "miss": False, "caller_identity": "test",
        }
        log_file.write_text(json.dumps(old_record) + "\n", encoding="utf-8")
        with patch.object(mod, "LOG_FILE", log_file):
            result = mod.load_log(since_hours=1)
        assert result == []

    def test_skips_malformed_lines(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.search_log as mod
        log_file = tmp_path / "logs" / "mcp-search.ndjson"
        log_file.parent.mkdir(parents=True)
        log_file.write_text("not-json\n{broken\n", encoding="utf-8")
        with patch.object(mod, "LOG_FILE", log_file):
            result = mod.load_log()
        assert result == []


class TestTopMissQueries:
    def test_empty_when_no_log(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.search_log as mod
        log_file = tmp_path / "mcp-search.ndjson"
        with patch.object(mod, "LOG_FILE", log_file):
            result = mod.top_miss_queries()
        assert result == []

    def test_groups_by_normalized_query(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.search_log as mod
        from datetime import datetime, timezone
        log_file = tmp_path / "logs" / "mcp-search.ndjson"
        log_file.parent.mkdir(parents=True)
        now = datetime.now(timezone.utc).isoformat()
        lines = [
            {"ts": now, "query": "SIP Registration", "scope": "", "top_score": 0.1,
             "result_count": 0, "miss": True, "caller_identity": "test"},
            {"ts": now, "query": "sip registration", "scope": "", "top_score": 0.1,
             "result_count": 0, "miss": True, "caller_identity": "test"},
        ]
        log_file.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
        with patch.object(mod, "LOG_FILE", log_file):
            result = mod.top_miss_queries()
        assert len(result) == 1
        assert result[0]["miss_count"] == 2

    def test_skips_non_miss_queries(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.search_log as mod
        from datetime import datetime, timezone
        log_file = tmp_path / "logs" / "mcp-search.ndjson"
        log_file.parent.mkdir(parents=True)
        now = datetime.now(timezone.utc).isoformat()
        record = {"ts": now, "query": "SIP", "scope": "", "top_score": 0.9,
                  "result_count": 5, "miss": False, "caller_identity": "test"}
        log_file.write_text(json.dumps(record) + "\n", encoding="utf-8")
        with patch.object(mod, "LOG_FILE", log_file):
            result = mod.top_miss_queries()
        assert result == []


# ---------------------------------------------------------------------------
# remember tool helpers (tools/remember.py)
# ---------------------------------------------------------------------------

class TestJaccard:
    def test_identical_strings(self):
        from atlas_plus.mcp.memory_server.tools.remember import _jaccard
        assert _jaccard("hello world this test", "hello world this test") == 1.0

    def test_no_overlap(self):
        from atlas_plus.mcp.memory_server.tools.remember import _jaccard
        result = _jaccard("atlas brain memory scheduler", "python django flask webapp")
        assert result == 0.0

    def test_partial_overlap(self):
        from atlas_plus.mcp.memory_server.tools.remember import _jaccard
        result = _jaccard("atlas brain memory scheduler", "atlas memory facts corpus")
        assert 0.0 < result < 1.0

    def test_short_strings_return_zero(self):
        from atlas_plus.mcp.memory_server.tools.remember import _jaccard
        assert _jaccard("hi", "hi there") == 0.0

    def test_symmetric(self):
        from atlas_plus.mcp.memory_server.tools.remember import _jaccard
        a = "atlas memory brain scheduler"
        b = "memory atlas corpus"
        assert abs(_jaccard(a, b) - _jaccard(b, a)) < 0.001


class TestFindDuplicate:
    def test_returns_none_when_few_words(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.remember as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.mcp.memory_server.fts.search", return_value=[]):
            result = mod._find_duplicate("hi", tmp_path / "feedback/pref.md")
        assert result is None

    def test_returns_none_when_fts_fails(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.remember as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.mcp.memory_server.fts.search",
                   side_effect=RuntimeError("db gone")):
            result = mod._find_duplicate(
                "atlas brain scheduler memory temporal worker", tmp_path / "x.md"
            )
        assert result is None

    def test_returns_none_when_no_similar_results(self, tmp_path):
        import atlas_plus.mcp.memory_server.tools.remember as mod
        with patch.object(mod, "MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.mcp.memory_server.fts.search", return_value=[]):
            result = mod._find_duplicate(
                "atlas memory brain scheduler temporal worker", tmp_path / "feedback/pref.md"
            )
        assert result is None


# ---------------------------------------------------------------------------
# tickets MCP tool (tools/tickets.py)
# ---------------------------------------------------------------------------

class TestMcpTicketsList:
    def test_returns_empty_list_when_no_tickets(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_list
        result = tickets_list(project="test", tickets_root=tmp_path)
        assert result == []

    def test_filters_by_status(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_list
        from atlas_plus.tickets.store import create_ticket
        create_ticket(title="A", project="test", assignee="human", tickets_root=tmp_path)
        all_tickets = tickets_list(project="test", tickets_root=tmp_path)
        todo_tickets = tickets_list(project="test", status="todo", tickets_root=tmp_path)
        assert len(all_tickets) >= 1
        assert all(t["status"] == "todo" for t in todo_tickets)

    def test_as_dict_has_expected_keys(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_list
        from atlas_plus.tickets.store import create_ticket
        create_ticket(title="Test ticket", project="p", assignee="human", tickets_root=tmp_path)
        result = tickets_list(project="p", tickets_root=tmp_path)
        assert len(result) == 1
        t = result[0]
        assert "id" in t
        assert "title" in t
        assert "status" in t


class TestMcpTicketsOpen:
    def test_returns_only_open_tickets(self, tmp_path):
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_open
        from atlas_plus.tickets.store import create_ticket, update_ticket_status
        t1 = create_ticket(title="Open", project="p", assignee="human", tickets_root=tmp_path)
        t2 = create_ticket(title="Done", project="p", assignee="human", tickets_root=tmp_path)
        update_ticket_status(t2.id, "done", tickets_root=tmp_path)
        result = tickets_open(project="p", tickets_root=tmp_path)
        ids = [t["id"] for t in result]
        assert t1.id in ids


# ---------------------------------------------------------------------------
# temporal_entities_tool (MCP tool 27)
# ---------------------------------------------------------------------------

class TestTemporalEntitiesTool:
    def test_returns_entities_list(self):
        from unittest.mock import MagicMock
        entity = MagicMock()
        entity.name = "Atlas MCP Tunnel"
        entity.labels = {"service"}
        entity.aliases = {"MCP"}
        entity.first_seen_at = "2026-01-01T00:00:00Z"
        entity.last_seen_at = "2026-05-21T00:00:00Z"

        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {"entities": 1, "facts_active": 5}
            instance.list_entities.return_value = [entity]
            from atlas_plus.mcp.memory_server.server import temporal_entities_tool
            result = temporal_entities_tool()

        assert "entities" in result
        assert "total" in result
        assert result["total"] == 1
        assert result["entities"][0]["name"] == "Atlas MCP Tunnel"

    def test_empty_graph_returns_empty_list(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {"entities": 0, "facts_active": 0}
            instance.list_entities.return_value = []
            from atlas_plus.mcp.memory_server.server import temporal_entities_tool
            result = temporal_entities_tool()

        assert result["entities"] == []
        assert result["total"] == 0

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
            instance.stats.return_value = {"entities": 1, "facts_active": 3}
            instance.list_entities.return_value = [entity]
            from atlas_plus.mcp.memory_server.server import temporal_entities_tool
            result = temporal_entities_tool()

        e = result["entities"][0]
        assert "name" in e
        assert "labels" in e
        assert "aliases" in e
        assert "first_seen_at" in e
        assert "last_seen_at" in e

    def test_error_returns_error_key(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   side_effect=RuntimeError("db not found")):
            from atlas_plus.mcp.memory_server.server import temporal_entities_tool
            result = temporal_entities_tool()
        assert "error" in result
        assert result["entities"] == []

    def test_limit_param_passed_through(self):
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            instance = mock_cls.return_value
            instance.stats.return_value = {"entities": 0, "facts_active": 0}
            instance.list_entities.return_value = []
            from atlas_plus.mcp.memory_server.server import temporal_entities_tool
            temporal_entities_tool(limit=10)

        instance.list_entities.assert_called_with(limit=10)


class TestMcpTicketsCreate:
    def test_blocked_without_autonomous(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_create
        result = tickets_create("Test", "p", tickets_root=tmp_path)
        assert "error" in result

    def test_creates_ticket_when_autonomous(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_create
        result = tickets_create("Test ticket", "p", assignee="human", tickets_root=tmp_path)
        assert "error" not in result or result.get("error") is None
        assert "id" in result

    def test_blocks_claude_code_assignee_without_dispatch_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        monkeypatch.delenv("ATLAS_ALLOW_REMOTE_DISPATCH", raising=False)
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_create
        result = tickets_create("Task", "p", assignee="claude-code", tickets_root=tmp_path)
        assert "error" in result
        assert "blocked" in result["error"].lower() or "dispatch" in result["error"].lower()

    def test_allows_claude_code_with_dispatch_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        monkeypatch.setenv("ATLAS_ALLOW_REMOTE_DISPATCH", "1")
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_create
        result = tickets_create("Task", "p", assignee="claude-code", tickets_root=tmp_path)
        assert "id" in result


class TestMcpTicketsUpdateStatus:
    def test_blocked_without_autonomous(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_update_status
        result = tickets_update_status("ATL-001", "done", tickets_root=tmp_path)
        assert "error" in result

    def test_updates_status_when_autonomous(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        from atlas_plus.tickets.store import create_ticket
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_update_status
        t = create_ticket(title="Ticket", project="p", assignee="human", tickets_root=tmp_path)
        result = tickets_update_status(t.id, "done", tickets_root=tmp_path)
        assert "error" not in result or result.get("error") is None
        assert result["status"] == "done"

    def test_returns_error_for_unknown_ticket(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_update_status
        result = tickets_update_status("ATL-NONEXISTENT", "done", tickets_root=tmp_path)
        assert "error" in result


# ---------------------------------------------------------------------------
# temporal_history_tool
# ---------------------------------------------------------------------------

class TestTemporalHistoryTool:
    def _make_hist(self, entity_name="Atlas MCP Tunnel", n_facts=2):
        from unittest.mock import MagicMock
        entity = MagicMock()
        entity.entity_id = "e-001"
        entity.name = entity_name
        entity.labels = {"project"}
        entity.aliases = set()
        entity.first_seen_at = "2026-01-01T00:00:00Z"
        entity.last_seen_at = "2026-05-21T00:00:00Z"

        facts = []
        for i in range(n_facts):
            f = MagicMock()
            f.fact_text = f"Fact {i}"
            f.valid_from = "2026-01-01T00:00:00Z"
            f.invalid_at = None
            f.supersedes_fact_id = None
            f.confidence = 0.9
            f.predicate = "is"
            f.object_value = f"val {i}"
            facts.append(f)

        hist = MagicMock()
        hist.entity = entity
        hist.facts = facts
        hist.current_fact = facts[-1] if facts else None
        hist.warnings = []
        return hist

    def test_returns_entity_and_history(self):
        from unittest.mock import patch, MagicMock
        hist = self._make_hist(n_facts=3)
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            mock_cls.return_value.history.return_value = hist
            from atlas_plus.mcp.memory_server.server import temporal_history_tool
            result = temporal_history_tool("Atlas MCP Tunnel")
        assert result["entity"]["name"] == "Atlas MCP Tunnel"
        assert len(result["history"]) == 3

    def test_current_fact_is_populated(self):
        from unittest.mock import patch
        hist = self._make_hist(n_facts=2)
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            mock_cls.return_value.history.return_value = hist
            from atlas_plus.mcp.memory_server.server import temporal_history_tool
            result = temporal_history_tool("test")
        assert result["current"] is not None
        assert "fact" in result["current"]

    def test_empty_history_returns_empty_list(self):
        from unittest.mock import patch, MagicMock
        hist = MagicMock()
        hist.entity = MagicMock()
        hist.entity.entity_id = "e-001"
        hist.entity.name = "EmptyEntity"
        hist.entity.labels = set()
        hist.entity.aliases = set()
        hist.entity.first_seen_at = "2026-01-01T00:00:00Z"
        hist.entity.last_seen_at = "2026-01-01T00:00:00Z"
        hist.facts = []
        hist.current_fact = None
        hist.warnings = []
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider") as mock_cls:
            mock_cls.return_value.history.return_value = hist
            from atlas_plus.mcp.memory_server.server import temporal_history_tool
            result = temporal_history_tool("EmptyEntity")
        assert result["history"] == []
        assert result["current"] is None

    def test_error_returns_error_key(self):
        from unittest.mock import patch
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   side_effect=RuntimeError("db error")):
            from atlas_plus.mcp.memory_server.server import temporal_history_tool
            result = temporal_history_tool("anything")
        assert "error" in result
