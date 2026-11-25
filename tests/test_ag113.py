"""Sweep 32 AG113: small truths in sync, tickets and the Agents tab (SEX-023, SEX-026, SEX-025, SEX-020).

Every test runs the fixed path in-process: the sync CLI against an in-process hub, the tickets store and the
web app through FastAPI's TestClient, the duplicate scan with a fake clock. No live service is reached: every
service URL points at a dead port.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# -- SEX-023: the sync CLI ------------------------------------------------------------------------------

@pytest.fixture
def world(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    hub_root = tmp_path / "hub-mem"
    spoke_root = tmp_path / "spoke-mem"
    (hub_root / "notes").mkdir(parents=True)
    (spoke_root / "notes").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub_root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    monkeypatch.delenv("ATLAS_SYNC_HUB", raising=False)
    monkeypatch.setattr(api, "_corpus_root", lambda: hub_root)
    _, token = devices.pair("spoke")

    def client():
        return SyncClient(token=token, http_client=TestClient(api.create_app(), raise_server_exceptions=False))

    from atlas_plus.sync import client as client_mod
    monkeypatch.setattr(client_mod, "SyncClient", lambda hub, tok, *a, **k: client())
    return client, hub_root, spoke_root


def _run_sync_cli(monkeypatch, capsys, argv, memory_root=None):
    from atlas_plus.sync import cli
    if memory_root is not None:
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(memory_root))
    monkeypatch.setattr(sys, "argv", ["sextant-sync", *argv])
    with pytest.raises(SystemExit) as info:
        cli.main()
    out = capsys.readouterr()
    return info.value.code, out.out, out.err


HUB = ["--hub", "http://127.0.0.1:9", "--token", "x"]


def test_a_dry_run_names_the_files_it_would_move(world, monkeypatch, capsys):
    _make, hub_root, spoke_root = world
    (hub_root / "notes" / "from-hub.md").write_text("hub note", encoding="utf-8")
    (spoke_root / "notes" / "from-spoke.md").write_text("spoke note", encoding="utf-8")
    code, out, _ = _run_sync_cli(monkeypatch, capsys, ["sync", "--dry-run", *HUB], memory_root=spoke_root)
    assert code == 0
    assert "would pull 1, would push 1" in out
    assert "would pull: notes/from-hub.md" in out
    assert "would push: notes/from-spoke.md" in out
    assert not (spoke_root / "notes" / "from-hub.md").exists()  # still a dry run


def test_checkin_exits_1_when_not_current_and_0_in_sync(world, monkeypatch, capsys):
    _make, hub_root, spoke_root = world
    (hub_root / "notes" / "a.md").write_text("a", encoding="utf-8")
    code, out, _ = _run_sync_cli(monkeypatch, capsys, ["checkin", *HUB], memory_root=spoke_root)
    assert "different corpus state" in out or "not current" in out
    assert code == 1
    code, _out, _ = _run_sync_cli(monkeypatch, capsys, ["sync", *HUB], memory_root=spoke_root)
    assert code == 0
    code, out, _ = _run_sync_cli(monkeypatch, capsys, ["checkin", *HUB], memory_root=spoke_root)
    assert out.strip() == "in sync with hub" and code == 0


@pytest.mark.parametrize("hub,warned", [
    ("http://hub.example-org.test:8780", True),
    ("http://192.0.2.10:8780", True),
    ("https://hub.example-org.test", False),
    ("http://127.0.0.1:9", False),
    ("http://localhost:9", False),
])
def test_status_warns_about_a_plain_http_hub(tmp_path, monkeypatch, capsys, hub, warned):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "mem").mkdir()
    monkeypatch.setenv("ATLAS_SYNC_HUB", hub)
    code, out, _ = _run_sync_cli(monkeypatch, capsys, ["status"])
    assert code == 0 and f"hub: {hub}" in out
    assert ("plain http://" in out) is warned


def test_devices_all_columns_line_up_once_ids_show(tmp_path, monkeypatch, capsys):
    from atlas_plus.sync import devices
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    devices.pair("laptop")
    devices.revoke("laptop")
    devices.pair("laptop")
    devices.pair("p")
    code, out, _ = _run_sync_cli(monkeypatch, capsys, ["devices", "--all"])
    rows = [line for line in out.splitlines() if "last check-in:" in line]
    assert code == 0 and len(rows) == 3
    assert any("(" in r for r in rows)  # the ids show for the two `laptop` rows
    assert len({r.index("last check-in:") for r in rows}) == 1, rows


# -- SEX-026: tickets ------------------------------------------------------------------------------------

def _ticket(path: Path, tid: str, project: str, status: str = "todo") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {tid}\ntitle: ticket {tid}\ntype: ticket\nstatus: {status}\nproject: {project}\n"
        f"assignee: human\npriority: normal\neffort: M\nupdated: '2026-11-25'\n---\n\nbody\n",
        encoding="utf-8")


def test_list_project_shows_its_done_folder_like_plain_list(tmp_path):
    from atlas_plus.tickets.store import list_tickets
    root = tmp_path / "tickets"
    _ticket(root / "alpha" / "ALP-001.md", "ALP-001", "alpha")
    _ticket(root / "alpha" / "done" / "ALP-002.md", "ALP-002", "alpha", status="done")
    every = {t.id for t in list_tickets(tickets_root=root)}
    mine = {t.id for t in list_tickets(project="alpha", tickets_root=root)}
    assert every == {"ALP-001", "ALP-002"}
    assert mine == every


def test_json_names_the_folder_after_a_rename(tmp_path, monkeypatch):
    from atlas_plus.tickets import store
    root = tmp_path / "tickets"
    monkeypatch.setattr(store, "TICKETS_ROOT", root)
    _ticket(root / "beta-renamed" / "BET-001.md", "BET-001", "beta")
    _ticket(root / "gamma" / "GAM-001.md", "GAM-001", "gamma")
    by_id = {t.id: t.as_dict() for t in store.list_tickets()}
    assert by_id["BET-001"]["project"] == "beta" and by_id["BET-001"]["folder"] == "beta-renamed"
    assert "folder" not in by_id["GAM-001"]  # unchanged when folder and project agree
    # the MCP tickets list reads the same dict
    from atlas_plus.mcp.memory_server.tools import tickets as mcp_tickets
    rows = mcp_tickets.tickets_list(tickets_root=root)
    assert [r.get("folder") for r in rows if r["id"] == "BET-001"] == ["beta-renamed"]


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_WEB_API_KEY", "k-" + "ag113")
    monkeypatch.setenv("ATLAS_BRAIN_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.tickets import store
    root = tmp_path / "mem" / "tickets"
    monkeypatch.setattr(store, "TICKETS_ROOT", root)
    from fastapi.testclient import TestClient
    from atlas_plus.web import app as app_mod
    monkeypatch.setattr(app_mod, "BRAIN_URL", "http://127.0.0.1:9", raising=False)
    key = os.environ.get("ATLAS_WEB_API_KEY")
    api_key = getattr(app_mod, "API_KEY", None) or getattr(app_mod, "_API_KEY", None) or key
    return TestClient(app_mod.app, raise_server_exceptions=False), {"Authorization": f"Bearer {api_key}"}, root


def test_an_ambiguous_id_is_409_on_patch_status_too(web):
    client, auth, root = web
    _ticket(root / "one" / "DUP-001.md", "DUP-001", "one")
    _ticket(root / "two" / "DUP-001.md", "DUP-001", "two")
    r = client.patch("/api/tickets/DUP-001/status", json={"status": "done"}, headers=auth)
    assert r.status_code == 409, r.text
    assert "one/DUP-001" in r.text and "two/DUP-001" in r.text
    r = client.patch("/api/tickets/NOPE-404/status", json={"status": "done"}, headers=auth)
    assert r.status_code == 400  # unknown stays as documented
    r = client.patch("/api/tickets/one/DUP-001/status", json={"status": "done"}, headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "done"


# -- SEX-025: the Agents tab ------------------------------------------------------------------------------

def test_dispatch_is_disabled_with_the_reason_on_an_offline_agent(web):
    client, auth, _root = web
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"agents": [
        {"name": "brain", "model": "m", "online": True, "description": "d", "capabilities": []},
        {"name": "coder", "model": "m", "online": False, "description": "d", "capabilities": []},
    ]}
    resp.raise_for_status = MagicMock()
    http = MagicMock()
    http.get = AsyncMock(return_value=resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=http)
    ctx.__aexit__ = AsyncMock(return_value=False)
    with patch("atlas_plus.web.app.httpx.AsyncClient", return_value=ctx), \
         patch("atlas_plus.brain.improvement.all_agent_recent_scores", return_value={}), \
         patch("atlas_plus.brain.improvement.all_agent_stats", return_value=[]):
        r = client.get("/api/agents", headers=auth)
    assert r.status_code == 200, r.text
    assert 'quickDispatch("brain")' in r.text or "quickDispatch(&quot;brain&quot;)" in r.text or "quickDispatch(" in r.text
    assert r.text.count("quickDispatch(") == 1  # only the online agent is offered
    assert "Dispatch (offline)" in r.text and "disabled" in r.text and "offline: a task sent now would fail" in r.text


# -- SEX-020: the duplicate scan's budget (documented: the reading is not counted) -------------------------

def test_the_duplicate_budget_starts_after_the_notes_are_read(tmp_path, monkeypatch):
    """docs/CONFIGURATION.md: the clock starts once the notes are read. A reader that costs 10 (fake) seconds a
    note against a 5-second budget must leave the scan complete, not partial."""
    from atlas_plus.curation import _corpus_pass, duplicates as d
    root = tmp_path / "mem"
    (root / "notes").mkdir(parents=True)
    for i in range(40):
        (root / "notes" / f"n{i:02d}.md").write_text(
            f"# note {i}\n\nthe backup job runs nightly at two in the morning on the build host number {i % 3}\n",
            encoding="utf-8")
    monkeypatch.setenv("ATLAS_DUPLICATES_BUDGET_SECONDS", "5")
    offset = [0.0]
    real_mono = time.monotonic
    real_read = _corpus_pass.read_text
    reads = [0]

    def slow_read(p, *a, **k):
        offset[0] += 10.0
        reads[0] += 1
        return real_read(p, *a, **k)

    monkeypatch.setattr(_corpus_pass, "read_text", slow_read)
    monkeypatch.setattr(time, "monotonic", lambda: real_mono() + offset[0])
    pairs = d.find_duplicates(memory_root=root)
    assert reads[0] >= 40 and offset[0] >= 400  # the reading "took" far longer than the budget
    assert list(d._LAST_PARTIAL) == [0, 0]       # and the scan still ran whole
    assert pairs
    doc = Path(__file__).resolve().parents[1] / "docs" / "CONFIGURATION.md"
    assert "The clock starts once the notes are read" in doc.read_text(encoding="utf-8")
