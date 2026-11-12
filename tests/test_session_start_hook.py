"""Tests for atlas_plus.hooks.session_start -- session-start hook helpers."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# _read_file
# ---------------------------------------------------------------------------

class TestReadFile:
    def test_reads_plain_text(self, tmp_path):
        from atlas_plus.hooks.session_start import _read_file
        f = tmp_path / "test.md"
        f.write_text("Hello world\n", encoding="utf-8")
        result = _read_file(f)
        assert "Hello world" in result

    def test_strips_frontmatter(self, tmp_path):
        from atlas_plus.hooks.session_start import _read_file
        f = tmp_path / "test.md"
        f.write_text("---\ntitle: Test\n---\n\nActual content here.", encoding="utf-8")
        result = _read_file(f)
        assert "title:" not in result
        assert "Actual content here" in result

    def test_missing_file_returns_empty(self, tmp_path):
        from atlas_plus.hooks.session_start import _read_file
        result = _read_file(tmp_path / "nonexistent.md")
        assert result == ""

    def test_truncates_at_max_chars(self, tmp_path):
        from atlas_plus.hooks.session_start import _read_file
        f = tmp_path / "long.md"
        f.write_text("x" * 2000, encoding="utf-8")
        result = _read_file(f, max_chars=100)
        assert len(result) <= 100


# ---------------------------------------------------------------------------
# _house_rules
# ---------------------------------------------------------------------------

class TestHouseRules:
    """Launch blocker fixed 2026-10-07 (competitive-sweep raw/18a): house
    rules come from the USER'S corpus (identity/house-rules.md), never
    hardcoded -- the public product used to inject the maintainer's
    personal rules into every stranger's session."""

    def test_fresh_corpus_injects_no_rules(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
        from atlas_plus.hooks.session_start import _house_rules
        assert _house_rules() == ""

    def test_no_maintainer_rules_hardcoded_in_source(self):
        """The private-defaults leak must not return: none of the
        maintainer's rule phrases may appear in the hook's source."""
        import atlas_plus.hooks.session_start as mod
        from pathlib import Path
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for phrase in ("updates.txt", "no em-dashes", "versions/"):
            assert phrase not in src, f"maintainer-specific phrase in hook source: {phrase}"

    def test_user_rules_from_corpus_are_injected(self, tmp_path, monkeypatch):
        mem = tmp_path / "mem"
        (mem / "identity").mkdir(parents=True)
        (mem / "identity" / "house-rules.md").write_text(
            "---\nid: house-rules\n---\n\n# Rules\n\n- Always answer in French\n- Never touch prod\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
        from atlas_plus.hooks.session_start import _house_rules
        rules = _house_rules()
        assert "Always answer in French" in rules
        assert "Never touch prod" in rules
        assert "frontmatter" not in rules and "id: house-rules" not in rules

    def test_reads_only_the_configured_corpus(self, tmp_path, monkeypatch):
        """No live-machine fallback: with ATLAS_MEMORY_ROOT pointed at an
        empty world, the maintainer's real corpus must not bleed through
        (the old version of this test passed locally by reading it -- CI
        caught the dependency)."""
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "empty"))
        from atlas_plus.hooks.session_start import _house_rules
        assert _house_rules() == ""


# ---------------------------------------------------------------------------
# _operator_queue_line
# ---------------------------------------------------------------------------

class TestOperatorQueueLine:
    """Operator panel (2026-10-08): sessions open knowing how many steps wait
    on the human. Corpus-driven -- a fresh corpus injects nothing."""

    def _ticket_root(self, tmp_path, monkeypatch):
        from atlas_plus.tickets import store as _tstore
        root = tmp_path / "tickets"
        monkeypatch.setattr(_tstore, "TICKETS_ROOT", root)
        return root

    def _write(self, root, tid, assignee="human", status="todo", verified=None):
        from atlas_plus.tickets.store import Ticket
        d = root / "ops"
        d.mkdir(parents=True, exist_ok=True)
        t = Ticket(id=tid, title=f"Step {tid}", project="ops",
                   assignee=assignee, status=status, verified=verified)
        (d / f"{tid}.md").write_text(t.to_markdown(), encoding="utf-8")

    def test_fresh_corpus_injects_nothing(self, tmp_path, monkeypatch):
        self._ticket_root(tmp_path, monkeypatch)
        from atlas_plus.hooks.session_start import _operator_queue_line
        assert _operator_queue_line() == ""

    def test_counts_open_human_steps_and_ready(self, tmp_path, monkeypatch):
        root = self._ticket_root(tmp_path, monkeypatch)
        self._write(root, "OPS-001", verified="2026-10-08")
        self._write(root, "OPS-002")
        self._write(root, "OPS-003", assignee="claude-code")
        self._write(root, "OPS-004", status="done")
        from atlas_plus.hooks.session_start import _operator_queue_line
        line = _operator_queue_line()
        assert "2 step(s)" in line and "(1 verified ready)" in line

    def test_broken_store_never_blocks_session(self, tmp_path, monkeypatch):
        from atlas_plus.tickets import store as _tstore
        def _boom(**kwargs):
            raise RuntimeError("store down")
        monkeypatch.setattr(_tstore, "list_tickets", _boom)
        from atlas_plus.hooks.session_start import _operator_queue_line
        assert _operator_queue_line() == ""


# ---------------------------------------------------------------------------
# _detect_project
# ---------------------------------------------------------------------------

class TestDetectProject:
    def test_returns_none_when_not_in_project(self, tmp_path):
        from atlas_plus.hooks.session_start import _detect_project
        non_project_path = tmp_path / "not-a-project"
        non_project_path.mkdir()
        with patch("atlas_plus.hooks.session_start.PROJECTS_ROOT", tmp_path), \
             patch("pathlib.Path.cwd", return_value=non_project_path):
            result = _detect_project()
        assert result is None

    def test_returns_project_name_when_in_git_repo(self, tmp_path):
        from atlas_plus.hooks.session_start import _detect_project
        project = tmp_path / "my-project"
        project.mkdir()
        (project / ".git").mkdir()
        with patch("atlas_plus.hooks.session_start.PROJECTS_ROOT", tmp_path), \
             patch("pathlib.Path.cwd", return_value=project):
            result = _detect_project()
        assert result is not None
        assert result[0] == "my-project"
        assert result[1] == project


# ---------------------------------------------------------------------------
# _open_tickets
# ---------------------------------------------------------------------------

class TestOpenTickets:
    def test_returns_none_message_when_no_open_tickets(self):
        from atlas_plus.hooks.session_start import _open_tickets
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            result = _open_tickets("atlas-memory+")
        assert "(none)" in result

    def test_shows_open_tickets(self):
        from atlas_plus.hooks.session_start import _open_tickets
        from atlas_plus.tickets.store import Ticket
        tickets = [
            Ticket(id="ATL-001", title="Fix login", project="atlas",
                   status="todo", assignee="human", path=Path("t.md")),
        ]
        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets):
            result = _open_tickets("atlas")
        assert "ATL-001" in result
        assert "Fix login" in result

    def test_filters_done_tickets(self):
        from atlas_plus.hooks.session_start import _open_tickets
        from atlas_plus.tickets.store import Ticket
        done = Ticket(id="ATL-002", title="Done already", project="atlas",
                      status="done", path=Path("t.md"))
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[done]):
            result = _open_tickets("atlas")
        assert "(none)" in result

    def test_exception_returns_unavailable_message(self):
        from atlas_plus.hooks.session_start import _open_tickets
        with patch("atlas_plus.tickets.store.list_tickets",
                   side_effect=Exception("db error")):
            result = _open_tickets("atlas")
        assert "unavailable" in result.lower() or "ticket" in result.lower()


# ---------------------------------------------------------------------------
# Shared ticket scan (perf regression pin, 2026-10-09)
# ---------------------------------------------------------------------------

class TestSharedTicketScan:
    """main() scans the ticket store ONCE and both sections filter in memory.

    Before 2026-10-09 the operator-queue line and the open-tickets section
    each ran their own full-store disk pass (~160ms of every session start).
    These tests pin that a provided ticket list is used as-is: if either
    helper regresses to fetching its own, the poisoned store below trips it.
    """

    def _tickets(self):
        from atlas_plus.tickets.store import Ticket
        return [
            Ticket(id="OPS-001", title="Flip the switch", project="infra",
                   status="todo", assignee="human", verified="2026-10-09",
                   path=Path("t1.md")),
            Ticket(id="ATL-100", title="Refactor thing", project="atlas",
                   status="todo", assignee="claude-code", path=Path("t2.md")),
        ]

    def test_operator_line_uses_provided_list_without_store_access(self):
        from atlas_plus.hooks.session_start import _operator_queue_line
        with patch("atlas_plus.tickets.store.list_tickets",
                   side_effect=AssertionError("store must not be scanned again")):
            line = _operator_queue_line(self._tickets())
        assert "1 step(s)" in line
        assert "(1 verified ready)" in line

    def test_open_tickets_uses_provided_list_without_store_access(self):
        from atlas_plus.hooks.session_start import _open_tickets
        with patch("atlas_plus.tickets.store.list_tickets",
                   side_effect=AssertionError("store must not be scanned again")):
            result = _open_tickets("atlas", self._tickets())
        assert "ATL-100" in result
        assert "OPS-001" not in result  # other project filtered out

    def test_all_tickets_returns_none_on_broken_store(self, tmp_path):
        from atlas_plus.hooks import session_start as ss
        (tmp_path / "tickets").mkdir()   # a store exists, and it is broken
        with patch.object(ss, "MEMORY_ROOT", tmp_path), \
                patch("atlas_plus.tickets.store.list_tickets", side_effect=Exception("db error")):
            assert ss._all_tickets() is None

    def test_all_tickets_skips_the_store_when_no_tickets_dir(self, tmp_path):
        """No tickets directory: no store import (it pulls in yaml, 30 ms of the hook; AG11)."""
        import sys
        from atlas_plus.hooks import session_start as ss
        with patch.object(ss, "MEMORY_ROOT", tmp_path):
            sys.modules.pop("atlas_plus.tickets.store", None)
            assert ss._all_tickets() == []
            assert "atlas_plus.tickets.store" not in sys.modules


# ---------------------------------------------------------------------------
# _anti_memory
# ---------------------------------------------------------------------------

class TestAntiMemory:
    def test_returns_empty_string_when_no_entries(self):
        from atlas_plus.hooks.session_start import _anti_memory
        with patch("atlas_plus.mcp.memory_server.tools.anti.list_anti", return_value=[]):
            result = _anti_memory("atlas")
        assert result == ""

    def test_returns_relevant_workspace_entries(self):
        from atlas_plus.hooks.session_start import _anti_memory
        entries = [{"reject_pattern": "do not suggest X", "applies_to": "workspace"}]
        with patch("atlas_plus.mcp.memory_server.tools.anti.list_anti", return_value=entries):
            result = _anti_memory("atlas")
        assert "do not suggest X" in result

    def test_filters_entries_by_project(self):
        from atlas_plus.hooks.session_start import _anti_memory
        entries = [
            {"reject_pattern": "atlas specific", "applies_to": "project", "project": "atlas"},
            {"reject_pattern": "other project", "applies_to": "project", "project": "harbor-app"},
        ]
        with patch("atlas_plus.mcp.memory_server.tools.anti.list_anti", return_value=entries):
            result = _anti_memory("atlas")
        assert "atlas specific" in result
        assert "other project" not in result

    def test_exception_returns_empty_string(self):
        from atlas_plus.hooks.session_start import _anti_memory
        with patch("atlas_plus.mcp.memory_server.tools.anti.list_anti",
                   side_effect=ImportError("no anti module")):
            result = _anti_memory("atlas")
        assert result == ""


# ---------------------------------------------------------------------------
# _temporal_snapshot cache (2026-10-11 perf: the rendered block is cached on
# facts.jsonl identity; a hit must never import/load the temporal stack,
# which cost every project session start ~280ms)
# ---------------------------------------------------------------------------

class TestTemporalSnapshotCache:
    def _state(self, tmp_path, monkeypatch) -> Path:
        state = tmp_path / ".atlas"
        (state / "temporal_graph").mkdir(parents=True)
        (state / "temporal_graph" / "facts.jsonl").write_text(
            '{"fact_id": "f1"}\n', encoding="utf-8"
        )
        monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
        return state

    def test_no_graph_returns_empty_without_building(self, tmp_path, monkeypatch):
        from atlas_plus.hooks import session_start as h
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "empty-state"))
        monkeypatch.setattr(
            h, "_build_temporal_snapshot",
            lambda name: (_ for _ in ()).throw(AssertionError("must not build")),
        )
        assert h._temporal_snapshot("proj") == ""

    def test_miss_builds_and_writes_cache(self, tmp_path, monkeypatch):
        from atlas_plus.hooks import session_start as h
        state = self._state(tmp_path, monkeypatch)
        monkeypatch.setattr(h, "_build_temporal_snapshot", lambda name: "- fact one")
        assert h._temporal_snapshot("proj") == "- fact one"
        import json as _json
        cached = _json.loads(
            (state / "session-temporal-cache.json").read_text(encoding="utf-8")
        )
        assert cached["snapshot"] == "- fact one"

    def test_hit_returns_cached_without_building(self, tmp_path, monkeypatch):
        from atlas_plus.hooks import session_start as h
        self._state(tmp_path, monkeypatch)
        monkeypatch.setattr(h, "_build_temporal_snapshot", lambda name: "- fact one")
        h._temporal_snapshot("proj")
        monkeypatch.setattr(
            h, "_build_temporal_snapshot",
            lambda name: (_ for _ in ()).throw(AssertionError("hit must not build")),
        )
        assert h._temporal_snapshot("proj") == "- fact one"

    def test_graph_change_invalidates(self, tmp_path, monkeypatch):
        from atlas_plus.hooks import session_start as h
        state = self._state(tmp_path, monkeypatch)
        monkeypatch.setattr(h, "_build_temporal_snapshot", lambda name: "- old")
        h._temporal_snapshot("proj")
        with (state / "temporal_graph" / "facts.jsonl").open("a", encoding="utf-8") as fh:
            fh.write('{"fact_id": "f2"}\n')  # any graph write appends here
        monkeypatch.setattr(h, "_build_temporal_snapshot", lambda name: "- new")
        assert h._temporal_snapshot("proj") == "- new"

    def test_project_name_is_part_of_the_key(self, tmp_path, monkeypatch):
        from atlas_plus.hooks import session_start as h
        self._state(tmp_path, monkeypatch)
        monkeypatch.setattr(h, "_build_temporal_snapshot", lambda name: f"- for {name}")
        assert h._temporal_snapshot("alpha") == "- for alpha"
        assert h._temporal_snapshot("beta") == "- for beta"

    def test_corrupt_cache_rebuilds(self, tmp_path, monkeypatch):
        from atlas_plus.hooks import session_start as h
        state = self._state(tmp_path, monkeypatch)
        (state / "session-temporal-cache.json").write_text(
            "not json{", encoding="utf-8"
        )
        monkeypatch.setattr(h, "_build_temporal_snapshot", lambda name: "- rebuilt")
        assert h._temporal_snapshot("proj") == "- rebuilt"

