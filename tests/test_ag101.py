"""AG101 (sweep 26): the test suite a contributor runs must never write into that contributor's own Sextant.

README and GETTING-STARTED tell a new user to set ATLAS_WORKSPACE_ROOT / ATLAS_MEMORY_ROOT / ATLAS_STATE_DIR
(and persist them with setx or a shell profile), then run `python -m pytest tests -q` in the same shell; the
upgrade path runs the suite again after real use. tests/conftest.py used `os.environ.setdefault`, so a set
variable won: one full run wrote 254 fake events (ticket TES-001 in "testproj", brain task_running/task_done)
into the contributor's live activity feed, a contradiction report, a supervisor report, backup_state.json,
search.sqlite and a version copy full of git conflict markers into the real corpus' versions/ folder. Any other
ATLAS_* variable in the shell (a live ATLAS_EMBED_URL, ATLAS_BRAIN_URL, ATLAS_MCP_TOKEN, ATLAS_SEMANTIC_SEARCH)
also reached the suite, so the developer's machine and CI ran different suites.

These tests run real pytest sessions in a subprocess with a contributor-style environment and check the
outcome on disk: the contributor's roots stay empty and the session sees the CI environment.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _contributor_env(tmp_path: Path) -> tuple[dict, Path]:
    live = tmp_path / "my-sextant"
    for sub in (".memory", ".atlas", "projects"):
        (live / sub).mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_")}
    env.update({
        "ATLAS_WORKSPACE_ROOT": str(live),
        "ATLAS_MEMORY_ROOT": str(live / ".memory"),
        "ATLAS_STATE_DIR": str(live / ".atlas"),
        "ATLAS_PROJECTS_ROOT": str(live / "projects"),
        # what a real install also carries: a service URL and a search toggle from the user's setup
        "ATLAS_BRAIN_URL": "http://127.0.0.1:9",
        "ATLAS_SEMANTIC_SEARCH": "off",
    })
    return env, live


def _files_under(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _pytest(args: list[str], env: dict, tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--basetemp", str(tmp_path / "inner-basetemp"), *args],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=240,
    )


def test_suite_run_with_the_documented_env_leaves_the_users_sextant_untouched(tmp_path):
    """Two real tests that wrote into the contributor's tree on 91964d6: the activity feed (ticket events)
    and the session writer's version copy (a day log with conflict markers)."""
    env, live = _contributor_env(tmp_path)
    before = _files_under(live)
    res = _pytest([
        "tests/test_activity_instrumentation.py",
        "tests/test_sweep17.py::test_day_log_with_markers_is_refused_by_the_writer",
    ], env, tmp_path)
    assert res.returncode == 0, res.stdout[-2000:] + res.stderr[-2000:]
    after = _files_under(live)
    assert after == before, f"the test suite wrote into the contributor's own Sextant: {sorted(set(after) - set(before))}"


def test_suite_sees_the_ci_environment_not_the_contributors(tmp_path):
    """A probe test inside the real conftest: the four roots point at the throwaway tree and no other ATLAS_*
    value from the contributor's shell survives (CI sets none)."""
    env, live = _contributor_env(tmp_path)
    probe = REPO / "tests" / f"test_zz_ag101_probe_{os.getpid()}.py"
    probe.write_text(
        "import os\n"
        "def test_probe():\n"
        f"    live = {str(live)!r}\n"
        "    for k in ('ATLAS_WORKSPACE_ROOT', 'ATLAS_MEMORY_ROOT', 'ATLAS_STATE_DIR', 'ATLAS_PROJECTS_ROOT'):\n"
        "        assert not os.environ[k].startswith(live), (k, os.environ[k])\n"
        "    assert 'ATLAS_BRAIN_URL' not in os.environ\n"
        "    assert 'ATLAS_SEMANTIC_SEARCH' not in os.environ\n",
        encoding="utf-8",
    )
    try:
        res = _pytest([str(probe.relative_to(REPO).as_posix())], env, tmp_path)
    finally:
        probe.unlink()
    assert res.returncode == 0, res.stdout[-2000:] + res.stderr[-2000:]


def test_which_module_is_collected_first_does_not_decide_the_service_keys(tmp_path):
    """The dashboard and the Brain freeze their API key at first import; each test module set its own key with
    setdefault, so the first module collected won. A contributor running two files got a wall of 401s."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_")}
    res = _pytest(["tests/test_mn74.py", "tests/test_web_app.py",
                   "-k", "test_valid_key_passes or test_key_via_x_atlas_key_header"], env, tmp_path)
    assert res.returncode == 0 and "2 passed" in res.stdout, res.stdout[-2000:]
    res = _pytest(["tests/test_clip_inbox.py", "tests/test_brain_server.py",
                   "-k", "TestDiagnostic and test_returns_status_ok_field"], env, tmp_path)
    assert res.returncode == 0 and "1 passed" in res.stdout, res.stdout[-2000:]


def test_unit_tests_never_dial_the_contributors_ollama(tmp_path):
    """With OLLAMA_HOST naming the contributor's Ollama (here a recorder), the Brain's smart router must not
    send it chat requests from the unit tests."""
    import http.server
    import threading

    hits: list[str] = []

    class _Rec(http.server.BaseHTTPRequestHandler):
        def _any(self):
            hits.append(f"{self.command} {self.path}")
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
        do_GET = do_POST = _any

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Rec)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_")}
        env["OLLAMA_HOST"] = f"127.0.0.1:{srv.server_address[1]}"
        res = _pytest(["tests/test_brain_agents.py"], env, tmp_path)
    finally:
        srv.shutdown()
        srv.server_close()
    assert res.returncode == 0, res.stdout[-2000:]
    assert hits == [], hits


def test_a_locked_index_waits_only_as_long_as_the_configured_lock_wait(tmp_path, monkeypatch):
    """`PRAGMA busy_timeout=30000` ran after `sqlite3.connect(timeout=...)` and replaced it, so a shorter wait
    never took effect (test_w3a's 'shrink wait so test is fast' still cost 30 s per run)."""
    import sqlite3
    import time

    from atlas_plus.mcp.memory_server import fts
    monkeypatch.setattr(fts, "MEMORY_ROOT", tmp_path / "mem")
    (tmp_path / "mem").mkdir()
    db = tmp_path / "state" / "fts.sqlite"
    monkeypatch.setattr(fts, "FTS_DB", db)
    fts._connect().close()
    monkeypatch.setattr(fts, "_BUSY_TIMEOUT_S", 0.2)
    blocker = sqlite3.connect(str(db), isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        t0 = time.monotonic()
        try:
            fts.search("anything")
        except sqlite3.Error:
            pass
        elapsed = time.monotonic() - t0
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert elapsed < 5.0, elapsed
