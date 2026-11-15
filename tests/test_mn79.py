"""MN79 (sweep 23 manual wave, expert): ticket ids in archive folders a person invents, ids with a digit in the
prefix, an ambiguous id dispatched from the dashboard, the banded scan's signature pass and its budget, quoted
status words, dot segments in a hook's workspace, and `_archive` in doctor's tag figures."""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def troot(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.tickets import store
    store._NEXT_MEMO.clear()
    root = tmp_path / "tickets"
    root.mkdir()
    monkeypatch.setattr(store, "TICKETS_ROOT", root)
    return root


def _mk(root, project, k=1, **kw):
    from atlas_plus.tickets import store
    return [store.create_ticket(f"t {project} {i}", project, tickets_root=root, interactive=True, **kw).id
            for i in range(k)]


def _status(path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip()
    return None


# ---------------------------------------------------------------- numbering
@pytest.mark.parametrize("archive", ["alpha/_archive", "_done", "alpha/_old/2026"])
def test_a_ticket_moved_into_an_invented_archive_folder_keeps_its_number(troot, archive):
    from atlas_plus.tickets import store
    assert _mk(troot, "alpha", 3) == ["ALP-001", "ALP-002", "ALP-003"]
    dest = troot / archive
    dest.mkdir(parents=True)
    (troot / "alpha" / "ALP-003.md").rename(dest / "ALP-003.md")
    store._NEXT_MEMO.clear()  # a fresh process, as the next `atlas-tickets create` is
    assert _mk(troot, "alpha") == ["ALP-004"]                        # cbd4e02: ALP-003 again


# ---------------------------------------------------------------- ids with a digit in the prefix
def test_requires_takes_an_id_the_store_mints(troot):
    assert _mk(troot, "k8s") == ["K8S-001"]
    assert _mk(troot, "k8s", requires="K8S-001") == ["K8S-002"]     # cbd4e02: ValueError "Expected format: ATL-001"


def test_a_finished_brain_task_closes_a_ticket_with_a_digit_in_its_prefix(troot, tmp_path):
    from atlas_plus.brain import tasks
    assert _mk(troot, "k8s") == ["K8S-001"]
    db = tmp_path / "brain.db"
    t = tasks.create_task("upgrade", session_id="ticket-K8S-001", db_path=db)
    assert tasks.mark_done(t.task_id, "finished: the change is in and the tests pass", db_path=db)
    assert _status(troot / "k8s" / "K8S-001.md") == "done"           # cbd4e02: todo


def test_telegram_verify_takes_an_id_the_store_mints():
    pytest.importorskip("telegram")
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from atlas_plus.telegram import commands
    update, ctx = MagicMock(), MagicMock()
    update.message.reply_text = AsyncMock()
    ctx.args = ["K8S-001"]
    asyncio.run(commands.cmd_verify_ticket(update, ctx))
    out = " ".join(str(c.args[0]) for c in update.message.reply_text.call_args_list)
    assert "Invalid ticket ID format" not in out, out                 # cbd4e02: refused


# ---------------------------------------------------------------- an ambiguous id dispatched from the dashboard
def test_the_task_of_a_dispatched_ambiguous_id_closes_that_ticket(troot, tmp_path, monkeypatch):
    key = "mn79-test-key-" + "0123456789abcdef"
    monkeypatch.setenv("ATLAS_WEB_API_KEY", key)
    from fastapi.testclient import TestClient
    from atlas_plus.web import app as webapp
    from atlas_plus.brain import tasks
    monkeypatch.setattr(webapp, "API_KEY", key, raising=False)
    for folder, title in (("alpha", "new one"), ("archive", "old one")):
        d = troot / folder
        d.mkdir()
        (d / "ALP-005.md").write_text(f"---\nid: ALP-005\nproject: alpha\nstatus: todo\ntitle: {title}\nassignee: human\n---\nb\n",
                                      encoding="utf-8")
    sent = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"task_id": "t-mn79"}

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
    r = c.post("/api/tickets/archive/ALP-005/dispatch", json={})
    assert r.status_code == 202, r.text
    assert _status(troot / "archive" / "ALP-005.md") == "in-progress"
    db = tmp_path / "brain.db"
    t = tasks.create_task("old one", session_id=sent[0]["session_id"], db_path=db)  # what the Brain received
    assert tasks.mark_done(t.task_id, "finished: the change is in and the tests pass", db_path=db)
    assert _status(troot / "archive" / "ALP-005.md") == "done"       # cbd4e02: in-progress for ever
    assert _status(troot / "alpha" / "ALP-005.md") == "todo"


# ---------------------------------------------------------------- the banded scan's signature pass
def test_the_signature_pass_honours_a_deadline_that_has_passed():
    import time
    from atlas_plus.curation.duplicates import _lsh_pairs
    notes = [[f"only{i}a", f"only{i}b"] for i in range(600)]
    notes[598] = notes[599] = ["shared1", "shared2", "shared3"]
    out = list(_lsh_pairs(notes, 0.4, deadline=time.monotonic() - 1))
    assert None in out, out                                            # cbd4e02: [(598, 599)], every note signed, no stop


# ---------------------------------------------------------------- redaction
def test_a_quoted_status_word_after_a_key_stays_readable():
    from atlas_plus.redaction import redact
    for s in ('"api_key": "revoked"', "api_key: 'revoked'", 'password: "pending"'):
        assert redact(s) == s, redact(s)                               # cbd4e02: "[REDACTED]"
    fake = "Zx9Kq2" + "Lm7Rt4Wv8"  # assembled at runtime so secret scanners do not flag the fixture
    assert fake not in redact('"api_key": "' + fake + '"')


# ---------------------------------------------------------------- the setup hook parser
def _hook(v):
    return {"hooks": [{"type": "command",
                       "command": f"python -m atlas_plus.hooks.run --env 'ATLAS_WORKSPACE_ROOT={v}' stop"}]}


def test_dot_segments_name_the_same_windows_workspace():
    from atlas_plus.setup_cmd import _hook_workspace
    base = _hook_workspace(_hook(r"E:\work\ws"))
    assert _hook_workspace(_hook("E:\\work\\ws\\.")) == base          # cbd4e02: a second Stop hook
    assert _hook_workspace(_hook(r"E:\work\tmp\..\ws")) == base


def test_a_double_slash_path_on_posix_keeps_its_case(monkeypatch):
    from atlas_plus import setup_cmd
    monkeypatch.setattr(setup_cmd.os, "name", "posix")
    a = setup_cmd._hook_workspace(_hook("//home/u/WS"))
    b = setup_cmd._hook_workspace(_hook("//home/u/ws"))
    assert a != b, (a, b)                                              # cbd4e02: both '\\\\home\\u\\ws'


# ---------------------------------------------------------------- doctor's tag figures
def test_doctor_leaves_an_underscore_archive_out_of_the_untagged_figures(tmp_path):
    from atlas_plus.doctor.cli import check_memory_quality, check_untagged_by_scope, scan_memory_files
    root = tmp_path / ".memory"
    for rel, tags in (("projects/a.md", "tags: [x]"), ("projects/b.md", ""), ("_archive/old.md", ""),
                      ("projects/_archive/old.md", "")):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntitle: n\n{tags}\n---\n\nbody\n", encoding="utf-8")
    scan = scan_memory_files(root)
    mq = check_memory_quality(scan)
    assert (mq["total_files"], mq["untagged_files"]) == (2, 1), mq     # cbd4e02: 4 files, 3 untagged
    assert check_untagged_by_scope(scan) == {"projects": 1}            # cbd4e02: _archive(1), projects(2)
