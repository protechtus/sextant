"""Round C (2026-11-10): three independent reviewers re-tested the wave-2 fixes. Every
reproduced finding is pinned here.

Lane A (sync): conflicted file deleted on the hub, local deletes never resurrected, share runs
keep the out-of-share base, the hub's allowlist is intersected, non-protocol responses are
per-file errors, non-portable names refused, dedupe without globbing the stem, non-dict
sidecars, old hubs refused without a share.
Lane B (Brain): heartbeat keeper stops on a lost lease, attempt in the worker fence, fence
implies running, dedup keyed on kind and never on an explicit task_id, /task 503 on a busy store.
Lane C: reparse guard keys on the name-surrogate tag, import copies by original_path, inbox
RecursionError and non-string content, backup status on odd state, anti slug strip.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

WINDOWS = sys.platform == "win32"


# =============================================================================
# Lane A: sync
# =============================================================================

@pytest.fixture
def world(tmp_path, monkeypatch):
    """Hub + spoke in process; pair() takes kind/prefixes so one fixture serves every case."""
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    hub_root = tmp_path / "hub-mem"
    spoke_root = tmp_path / "spoke-mem"
    (hub_root / "reference").mkdir(parents=True)
    (spoke_root / "reference").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub_root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")

    def make(kind="device", prefixes=()):
        _, token = devices.pair("spoke", kind=kind, scope="rw", prefixes=tuple(prefixes))
        http = TestClient(api.create_app(), raise_server_exceptions=False)
        client = SyncClient(token=token, http_client=http)
        sent: list[list[dict]] = []
        original = client.push

        def recording_push(changes):
            sent.append(json.loads(json.dumps(changes)))
            return original(changes)

        client.push = recording_push  # type: ignore[method-assign]
        return client, sent

    return make, hub_root, spoke_root


def test_hub_delete_of_a_conflicted_file_is_reported_not_an_error(world):
    make, hub_root, spoke_root = world
    client, _ = make()
    (hub_root / "reference" / "c.md").write_text("base", encoding="utf-8")
    client.sync_once(spoke_root)
    (hub_root / "reference" / "c.md").write_text("hub edit", encoding="utf-8")
    (spoke_root / "reference" / "c.md").write_text("spoke edit", encoding="utf-8")
    assert client.sync_once(spoke_root).conflicts == ["reference/c.md"]
    (hub_root / "reference" / "c.md").unlink()
    for _ in range(3):
        r = client.sync_once(spoke_root)
        assert r.errors == [] and r.remote_deleted == ["reference/c.md"] and r.conflicts == []
        assert (spoke_root / "reference" / "c.md").read_text(encoding="utf-8") == "spoke edit"


def test_local_delete_is_reported_every_time_and_never_resurrected(world):
    make, hub_root, spoke_root = world
    client, _ = make()
    (hub_root / "reference" / "note.md").write_text("hub v1", encoding="utf-8")
    client.sync_once(spoke_root)
    (spoke_root / "reference" / "note.md").unlink()
    for _ in range(3):
        r = client.sync_once(spoke_root)
        assert r.local_deleted == ["reference/note.md"] and r.pulled == []
        assert not (spoke_root / "reference" / "note.md").exists()
    assert (hub_root / "reference" / "note.md").read_text(encoding="utf-8") == "hub v1"


def test_a_share_run_keeps_the_base_a_full_run_knew(world):
    make, hub_root, spoke_root = world
    client, _ = make()
    (hub_root / "reference" / "gone.md").write_text("hub", encoding="utf-8")
    (spoke_root / "projects").mkdir()
    (spoke_root / "projects" / "p.md").write_text("p", encoding="utf-8")
    client.sync_once(spoke_root)
    assert (spoke_root / "reference" / "gone.md").exists()
    client.sync_once(spoke_root, share=["projects"])
    (spoke_root / "reference" / "gone.md").unlink()
    r = client.sync_once(spoke_root)
    assert r.pulled == [] and r.local_deleted == ["reference/gone.md"]
    assert not (spoke_root / "reference" / "gone.md").exists()


def test_hub_allowlist_is_intersected_before_bytes_are_read(world):
    make, hub_root, spoke_root = world
    client, sent = make(kind="device", prefixes=("projects/",))
    (spoke_root / "reference" / "semi-private.md").write_text("SEMI PRIVATE", encoding="utf-8")
    (spoke_root / "projects").mkdir()
    (spoke_root / "projects" / "ok.md").write_text("ok", encoding="utf-8")
    for _ in range(2):
        r = client.sync_once(spoke_root)
        assert r.denied == [] and r.errors == []
    paths = [c["path"] for batch in sent for c in batch]
    assert paths == ["projects/ok.md"]
    assert "SEMI PRIVATE" not in json.dumps(sent)
    assert not (hub_root / "reference" / "semi-private.md").exists()


def test_share_matching_nothing_is_visible_in_the_report(world):
    make, hub_root, spoke_root = world
    client, _ = make()
    (spoke_root / "reference" / "x.md").write_text("x", encoding="utf-8")
    r = client.sync_once(spoke_root, share=["Reference/Shared"])
    assert r.local_files == 0 and r.pushed == []


def _mock_client(handler):
    from atlas_plus.sync.client import SyncClient
    return SyncClient(base_url="http://hub.invalid", token="t", transport=httpx.MockTransport(handler))


def _manifest(files: dict, principal=None) -> dict:
    out = {"generated_utc": "2026-11-10T00:00:00+00:00", "manifest_hash": "x",
           "files": {rel: {"sha256": __import__("hashlib").sha256(b).hexdigest(), "size": len(b), "mtime": 0}
                     for rel, b in files.items()}}
    if principal is not None:
        out["principal"] = principal
    return out


DEVICE = {"name": "spoke", "kind": "device", "scope": "rw", "prefixes": []}


def test_non_protocol_responses_are_per_file_errors_and_the_run_completes(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.sync import store
    root = tmp_path / "spoke"
    (root / "reference").mkdir(parents=True)
    files = {"reference/a.md": b"A", "reference/b.md": b"B", "reference/c.md": b"C"}

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/sync/v1/manifest":
            return httpx.Response(200, json=_manifest(files, DEVICE))
        if p == "/sync/v1/file":
            rel = request.url.params["path"]
            if rel == "reference/a.md":
                return httpx.Response(200, content=b"A")
            if rel == "reference/b.md":
                return httpx.Response(409, json={"detail": "file unreadable on hub"})
            return httpx.Response(403, text="<html><body>Access denied by ingress</body></html>")
        if p == "/sync/v1/checkin":
            return httpx.Response(200, json={"acknowledged": True})
        raise AssertionError(p)

    with _mock_client(handler) as client:
        r = client.sync_once(root)
    assert r.pulled == ["reference/a.md"]
    assert len(r.errors) == 2 and all("Traceback" not in e for e in r.errors)
    assert any("unreadable" in e for e in r.errors) and any("Access denied" in e for e in r.errors)
    base = store.load_base_manifest("hub")
    assert base is not None and "reference/a.md" in base["files"]
    assert "reference/b.md" not in base["files"]  # unresolved: fetched again next time


def test_push_failure_never_aborts_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.sync import store
    root = tmp_path / "spoke"
    (root / "reference").mkdir(parents=True)
    (root / "reference" / "mine.md").write_text("mine", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/sync/v1/manifest":
            return httpx.Response(200, json=_manifest({}, DEVICE))
        if p == "/sync/v1/push":
            return httpx.Response(500, text="Internal Server Error")
        if p == "/sync/v1/checkin":
            return httpx.Response(200, json={"acknowledged": True})
        raise AssertionError(p)

    with _mock_client(handler) as client:
        r = client.sync_once(root)
    assert r.pushed == [] and r.errors and "HTTP 500" in r.errors[0]
    base = store.load_base_manifest("hub")
    assert base is not None and "reference/mine.md" not in base["files"]  # pushed again next time


def test_old_hub_without_principal_is_refused_without_a_share(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.sync.client import SyncClientError
    root = tmp_path / "spoke"
    (root / "identity").mkdir(parents=True)
    (root / "identity" / "private.md").write_text("PRIVATE", encoding="utf-8")
    pushed = []

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/sync/v1/manifest":
            return httpx.Response(200, json=_manifest({}))
        if p == "/sync/v1/push":
            pushed.append(request.content)
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"acknowledged": True})

    with _mock_client(handler) as client:
        with pytest.raises(SyncClientError, match="declare what may leave"):
            client.sync_once(root)
        assert pushed == []
        r = client.sync_once(root, share=["reference"])
        assert r.errors == [] and pushed == []


@pytest.mark.parametrize("bad", ["q?.md", "a<b.md", 'x"y.md', "pipe|.md", "star*.md", "ctl\x01.md"])
def test_non_portable_names_are_refused(bad):
    from atlas_plus.sync import policy
    assert policy.is_portable_component(bad) is False
    assert policy.deny_reason(f"reference/{bad}") is not None


def test_quarantine_dedupe_survives_bracket_names_and_junk_sidecars(tmp_path):
    from atlas_plus.sync import apply
    root = tmp_path / "c"
    root.mkdir()
    first = apply.quarantine(root, "reference/notes [draft].md", b"same", reason="r")
    assert apply.quarantine(root, "reference/notes [draft].md", b"same", reason="r") == first
    # a sidecar-named file holding non-object JSON is ignored, never a crash
    (first.parent / "notes [draft]_junk.md.meta.json").write_text("[]", encoding="utf-8")
    assert apply.quarantine(root, "reference/notes [draft].md", b"same", reason="r") == first
    assert len(list(first.parent.glob("notes*.md"))) == 1
    # inbox (no day dirs) dedupes the same way
    a = apply.inbox(root, "peer", "reference/x [1].md", b"p")
    assert apply.inbox(root, "peer", "reference/x [1].md", b"p") == a


def test_hub_push_never_500s_on_a_bad_sidecar(world):
    make, hub_root, spoke_root = world
    client, _ = make()
    http = client._client
    import base64, hashlib
    day_dir = hub_root / "_atlas" / "sync-conflicts"
    (hub_root / "reference" / "x.md").write_text("hub", encoding="utf-8")
    body = b"spoke"
    change = {"path": "reference/x.md", "sha256": hashlib.sha256(body).hexdigest(),
              "base_sha256": "0" * 64, "content_b64": base64.b64encode(body).decode()}
    r = http.post("/sync/v1/push", json={"changes": [change]})
    assert r.status_code == 200 and r.json()["results"][0]["result"] == "conflict"
    junk = next(day_dir.rglob("*.meta.json"))
    junk.with_name("x_junk.md.meta.json").write_text("[]", encoding="utf-8")
    r = http.post("/sync/v1/push", json={"changes": [change]})
    assert r.status_code == 200 and r.json()["results"][0]["result"] == "conflict"


# =============================================================================
# Lane B: Brain task store and worker
# =============================================================================

@pytest.fixture
def db(tmp_path):
    return tmp_path / "state.sqlite"


def test_heartbeat_keeper_stops_when_the_lease_is_lost(db):
    from atlas_plus.brain import tasks as ts, worker
    t = ts.create_task("x", db_path=db, max_attempts=3)
    ts.claim_next_task("w-A", lease_seconds=0, db_path=db)
    b = ts.claim_next_task("w-B", db_path=db)
    assert b is not None and b.lease_owner == "w-B"
    lease_before = ts.get_task(t.task_id, db_path=db).lease_expires_at
    with worker._HeartbeatKeeper(t.task_id, db_path=db, interval=1, worker_id="w-A") as keeper:
        deadline = time.time() + 5
        while not keeper.lost.is_set() and time.time() < deadline:
            time.sleep(0.1)
    assert keeper.lost.is_set()
    row = ts.get_task(t.task_id, db_path=db)
    assert row.lease_owner == "w-B" and row.lease_expires_at == lease_before


def test_worker_fence_includes_the_attempt(db):
    from atlas_plus.brain import tasks as ts, worker, executors
    t = ts.create_task("x", db_path=db, max_attempts=3)
    first = ts.claim_next_task("same-id", lease_seconds=0, db_path=db)
    second = ts.claim_next_task("same-id", db_path=db)
    assert first.attempts == 1 and second.attempts == 2
    worker._finish_task(t.task_id, executors.ExecutionResult(status="done", text="late"),
                        db_path=db, task=first, owner="same-id")
    row = ts.get_task(t.task_id, db_path=db)
    assert row.status == "running" and row.attempts == 2 and row.result is None
    attempts = ts.list_task_attempts(t.task_id, db_path=db)
    assert [a["outcome"] for a in attempts] == ["refused-done"]
    worker._finish_task(t.task_id, executors.ExecutionResult(status="done", text="ok"),
                        db_path=db, task=second, owner="same-id")
    assert ts.get_task(t.task_id, db_path=db).status == "done"
    assert [a["outcome"] for a in ts.list_task_attempts(t.task_id, db_path=db)] == ["refused-done", "done"]


def test_any_fence_implies_a_running_row(db):
    from atlas_plus.brain import tasks as ts
    t = ts.create_task("x", db_path=db)
    ts.claim_next_task("w", db_path=db)
    assert ts.mark_done(t.task_id, "ok", db_path=db, owner="w") is True
    assert ts.mark_failed(t.task_id, "late", db_path=db, attempt=1) is False
    assert ts.get_task(t.task_id, db_path=db).status == "done"


def test_dedup_keys_on_kind_and_never_on_an_explicit_task_id(db):
    from atlas_plus.brain import tasks as ts
    a = ts.create_task("same text", kind="agent", db_path=db, dedup=True)
    v = ts.create_task("same text", kind="verify", db_path=db, dedup=True)
    assert v.task_id != a.task_id and v.deduplicated is False
    again = ts.create_task("same text", kind="agent", db_path=db, dedup=True)
    assert again.task_id == a.task_id and again.deduplicated is True
    mine = ts.create_task("same text", kind="agent", task_id="client-key-1", db_path=db, dedup=True)
    assert mine.task_id == "client-key-1" and mine.deduplicated is False


def test_submit_task_returns_503_when_the_store_is_busy(monkeypatch):
    from fastapi.testclient import TestClient
    from atlas_plus.brain import server as srv
    monkeypatch.setattr(srv, "_API_KEY", "b" * 32)
    monkeypatch.setattr(srv.task_store, "find_duplicate_pending", lambda *a, **k: None)

    def busy(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(srv.task_store, "create_task", busy)
    srv.AUTH_FAILURES.clear()
    c = TestClient(srv.app, raise_server_exceptions=False)
    r = c.post("/task", json={"instruction": "do it"}, headers={"Authorization": "Bearer " + "b" * 32})
    assert r.status_code == 503
    srv.AUTH_FAILURES.clear()


# =============================================================================
# Lane C
# =============================================================================

class _Stat:
    def __init__(self, tag, attrs=0x410):
        self.st_reparse_tag = tag
        self.st_file_attributes = attrs


@pytest.mark.parametrize("tag,expected", [
    (0xA0000003, True),   # mount point (junction)
    (0xA000000C, True),   # symlink
    (0x9000001A, False),  # OneDrive cloud placeholder
    (0x8000001B, False),  # app exec link (python3.exe shim)
    (0x80000017, False),  # WOF compression
])
def test_reparse_guard_refuses_only_name_surrogates(monkeypatch, tag, expected, tmp_path):
    from atlas_plus.mcp.memory_server import routing
    from atlas_plus.sync import policy
    monkeypatch.setattr(Path, "lstat", lambda self: _Stat(tag))
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    assert routing._is_reparse_point(tmp_path / "x") is expected
    assert policy._is_reparse_point(tmp_path / "x") is expected


def test_import_copies_are_matched_by_origin_not_by_name(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    from atlas_plus import import_cmd
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "notes [draft].md"
    counts = []
    for text in ("one", "one", "two", "two"):
        note.write_text(text, encoding="utf-8")
        import_cmd.import_obsidian(vault, "reference/obsidian", execute=True)
        counts.append(len(list((tmp_path / "mem").rglob("notes*.md"))))
    assert counts == [1, 1, 2, 2]
    # sibling vault notes a.md and a_2.md are not copies of each other
    (vault / "a.md").write_text("alpha", encoding="utf-8")
    (vault / "a_2.md").write_text("beta", encoding="utf-8")
    import_cmd.import_obsidian(vault, "reference/obsidian", execute=True)
    (vault / "a.md").write_text("beta", encoding="utf-8")  # now equals its sibling's body
    r = import_cmd.import_obsidian(vault, "reference/obsidian", execute=True)
    assert r["imported"] == 1
    bodies = {p.read_text(encoding="utf-8").rsplit("\n", 1)[-1] for p in (tmp_path / "mem").rglob("a*.md")}
    assert "beta" in bodies and len(list((tmp_path / "mem").rglob("a*.md"))) == 3


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "memory" / "identity").mkdir(parents=True)
    from fastapi.testclient import TestClient
    from atlas_plus.web import app as web_app
    monkeypatch.setattr(web_app, "API_KEY", "k" * 32)
    monkeypatch.setattr(web_app, "_ALLOW_NO_AUTH", False)
    web_app.AUTH_FAILURES.clear()
    yield TestClient(web_app.app, raise_server_exceptions=False)
    web_app.AUTH_FAILURES.clear()


def test_inbox_deep_nesting_and_non_string_content_are_400(web):
    h = {"Authorization": "Bearer " + "k" * 32, "Content-Type": "application/json"}
    assert web.post("/api/inbox/conversation", content=b"[" * 100000 + b"]" * 100000, headers=h).status_code == 400
    assert web.post("/api/inbox/conversation", content=b"{" * 100000, headers=h).status_code == 400
    body = json.dumps({"messages": [{"role": "user", "content": {"not": "text"}}]})
    assert web.post("/api/inbox/conversation", content=body, headers=h).status_code == 400


@pytest.mark.parametrize("state", [{"last_run": None}, {"last_run": 5, "errors": "x"}, [1], "hello"])
def test_backup_status_survives_odd_state(monkeypatch, state):
    from atlas_plus import backup as b
    monkeypatch.setattr(b, "_read_state", lambda: state)
    b.status()  # no exception


def test_anti_project_slug_matches_routing(tmp_path):
    from atlas_plus.mcp.memory_server.tools import anti
    root = tmp_path / "mem"
    (root / "projects").mkdir(parents=True)
    note = root / "projects" / "foo-bar.md"
    note.write_text("---\nid: foo-bar\n---\n\nAdopt the redis cache layer for sessions.\n", encoding="utf-8")
    with patch("atlas_plus.mcp.memory_server.routing.MEMORY_ROOT", root):
        res = anti.propagate_anti_rejection("adopt redis cache layer", "libs", memory_root=root,
                                            applies_to="project", project=" Foo Bar ")
    assert res["flagged"] == 1
