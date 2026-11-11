"""Wave-2 six-lane validation (the 2026-11-09 six-agent run against candidate bee1a59), verified
against the fixed source. The seven R2 blockers plus the P2 rows; items the lane found on
candidate bee1a59 that rounds A/B had already closed are asserted here too, so the record
is one file.

Blockers: R2-01 federation egress, R2-02 junctions (manifest walk + MCP write path),
R2-03 anti scope, R2-04/05 concurrent ticket + anti writes, R2-06 conflict ancestry,
R2-07 fenced task finalization.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

WINDOWS = sys.platform == "win32"


def _junction(link: Path, target: Path) -> bool:
    """Create a directory junction (Windows) or a symlink; False when the OS refuses."""
    try:
        if WINDOWS:
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                               capture_output=True, text=True)
            return r.returncode == 0
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        return False


# =============================================================================
# R2-02: the manifest walk never enters a junction
# =============================================================================

def test_manifest_walk_does_not_enter_a_junction(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.sync import manifest
    corpus = tmp_path / "corpus"
    (corpus / "reference").mkdir(parents=True)
    (corpus / "reference" / "ok.md").write_text("ok", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private-note.md").write_text("SYNTHETIC PRIVATE", encoding="utf-8")
    if not _junction(corpus / "reference" / "bridge", outside):
        pytest.skip("cannot create a junction/symlink here")
    files = manifest.build_manifest(corpus)["files"]
    assert "reference/ok.md" in files
    assert not any(p.startswith("reference/bridge") for p in files)


# =============================================================================
# R2-02 / DATA-04: an MCP write never crosses a junction below the corpus root
# =============================================================================

def test_resolve_target_refuses_a_junction_component(tmp_path):
    from atlas_plus.mcp.memory_server import routing as mod
    corpus = tmp_path / "mem"
    corpus.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    if not _junction(corpus / "projects", outside):
        pytest.skip("cannot create a junction/symlink here")
    with patch.object(mod, "MEMORY_ROOT", corpus):
        with pytest.raises(ValueError, match="symlink or junction"):
            mod.resolve_target("project:demo", for_read=True)
    assert not list(outside.iterdir())
    # a plain directory tree is unaffected
    (corpus / "projects").unlink() if (corpus / "projects").is_symlink() else os.rmdir(corpus / "projects")
    (corpus / "projects").mkdir()
    with patch.object(mod, "MEMORY_ROOT", corpus):
        target, _ = mod.resolve_target("project:demo", for_read=True)
    assert target == corpus / "projects" / "demo.md"


# =============================================================================
# R2-01: federation egress is decided locally
# =============================================================================

@pytest.fixture
def federation(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    hub_root = tmp_path / "hub-mem"
    spoke_root = tmp_path / "spoke-mem"
    (hub_root / "reference" / "shared").mkdir(parents=True)
    (hub_root / "reference" / "shared" / "public.md").write_text("public v1", encoding="utf-8")
    (spoke_root / "reference" / "shared").mkdir(parents=True)
    (spoke_root / "reference" / "shared" / "public.md").write_text("public v1", encoding="utf-8")
    (spoke_root / "identity").mkdir()
    (spoke_root / "identity" / "private.md").write_text(
        "SYNTHETIC PRIVATE SPOKE NOTE NEVER AUTHORIZED FOR PEER", encoding="utf-8")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub_root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    _, token = devices.pair("narrow-peer", kind="peer", scope="rw", prefixes=("reference/shared/",))
    http = TestClient(api.create_app(), raise_server_exceptions=False)
    client = SyncClient(token=token, http_client=http)
    sent: list[dict] = []
    original = client.push

    def recording_push(changes):
        sent.append(json.loads(json.dumps(changes)))
        return original(changes)

    client.push = recording_push  # type: ignore[method-assign]
    return client, hub_root, spoke_root, sent


def test_peer_spoke_refuses_to_sync_without_a_share(federation):
    from atlas_plus.sync.client import SyncClientError
    client, hub_root, spoke_root, sent = federation
    with pytest.raises(SyncClientError, match="declare what may leave"):
        client.sync_once(spoke_root)
    with pytest.raises(SyncClientError):
        client.plan(spoke_root)
    assert sent == []
    assert not (hub_root / "_atlas").exists()


def test_share_filters_bytes_before_they_are_read(federation):
    client, hub_root, spoke_root, sent = federation
    first = client.sync_once(spoke_root, share=["reference/shared"])
    assert first.pushed == [] and first.errors == []
    (spoke_root / "reference" / "shared" / "public.md").write_text("public v2", encoding="utf-8")
    (spoke_root / "identity" / "private.md").write_text("still private, edited", encoding="utf-8")
    second = client.sync_once(spoke_root, share=["reference/shared"])
    assert second.pushed == ["reference/shared/public.md"]
    pushed_paths = {c["path"] for batch in sent for c in batch}
    assert pushed_paths == {"reference/shared/public.md"}
    blob = json.dumps(sent)
    assert "identity/private.md" not in blob and "NEVER AUTHORIZED" not in blob
    # the hub parked the peer's push as a proposal, not canonical content
    inbox = list((hub_root / "_atlas" / "sync-inbox" / "narrow-peer").rglob("public.md"))
    assert inbox and inbox[0].read_text(encoding="utf-8") == "public v2"
    assert (hub_root / "reference" / "shared" / "public.md").read_text(encoding="utf-8") == "public v1"
    # an identical re-push is not parked twice, and the file stays unresolved (base kept)
    third = client.sync_once(spoke_root, share=["reference/shared"])
    assert len(list((hub_root / "_atlas" / "sync-inbox" / "narrow-peer").rglob("public.md"))) == 1
    assert (spoke_root / "reference" / "shared" / "public.md").read_text(encoding="utf-8") == "public v2"
    assert third.errors == []


def test_share_also_bounds_what_is_pulled_in(federation):
    client, hub_root, spoke_root, sent = federation
    (hub_root / "reference" / "other.md").write_text("hub only", encoding="utf-8")
    client.sync_once(spoke_root, share=["reference/shared"])
    assert not (spoke_root / "reference" / "other.md").exists()


@pytest.mark.parametrize("bad", ["../x", "a//b", "c:x", "a\\b"])
def test_share_prefix_must_be_a_safe_relpath(bad):
    from atlas_plus.sync.client import SyncClientError, normalize_share
    with pytest.raises(SyncClientError):
        normalize_share([bad])
    assert normalize_share(["reference/a/", " reference/b , projects/c"]) == (
        "reference/a/", "reference/b/", "projects/c/")


# =============================================================================
# R2-06: an unresolved conflict stays a conflict
# =============================================================================

@pytest.fixture
def device_world(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    hub_root = tmp_path / "hub-mem"
    spoke_root = tmp_path / "spoke-mem"
    (hub_root / "reference").mkdir(parents=True)
    (hub_root / "reference" / "conflict.md").write_text("base", encoding="utf-8")
    spoke_root.mkdir()
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub_root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    _, token = devices.pair("laptop")
    http = TestClient(api.create_app(), raise_server_exceptions=False)
    return SyncClient(token=token, http_client=http), hub_root, spoke_root


def test_repeated_sync_never_adopts_the_other_side_over_an_unresolved_conflict(device_world):
    client, hub_root, spoke_root = device_world
    client.sync_once(spoke_root)
    note_hub = hub_root / "reference" / "conflict.md"
    note_spoke = spoke_root / "reference" / "conflict.md"
    note_hub.write_text("HUB independently edited", encoding="utf-8")
    note_spoke.write_text("SPOKE independently edited", encoding="utf-8")
    first = client.sync_once(spoke_root)
    assert first.conflicts == ["reference/conflict.md"]
    for _ in range(3):  # the lane's repeat, and then some
        again = client.sync_once(spoke_root)
        assert again.conflicts == ["reference/conflict.md"]
        assert again.pulled == [] and again.pushed == []
        assert note_spoke.read_text(encoding="utf-8") == "SPOKE independently edited"
        assert note_hub.read_text(encoding="utf-8") == "HUB independently edited"
    parked = list((spoke_root / "_atlas" / "sync-conflicts").rglob("conflict*.md"))
    assert len(parked) == 1 and parked[0].read_text(encoding="utf-8") == "HUB independently edited"
    # explicit resolution: the operator adopts the hub's text -> both sides agree, base moves on
    note_spoke.write_text("HUB independently edited", encoding="utf-8")
    resolved = client.sync_once(spoke_root)
    assert resolved.conflicts == [] and resolved.errors == []
    clean = client.sync_once(spoke_root)
    assert clean.conflicts == [] and clean.pulled == [] and clean.pushed == []


def test_quarantine_dedupes_identical_bytes(tmp_path):
    from atlas_plus.sync import apply
    root = tmp_path / "c"
    root.mkdir()
    first = apply.quarantine(root, "reference/x.md", b"same", reason="r1")
    second = apply.quarantine(root, "reference/x.md", b"same", reason="r2")
    third = apply.quarantine(root, "reference/x.md", b"different", reason="r3")
    assert first == second and third != first
    assert len(list((root / "_atlas" / "sync-conflicts").rglob("x*.md"))) == 2


# =============================================================================
# R2-07 / TASK-02 / TASK-03: task store
# =============================================================================

@pytest.fixture
def db(tmp_path):
    return tmp_path / "state.sqlite"


def test_stale_finalization_cannot_touch_a_reclaimed_task(db):
    from atlas_plus.brain import tasks as ts
    t = ts.create_task("harmless", db_path=db, max_attempts=3)
    a = ts.claim_next_task("worker-A", lease_seconds=0, db_path=db)
    assert a is not None and a.lease_owner == "worker-A"
    b = ts.claim_next_task("worker-B", db_path=db)
    assert b is not None and b.lease_owner == "worker-B" and b.attempts == 2
    assert ts.heartbeat_task(t.task_id, "worker-A", db_path=db) is False
    # A's late completion changes NOTHING
    assert ts.mark_done(t.task_id, "Old client A stale completion", db_path=db, owner="worker-A") is False
    row = ts.get_task(t.task_id, db_path=db)
    assert row.status == "running" and row.lease_owner == "worker-B" and row.result is None
    assert ts.mark_failed(t.task_id, "stale", db_path=db, owner="worker-A", attempt=1) is False
    assert ts.mark_blocked(t.task_id, "stale", db_path=db, owner="worker-A") is False
    # B finishes; A's late completion still cannot replace B's result
    assert ts.mark_done(t.task_id, "New client B completion", db_path=db, owner="worker-B") is True
    assert ts.mark_done(t.task_id, "Old client A stale completion", db_path=db, owner="worker-A") is False
    row = ts.get_task(t.task_id, db_path=db)
    assert row.status == "done" and row.result == "New client B completion"


def test_worker_finishes_under_its_own_lease(db, tmp_path, monkeypatch):
    from atlas_plus.brain import tasks as ts, worker, executors
    t = ts.create_task("x", db_path=db, max_attempts=3)
    a = ts.claim_next_task("w-A", lease_seconds=0, db_path=db)
    ts.claim_next_task("w-B", db_path=db)
    res = executors.ExecutionResult(status="done", text="from A, late")
    worker._finish_task(t.task_id, res, db_path=db, task=a, owner="w-A")
    row = ts.get_task(t.task_id, db_path=db)
    assert row.status == "running" and row.lease_owner == "w-B"


def test_cleanup_requeues_while_attempts_remain_and_fails_when_spent(db):
    import sqlite3
    from atlas_plus.brain import tasks as ts
    t = ts.create_task("crashy", db_path=db, max_attempts=3)
    ts.claim_next_task("w-1", db_path=db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE brain_tasks SET heartbeat_at='2020-01-01T00:00:00+00:00' WHERE task_id=?",
                     (t.task_id,))
    assert ts.cleanup_zombie_tasks(max_running_minutes=30, db_path=db) == 1
    row = ts.get_task(t.task_id, db_path=db)
    assert row.status == "pending" and row.lease_owner is None and row.attempts == 1
    assert "requeued" in (row.error or "")
    again = ts.claim_next_task("w-2", db_path=db)
    assert again is not None and again.attempts == 2
    # budget spent -> terminal
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE brain_tasks SET attempts=max_attempts, "
                     "heartbeat_at='2020-01-01T00:00:00+00:00' WHERE task_id=?", (t.task_id,))
    assert ts.cleanup_zombie_tasks(max_running_minutes=30, db_path=db) == 1
    assert ts.get_task(t.task_id, db_path=db).status == "failed"


def test_cleanup_still_fails_rows_that_were_never_leased(db):
    import sqlite3
    from atlas_plus.brain import tasks as ts
    t = ts.create_task("inline", db_path=db)
    ts.mark_running(t.task_id, db_path=db)
    with sqlite3.connect(str(db)) as conn:
        conn.execute("UPDATE brain_tasks SET heartbeat_at='2020-01-01T00:00:00+00:00' WHERE task_id=?",
                     (t.task_id,))
    assert ts.cleanup_zombie_tasks(max_running_minutes=30, db_path=db) == 1
    assert ts.get_task(t.task_id, db_path=db).status == "failed"


def test_concurrent_dedup_yields_one_task(db):
    from atlas_plus.brain import tasks as ts
    ts.create_task("warm up schema", db_path=db)
    ids: list[str] = []
    flags: list[bool] = []
    barrier = threading.Barrier(8)
    lock = threading.Lock()

    def submit():
        barrier.wait()
        t = ts.create_task("same instruction", agent="brain", db_path=db, dedup=True)
        with lock:
            ids.append(t.task_id)
            flags.append(t.deduplicated)

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(set(ids)) == 1 and flags.count(False) == 1
    assert len([t for t in ts.list_tasks(db_path=db) if t.instruction == "same instruction"]) == 1


def test_normal_concurrent_claims_stay_unique(db):
    from atlas_plus.brain import tasks as ts
    for i in range(24):
        ts.create_task(f"task {i}", db_path=db)
    claimed: list[str] = []
    lock = threading.Lock()

    def run(worker):
        while True:
            t = ts.claim_next_task(worker, db_path=db)
            if t is None:
                return
            with lock:
                claimed.append(t.task_id)
            ts.mark_done(t.task_id, "ok", db_path=db, owner=worker)

    threads = [threading.Thread(target=run, args=(f"w{i}",)) for i in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(claimed) == 24 and len(set(claimed)) == 24


# =============================================================================
# R2-03 / R2-04 / R2-05 / DATA-06 (closed by rounds A and B; held here)
# =============================================================================

def test_anti_scope_cannot_leave_the_anti_directory(tmp_path):
    from atlas_plus.mcp.memory_server.tools.anti import anti_remember
    anti_root = tmp_path / "mem" / "anti"
    sibling = tmp_path / "mem" / "identity.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("---\nid: identity\n---\nuntouched\n", encoding="utf-8")
    with patch("atlas_plus.mcp.memory_server.tools.anti.ANTI_ROOT", anti_root), \
         patch("atlas_plus.autonomous.require_autonomous_writes"):
        for bad in ("../identity", "..\\identity", "/abs", "c:x", "a/b", ".hidden"):
            r = anti_remember(bad, "never do this again please")
            assert r["ok"] is False, bad
    assert sibling.read_text(encoding="utf-8").endswith("untouched\n")
    assert not anti_root.exists() or not list(anti_root.iterdir())


def test_96_concurrent_anti_writes_all_survive(tmp_path):
    from atlas_plus.mcp.memory_server.tools.anti import anti_remember
    anti_root = tmp_path / "anti"
    errors: list[str] = []
    barrier = threading.Barrier(8)

    def writer(n):
        barrier.wait()
        for i in range(12):
            r = anti_remember("libs", f"marker-{n}-{i} is rejected for reasons number {n}{i}")
            if not r["ok"]:
                errors.append(r["error"])

    with patch("atlas_plus.mcp.memory_server.tools.anti.ANTI_ROOT", anti_root), \
         patch("atlas_plus.autonomous.require_autonomous_writes"), \
         patch("atlas_plus.mcp.memory_server.tools.anti.propagate_anti_rejection",
               return_value={"flagged": 0, "skipped": 0, "paths": []}):
        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    assert errors == []
    text = (anti_root / "libs.md").read_text(encoding="utf-8")
    missing = [f"marker-{n}-{i}" for n in range(8) for i in range(12) if f"marker-{n}-{i} " not in text]
    assert missing == []


def test_96_concurrent_ticket_creates_all_survive(tmp_path):
    from atlas_plus.tickets import store
    root = tmp_path / "tickets"
    root.mkdir()
    ids: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def creator(n):
        barrier.wait()
        for i in range(12):
            t = store.create_ticket(f"marker {n}-{i}", "app", tickets_root=root)
            with lock:
                ids.append(t.id)

    threads = [threading.Thread(target=creator, args=(n,)) for n in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(ids) == 96 and len(set(ids)) == 96
    files = list((root / "app").glob("APP-*.md"))
    assert len(files) == 96
    titles = {store._parse_ticket(f).title for f in files}
    assert titles == {f"marker {n}-{i}" for n in range(8) for i in range(12)}


def test_ticket_project_dotdot_is_refused(tmp_path):
    from atlas_plus.tickets import store
    root = tmp_path / "corpus" / "tickets"
    root.mkdir(parents=True)
    for bad in ("..", ".", "...", "../x", "nul", "con.md"):
        with pytest.raises(ValueError):
            store.create_ticket("t", bad, tickets_root=root)
    assert not list((tmp_path / "corpus").glob("*.md"))


# =============================================================================
# DATA-07: project-scoped propagation sees projects/<slug>.md
# =============================================================================

def test_project_propagation_flags_the_project_file(tmp_path):
    from atlas_plus.mcp.memory_server.tools import anti
    root = tmp_path / "mem"
    (root / "projects").mkdir(parents=True)
    note = root / "projects" / "demo.md"
    note.write_text("---\nid: demo\n---\n\nWe should adopt the redis cache layer for sessions.\n",
                    encoding="utf-8")
    other = root / "projects" / "other.md"
    other.write_text("---\nid: other\n---\n\nAdopt the redis cache layer here too.\n", encoding="utf-8")
    with patch("atlas_plus.mcp.memory_server.routing.MEMORY_ROOT", root):
        res = anti.propagate_anti_rejection("adopt redis cache layer", "libs", memory_root=root,
                                            applies_to="project", project="Demo")
    assert res["flagged"] == 1 and res["paths"] == [str(note)]
    assert "anti_flagged: true" in note.read_text(encoding="utf-8")
    assert "anti_flagged" not in other.read_text(encoding="utf-8")


# =============================================================================
# Imports: reimport idempotence across collision copies; entry shapes
# =============================================================================

def test_reimport_of_a_changed_note_does_not_grow_forever(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    from atlas_plus import import_cmd
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "gamma.md"
    note.write_text("version one", encoding="utf-8")
    counts = []
    for text in ("version one", "version one", "version two", "version two", "version two"):
        note.write_text(text, encoding="utf-8")
        import_cmd.import_obsidian(vault, "reference/obsidian", execute=True)
        counts.append(len(list((tmp_path / "mem").rglob("gamma*.md"))))
    assert counts == [1, 1, 2, 2, 2]


def test_wrong_shaped_export_entries_are_skipped_not_crashed(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    from atlas_plus import import_cmd
    f = tmp_path / "conversations.json"
    f.write_text(json.dumps([None, "x", 42, {"title": "t", "mapping": "bad"},
                             {"title": "ok", "create_time": 1735689600,
                              "mapping": {"n": {"message": {"author": {"role": "user"},
                                                            "create_time": 1, "content": {"parts": ["hi"]}}}}}]),
                 encoding="utf-8")
    r = import_cmd.import_chatgpt(f, execute=True)
    assert r["imported"] == 1 and r["skipped_empty"] == 4
    c = tmp_path / "claude.json"
    c.write_text(json.dumps([None, {"name": "n", "chat_messages": "bad"},
                             {"name": "ok", "created_at": "2025-01-01T00:00:00Z",
                              "chat_messages": [None, {"sender": "human", "text": "hi"}]}]), encoding="utf-8")
    r = import_cmd.import_claude(c, execute=True)
    assert r["imported"] == 1 and r["skipped_empty"] == 2


# =============================================================================
# Web: inbox JSON shapes, bearer casing, WS shares the failure accounting
# =============================================================================

@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "memory" / "identity").mkdir(parents=True)
    (tmp_path / "memory" / "identity" / "user.md").write_text("---\nid: user\n---\n", encoding="utf-8")
    from fastapi.testclient import TestClient
    from atlas_plus.web import app as web_app
    monkeypatch.setattr(web_app, "API_KEY", "k" * 32)
    monkeypatch.setattr(web_app, "_ALLOW_NO_AUTH", False)
    web_app.AUTH_FAILURES.clear()
    c = TestClient(web_app.app, raise_server_exceptions=False)
    yield c, web_app
    web_app.AUTH_FAILURES.clear()


@pytest.mark.parametrize("body", ["null", "[]", "42", '"string"', '{"messages": [1, 2]}'])
def test_inbox_rejects_wrong_json_shapes_with_400(web, body):
    c, _ = web
    r = c.post("/api/inbox/conversation", content=body,
               headers={"Authorization": "Bearer " + "k" * 32, "Content-Type": "application/json"})
    assert r.status_code == 400, r.text


def test_bearer_scheme_is_case_insensitive(web):
    c, _ = web
    for scheme in ("Bearer", "bearer", "BEARER"):
        r = c.get("/api/projects", headers={"Authorization": f"{scheme} " + "k" * 32})
        assert r.status_code == 200, scheme
    assert c.get("/api/projects", headers={"Authorization": "bearer wrong"}).status_code == 401


def test_ws_handshake_shares_the_lockout(web):
    from starlette.websockets import WebSocketDisconnect
    c, web_app = web
    for _ in range(web_app.AUTH_FAILURES.max_failures_per_window):
        web_app.AUTH_FAILURES.record(path="/api/x", client_ip="testclient", reason="bad_key")
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/ws/chat/s1", headers={"Authorization": "Bearer " + "k" * 32}):
            pass
    assert exc.value.code == 4429
    web_app.AUTH_FAILURES.clear()
    before = web_app.AUTH_FAILURES.stats()["total"]
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/chat/s1", headers={"Authorization": "Bearer nope"}):
            pass
    assert web_app.AUTH_FAILURES.stats()["total"] == before + 1


# =============================================================================
# Sync hub and Brain: failed-auth throttling
# =============================================================================

def test_sync_hub_throttles_a_source_after_repeated_bad_tokens(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "mem").mkdir()
    _, token = devices.pair("laptop")
    api.AUTH_FAILURES.clear()
    c = TestClient(api.create_app(), raise_server_exceptions=False)
    try:
        fwd = {"Cf-Connecting-Ip": "203.0.113.9"}
        # TestClient's peer is "testclient" (not loopback): counted per peer, forwarded header ignored
        codes = [c.get("/sync/v1/manifest", headers={"Authorization": "Bearer wrong", **fwd}).status_code
                 for _ in range(api.AUTH_FAILURES.max_failures_per_window + 2)]
        assert codes[0] == 401 and codes[-1] == 429
        r = c.get("/sync/v1/manifest", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 429 and "Retry-After" in r.headers
    finally:
        api.AUTH_FAILURES.clear()
    r = c.get("/sync/v1/manifest", headers={"Authorization": f"bearer {token}"})
    assert r.status_code == 200 and r.json()["principal"]["kind"] == "device"


def test_brain_throttles_bad_tokens_and_accepts_lowercase_scheme(monkeypatch):
    from fastapi.testclient import TestClient
    from atlas_plus.brain import server as srv
    monkeypatch.setattr(srv, "_API_KEY", "b" * 32)
    monkeypatch.setattr(srv, "_ALLOW_NO_AUTH", False)
    srv.AUTH_FAILURES.clear()
    c = TestClient(srv.app, raise_server_exceptions=False)
    try:
        assert c.get("/health").status_code == 200
        codes = [c.get("/tasks", headers={"Authorization": "Bearer wrong"}).status_code
                 for _ in range(srv.AUTH_FAILURES.max_failures_per_window + 2)]
        assert codes[0] == 401 and codes[-1] == 429
        assert c.get("/tasks", headers={"Authorization": "Bearer " + "b" * 32}).status_code == 429
        srv.AUTH_FAILURES.clear()
        r = c.get("/tasks", headers={"Authorization": "bearer " + "b" * 32})
        assert r.status_code == 200
    finally:
        srv.AUTH_FAILURES.clear()


# =============================================================================
# Startup entry points
# =============================================================================

def test_mcp_no_auth_serves_through_the_uvicorn_path(monkeypatch):
    from atlas_plus.mcp.memory_server import server as mcp_srv
    calls = []
    monkeypatch.setattr(mcp_srv, "_MCP_TOKEN", "")  # CI exports a short smoke token; --no-auth must not read it
    monkeypatch.setattr(mcp_srv, "_run_http_with_auth", lambda token: calls.append(token))
    monkeypatch.setattr(mcp_srv.mcp, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("FastMCP.run used")))
    monkeypatch.setattr(sys, "argv", ["sextant-mcp", "--no-auth"])
    monkeypatch.delenv("ATLAS_MCP_HOST", raising=False)
    mcp_srv.main()
    assert calls == [""]
    monkeypatch.setenv("ATLAS_MCP_HOST", "0.0.0.0")
    with pytest.raises(SystemExit):
        mcp_srv.main()


def test_web_and_brain_module_entry_serve_the_configured_app_object(monkeypatch):
    import uvicorn
    from atlas_plus.web import app as web_app
    from atlas_plus.brain import server as brain
    served = []
    # record the originals FIRST so teardown restores "auth required" for later tests
    monkeypatch.setattr(web_app, "_ALLOW_NO_AUTH", False)
    monkeypatch.setattr(brain, "_ALLOW_NO_AUTH", False)
    monkeypatch.setattr(uvicorn, "run", lambda target, **kw: served.append(target))
    # earlier tests already drove these apps through TestClient, so the middleware stack is
    # built; main() adds TrustedHost before serving in real life -- not what is under test
    monkeypatch.setattr(web_app.app, "add_middleware", lambda *a, **k: None)
    monkeypatch.setattr(brain.app, "add_middleware", lambda *a, **k: None)
    monkeypatch.setattr(web_app, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(web_app, "API_KEY", "")
    monkeypatch.setattr(sys, "argv", ["sextant-web", "--no-auth"])
    web_app.main()
    monkeypatch.setattr(brain, "_HOST", "127.0.0.1")
    monkeypatch.setattr(brain, "_API_KEY", "")
    monkeypatch.setattr(sys, "argv", ["sextant-brain", "--no-auth"])
    brain.main()
    assert served[0] is web_app.app and served[1] is brain.app
    assert web_app._ALLOW_NO_AUTH is True and brain._ALLOW_NO_AUTH is True


# =============================================================================
# Backup status keeps the last real run
# =============================================================================

def test_dry_run_never_overwrites_the_last_real_backup(tmp_path, monkeypatch):
    from atlas_plus import backup as b
    src = tmp_path / "mem"
    (src / "identity").mkdir(parents=True)
    for i in range(8):
        (src / f"f{i}.md").write_text("x", encoding="utf-8")

    monkeypatch.setattr(b, "RCLONE_REMOTE", "r")  # a named remote (no default since the persona pass)
    class R:
        returncode = 0
        stdout = f"{b.RCLONE_REMOTE}:" + chr(10)  # listremotes: the remote exists
        stderr = ""
    monkeypatch.setattr(b, "SYNC_TARGETS", [{"name": "memory corpus", "source": str(src), "dest": "r:x/m", "exclude": []}])
    monkeypatch.setattr(b, "ARCHIVE_TARGETS", [])
    monkeypatch.setattr(b, "_rclone", lambda *a, **k: R())
    monkeypatch.setattr(b, "_log", lambda *a, **k: None)
    monkeypatch.setattr(b, "BACKUP_STATE", tmp_path / "backup-state.json")
    monkeypatch.setattr(b, "LOG_DIR", tmp_path)
    b.now(dry_run=False, skip_archive=True)
    real = json.loads((tmp_path / "backup-state.json").read_text(encoding="utf-8"))
    assert real["dry_run"] is False and real["last_real_run"] == real["last_run"]
    b.now(dry_run=True, skip_archive=True)
    after = json.loads((tmp_path / "backup-state.json").read_text(encoding="utf-8"))
    assert after["dry_run"] is True
    assert after["last_real_run"] == real["last_run"] and after["last_real_success"] == real["last_run"]
    assert after["last_run"] != real["last_run"] or after["last_run"] >= real["last_run"]


def test_backup_install_fallback_names_a_real_command():
    import inspect
    from atlas_plus import backup as b
    src = inspect.getsource(b.install)
    assert "sextant-backup now" in src and "sextant-backup run" not in src


# =============================================================================
# Hooks: project detection by resolved ancestry (HOOK-01)
# =============================================================================

def test_project_detection_uses_resolved_ancestry_not_string_prefix(tmp_path, monkeypatch):
    from atlas_plus.hooks import session_start as hook
    projects = tmp_path / "Projects"
    (projects / "demo" / ".git").mkdir(parents=True)
    (tmp_path / "Projects-copy" / "demo" / ".git").mkdir(parents=True)
    spelled = Path(str(projects).lower()) if WINDOWS else projects
    with patch.object(hook, "PROJECTS_ROOT", spelled):
        monkeypatch.chdir(projects / "demo")
        found = hook._detect_project()
        assert found is not None and found[0] == "demo"
        monkeypatch.chdir(tmp_path / "Projects-copy" / "demo")
        assert hook._detect_project() is None
