"""P10 tests — ticket store and MCP tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas_plus.tickets.store import (
    create_ticket,
    get_ticket,
    list_tickets,
    update_ticket_status,
)


@pytest.fixture()
def tickets_root(tmp_path) -> Path:
    root = tmp_path / "tickets"
    root.mkdir()
    return root


class TestCreateTicket:
    def test_creates_file(self, tickets_root):
        t = create_ticket("Fix the bug", "myproject", tickets_root=tickets_root)
        assert t.path is not None
        assert t.path.exists()

    def test_auto_assigns_id(self, tickets_root):
        t = create_ticket("First ticket", "myproject", tickets_root=tickets_root)
        assert t.id == "MYP-001"

    def test_increments_id(self, tickets_root):
        create_ticket("First", "myproject", tickets_root=tickets_root)
        t2 = create_ticket("Second", "myproject", tickets_root=tickets_root)
        assert t2.id == "MYP-002"

    def test_different_projects_independent(self, tickets_root):
        t1 = create_ticket("Alpha", "proj-a", tickets_root=tickets_root)
        t2 = create_ticket("Beta", "proj-b", tickets_root=tickets_root)
        assert t1.id == "PRO-001"
        assert t2.id == "PRO-001"

    def test_markdown_written_correctly(self, tickets_root):
        t = create_ticket("Do something", "myproject", assignee="claude-code", effort="L", tickets_root=tickets_root)
        content = t.path.read_text(encoding="utf-8")
        assert "claude-code" in content
        assert "effort: L" in content
        assert "Do something" in content

    def test_with_body(self, tickets_root):
        t = create_ticket("Task", "proj", body="## What\n\nBuild it.", tickets_root=tickets_root)
        content = t.path.read_text(encoding="utf-8")
        assert "Build it." in content


class TestVerifiedField:
    """Operator panel (2026-10-08): `verified` marks a step's instructions as
    researched and ready to execute."""

    def test_round_trips_through_markdown(self, tickets_root):
        t = create_ticket("Operator step", "proj", verified="2026-10-08",
                          tickets_root=tickets_root)
        assert "verified: '2026-10-08'" in t.path.read_text(encoding="utf-8") or \
               "verified: 2026-10-08" in t.path.read_text(encoding="utf-8")
        back = get_ticket(t.id, tickets_root=tickets_root)
        assert back.verified == "2026-10-08"
        assert back.as_dict()["verified"] == "2026-10-08"

    def test_absent_by_default(self, tickets_root):
        t = create_ticket("Plain step", "proj", tickets_root=tickets_root)
        back = get_ticket(t.id, tickets_root=tickets_root)
        assert back.verified is None
        assert "verified" not in back.as_dict()
        assert "verified" not in t.path.read_text(encoding="utf-8")

    def test_rejects_non_date(self, tickets_root):
        with pytest.raises(ValueError, match="verified"):
            create_ticket("Bad", "proj", verified="yes", tickets_root=tickets_root)

    def test_survives_status_update(self, tickets_root):
        t = create_ticket("Step", "proj", verified="2026-10-08",
                          tickets_root=tickets_root)
        update_ticket_status(t.id, "done", tickets_root=tickets_root)
        assert get_ticket(t.id, tickets_root=tickets_root).verified == "2026-10-08"


class TestGetTicket:
    def test_finds_by_id(self, tickets_root):
        created = create_ticket("Find me", "proj", tickets_root=tickets_root)
        found = get_ticket(created.id, tickets_root=tickets_root)
        assert found is not None
        assert found.id == created.id
        assert found.title == "Find me"

    def test_returns_none_for_missing(self, tickets_root):
        assert get_ticket("ZZZ-999", tickets_root=tickets_root) is None


class TestListTickets:
    def test_lists_all(self, tickets_root):
        create_ticket("A", "proj1", tickets_root=tickets_root)
        create_ticket("B", "proj2", tickets_root=tickets_root)
        tickets = list_tickets(tickets_root=tickets_root)
        assert len(tickets) == 2

    def test_filters_by_project(self, tickets_root):
        create_ticket("A", "proj1", tickets_root=tickets_root)
        create_ticket("B", "proj2", tickets_root=tickets_root)
        tickets = list_tickets(project="proj1", tickets_root=tickets_root)
        assert len(tickets) == 1
        assert tickets[0].title == "A"

    def test_filters_by_status(self, tickets_root):
        t = create_ticket("A", "proj", tickets_root=tickets_root)
        create_ticket("B", "proj", tickets_root=tickets_root)
        update_ticket_status(t.id, "done", tickets_root=tickets_root)
        open_tickets = list_tickets(status="todo", tickets_root=tickets_root)
        assert len(open_tickets) == 1
        assert open_tickets[0].title == "B"

    def test_empty_dir_returns_empty(self, tickets_root):
        assert list_tickets(tickets_root=tickets_root) == []

    def test_excludes_template(self, tickets_root):
        (tickets_root / "_template.md").write_text("---\nid: template\n---\n", encoding="utf-8")
        tickets = list_tickets(tickets_root=tickets_root)
        assert tickets == []


class TestUpdateStatus:
    def test_updates_status(self, tickets_root):
        t = create_ticket("Task", "proj", tickets_root=tickets_root)
        updated = update_ticket_status(t.id, "in-progress", tickets_root=tickets_root)
        assert updated.status == "in-progress"
        # Verify persisted
        reloaded = get_ticket(t.id, tickets_root=tickets_root)
        assert reloaded.status == "in-progress"

    def test_invalid_status_raises(self, tickets_root):
        t = create_ticket("Task", "proj", tickets_root=tickets_root)
        with pytest.raises(ValueError, match="Invalid status"):
            update_ticket_status(t.id, "banana", tickets_root=tickets_root)

    def test_missing_ticket_raises(self, tickets_root):
        with pytest.raises(FileNotFoundError):
            update_ticket_status("ZZZ-999", "done", tickets_root=tickets_root)


class TestReopenCount:
    def test_reopen_count_zero_by_default(self, tickets_root):
        t = create_ticket("Task", "proj", tickets_root=tickets_root)
        assert t.reopen_count == 0

    def test_reopen_count_increments_when_todo_from_in_progress(self, tickets_root):
        t = create_ticket("Task", "proj", tickets_root=tickets_root)
        update_ticket_status(t.id, "in-progress", tickets_root=tickets_root)
        updated = update_ticket_status(t.id, "todo", tickets_root=tickets_root)
        assert updated.reopen_count == 1

    def test_reopen_count_increments_when_todo_from_done(self, tickets_root):
        t = create_ticket("Task", "proj", tickets_root=tickets_root)
        update_ticket_status(t.id, "done", tickets_root=tickets_root)
        updated = update_ticket_status(t.id, "todo", tickets_root=tickets_root)
        assert updated.reopen_count == 1

    def test_reopen_count_not_incremented_for_non_todo_transitions(self, tickets_root):
        t = create_ticket("Task", "proj", tickets_root=tickets_root)
        update_ticket_status(t.id, "in-progress", tickets_root=tickets_root)
        updated = update_ticket_status(t.id, "done", tickets_root=tickets_root)
        assert updated.reopen_count == 0

    def test_reopen_count_not_incremented_when_already_todo(self, tickets_root):
        t = create_ticket("Task", "proj", tickets_root=tickets_root)
        # todo -> todo should NOT increment (already at todo)
        updated = update_ticket_status(t.id, "todo", tickets_root=tickets_root)
        assert updated.reopen_count == 0

    def test_reopen_count_accumulates_across_multiple_loops(self, tickets_root):
        t = create_ticket("Looping task", "proj", tickets_root=tickets_root)
        for _ in range(3):
            update_ticket_status(t.id, "in-progress", tickets_root=tickets_root)
            update_ticket_status(t.id, "todo", tickets_root=tickets_root)
        reloaded = get_ticket(t.id, tickets_root=tickets_root)
        assert reloaded.reopen_count == 3

    def test_reopen_count_persisted_in_frontmatter(self, tickets_root):
        t = create_ticket("Task", "proj", tickets_root=tickets_root)
        update_ticket_status(t.id, "in-progress", tickets_root=tickets_root)
        update_ticket_status(t.id, "todo", tickets_root=tickets_root)
        content = t.path.read_text(encoding="utf-8")
        assert "reopen_count: 1" in content

    def test_reopen_count_not_in_frontmatter_when_zero(self, tickets_root):
        t = create_ticket("Task", "proj", tickets_root=tickets_root)
        content = t.path.read_text(encoding="utf-8")
        assert "reopen_count" not in content

    def test_as_dict_includes_reopen_count_when_nonzero(self, tickets_root):
        t = create_ticket("Task", "proj", tickets_root=tickets_root)
        update_ticket_status(t.id, "in-progress", tickets_root=tickets_root)
        updated = update_ticket_status(t.id, "todo", tickets_root=tickets_root)
        d = updated.as_dict()
        assert "reopen_count" in d
        assert d["reopen_count"] == 1

    def test_as_dict_excludes_reopen_count_when_zero(self, tickets_root):
        t = create_ticket("Task", "proj", tickets_root=tickets_root)
        d = t.as_dict()
        assert "reopen_count" not in d


class TestMcpTools:
    def test_tickets_list_tool(self, tickets_root, monkeypatch):
        import atlas_plus.tickets.store as store_mod
        monkeypatch.setattr(store_mod, "TICKETS_ROOT", tickets_root)
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_list
        create_ticket("A", "proj", tickets_root=tickets_root)
        result = tickets_list(tickets_root=tickets_root)
        assert len(result) == 1
        assert result[0]["title"] == "A"

    def test_tickets_open_tool(self, tickets_root):
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_open
        t = create_ticket("Open", "proj", tickets_root=tickets_root)
        t2 = create_ticket("Done", "proj", tickets_root=tickets_root)
        update_ticket_status(t2.id, "done", tickets_root=tickets_root)
        result = tickets_open(project="proj", tickets_root=tickets_root)
        assert len(result) == 1
        assert result[0]["title"] == "Open"

    def test_tickets_create_tool(self, tickets_root, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_create
        result = tickets_create(
            title="New ticket",
            project="proj",
            tickets_root=tickets_root,
        )
        assert "id" in result
        assert result["title"] == "New ticket"

    def test_tickets_status_tool_error(self, tickets_root):
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_update_status
        result = tickets_update_status("ZZZ-999", "done", tickets_root=tickets_root)
        assert "error" in result


class TestCreateTicketValidation:
    def test_rejects_invalid_assignee(self, tickets_root):
        with pytest.raises(ValueError, match="Invalid assignee"):
            create_ticket("T", "proj", assignee="robot", tickets_root=tickets_root)

    def test_rejects_invalid_priority(self, tickets_root):
        with pytest.raises(ValueError, match="Invalid priority"):
            create_ticket("T", "proj", priority="urgent", tickets_root=tickets_root)

    def test_rejects_invalid_effort(self, tickets_root):
        with pytest.raises(ValueError, match="Invalid effort"):
            create_ticket("T", "proj", effort="XXL", tickets_root=tickets_root)

    def test_rejects_empty_title(self, tickets_root):
        with pytest.raises(ValueError):
            create_ticket("", "proj", tickets_root=tickets_root)

    def test_rejects_title_too_long(self, tickets_root):
        with pytest.raises(ValueError, match="200 characters"):
            create_ticket("x" * 201, "proj", tickets_root=tickets_root)

    def test_rejects_body_too_long(self, tickets_root):
        with pytest.raises(ValueError, match="10,000"):
            create_ticket("T", "proj", body="x" * 10_001, tickets_root=tickets_root)

    def test_rejects_invalid_requires_format(self, tickets_root):
        with pytest.raises(ValueError, match="requires"):
            create_ticket("T", "proj", requires="not-a-ticket-id", tickets_root=tickets_root)

    def test_valid_requires_accepted(self, tickets_root):
        t = create_ticket("T", "proj", requires="ATL-001", tickets_root=tickets_root)
        assert t.requires == "ATL-001"

    def test_all_valid_assignees_accepted(self, tickets_root):
        from atlas_plus.tickets.store import VALID_ASSIGNEES
        for assignee in VALID_ASSIGNEES:
            if assignee == "claude-code":
                continue  # tested separately via MCP layer
            create_ticket(f"Task for {assignee}", "proj", assignee=assignee, tickets_root=tickets_root)


class TestUpdateTicketVersioning:
    def test_versions_file_before_rewrite(self, tickets_root, tmp_path):
        t = create_ticket("Task", "proj", tickets_root=tickets_root)
        original_content = t.path.read_text(encoding="utf-8")

        versions_dir = tmp_path / "versions"
        versions_dir.mkdir()

        versioned = []
        def fake_version(path):
            versioned.append(path)

        from unittest.mock import patch
        with patch("atlas_plus.mcp.memory_server.versioning.version_before_write", side_effect=fake_version):
            update_ticket_status(t.id, "done", tickets_root=tickets_root)

        assert len(versioned) == 1
        assert versioned[0] == t.path


class TestRemoteDispatchBlock:
    def test_claude_code_blocked_without_env(self, tickets_root, monkeypatch):
        monkeypatch.delenv("ATLAS_ALLOW_REMOTE_DISPATCH", raising=False)
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_create
        result = tickets_create(
            title="Run this",
            project="proj",
            assignee="claude-code",
            tickets_root=tickets_root,
        )
        assert "error" in result
        assert "blocked" in result["error"]

    def test_claude_code_allowed_with_env(self, tickets_root, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        monkeypatch.setenv("ATLAS_ALLOW_REMOTE_DISPATCH", "1")
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_create
        result = tickets_create(
            title="Run this",
            project="proj",
            assignee="claude-code",
            tickets_root=tickets_root,
        )
        assert "error" not in result
        assert result["assignee"] == "claude-code"

    def test_human_assignee_not_blocked(self, tickets_root, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        monkeypatch.delenv("ATLAS_ALLOW_REMOTE_DISPATCH", raising=False)
        from atlas_plus.mcp.memory_server.tools.tickets import tickets_create
        result = tickets_create(
            title="Human task",
            project="proj",
            assignee="human",
            tickets_root=tickets_root,
        )
        assert "error" not in result


class TestArchiveExclusion:
    """_archive/ tickets are history, not live work (the ATL-010 ghost, 2026-08-05)."""

    def _archive_a_ticket(self, tickets_root):
        t = create_ticket("Old work", "myproject", tickets_root=tickets_root)
        archive_dir = tickets_root / "_archive" / "2026-06-08" / "myproject"
        archive_dir.mkdir(parents=True)
        t.path.rename(archive_dir / t.path.name)
        return t

    def test_archived_tickets_hidden_from_global_list(self, tickets_root):
        self._archive_a_ticket(tickets_root)
        live = create_ticket("Current work", "myproject", tickets_root=tickets_root)
        listed = [t.id for t in list_tickets(tickets_root=tickets_root)]
        assert live.id in listed
        assert len(listed) == 1

    def test_archived_ticket_not_found_by_get(self, tickets_root):
        old = self._archive_a_ticket(tickets_root)
        assert get_ticket(old.id, tickets_root=tickets_root) is None

    def test_archived_ids_stay_reserved(self, tickets_root):
        old = self._archive_a_ticket(tickets_root)  # MYP-001, archived
        fresh = create_ticket("New after clear", "myproject", tickets_root=tickets_root)
        assert fresh.id != old.id
        assert fresh.id == "MYP-002"
