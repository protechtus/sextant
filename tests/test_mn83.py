"""MN83 (sweep 23 manual wave, dashboard in a real browser): the ticket detail's Dispatch button for a ticket in
a renamed project folder dispatches THAT ticket (it sent the frontmatter project: a 404, or another ticket)."""
from __future__ import annotations

import html
import re

import pytest


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    key = "mn83-test-key-" + "0123456789abcdef"
    monkeypatch.setenv("ATLAS_WEB_API_KEY", key)
    from fastapi.testclient import TestClient
    from atlas_plus.tickets import store
    from atlas_plus.web import app as webapp
    store._NEXT_MEMO.clear()
    root = tmp_path / "tickets"
    root.mkdir()
    monkeypatch.setattr(store, "TICKETS_ROOT", root)
    monkeypatch.setattr(webapp, "API_KEY", key, raising=False)
    sent = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"task_id": "t-mn83"}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def post(self, url, json=None, **k):
            sent.append(json)
            return _Resp()

    async def _nobroadcast(*a, **k):
        return None

    monkeypatch.setattr(webapp, "_PeerClient", _Client)
    monkeypatch.setattr(webapp, "_broadcast", _nobroadcast)
    c = TestClient(webapp.app, raise_server_exceptions=False)
    c.headers["Authorization"] = "Bearer " + key
    return c, root, sent


def _write(root, folder, title):
    d = root / folder
    d.mkdir(exist_ok=True)
    (d / "ALP-003.md").write_text(
        f"---\nid: ALP-003\nproject: alpha\nstatus: todo\ntitle: {title}\nassignee: human\n---\nbody of {title}\n",
        encoding="utf-8")


def _status(path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()


def _press_dispatch(c, ref):
    """What the browser does: open the detail panel, then run the Dispatch button's onclick."""
    page = c.get(f"/api/tickets/{ref}/detail")
    assert page.status_code == 200, page.text
    m = re.search(r'onclick="dispatchTicketToBrain\((.*?)\)"', page.text)
    assert m, page.text
    first = html.unescape(m.group(1)).split('","')[0].strip('"')
    return c.post(f"/api/tickets/{first}/dispatch", json={"instruction": f"Ticket {first}"})


def test_dispatch_from_a_renamed_folder_reaches_the_ticket(web):
    c, root, sent = web
    _write(root, "alpha-old", "the one I clicked")         # `alpha` renamed by hand; frontmatter still says alpha
    r = _press_dispatch(c, "alpha-old/ALP-003")
    assert r.status_code == 202, r.text                   # 73910bc: 404 "Ticket alpha/ALP-003 not found"
    assert _status(root / "alpha-old" / "ALP-003.md") == "in-progress"


def test_dispatch_from_a_renamed_folder_never_starts_another_ticket(web):
    c, root, sent = web
    _write(root, "alpha-old", "the one I clicked")
    _write(root, "alpha", "a restored copy")               # a backup of the old folder put back
    r = _press_dispatch(c, "alpha-old/ALP-003")
    assert r.status_code == 202, r.text
    assert _status(root / "alpha-old" / "ALP-003.md") == "in-progress"
    assert _status(root / "alpha" / "ALP-003.md") == "todo"   # 73910bc: THIS one went in-progress
    assert sent[0]["session_id"] == "ticket-alpha-old/ALP-003"  # so the finished task closes the clicked ticket


def test_the_brain_dispatcher_closes_the_ticket_an_id_in_two_folders_names(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.tickets import store
    from atlas_plus.brain import dispatcher, tasks
    root = tmp_path / "tickets"
    for folder, title in (("alpha-old", "the original"), ("alpha", "a restored copy")):
        (root / folder).mkdir(parents=True)
        (root / folder / "ALP-009.md").write_text(
            f"---\nid: ALP-009\nproject: alpha\nstatus: todo\ntitle: {title}\nassignee: local\n---\nbody\n",
            encoding="utf-8")
    monkeypatch.setattr(store, "TICKETS_ROOT", root)
    db = tmp_path / "brain.db"
    _create, _list = tasks.create_task, tasks.list_tasks
    monkeypatch.setattr(tasks, "create_task", lambda *a, **k: _create(*a, db_path=db, **k))
    monkeypatch.setattr(tasks, "list_tasks", lambda *a, **k: _list(*a, db_path=db, **k))
    made = [r for r in (dispatcher.dispatch_ticket(t) for t in store.list_tickets(status="todo")) if r]
    assert len(made) == 2                                   # 73910bc: the second was "already dispatched"
    for folder in ("alpha-old", "alpha"):
        assert _status(root / folder / "ALP-009.md") == "in-progress"   # 73910bc: both stayed todo
    for r in made:
        assert tasks.mark_done(r.task_id, "finished: the change is in and the tests pass", db_path=db)
    for folder in ("alpha-old", "alpha"):
        assert _status(root / folder / "ALP-009.md") == "done"          # 73910bc: neither closed
