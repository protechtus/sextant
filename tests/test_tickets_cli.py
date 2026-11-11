"""Tests for atlas_plus.tickets.cli -- atlas-tickets CLI commands."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

runner = CliRunner()


def _make_ticket(tid="ATL-001", title="Fix login", status="todo",
                 assignee="human", project="atlas"):
    from atlas_plus.tickets.store import Ticket
    return Ticket(id=tid, title=title, project=project, status=status,
                  assignee=assignee, path=Path(f"{tid}.md"))


# ---------------------------------------------------------------------------
# cmd_list
# ---------------------------------------------------------------------------

class TestCmdList:
    def test_empty_tickets_shows_no_tickets(self):
        from atlas_plus.tickets.cli import app
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "No tickets" in result.output or "no tickets" in result.output.lower()

    def test_shows_ticket_in_table(self):
        from atlas_plus.tickets.cli import app
        tickets = [_make_ticket()]
        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0
        assert "ATL-001" in result.output

    def test_json_flag_outputs_json(self):
        from atlas_plus.tickets.cli import app
        import json
        tickets = [_make_ticket()]
        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets):
            result = runner.invoke(app, ["list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["id"] == "ATL-001"

    def test_open_flag_filters_status(self):
        from atlas_plus.tickets.cli import app
        tickets = [_make_ticket(status="todo"), _make_ticket("ATL-002", "B", status="done")]
        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets):
            result = runner.invoke(app, ["list", "--open"])
        assert result.exit_code == 0
        assert "ATL-001" in result.output
        assert "ATL-002" not in result.output

    def test_shows_ticket_count(self):
        from atlas_plus.tickets.cli import app
        tickets = [_make_ticket(), _make_ticket("ATL-002", "B")]
        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets):
            result = runner.invoke(app, ["list"])
        assert "2" in result.output


# ---------------------------------------------------------------------------
# cmd_show
# ---------------------------------------------------------------------------

class TestCmdShow:
    def test_shows_ticket_detail(self):
        from atlas_plus.tickets.cli import app
        t = _make_ticket()
        with patch("atlas_plus.tickets.store.get_ticket", return_value=t):
            result = runner.invoke(app, ["show", "ATL-001"])
        assert result.exit_code == 0
        assert "ATL-001" in result.output
        assert "Fix login" in result.output

    def test_not_found_exits_with_error(self):
        from atlas_plus.tickets.cli import app
        with patch("atlas_plus.tickets.store.get_ticket", return_value=None):
            result = runner.invoke(app, ["show", "ATL-999"])
        assert result.exit_code != 0
        assert "not found" in result.output.lower() or "ATL-999" in result.output


# ---------------------------------------------------------------------------
# cmd_create
# ---------------------------------------------------------------------------

class TestCmdCreate:
    def test_creates_ticket_and_shows_id(self):
        from atlas_plus.tickets.cli import app
        fake = _make_ticket(tid="ATL-042", title="New feature")
        fake.path = Path("ATL-042.md")
        with patch("atlas_plus.tickets.store.create_ticket", return_value=fake):
            result = runner.invoke(app, ["create", "New feature",
                                         "--project", "atlas"])
        assert result.exit_code == 0
        assert "ATL-042" in result.output


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

class TestCmdStatus:
    def test_updates_status_successfully(self):
        from atlas_plus.tickets.cli import app
        updated = _make_ticket(status="done")
        with patch("atlas_plus.tickets.store.update_ticket_status", return_value=updated):
            result = runner.invoke(app, ["status", "ATL-001", "done"])
        assert result.exit_code == 0
        assert "ATL-001" in result.output
        assert "done" in result.output

    def test_not_found_exits_with_error(self):
        from atlas_plus.tickets.cli import app
        with patch("atlas_plus.tickets.store.update_ticket_status",
                   side_effect=FileNotFoundError("not found")):
            result = runner.invoke(app, ["status", "ATL-999", "done"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# cmd_open
# ---------------------------------------------------------------------------

class TestCmdOpen:
    def test_no_open_tickets_shows_message(self):
        from atlas_plus.tickets.cli import app
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            result = runner.invoke(app, ["open"])
        assert result.exit_code == 0
        assert "No open" in result.output or "no open" in result.output.lower()

    def test_shows_open_tickets(self):
        from atlas_plus.tickets.cli import app
        tickets = [_make_ticket(status="todo"), _make_ticket("ATL-002", "B", status="in-progress")]
        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets):
            result = runner.invoke(app, ["open"])
        assert result.exit_code == 0
        assert "ATL-001" in result.output
        assert "ATL-002" in result.output

    def test_filters_done_tickets_from_open(self):
        from atlas_plus.tickets.cli import app
        tickets = [_make_ticket(status="todo"), _make_ticket("ATL-003", "C", status="done")]
        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets):
            result = runner.invoke(app, ["open"])
        assert "ATL-001" in result.output
        assert "ATL-003" not in result.output
