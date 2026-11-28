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
# The suite runs in the CI environment whatever the contributor's shell holds (AG101, sweep 26): README tells a
# new user to set (and setx) the ATLAS_* roots, then run pytest in the same shell, and `setdefault` let those win,
# so a run wrote ticket events, reports and version copies into the contributor's own Sextant; a set
# ATLAS_WEB_API_KEY failed 182 dashboard tests. Every ATLAS_* variable is dropped, the roots are forced.
# SEXTANT_* too: the package adopts SEXTANT_<NAME> as ATLAS_<NAME> at import (SEX-043, AG132), so a leaked new
# spelling would refill what this loop just cleared, in this process and in every child a test starts.
for _var in [_k for _k in _os.environ if _k.upper().startswith(("ATLAS_", "SEXTANT_"))]:
    del _os.environ[_var]
for _var, _sub in (("ATLAS_WORKSPACE_ROOT", ""), ("ATLAS_MEMORY_ROOT", ".memory"),
                   ("ATLAS_STATE_DIR", ".atlas"), ("ATLAS_PROJECTS_ROOT", "projects")):
    _os.environ[_var] = str(_SUITE_ISOLATION_ROOT / _sub) if _sub else str(_SUITE_ISOLATION_ROOT)
# One key per service for the whole session: the dashboard and the Brain freeze their key when first imported, and
# whichever test module was collected first used to decide it (`pytest tests/test_mn74.py tests/test_web_app.py`:
# 182 failed; `pytest tests/test_clip_inbox.py tests/test_brain_server.py`: 67 failed). Modules read these back.
_os.environ["ATLAS_WEB_API_KEY"] = "test-key-abc"
_os.environ["ATLAS_BRAIN_API_KEY"] = "brain-key"
# Unit tests never reach the contributor's own Ollama: the Brain's smart router POSTed /api/chat (qwen3:1.7b) to
# localhost:11434 from tests/test_brain_agents.py (7 s per test on a machine whose Ollama answers). Every Ollama
# reader goes through atlas_plus.llm.ollama_url, which takes this first; the embedder has its own variable.
# tests/integration (marked `ollama`) dial their own URL on purpose.
_os.environ["ATLAS_BRAIN_OLLAMA_URL"] = "http://127.0.0.1:9"
_os.environ["ATLAS_EMBED_URL"] = "http://127.0.0.1:9"
# The suite reads and writes UTF-8 whatever terminal it runs in. In a real Windows console (code page 437 or 1252)
# the CLI entry points re-encode stdout to the console's code page, which is right for a pipe a shell decodes and
# wrong for pytest's capture buffer, which pytest always decodes as UTF-8: the first test that printed a non-ASCII
# path died in capsys and every test after it errored at setup and teardown (operator's laptop, 2026-11-21:
# 1 failed, 352 passed, 12,133 errors). CI has no console and agent shells export this variable, so neither saw it.
_os.environ["PYTHONIOENCODING"] = "utf-8"
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
    if mod is not None and hasattr(mod, "_TTL_CACHE"):
        mod._TTL_CACHE.clear()  # the services/convergence TTL cache (sweep 17)
    if mod is not None and hasattr(mod, "_PeerClient"):
        mod._PeerClient._down.clear()  # the dead-peer breaker (sweep 17) must not carry a dead port from one test to the next
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


_AGENT_PROGRAMS = frozenset({"claude", "codex", "opencode"})


@pytest.fixture(autouse=True)
def _no_real_agent_program(monkeypatch):
    """No test finds a real agent program on PATH (LES-087, guardrail G17). A supervisor test that
    did not pin ``shutil.which`` started the real ``claude`` with a prompt on every machine that
    has it installed (KL4, sweep 32). A test that needs the program present patches
    ``shutil.which`` itself, or passes a fake program it wrote."""
    import shutil as _shutil

    real = _shutil.which

    def which(cmd, *args, **kwargs):
        name = _os.path.basename(str(cmd)).lower()
        if name.rsplit(".", 1)[0] in _AGENT_PROGRAMS or name in _AGENT_PROGRAMS:
            return None
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(_shutil, "which", which)
    yield
