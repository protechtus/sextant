"""Shared pytest fixtures for atlas-memory+.

Autouse: the production default for ATLAS_AUTONOMOUS is fail-closed -- "off" when
unset (see src/atlas_plus/autonomous.py, hardened under ATL-013 on 2026-05-28).
The test suite exercises memory-write tools (remember / decision / anti / tickets)
that call require_autonomous_writes(); those must be permitted to write during
tests. This fixture opts the whole suite into autonomous writes.

Tests that specifically verify the kill switch (tests/test_autonomous.py) override
or clear ATLAS_AUTONOMOUS within their own patch.dict context, so this fixture does
not mask them.
"""
from __future__ import annotations

import pytest



# --- Suite isolation (data-safety audit 2026-11-09) -------------------------------------
# Several tests resolved ATLAS_STATE_DIR / ATLAS_MEMORY_ROOT at import time and, when the
# environment left them unset, wrote reports and sqlite files into the DEVELOPER'S live state
# dir. Point every default at a throwaway tree before any atlas_plus module is imported.
import os as _os
import tempfile as _tempfile
from pathlib import Path as _Path

_SUITE_ISOLATION_ROOT = _Path(_tempfile.mkdtemp(prefix="sextant-tests-"))
for _var, _sub in (("ATLAS_WORKSPACE_ROOT", ""), ("ATLAS_MEMORY_ROOT", ".memory"),
                   ("ATLAS_STATE_DIR", ".atlas"), ("ATLAS_PROJECTS_ROOT", "projects")):
    _os.environ.setdefault(_var, str(_SUITE_ISOLATION_ROOT / _sub) if _sub else str(_SUITE_ISOLATION_ROOT))
(_SUITE_ISOLATION_ROOT / ".memory").mkdir(parents=True, exist_ok=True)
(_SUITE_ISOLATION_ROOT / ".atlas").mkdir(parents=True, exist_ok=True)
(_SUITE_ISOLATION_ROOT / ".atlas" / ".sextant-state").write_text("test suite\n", encoding="utf-8")
# ----------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_web_panel_cache(monkeypatch):
    """The dashboard caches slow panels on the corpus signature (sweep 15 harden pass); tests
    swap roots and monkeypatch the finders mid-test, so the cache is off and cleared per test."""
    import sys as _sys
    monkeypatch.setenv("ATLAS_WEB_PANEL_CACHE", "off")
    monkeypatch.setenv("ATLAS_WEB_REPORT_SUBPROCESS", "off")  # tests monkeypatch module roots a worker process cannot see
    mod = _sys.modules.get("atlas_plus.web.app")
    if mod is not None and hasattr(mod, "_PANEL_CACHE"):
        mod._PANEL_CACHE.clear()
    yield


@pytest.fixture(autouse=True)
def _enable_autonomous_writes(monkeypatch):
    monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")


@pytest.fixture(autouse=True)
def _reset_auth_failure_log():
    """Clear the Web UI's shared auth-failure log before each test.

    The log is a module singleton (atlas_plus.web.app.AUTH_FAILURES); without a
    reset, failures from one test accumulate and could trip the 429 throttle in
    another. Lazy via sys.modules so non-web tests pay nothing and the web module
    is never imported just for this.
    """
    import sys

    mod = sys.modules.get("atlas_plus.web.app")
    if mod is not None and hasattr(mod, "AUTH_FAILURES"):
        mod.AUTH_FAILURES.clear()
    yield


@pytest.fixture(autouse=True)
def _isolated_tickets_root(tmp_path, monkeypatch):
    """Tests must never touch the LIVE ticket store.

    TICKETS_ROOT is a module constant bound at import from the memory root,
    so a per-test ATLAS_MEMORY_ROOT cannot redirect it -- the same def-time
    binding class as the 2026-10-12 state.sqlite bug. Every store function
    resolves `tickets_root or TICKETS_ROOT` at call time, so pointing the
    module attribute at a per-test directory isolates every default-rooted
    read and write (found by the P08 contributor persona 2026-10-16: a bare
    `pytest` on a contributor's machine created ticket dirs in their real
    ~/.memory). This replaces the old after-the-fact cleanup fixture, which
    only covered three project slugs. A test that needs a specific root
    passes tickets_root= explicitly or monkeypatches TICKETS_ROOT itself.
    """
    try:
        from atlas_plus.tickets import store as _tickets_store
    except Exception:
        yield
        return
    monkeypatch.setattr(
        _tickets_store, "TICKETS_ROOT", tmp_path / "tickets-isolated", raising=False
    )
    yield


@pytest.fixture(autouse=True)
def _isolated_activity_feed(tmp_path, monkeypatch):
    """Tests must never pulse the LIVE activity feed.

    activity.record() resolves its file at call time from ATLAS_STATE_DIR,
    so any test that creates tickets / finishes tasks WITHOUT redirecting
    that env writes events onto the operator's real Map ticker (found
    2026-10-12: suite runs left ticket_created events on the live feed).
    When a test sets ATLAS_STATE_DIR the real resolution logic runs
    unchanged; when it does not, events land in a per-test file instead
    of the live one.
    """
    import os as _os
    from atlas_plus import activity as _activity

    real_path = _activity.activity_path

    def _test_path():
        if _os.environ.get("ATLAS_STATE_DIR"):
            return real_path()
        return tmp_path / "activity-isolated" / "activity.ndjson"

    monkeypatch.setattr(_activity, "activity_path", _test_path)


@pytest.fixture(autouse=True)
def _isolated_state_db(tmp_path, monkeypatch):
    """Tests must never touch the LIVE state.sqlite.

    Found 2026-10-12: tests that hit the real task store through module
    defaults (test_web_app's verify-badge test, test_brain_server's /task
    submit) were planting REAL tasks ("test instruction", "Verify ticket
    TES-001") in the live queue -- every suite run bred them, the live
    worker executed them, and the verify kind re-ran the suite inside the
    worker's env, breeding the next generation (61 failed verify tasks in
    14 days). The brain state modules now resolve _DB_PATH at call time,
    so pointing each module's _DB_PATH at a per-test file makes the suite
    physically unable to write operator state. Unconditional on purpose:
    unlike the activity fixture above there is no ATLAS_STATE_DIR
    passthrough, because the Brain verify runner is exactly the caller
    that has that env set. A test that needs a specific DB passes
    db_path= explicitly or monkeypatches _DB_PATH itself.
    """
    import importlib

    db = tmp_path / "state-isolated" / "state.sqlite"
    for mod_name in ("tasks", "history", "improvement", "task_facts"):
        try:
            mod = importlib.import_module(f"atlas_plus.brain.{mod_name}")
        except Exception:
            continue
        monkeypatch.setattr(mod, "_DB_PATH", db, raising=False)


@pytest.fixture(autouse=True)
def _void_fts_freshness_window():
    """The FTS freshness debounce (quality loop run 27) is module-level
    state; a window armed by one test must never hide another test's fresh
    tmp corpus. Real usage: each corpus world is its own process."""
    try:
        from atlas_plus.mcp.memory_server import fts as _fts
        _fts.mark_dirty()
    except Exception:
        pass
    # Same hazard for the vector cache (run 31): module state keyed by a DB
    # stat must never leak one test's vectors into another's fresh world.
    try:
        from atlas_plus.mcp.memory_server import embeddings as _emb
        _emb._vec_cache = None
    except Exception:
        pass
    yield
