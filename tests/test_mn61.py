"""MN61 (sweep 20 manual wave, expert review by running it): the autocommit lock gets the summarizer lock's AG67 fixes,
a no-op autocommit beside an untracked file is not a failure, a denied write lock is named as such, and a hung
`docker compose ps` leaves no child behind."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from atlas_plus.mcp.memory_server.tools import remember as rem
from atlas_plus.summarizer import renderer


def test_autocommit_lock_rechecks_before_breaking(tmp_path, monkeypatch):
    """Two waiters judged the same dead owner; the second unlinked the lock the first had just taken (12 writers: 1-3
    overlaps per round). The breaker must re-read the lock and leave a NEW owner's lock alone."""
    meta = tmp_path / ".git"
    meta.mkdir()
    lock = meta / "sextant-autocommit.lock"
    lock.write_text("99999990")  # a dead owner
    live = str(os.getppid() if renderer._pid_alive(os.getppid()) else os.getpid())
    real = renderer._lock_owner_dead
    calls = {"n": 0}

    def judged_then_retaken(p):
        calls["n"] += 1
        if calls["n"] == 1:
            lock.write_text(live)  # another waiter broke the dead lock and took it between our judgement and our unlink
            return True
        return real(p)

    monkeypatch.setattr(renderer, "_lock_owner_dead", judged_then_retaken)
    with rem._autocommit_lock(meta, timeout=0.5) as held:
        assert held is False, "the live owner's lock was broken: two autocommits ran at once"
    assert lock.read_text() == live


def test_autocommit_lock_release_survives_a_reader(tmp_path, monkeypatch):
    """A waiter reading the pid makes a Windows unlink fail; the failure was swallowed and the live server's own lock
    stayed, so every writer (the owner too) waited 30 s and reported `another writer held ...`."""
    meta = tmp_path / ".git"
    meta.mkdir()
    lock = meta / "sextant-autocommit.lock"
    real_unlink = Path.unlink
    fails = {"n": 2}

    def flaky(self, *a, **k):
        if self.name == lock.name and fails["n"]:
            fails["n"] -= 1
            raise PermissionError(13, "in use by another process")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky)
    with rem._autocommit_lock(meta, timeout=1) as held:
        assert held
    assert not lock.exists(), "the owner's lock survived its release"


def test_autocommit_noop_beside_untracked_file_is_not_a_failure(tmp_path, monkeypatch):
    """A peer's `commit -- note` already took this write; git then says `nothing added to commit but untracked files
    present`, which was reported as `not auto-committed: git said 'On branch main'` (22 of 200 writes)."""
    root = tmp_path / "mem"
    (root / "reference").mkdir(parents=True)
    note = root / "reference" / "notes.md"
    note.write_text("a fact\n", encoding="utf-8")

    def git(*a):
        subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True, stdin=subprocess.DEVNULL)

    git("init", "-q")
    git("config", "user.name", "t")
    git("config", "user.email", "t@example.org")
    git("add", "--", "reference/notes.md")
    git("commit", "-qm", "init")
    (root / "reference" / "untracked.md").write_text("mine\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_MEMORY_AUTOCOMMIT", "on")
    assert rem._git_autocommit(note, "reference:notes", root) == ""


def test_write_lock_access_denied_is_named(tmp_path, monkeypatch):
    """A read-only state dir waited the whole timeout and then blamed `another summarizer run` for a lock that did not exist."""
    monkeypatch.setattr(renderer.atlas_paths, "state_dir", lambda: tmp_path)
    real_open = os.open

    def denied(path, flags, *a):
        if str(path).endswith("write.lock"):
            raise PermissionError(13, "Access is denied", str(path))
        return real_open(path, flags, *a)

    monkeypatch.setattr(renderer.os, "open", denied)
    with pytest.raises(RuntimeError) as ei:
        with renderer._write_lock(timeout=0.3):
            pass
    msg = str(ei.value)
    assert "another summarizer run" not in msg and "denied" in msg.lower()


def test_compose_timeout_leaves_no_child(tmp_path, monkeypatch):
    """`docker compose ps` hung: the timeout killed docker.exe only, its docker-compose.exe child lived on (one per call)."""
    from atlas_plus.project_state import compose

    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    pidfile = tmp_path / "child.pid"
    code = ("import subprocess, sys, time; c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            f"open({str(pidfile)!r}, 'w').write(str(c.pid)); time.sleep(60)")
    monkeypatch.setattr(compose, "_PS_CMD", [sys.executable, "-c", code], raising=False)
    monkeypatch.setattr(compose, "_PS_TIMEOUT", 3.0, raising=False)
    t0 = time.monotonic()
    out = compose.compose_state(tmp_path)
    assert out.get("docker_unavailable") is True and time.monotonic() - t0 < 20
    assert pidfile.exists(), "the stub never ran"
    child = int(pidfile.read_text())
    for _ in range(40):
        if not renderer._pid_alive(child):
            break
        time.sleep(0.1)
    alive = renderer._pid_alive(child)
    if alive:
        subprocess.run(["taskkill", "/F", "/PID", str(child)] if os.name == "nt" else ["kill", "-9", str(child)], capture_output=True)
    assert not alive, "the hung command's child outlived the timeout"
