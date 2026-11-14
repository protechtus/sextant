"""Ticket store data safety.

Regression tests for the 2026-11-09 data-safety audit:
- row 9: the parser split on the first "---" anywhere, so a title such as
  "Migrate a---b config" was truncated on the next status change, the rest of
  the frontmatter fell into the body, and unknown keys were dropped;
- row 24: project "." / ".." escaped the tickets root, and two concurrent
  creators could pick the same id and overwrite each other.
"""
from __future__ import annotations

import threading
from datetime import date
from pathlib import Path

import pytest
import yaml

from atlas_plus.tickets import store
from atlas_plus.tickets.store import create_ticket, get_ticket, list_tickets, update_ticket_status


@pytest.fixture()
def tickets_root(tmp_path) -> Path:
    root = tmp_path / "corpus" / "tickets"
    root.mkdir(parents=True)
    return root


def _fm(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    end = lines.index("---", 1)
    return yaml.safe_load("\n".join(lines[1:end]))


# -- row 9: parser + round trip ---------------------------------------------------

def test_title_with_triple_dash_survives_status_change(tickets_root):
    t = create_ticket("Migrate a---b config", "app", tickets_root=tickets_root)
    before = t.path.read_bytes()
    update_ticket_status(t.id, "in-progress", tickets_root=tickets_root)
    after = t.path.read_bytes()
    reread = get_ticket(t.id, tickets_root)
    assert reread.title == "Migrate a---b config"
    assert reread.status == "in-progress"
    assert after.count(b"\n---") == before.count(b"\n---")   # no doubled header
    # Byte for byte, except the status and updated lines.
    changed = [
        (a, b) for a, b in zip(before.splitlines(), after.splitlines()) if a != b
    ]
    assert len(before.splitlines()) == len(after.splitlines())
    assert all(a.startswith((b"status:", b"updated:")) for a, _ in changed), changed


def test_unknown_frontmatter_keys_comments_and_body_round_trip(tickets_root):
    proj = tickets_root / "app"
    proj.mkdir()
    path = proj / "APP-001.md"
    original = (
        "---\n"
        "id: APP-001\n"
        "title: 'Ship it --- carefully'\n"
        "type: ticket\n"
        "status: todo\n"
        "project: app\n"
        "labels: [infra, urgent]   # user comment\n"
        "due: 2026-12-01\n"
        "notes: |\n"
        "  a block scalar line\n"
        "  ---\n"
        "  still the block\n"
        "updated: '2026-01-01'\n"
        "---\n"
        "\n"
        "## What\n\nbody with --- inside\n\n---\n\nafter a rule\n"
    )
    path.write_bytes(original.encode("utf-8"))
    update_ticket_status("APP-001", "done", tickets_root=tickets_root)
    text = path.read_bytes().decode("utf-8")
    today = date.today().isoformat()
    expected = original.replace("status: todo\n", "status: done\n").replace(
        "updated: '2026-01-01'\n", f"updated: '{today}'\n")
    assert text == expected
    fm = _fm(path)
    assert fm["labels"] == ["infra", "urgent"] and str(fm["due"]) == "2026-12-01"
    assert get_ticket("APP-001", tickets_root).body.endswith("after a rule")


def test_crlf_ticket_keeps_its_line_endings(tickets_root):
    proj = tickets_root / "app"
    proj.mkdir()
    path = proj / "APP-001.md"
    path.write_bytes(b"---\r\nid: APP-001\r\ntitle: x---y\r\nstatus: todo\r\nlabels: [a]\r\n---\r\n\r\nbody\r\n")
    update_ticket_status("APP-001", "todo", tickets_root=tickets_root)  # no-op status
    update_ticket_status("APP-001", "blocked", tickets_root=tickets_root)
    data = path.read_bytes()
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert b"status: blocked\r\n" in data and b"labels: [a]\r\n" in data and b"title: x---y\r\n" in data
    assert b"updated: " in data  # a missing key is added inside the block


def test_reopen_count_added_and_incremented(tickets_root):
    t = create_ticket("Loop", "app", tickets_root=tickets_root)
    update_ticket_status(t.id, "done", tickets_root=tickets_root)
    update_ticket_status(t.id, "todo", tickets_root=tickets_root)
    assert _fm(t.path)["reopen_count"] == 1
    update_ticket_status(t.id, "done", tickets_root=tickets_root)
    update_ticket_status(t.id, "todo", tickets_root=tickets_root)
    assert _fm(t.path)["reopen_count"] == 2
    assert get_ticket(t.id, tickets_root).reopen_count == 2


def test_status_write_is_atomic(tickets_root, monkeypatch):
    t = create_ticket("Atomic", "app", tickets_root=tickets_root)
    before = t.path.read_bytes()
    import os

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        update_ticket_status(t.id, "done", tickets_root=tickets_root)
    assert t.path.read_bytes() == before
    assert sorted(p.name for p in t.path.parent.iterdir()) == [t.path.name]   # no tmp left


def test_parser_requires_delimiter_lines(tickets_root):
    proj = tickets_root / "app"
    proj.mkdir()
    (proj / "APP-001.md").write_text("---id: x\ntitle: y\n---\nbody\n", encoding="utf-8")
    (proj / "APP-002.md").write_text("---\nid: APP-002\ntitle: unterminated\n", encoding="utf-8")
    assert list_tickets(project="app", tickets_root=tickets_root) == []


# -- row 24: project confinement + id claim ------------------------------------------

@pytest.mark.parametrize("bad", [".", "..", "...", ".hidden", "trail.", "nul", "CON", "com1", "LPT9.x",
                                 "a/b", "a\\b", "", "../x"])
def test_unsafe_project_rejected(tickets_root, bad):
    with pytest.raises(ValueError):
        create_ticket("x", bad, tickets_root=tickets_root)
    assert not list(tickets_root.parent.glob("*.md"))


@pytest.mark.parametrize("good", ["app", "my.app", "my_app", "a+b", "v1.2-x"])  # `_inbox` is an internal folder the ticket walker prunes (AG38, sweep 15)
def test_ordinary_projects_still_accepted(tickets_root, good):
    t = create_ticket("x", good, tickets_root=tickets_root)
    assert t.path.parent == tickets_root / good


def test_existing_file_at_next_id_is_never_overwritten(tickets_root, monkeypatch):
    proj = tickets_root / "app"
    proj.mkdir()
    squatter = proj / "APP-001.md"
    squatter.write_bytes(b"not mine\n")
    # Simulate a racing creator: the id scan still believes 001 is free.
    monkeypatch.setattr(store, "_next_num", lambda project, root: 1)
    t = create_ticket("Mine", "app", tickets_root=tickets_root)
    assert t.id == "APP-002"
    assert squatter.read_bytes() == b"not mine\n"
    assert get_ticket("APP-002", tickets_root).title == "Mine"


def test_concurrent_creators_get_distinct_ids(tickets_root, monkeypatch):
    monkeypatch.setattr(store, "_next_num", lambda project, root: 1)   # all race for 001
    made, errors = [], []
    barrier = threading.Barrier(8)

    def worker(i):
        try:
            barrier.wait()
            made.append(create_ticket(f"t{i}", "app", tickets_root=tickets_root))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    assert len({t.id for t in made}) == 8
    titles = {get_ticket(t.id, tickets_root).title for t in made}
    assert titles == {f"t{i}" for i in range(8)}
