"""AG67 (sweep 20 expert review): functional regressions, no source pins."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------- remove_session_entry vs a fenced quotation
def test_refresh_with_a_fenced_session_heading_leaves_no_orphan(tmp_path, monkeypatch):
    from atlas_plus.summarizer import renderer

    sessions = tmp_path / "sessions"
    monkeypatch.setattr(renderer, "MEMORY_SESSIONS_DIR", sessions)
    monkeypatch.setattr(renderer, "VERSIONS_DIR", tmp_path / "versions")
    monkeypatch.setattr(renderer, "UPDATES_TXT", tmp_path / "updates.txt")
    monkeypatch.setattr(renderer.atlas_paths, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")

    def entry(sid, turns, body):
        return f"## 10:{turns:02d} {sid}\n<!-- session:{sid} turns:{turns} -->\n{body}\n<!-- end session:{sid} -->\n"

    def write(sid, turns, body):
        renderer.write_outputs(entry(sid, turns, body), "x\n", "2026-02-02",
                               session_marker=f"<!-- session:{sid} turns:{turns}", replace_session=f"<!-- session:{sid}")

    write("other", 3, "other body")
    write("abc", 4, "format example:\n```\n## 09:00 Old\n<!-- session:zzz turns:1 -->\n```\nend of body")
    write("abc", 8, "refreshed abc body")
    text = (sessions / "2026-02-02.md").read_text(encoding="utf-8")
    assert text.count("<!-- end session:abc -->") == 1 and "```" not in text and "end of body" not in text
    write("other", 9, "other refreshed")
    text = (sessions / "2026-02-02.md").read_text(encoding="utf-8")
    assert text.count("<!-- session:other ") == 1 and "other body" not in text


def test_blank_line_fold_spares_fenced_blocks(tmp_path, monkeypatch):
    from atlas_plus.summarizer import renderer

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(renderer, "MEMORY_SESSIONS_DIR", sessions)
    monkeypatch.setattr(renderer, "VERSIONS_DIR", tmp_path / "versions")
    monkeypatch.setattr(renderer, "UPDATES_TXT", tmp_path / "updates.txt")
    monkeypatch.setattr(renderer.atlas_paths, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    day = sessions / "2026-01-01.md"
    day.write_text("## Mine\n\n```text\nline 1\n\n\n\nline 5\n```\n\n\n\nafter\n", encoding="utf-8")
    renderer.write_outputs("## New\n<!-- session:n turns:2 -->\nx\n<!-- end session:n -->\n", "u\n", "2026-01-01",
                           session_marker="<!-- session:n", replace_session="<!-- session:n")
    out = day.read_text(encoding="utf-8")
    assert "line 1\n\n\n\nline 5" in out          # inside the fence: kept byte for byte
    assert "```\n\nafter" in out                  # outside: folded to one blank line


# ---------------------------------------------------------------- write.lock
def test_write_lock_never_yields_unlocked_on_a_non_file_lock(tmp_path, monkeypatch):
    from atlas_plus.summarizer import renderer

    monkeypatch.setattr(renderer.atlas_paths, "state_dir", lambda: tmp_path)
    lock = tmp_path / "summarizer" / "write.lock"
    lock.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="directory"):
        with renderer._write_lock(timeout=0.5):
            pass


def test_write_lock_retries_a_delete_pending_permission_error(tmp_path, monkeypatch):
    from atlas_plus.summarizer import renderer

    monkeypatch.setattr(renderer.atlas_paths, "state_dir", lambda: tmp_path)
    real_open, calls = os.open, []

    def flaky(path, flags, *a):
        if str(path).endswith("write.lock") and flags & os.O_EXCL and len(calls) < 3:
            calls.append(1)
            raise PermissionError(13, "delete pending")
        return real_open(path, flags, *a)

    monkeypatch.setattr(renderer.os, "open", flaky)
    lock = tmp_path / "summarizer" / "write.lock"
    with renderer._write_lock(timeout=5.0):
        assert lock.read_text(encoding="ascii") == str(os.getpid())   # held for real, not waved through
    assert len(calls) == 3 and not lock.exists()


def test_release_survives_a_reader_holding_the_lock_open(tmp_path, monkeypatch):
    from atlas_plus.summarizer import renderer

    monkeypatch.setattr(renderer.atlas_paths, "state_dir", lambda: tmp_path)
    lock = tmp_path / "summarizer" / "write.lock"
    real_unlink, n = Path.unlink, []

    def busy(self, *a, **k):
        if self == lock and len(n) < 2:
            n.append(1)
            raise PermissionError(13, "in use by a reader")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", busy)
    with renderer._write_lock(timeout=2.0):
        pass
    assert n and not lock.exists()      # the old release swallowed the first PermissionError and left the lock


def test_empty_lock_is_not_released_by_the_previous_holder(tmp_path, monkeypatch):
    from atlas_plus.summarizer import renderer

    monkeypatch.setattr(renderer.atlas_paths, "state_dir", lambda: tmp_path)
    lock = tmp_path / "summarizer" / "write.lock"
    with renderer._write_lock(timeout=2.0):
        lock.write_text("", encoding="ascii")    # our lock was broken and a new owner created its file, pid not yet written
    assert lock.exists()


_RACE_WORKER = textwrap.dedent("""
    import json, os, sys, time
    from pathlib import Path
    from atlas_plus.summarizer import renderer
    state = Path(sys.argv[1]); out = Path(sys.argv[2]); go = state / "go"
    renderer.atlas_paths.state_dir = lambda: state
    lock = state / "summarizer" / "write.lock"
    while not go.exists():
        time.sleep(0.001)
    with renderer._write_lock(timeout=30.0):
        t0 = time.time()
        try:
            own = lock.read_text().strip() == str(os.getpid())
        except OSError:
            own = False
        time.sleep(0.003)
        t1 = time.time()
    with open(out, "a") as fh:
        fh.write(json.dumps([t0, t1, own]) + "\\n")
""")


@pytest.mark.parametrize("dead_owner", [False, True])
def test_write_lock_is_exclusive_across_processes(tmp_path, dead_owner):
    worker = tmp_path / "w.py"
    worker.write_text(_RACE_WORKER, encoding="utf-8")
    bad = 0
    for r in range(3):
        state = tmp_path / f"s{r}"
        (state / "summarizer").mkdir(parents=True)
        if dead_owner:
            (state / "summarizer" / "write.lock").write_text("4000000", encoding="ascii")
        out = state / "out.jsonl"
        procs = [subprocess.Popen([sys.executable, str(worker), str(state), str(out)]) for _ in range(10)]
        time.sleep(2.0)
        (state / "go").write_text("go")
        for p in procs:
            assert p.wait(timeout=90) == 0
        rows = sorted(json.loads(x) for x in out.read_text().splitlines())
        assert len(rows) == 10
        bad += sum(1 for a, b in zip(rows, rows[1:]) if b[0] < a[1] - 0.001) + sum(1 for x in rows if not x[2])
    assert bad == 0     # before AG67: overlapping holders in most rounds on Windows


# ---------------------------------------------------------------- per-session lock
def test_session_lock_release_spares_another_owner(tmp_path, monkeypatch):
    from atlas_plus.summarizer import cli as scli
    import atexit

    from atlas_plus import paths as atlas_paths
    monkeypatch.setattr(atlas_paths, "state_dir", lambda: tmp_path)
    registered = []
    monkeypatch.setattr(atexit, "register", lambda f, *a, **k: registered.append(f))
    assert scli._acquire_session_lock("sess1")
    lock = tmp_path / "summarizer" / "s-sess1.lock"
    lock.write_text("999999", encoding="ascii")     # another run took it over
    registered[-1]()
    assert lock.exists() and lock.read_text(encoding="ascii") == "999999"
    lock.write_text(str(os.getpid()), encoding="ascii")
    registered[-1]()
    assert not lock.exists()


# ---------------------------------------------------------------- docker compose ps timeout
def test_compose_timeout_is_bounded_when_a_grandchild_holds_stdout(tmp_path, monkeypatch):
    from atlas_plus.project_state import compose

    (tmp_path / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    grandchild = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(8)']); time.sleep(8)"
    monkeypatch.setattr(compose, "_PS_CMD", [sys.executable, "-c", grandchild])  # MN61: Popen + a tree kill, not subprocess.run
    monkeypatch.setattr(compose, "_PS_TIMEOUT", 1.5)
    t0 = time.monotonic()
    out = compose.compose_state(tmp_path)
    assert time.monotonic() - t0 < 10.0 and out["has_compose"]  # before AG67: waited for the grandchild's 30 s pipe


# ---------------------------------------------------------------- ambiguous bare ticket ids
def _two_folders(root: Path):
    for proj in ("alpha", "beta"):
        (root / proj).mkdir(parents=True, exist_ok=True)
        (root / proj / "ATL-001.md").write_text(
            f"---\nid: ATL-001\ntitle: {proj}\nproject: {proj}\nstatus: todo\nassignee: human\npriority: normal\n"
            "effort: M\nupdated: 2026-11-01\n---\nbody\n", encoding="utf-8")


def test_ambiguous_id_refusals_name_the_choices(tmp_path, monkeypatch):
    from atlas_plus.tickets import store

    _two_folders(tmp_path)
    for call in (lambda: store.update_ticket_status("ATL-001", "done", tmp_path),
                 lambda: store.update_ticket_fields("ATL-001", {"priority": "high"}, tmp_path)):
        with pytest.raises(FileNotFoundError, match="alpha/ATL-001, beta/ATL-001"):
            call()
    assert all("status: todo" in p.read_text() for p in tmp_path.rglob("ATL-001.md"))


def test_ambiguity_record_is_per_thread(tmp_path):
    from atlas_plus.tickets import store

    _two_folders(tmp_path)
    assert store.get_ticket("ATL-001", tmp_path) is None
    seen = []
    t = threading.Thread(target=lambda: (store.get_ticket("ATL-999", tmp_path), seen.append(store.ambiguous_hint())))
    t.start(); t.join()
    assert seen == [""] and "beta/ATL-001" in store.ambiguous_hint()   # the other request's miss no longer clears ours
