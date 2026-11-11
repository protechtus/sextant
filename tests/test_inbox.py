"""ATL-026 tests -- inbox store (capture / list / update / promote + autonomous gating)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_plus.inbox.store import (
    InboxItem,
    create_item,
    get_item,
    list_items,
    promote_to_ticket,
    update_item,
)


@pytest.fixture()
def inbox_root(tmp_path) -> Path:
    root = tmp_path / "inbox"
    root.mkdir()
    return root


@pytest.fixture()
def tickets_root(tmp_path) -> Path:
    root = tmp_path / "tickets"
    root.mkdir()
    return root


class TestCreateItem:
    def test_creates_file(self, inbox_root):
        it = create_item(url="https://example.com/a", inbox_root=inbox_root)
        assert it.path is not None and it.path.exists()

    def test_auto_id_and_increment(self, inbox_root):
        a = create_item(url="https://example.com/a", inbox_root=inbox_root)
        b = create_item(text="a blurb", inbox_root=inbox_root)
        assert a.id == "INB-0001"
        assert b.id == "INB-0002"

    def test_markdown_contents(self, inbox_root):
        it = create_item(
            url="https://example.com/x",
            title="A title",
            text="some note",
            tags=["voip", "sip"],
            flagged_research=True,
            inbox_root=inbox_root,
        )
        content = it.path.read_text(encoding="utf-8")
        assert "type: inbox" in content
        assert "https://example.com/x" in content
        assert "A title" in content
        assert "flagged_research: true" in content
        assert "some note" in content

    def test_requires_url_or_text(self, inbox_root):
        with pytest.raises(ValueError, match="url or some text"):
            create_item(inbox_root=inbox_root)

    def test_invalid_source(self, inbox_root):
        with pytest.raises(ValueError, match="Invalid source"):
            create_item(source="carrier-pigeon", url="https://x.com", inbox_root=inbox_root)

    def test_url_too_long(self, inbox_root):
        with pytest.raises(ValueError, match="url must be"):
            create_item(url="https://x.com/" + "a" * 2100, inbox_root=inbox_root)


class TestGating:
    def test_blocked_when_off(self, inbox_root):
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "off"}):
            with pytest.raises(RuntimeError, match="blocked"):
                create_item(url="https://example.com/a", inbox_root=inbox_root)
        assert list_items(inbox_root=inbox_root) == []  # nothing written

    def test_allowed_when_on(self, inbox_root):
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "on"}):
            it = create_item(url="https://example.com/a", inbox_root=inbox_root)
        assert it.path.exists()


class TestListAndGet:
    def test_list_and_status_filter(self, inbox_root):
        create_item(url="https://a.com", inbox_root=inbox_root)
        b = create_item(url="https://b.com", inbox_root=inbox_root)
        update_item(b.id, status="summarized", inbox_root=inbox_root)
        assert len(list_items(inbox_root=inbox_root)) == 2
        new_only = list_items(status="new", inbox_root=inbox_root)
        assert len(new_only) == 1 and new_only[0].id != b.id

    def test_list_newest_first(self, inbox_root):
        # Construct two items with explicit created timestamps and write them directly.
        older = InboxItem(id="INB-0001", url="https://old.com", created="2026-06-01T00:00:00Z")
        newer = InboxItem(id="INB-0002", url="https://new.com", created="2026-06-08T00:00:00Z")
        (inbox_root / "INB-0001.md").write_text(older.to_markdown(), encoding="utf-8")
        (inbox_root / "INB-0002.md").write_text(newer.to_markdown(), encoding="utf-8")
        ids = [i.id for i in list_items(inbox_root=inbox_root)]
        assert ids == ["INB-0002", "INB-0001"]

    def test_get_item(self, inbox_root):
        a = create_item(url="https://a.com", inbox_root=inbox_root)
        assert get_item(a.id, inbox_root=inbox_root).id == a.id
        assert get_item("INB-9999", inbox_root=inbox_root) is None


class TestUpdateItem:
    def test_set_summary_and_tags(self, inbox_root):
        a = create_item(url="https://a.com", inbox_root=inbox_root)
        u = update_item(a.id, status="summarized", summary="A short summary.", tags=["ai"], inbox_root=inbox_root)
        assert u.status == "summarized"
        assert u.summary == "A short summary."
        assert u.tags == ["ai"]
        reread = get_item(a.id, inbox_root=inbox_root)
        assert reread.summary == "A short summary."

    def test_append_body_note(self, inbox_root):
        a = create_item(url="https://a.com", inbox_root=inbox_root)
        u = update_item(a.id, append_body="follow-up thought", inbox_root=inbox_root)
        assert "follow-up thought" in u.body
        assert "## Note (" in u.body

    def test_invalid_status(self, inbox_root):
        a = create_item(url="https://a.com", inbox_root=inbox_root)
        with pytest.raises(ValueError, match="Invalid status"):
            update_item(a.id, status="bogus", inbox_root=inbox_root)

    def test_missing_item(self, inbox_root):
        with pytest.raises(FileNotFoundError):
            update_item("INB-9999", status="archived", inbox_root=inbox_root)


class TestPromote:
    def test_promote_creates_ticket_and_marks_item(self, inbox_root, tickets_root):
        a = create_item(url="https://a.com", title="Investigate X", inbox_root=inbox_root)
        update_item(a.id, summary="Looks worth a deeper look.", inbox_root=inbox_root)
        item, ticket_id = promote_to_ticket(
            a.id, project="myproject", inbox_root=inbox_root, tickets_root=tickets_root
        )
        assert ticket_id == "MYP-001"
        assert item.status == "promoted"
        assert item.promoted_ticket == "MYP-001"
        # ticket file exists and references the inbox item
        ticket_file = tickets_root / "myproject" / "MYP-001.md"
        assert ticket_file.exists()
        assert a.id in ticket_file.read_text(encoding="utf-8")
