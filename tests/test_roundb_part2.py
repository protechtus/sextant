"""Round B part-2 re-audit fixes (2026-11-09): CSRF header + WS origin + bearer-only inbox,
one hardened git helper, identity-based destructive guard, FTS in its own file, decision
YAML, import scope on resolved parts, recovery sentinel, locked tag injection, ticket id by
file name, backup source signature and trash."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

GIT = shutil.which("git")


# ---------------- web ----------------

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
    c = TestClient(web_app.app, raise_server_exceptions=False)
    r = c.get("/?key=" + "k" * 32)
    assert r.status_code == 200
    tok = r.cookies.get("sextant_ui")
    assert tok
    return c, web_app, tok


def test_page_carries_the_csrf_header_plumbing(web):
    c, web_app, tok = web
    html = c.get("/", cookies={"sextant_ui": tok}).text
    assert "X-Sextant-Request" in html
    assert "hx-headers" in html
    assert "k" * 32 not in html


def test_cookie_post_needs_origin_and_custom_header(web):
    c, web_app, tok = web
    good = {"Origin": "http://testserver", "X-Sextant-Request": "1"}
    # no header: refused even from the right origin
    r = c.post("/api/config", json={"var": "NOPE"}, cookies={"sextant_ui": tok},
               headers={"Origin": "http://testserver"})
    assert r.status_code == 401
    # header but another port on the same host: refused
    r = c.post("/api/config", json={"var": "NOPE"}, cookies={"sextant_ui": tok},
               headers={"Origin": "http://testserver:3000", "X-Sextant-Request": "1"})
    assert r.status_code == 401
    # both: past auth (400 = allowlist, not auth)
    r = c.post("/api/config", json={"var": "NOPE"}, cookies={"sextant_ui": tok}, headers=good)
    assert r.status_code == 400
    # raw-body route with a text/plain body (the simple-request CSRF shape) is refused w/o header
    r = c.post("/api/memory/file", content=b'{"path":"identity/user.md","content":"x"}',
               headers={"Origin": "http://testserver", "Content-Type": "text/plain"},
               cookies={"sextant_ui": tok})
    assert r.status_code == 401
    # bearer still works without any of it
    r = c.get("/api/status", headers={"Authorization": "Bearer " + "k" * 32})
    assert r.status_code == 200


def test_inbox_conversation_is_bearer_only(web):
    c, web_app, tok = web
    body = {"title": "t", "source": "clip", "messages": [{"role": "user", "content": "hi"}]}
    r = c.post("/api/inbox/conversation", json=body, cookies={"sextant_ui": tok},
               headers={"Origin": "http://testserver", "X-Sextant-Request": "1"})
    assert r.status_code == 401


def test_curation_report_write_needs_post(web):
    c, web_app, tok = web
    r = c.get("/api/memory/curation-report?dry_run=false", headers={"Authorization": "Bearer " + "k" * 32})
    assert r.status_code == 405


def test_ws_chat_cookie_needs_matching_origin(web):
    c, web_app, tok = web
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/ws/chat/x", cookies={"sextant_ui": tok},
                                 headers={"Origin": "http://127.0.0.1:3000"}):
            pass
    assert exc.value.code == 4401


# ---------------- gitsafe ----------------

def _git(args, cwd, env=None):
    return subprocess.run([GIT, *args], cwd=str(cwd), capture_output=True, text=True, env=env)


@pytest.fixture
def evil_repo(tmp_path):
    if not GIT:
        pytest.skip("git not available")
    repo = tmp_path / "repo"; repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(["add", "a.txt"], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    py = "python"
    _git(["config", "core.fsmonitor", f"{py} -c \"open('fsm.marker','w')\""], repo)
    _git(["config", "filter.evil.clean",
          f"{py} -c \"import sys;open('clean.marker','w');sys.stdout.write(sys.stdin.read())\""], repo)
    _git(["config", "diff.external", f"{py} -c \"open('diff.marker','w')\""], repo)
    (repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    hooks = repo / ".git" / "hooks"; hooks.mkdir(exist_ok=True)
    hook = hooks / "post-commit"
    hook.write_text("#!/bin/sh\n: > post.marker\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    # control: can this environment make git execute config-driven commands at all?
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(["status", "--porcelain"], repo)
    _git(["add", "a.txt"], repo)
    if not ((repo / "fsm.marker").exists() or (repo / "clean.marker").exists()):
        pytest.skip("this environment does not run git config commands (no sh/python on git's PATH)")
    for m in ("fsm.marker", "clean.marker", "diff.marker", "post.marker"):
        (repo / m).unlink(missing_ok=True)
    return repo


def _markers(repo):
    return sorted(p.name for p in repo.glob("*.marker"))


def test_gitsafe_status_add_diff_commit_execute_nothing(evil_repo):
    from atlas_plus import gitsafe
    repo = evil_repo
    (repo / "a.txt").write_text("three\n", encoding="utf-8")
    assert gitsafe.run(["status", "--porcelain"], repo).returncode == 0
    assert gitsafe.run(["diff", "HEAD"], repo).returncode == 0
    assert gitsafe.run(["add", "a.txt"], repo).returncode == 0
    r = gitsafe.run(["commit", "-q", "-m", "x", "--no-verify"], repo)
    assert r.returncode == 0, r.stderr
    assert _markers(repo) == []


def test_project_state_and_hooks_and_autocommit_use_the_helper(evil_repo, monkeypatch):
    from atlas_plus.project_state import git as psgit
    from atlas_plus.hooks import session_start, pre_compact
    from atlas_plus.mcp.memory_server.tools import remember
    repo = evil_repo
    (repo / "a.txt").write_text("four\n", encoding="utf-8")
    psgit.git_state(repo)
    session_start._git_summary(repo)
    pre_compact._git_oneliner(repo)
    monkeypatch.setenv("ATLAS_MEMORY_AUTOCOMMIT", "on")
    remember._git_autocommit(repo / "a.txt", "test", memory_root=repo)
    assert _markers(repo) == []


def test_gitsafe_argv_blanks_repo_exec_keys(evil_repo):
    from atlas_plus import gitsafe
    argv = gitsafe.argv(["status"], evil_repo)
    joined = " ".join(argv)
    assert "core.fsmonitor=" in joined and "core.hooksPath=" in joined
    assert "filter.evil.clean=" in joined and "filter.evil.required=false" in joined
    assert "diff.external=" in joined


# ---------------- destructive guard identity ----------------

def test_contains_is_decided_by_identity(tmp_path):
    from atlas_plus import uninstall_cmd as u
    home = tmp_path / "home"; (home / "x").mkdir(parents=True)
    assert u._contains(tmp_path, home)
    assert u._contains(home, home / "x")
    assert not u._contains(home / "x", home)
    # a differently spelled but identical place
    spelled = Path(str(tmp_path).upper()) if sys.platform == "win32" else tmp_path
    assert u._contains(spelled, home)


def test_guard_refuses_resolved_unc(tmp_path, monkeypatch):
    from atlas_plus import uninstall_cmd as u
    monkeypatch.setattr(u, "_resolved", lambda p: Path(r"\\server\share\x\state"))
    out = u.validate_destructive_target("state", tmp_path / "state")
    assert out and "network" in out


# ---------------- FTS own file ----------------

def test_fts_db_is_not_the_brain_task_db():
    # the DEFAULT, read from source (other tests legitimately repoint the live attribute)
    import importlib, inspect
    src = inspect.getsource(importlib.import_module("atlas_plus.mcp.memory_server.config"))
    assert 'FTS_DB = STATE_DIR / "search.sqlite"' in src
    assert 'FTS_DB = STATE_DIR / "state.sqlite"' not in src


# ---------------- decision YAML ----------------

def test_decision_frontmatter_survives_colons_and_newlines(tmp_path, monkeypatch):
    import yaml
    from atlas_plus.mcp.memory_server.tools import decision
    monkeypatch.setattr(decision, "MEMORY_ROOT", tmp_path)
    monkeypatch.setattr("atlas_plus.autonomous.require_write", lambda *a, **k: None, raising=False)
    out = decision.memory_remember_decision("Use a: b", "because", project="x\nstatus: archived", source="test")
    assert out["ok"], out
    text = Path(out["path"]).read_text(encoding="utf-8")
    end = text.index("\n---", 3)
    fm = yaml.safe_load(text[3:end])
    assert fm["title"] == "Use a: b"
    assert fm["status"] == "active"
    assert "\n" not in fm["project"]
    assert fm["conditions"] == ["(none recorded)"]


# ---------------- import scope ----------------

@pytest.mark.parametrize("scope", ["versions.", "archive.", "_atlas.", "GIT~1/hooks", ".git", "projects/x.", "con/x"])
def test_import_scope_rejects_ntfs_aliases(scope, tmp_path, monkeypatch):
    from atlas_plus import import_cmd
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "memory"))
    with pytest.raises(ValueError):
        import_cmd._safe_scope(scope)
    assert import_cmd._safe_scope("projects/demo") == "projects/demo"


# ---------------- transcripts: recovery sentinel ----------------

def test_recovery_sentinel_holds_a_concurrent_append(tmp_path, monkeypatch):
    from atlas_plus.transcripts.store import TranscriptStore, TurnRecord
    from atlas_plus.transcripts import recovery
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    s = TranscriptStore(root=tmp_path / "transcripts")
    s.append(TurnRecord(conversation_id="c1", source="test", role="user", content="one"))
    path = s._handles["c1"].path
    sentinel = path.with_name(path.name + ".recovering")
    sentinel.write_text("x")
    done = {}

    def late():
        s.append(TurnRecord(conversation_id="c1", source="test", role="assistant", content="two"))
        done["t"] = time.monotonic()

    t0 = time.monotonic()
    th = threading.Thread(target=late); th.start()
    time.sleep(0.3)
    assert "t" not in done                      # the append waits while the sentinel exists
    sentinel.unlink()
    th.join(timeout=5)
    assert "t" in done and done["t"] - t0 >= 0.25
    recs = [json.loads(l) for l in path.read_bytes().splitlines()]
    assert [r["content"] for r in recs] == ["one", "two"]


def test_archive_torn_tail_refuses_when_already_recovering(tmp_path):
    from atlas_plus.transcripts import recovery
    p = tmp_path / "c.ndjson.partial"; p.write_bytes(b'{"a":1}\n{"torn')
    p.with_name(p.name + ".recovering").write_text("x")
    with pytest.raises(RuntimeError):
        recovery._archive_torn_tail(p, 8)
    assert p.read_bytes().endswith(b'{"torn')


# ---------------- locked tag injection ----------------

def test_inject_tags_keeps_a_concurrent_append_and_versions(tmp_path, monkeypatch):
    from atlas_plus.curation import tag_suggest
    from atlas_plus.mcp.memory_server import routing
    root = tmp_path / "memory"; (root / "projects").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root))
    monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
    monkeypatch.setattr(routing, "MEMORY_ROOT", root)
    import importlib
    monkeypatch.setattr(importlib.import_module("atlas_plus.mcp.memory_server.versioning"), "version_before_write",
                        lambda p, **k: (tmp_path / "versioned").write_text("yes"))
    monkeypatch.setattr("atlas_plus.autonomous.require_autonomous_writes", lambda *a, **k: None)
    note = root / "projects" / "demo.md"
    note.write_text("---\nid: demo\ntitle: Demo\n---\n\nbody\n", encoding="utf-8")
    assert tag_suggest.inject_tags(note, ["alpha", "beta"]) is True
    text = note.read_text(encoding="utf-8")
    assert "tags: [alpha, beta]" in text and text.endswith("body\n")
    assert (tmp_path / "versioned").exists()
    assert tag_suggest.inject_tags(note, ["gamma"]) is False   # idempotent


# ---------------- tickets by file name ----------------

def test_get_ticket_ignores_a_spoofed_id_in_another_file(tmp_path):
    from atlas_plus.tickets import store
    root = tmp_path / "tickets"; (root / "demo").mkdir(parents=True)
    (root / "demo" / "DEMO-001.md").write_text("---\nid: DEMO-001\ntitle: real\nproject: demo\nstatus: todo\nupdated: 2026-11-01\n---\nreal\n", encoding="utf-8")
    (root / "demo" / "DEMO-002.md").write_text("---\nid: DEMO-001\ntitle: spoof\nproject: demo\nstatus: todo\nupdated: 2026-11-09\n---\nspoof\n", encoding="utf-8")
    t = store.get_ticket("DEMO-001", tickets_root=root)
    assert t is not None and t.title == "real"


# ---------------- backup ----------------

def test_backup_refuses_a_source_without_the_install_signature(tmp_path, monkeypatch):
    from atlas_plus import backup as b
    src = tmp_path / "docs"; src.mkdir()
    for i in range(8):
        (src / f"f{i}.txt").write_text("x")
    calls = []

    monkeypatch.setattr(b, "RCLONE_REMOTE", "r")  # a named remote (no default since the persona pass)
    class R:
        returncode = 0
        stdout = f"{b.RCLONE_REMOTE}:" + chr(10)  # listremotes: the remote exists
        stderr = ""
    monkeypatch.setattr(b, "SYNC_TARGETS", [{"name": "memory corpus", "source": str(src), "dest": "r:x/memory", "exclude": []}])
    monkeypatch.setattr(b, "ARCHIVE_TARGETS", [])
    monkeypatch.setattr(b, "_rclone", lambda *a, **k: calls.append(a) or R())
    monkeypatch.setattr(b, "_log", lambda *a, **k: None)
    monkeypatch.setattr(b, "_notify", lambda *a, **k: None, raising=False)
    try:
        b.now(dry_run=True, skip_archive=True)
    except Exception:   # typer Exit on refusal
        pass
    assert not any(a and a[0] == "sync" for a in calls)
    (src / "identity").mkdir()
    try:
        b.now(dry_run=True, skip_archive=True)
    except Exception:   # typer Exit on refusal
        pass
    sync = next(a for a in calls if a and a[0] == "sync")
    assert "--backup-dir" in sync and "--max-delete" in sync
