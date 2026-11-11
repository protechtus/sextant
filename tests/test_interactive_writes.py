"""The launch-blocker fix (2026-08-26): interactive MCP writes work out of the
box on a fresh install; background writes stay behind ATLAS_AUTONOMOUS."""
from __future__ import annotations

import os

import pytest

from atlas_plus import autonomous


@pytest.fixture
def fresh_env(tmp_path, monkeypatch):
    """A stranger's machine: no ATLAS_AUTONOMOUS, isolated corpus."""
    monkeypatch.delenv("ATLAS_AUTONOMOUS", raising=False)
    monkeypatch.delenv("ATLAS_SESSION_AUTONOMOUS", raising=False)
    monkeypatch.delenv("ATLAS_INTERACTIVE_WRITES", raising=False)
    mem = tmp_path / "mem"
    (mem / "_atlas").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.mcp.memory_server import config, routing, versioning
    monkeypatch.setattr(config, "MEMORY_ROOT", mem)
    monkeypatch.setattr(routing, "MEMORY_ROOT", mem, raising=False)
    # versioning freezes MEMORY_ROOT at first import (possibly under another
    # test's tmp dir) -- patch it or version_before_write ValueErrors.
    monkeypatch.setattr(versioning, "MEMORY_ROOT", mem, raising=False)
    return mem


class TestGateSemantics:
    def test_interactive_allowed_by_default(self, fresh_env):
        autonomous.require_write("t", interactive=True)  # no raise

    def test_background_still_blocked(self, fresh_env):
        with pytest.raises(RuntimeError):
            autonomous.require_write("t", interactive=False)

    def test_interactive_kill_switch(self, fresh_env, monkeypatch):
        monkeypatch.setenv("ATLAS_INTERACTIVE_WRITES", "off")
        with pytest.raises(RuntimeError):
            autonomous.require_write("t", interactive=True)

    def test_autonomous_on_allows_background(self, fresh_env, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        autonomous.require_write("t", interactive=False)  # no raise


class TestStrangerFirstRemember:
    def test_interactive_remember_works_fresh(self, fresh_env):
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember
        result = memory_remember(
            scope="reference:staging", fact="staging server is 10.0.0.5",
            source="human-direct", interactive=True,
        )
        assert result["ok"] is True, result.get("error")
        assert (fresh_env / "reference" / "staging.md").exists()

    def test_background_remember_still_blocked_fresh(self, fresh_env):
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember
        with pytest.raises(RuntimeError):
            memory_remember(scope="reference:x", fact="background write", interactive=False)


class TestExpertPassHardening:
    """Expert-pass 2026-08-26: concurrency, reserved names, atomicity."""

    def test_concurrent_writers_lose_nothing(self, fresh_env):
        import threading
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember

        outcomes: dict[int, dict] = {}

        def writer(i):
            outcomes[i] = memory_remember(scope="reference:conc",
                                          fact=f"parallel fact {i}",
                                          interactive=True)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        errors = {i: r.get("error") for i, r in outcomes.items() if not r.get("ok")}
        assert not errors, f"writers reported errors: {errors}"
        text = (fresh_env / "reference" / "conc.md").read_text(encoding="utf-8")
        missing = [i for i in range(6) if f"parallel fact {i}" not in text]
        assert not missing, f"facts lost despite ok results: {missing}\n{text[-500:]}"

    def test_lock_acquire_retries_on_permission_error(self, fresh_env, monkeypatch):
        """Windows delete-pending: O_CREAT|O_EXCL on a lock mid-unlink raises
        EACCES, not EEXIST -- the acquire loop must treat it as busy-retry,
        not crash the writer (CI windows lane, quality loop run 22)."""
        from atlas_plus.mcp.memory_server import routing
        real_open = os.open
        denials = {"left": 3}

        def flaky_open(path, flags, *a, **kw):
            if ".lock" in str(path) and (flags & os.O_EXCL) and denials["left"] > 0:
                denials["left"] -= 1
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, flags, *a, **kw)

        monkeypatch.setattr(routing.os, "open", flaky_open)
        target = fresh_env / "reference" / "eacces.md"
        routing.append_fact(target, "fact despite transient EACCES",
                            {"id": "eacces", "title": "EACCES", "type": "reference"},
                            interactive=True)
        assert denials["left"] == 0, "the EACCES path was never exercised"
        assert "fact despite transient EACCES" in target.read_text(encoding="utf-8")

    def test_persistent_eacces_fails_fast_with_real_error(self, fresh_env, monkeypatch):
        """A genuinely unwritable lock dir must re-raise PermissionError in
        well under a second -- not stall to the 40s deadline and surface as
        RuntimeError, which escapes callers that catch OSError (expert pass
        2026-10-05, M2)."""
        import time as _t
        from atlas_plus.mcp.memory_server import routing
        real_open = os.open

        def always_denied(path, flags, *a, **kw):
            if ".lock" in str(path) and (flags & os.O_EXCL):
                raise PermissionError(13, "Permission denied", str(path))
            return real_open(path, flags, *a, **kw)

        monkeypatch.setattr(routing.os, "open", always_denied)
        target = fresh_env / "reference" / "denied.md"
        t0 = _t.monotonic()
        with pytest.raises(PermissionError):
            routing.append_fact(target, "never lands",
                                {"id": "denied", "title": "D", "type": "reference"},
                                interactive=True)
        assert _t.monotonic() - t0 < 5.0, "EACCES fail-fast took too long"

    def test_windows_reserved_names_rejected(self, fresh_env):
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember
        for bad in ("CON", "nul", "com1", "LPT9", "con.backup"):
            r = memory_remember(scope=f"reference:{bad}", fact="x", interactive=True)
            assert r["ok"] is False and "reserved" in r["error"], bad

    def test_write_is_atomic_no_tmp_left(self, fresh_env):
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember
        memory_remember(scope="reference:atomic", fact="one", interactive=True)
        memory_remember(scope="reference:atomic", fact="two", interactive=True)
        leftovers = list(fresh_env.rglob("*.w-tmp")) + list(fresh_env.rglob("*.lock"))
        assert leftovers == []
        text = (fresh_env / "reference" / "atomic.md").read_text(encoding="utf-8")
        assert "one" in text and "two" in text

    def test_stale_lock_broken(self, fresh_env, monkeypatch):
        from atlas_plus.mcp.memory_server import routing
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember
        # Crashed-writer recovery: an orphan lock must be broken after it has
        # been OBSERVED unchanged for the stale window (mtime age vs wall
        # clock is deliberately not trusted -- CI clock skew broke live locks).
        monkeypatch.setattr(routing, "_LOCK_STALE", 0.3)
        monkeypatch.setattr(routing, "_LOCK_TIMEOUT", 2.0)
        lock_dir = fresh_env / "_atlas" / ".locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        import hashlib
        target = fresh_env / "reference" / "stale.md"
        lock = lock_dir / (hashlib.sha256(str(target).lower().encode()).hexdigest()[:16] + ".lock")
        lock.write_text("", encoding="utf-8")  # orphan: no holder alive
        r = memory_remember(scope="reference:stale", fact="recovers", interactive=True)
        assert r["ok"] is True


class TestLockOwnership:
    """Expert review 2026-09-27 (CRITICAL): a slow holder whose lock was
    stale-broken must NOT release the waiter's fresh lock on exit."""

    def test_slow_holder_does_not_release_successors_lock(self, fresh_env, monkeypatch):
        # Windows blocks unlinking a file with an open fd, so a LIVE lock
        # cannot be stolen here (the exploit is POSIX-only) -- simulate the
        # post-steal state by swapping the file the instant our fd closes.
        import os as _os
        import atlas_plus.mcp.memory_server.routing as routing
        target = fresh_env / "reference" / "x.md"
        lock_dir = routing.MEMORY_ROOT / "_atlas" / ".locks"
        stolen = {}
        real_close = _os.close

        def close_then_steal(fd):
            real_close(fd)
            if stolen.get("path") and not stolen.get("done"):
                stolen["path"].unlink(missing_ok=True)
                stolen["path"].write_text("999999:someoneelsetoken", encoding="ascii")
                stolen["done"] = True

        monkeypatch.setattr(routing.os, "close", close_then_steal)
        with routing._write_lock(target):
            stolen["path"] = next(lock_dir.glob("*.lock"))
        lock_path = stolen["path"]
        # Our exit must have left the successor's lock in place.
        assert lock_path.exists(), "slow holder deleted the successor's lock"
        assert lock_path.read_text(encoding="ascii") == "999999:someoneelsetoken"
        lock_path.unlink()
