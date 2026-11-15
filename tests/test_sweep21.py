"""Sweep 21 agentic wave: functional tests that run the fixed paths."""
from __future__ import annotations

import importlib
import subprocess
import sys

import pytest


# ---------------------------------------------------------------- AG77 (docs truth audit)
@pytest.mark.parametrize("value,expected", [("ollama", "ollama"), ("anthropic", "claude"), ("claude", "claude"), ("auto", "auto"), ("openai", "ollama")])
def test_summarizer_provider_reads_the_documented_variable(monkeypatch, value, expected):
    monkeypatch.delenv("ATLAS_SUMMARIZER_PROVIDER", raising=False)
    monkeypatch.setenv("ATLAS_MODEL_SUMMARIZER_PROVIDER", value)
    from atlas_plus.summarizer import providers
    importlib.reload(providers)
    try:
        assert providers.FORCED_PROVIDER == expected
    finally:
        monkeypatch.delenv("ATLAS_MODEL_SUMMARIZER_PROVIDER", raising=False)
        importlib.reload(providers)


@pytest.mark.parametrize("value", ["no", "0", "false", "off"])
def test_default_on_switches_turn_off(monkeypatch, value):
    from atlas_plus.mcp.memory_server import embeddings
    from atlas_plus import autonomous
    monkeypatch.setenv("ATLAS_SEMANTIC_SEARCH", value)
    assert embeddings.enabled() is False
    assert value in autonomous._INTERACTIVE_OFF_VALUES


def test_a_non_number_is_refused_with_its_name():
    code = "import atlas_plus.mcp.memory_server.server"
    import os
    env = dict(os.environ, ATLAS_MCP_LOCKOUT_THRESHOLD="ten")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120, stdin=subprocess.DEVNULL)
    assert r.returncode != 0 and "ATLAS_MCP_LOCKOUT_THRESHOLD must be a whole number" in (r.stderr + r.stdout)
    assert "Traceback" not in r.stderr


def test_clock_line_day_markers_follow_the_lead_zone(monkeypatch):
    from datetime import datetime, timezone
    from atlas_plus.mcp.memory_server.tools import now_state as ns

    class _Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 15, 1, 46, tzinfo=timezone.utc)   # 18:46 in Denver on the 14th

    monkeypatch.setattr(ns, "datetime", _Fixed)
    monkeypatch.setattr(ns, "HOOK_ZONES", [("America/Denver", "Denver"), ("UTC", "UTC"), ("America/New_York", "New York"), ("Asia/Tokyo", "Tokyo")])
    line = ns.world_clock_line()
    assert "New York 20:46" in line and "New York 20:46 EST (-1d)" not in line
    assert "Tokyo 10:46 JST (+1d)" in line


def test_opt_outs_accept_on(monkeypatch):
    from unittest.mock import MagicMock, patch
    from atlas_plus.summarizer.hook_entry import main
    for v in ("ATLAS_DISABLE_AUTOSUMMARIZE", "ATLAS_DISABLE_SUGGEST_FACTS", "ATLAS_DISABLE_TRANSCRIPT_MINING"):
        monkeypatch.setenv(v, "on")                                             # `on` was ignored (AG77)
    calls = []
    with patch("atlas_plus.summarizer.hook_entry.is_session_autonomous", return_value=True),          patch("atlas_plus.summarizer.hook_entry._summarizer_unavailable_reason", return_value=""),          patch("subprocess.Popen", side_effect=lambda cmd, **k: calls.append(cmd) or MagicMock()),          patch("atlas_plus.summarizer.hook_entry._capture_fsdiffs"):
        assert main() == 0
    joined = [" ".join(str(c) for c in cmd) for cmd in calls]
    assert not any("summarizer.cli" in j or "suggest" in j or "mining" in j for j in joined), joined


from pathlib import Path  # noqa: E402


# ---------------------------------------------------------------- AG76/AG78 minors
def test_second_install_hooks_merge_keeps_the_first():
    from atlas_plus.setup_cmd import _merge
    a = {"command": "py -m atlas_plus.hooks.run --env 'ATLAS_WORKSPACE_ROOT=E:\\ws1' stop"}
    b = {"command": "py -m atlas_plus.hooks.run --env 'ATLAS_WORKSPACE_ROOT=E:\\ws2' stop"}
    user = {"command": "my-own-hook.sh"}
    assert _merge([a, user], [b]) == [a, user, b]                               # two workspaces, two installs
    a2 = {"command": "py2 -m atlas_plus.hooks.run --env 'ATLAS_WORKSPACE_ROOT=E:\\ws1' stop"}
    assert _merge([a, user], [a2]) == [user, a2]                                # the same workspace is replaced


def test_doctor_names_cloud_drive_conflict_copies(tmp_path):
    from unittest.mock import patch
    from atlas_plus.doctor.cli import check_memory_corpus
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "orca.md").write_text("---\ntitle: orca\n---\nfact\n", encoding="utf-8")
    (tmp_path / "projects" / "orca (1).md").write_text("---\ntitle: orca\n---\nfact\n", encoding="utf-8")
    with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
        result = check_memory_corpus()
    assert result.get("conflict_copies") == ["projects/orca (1).md"] and "keep one" in result["conflict_copies_hint"]


def test_duplicate_scan_stops_at_its_budget(tmp_path, monkeypatch):
    from atlas_plus.curation import duplicates as d
    for i in range(300):
        (tmp_path / f"n{i}.md").write_text("alpha beta gamma delta epsilon zeta eta theta iota kappa " + f"w{i % 7}", encoding="utf-8")
    import itertools, time as _t
    _ticks = itertools.count()
    monkeypatch.setattr(_t, "monotonic", lambda: next(_ticks) * 1.0)  # a fake clock: Windows ticks every ~15 ms, so a 1 us budget never ran out on CI
    monkeypatch.setenv("ATLAS_DUPLICATES_BUDGET_SECONDS", "0.000001")
    d.find_duplicates(memory_root=tmp_path)
    assert d._LAST_PARTIAL[1] == 300 and d._LAST_PARTIAL[0] < 300           # stopped early and said where
    monkeypatch.undo()
    monkeypatch.setenv("ATLAS_DUPLICATES_BUDGET_SECONDS", "0")
    pairs = d.find_duplicates(memory_root=tmp_path)
    assert d._LAST_PARTIAL == [0, 0] and pairs                                  # no limit: the full scan


# ---------------------------------------------------------------- AG74 (crash durability)
def test_crash_leftovers_never_sync_and_doctor_names_them(tmp_path):
    from unittest.mock import patch as _patch
    from atlas_plus.sync.policy import deny_reason
    for rel in ("reference/notes.md.1234-5678.w-tmp", "reference/x.md.99.imp-tmp", "sessions/.2026-11-14.md.1234.tmp"):
        assert deny_reason(rel) == "temporary file"
    assert deny_reason("reference/notes.md") is None
    from atlas_plus.doctor.cli import check_memory_corpus
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "notes.md").write_text("---\ntitle: n\n---\nfact\n", encoding="utf-8")
    (tmp_path / "reference" / "notes.md.1-2.w-tmp").write_text("copy", encoding="utf-8")
    with _patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
        r = check_memory_corpus()
    assert r.get("crash_leftovers") == ["reference/notes.md.1-2.w-tmp"]


def test_empty_lock_token_is_a_dead_writer_after_two_seconds(tmp_path):
    import os, time
    from atlas_plus.mcp.memory_server.routing import _lock_holder_dead
    lock = tmp_path / "x.lock"
    lock.write_text("", encoding="ascii")
    assert _lock_holder_dead(lock) is False                                     # a writer may be about to fill it
    old = time.time() - 5
    os.utime(lock, (old, old))
    assert _lock_holder_dead(lock) is True


def test_init_repairs_a_zero_byte_scaffold_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus import init_cmd
    init_cmd.run_init()
    user = tmp_path / "mem" / "identity" / "user.md"
    user.write_text("", encoding="utf-8")
    init_cmd.run_init()
    assert user.stat().st_size > 0


def test_import_scope_accepts_remember_grammar():
    from atlas_plus import import_cmd
    assert import_cmd._safe_scope("reference:vault2").replace("\\", "/").endswith("reference/vault2")


# ---------------------------------------------------------------- MN71 (two devices for a week)
def _hub_and_spoke(tmp_path, monkeypatch):
    hub, spoke = tmp_path / "hub", tmp_path / "spoke"
    (hub / "reference").mkdir(parents=True)
    (spoke / "reference").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    _, tok = devices.pair("laptop")
    return hub, spoke, SyncClient(token=tok, http_client=TestClient(api.create_app(), raise_server_exceptions=False))


def test_a_restored_hub_does_not_roll_a_device_back(tmp_path, monkeypatch):
    import shutil
    from atlas_plus.sync import store
    hub, spoke, c = _hub_and_spoke(tmp_path, monkeypatch)
    (spoke / "reference" / "a.md").write_text("v1", encoding="utf-8")
    c.sync_once(spoke)
    backup = tmp_path / "backup"
    shutil.copytree(hub, backup / "hub")
    shutil.copytree(store.sync_dir(), backup / "sync")                 # the state the docs say to restore
    (spoke / "reference" / "a.md").write_text("v2 newer", encoding="utf-8")
    assert c.sync_once(spoke).pushed == ["reference/a.md"]
    shutil.rmtree(hub)
    shutil.copytree(backup / "hub", hub)                                 # restore yesterday's hub, pairings included
    for name in ("generation", "devices.json"):  # the hub's own state; in this test the device shares the folder, so its base stays
        shutil.copy2(backup / "sync" / name, store.sync_dir() / name)
    r = c.sync_once(spoke)
    assert (spoke / "reference" / "a.md").read_text(encoding="utf-8") == "v2 newer"   # never pulled over
    assert not r.pulled and any("restored from a backup" in e for e in r.errors)


def test_more_than_500_changes_push_in_batches(tmp_path, monkeypatch):
    hub, spoke, c = _hub_and_spoke(tmp_path, monkeypatch)
    for i in range(620):
        (spoke / "reference" / f"n{i}.md").write_text(f"note {i}", encoding="utf-8")
    r = c.sync_once(spoke)
    assert len(r.pushed) == 620 and not r.errors
    assert len(list((hub / "reference").glob("n*.md"))) == 620


# ---------------------------------------------------------------- MN72 (newcomer and upgrade)
def test_mn72_upper_md_is_indexed(tmp_path, monkeypatch):
    from atlas_plus.mcp.memory_server import fts
    mem = tmp_path / "mem"
    (mem / "reference").mkdir(parents=True)
    (mem / "reference" / "UPPER.MD").write_text("the axolotl lives here", encoding="utf-8")
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "fts.sqlite")
    fts.rebuild(force=True)
    assert any(r["path"].endswith("UPPER.MD") for r in fts.search("axolotl"))


def test_mn72_reset_state_right_after_init_is_nothing_to_clear(tmp_path, monkeypatch):
    from atlas_plus import uninstall_cmd as u
    state = tmp_path / "state"
    state.mkdir()
    (state / u.STATE_MARKER).write_text("", encoding="utf-8")
    monkeypatch.setattr(u.atlas_paths, "state_dir", lambda: state)
    monkeypatch.setattr(u, "validate_destructive_target", lambda *a, **k: "")
    assert u.derived_state_refusal() is None
    assert "nothing to clear" in u.clear_derived_state(yes=True)


def test_mn72_double_question_marks_in_a_pipe_are_refused():
    from atlas_plus.cli import _stdin_looks_mangled
    assert _stdin_looks_mangled("Gr??e")
    assert _stdin_looks_mangled("the staging server lives in ?? now")
    assert not _stdin_looks_mangled("is it done? yes")


# ---------------------------------------------------------------- sweep 21 manual open items
def test_doctor_backup_row_reads_the_real_log(tmp_path):
    from unittest.mock import patch as _patch
    from atlas_plus.doctor import cli as dc
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "backup.log").write_text("2026-11-14 start\nBackup complete in 1.1s\n", encoding="utf-8")
    with _patch.object(dc, "STATE_DIR", tmp_path):
        r = dc.check_backup()
    assert r["result"] == "ok" and "Backup complete" in r["last_run"]


def test_fact_duplicate_scan_honours_the_budget(tmp_path, monkeypatch):
    from atlas_plus.curation import duplicates as d
    for i in range(200):
        (tmp_path / f"n{i}.md").write_text("\n".join(f"- the staging server alpha beta gamma delta number {i} {k}" for k in range(5)), encoding="utf-8")
    import itertools, time as _t
    _ticks = itertools.count()
    monkeypatch.setattr(_t, "monotonic", lambda: next(_ticks) * 1.0)  # a fake clock: Windows ticks every ~15 ms, so a 1 us budget never ran out on CI
    monkeypatch.setenv("ATLAS_DUPLICATES_BUDGET_SECONDS", "0.000001")
    d.find_fact_duplicates(memory_root=tmp_path)
    assert d._LAST_PARTIAL[1] > 0 and d._LAST_PARTIAL[0] < d._LAST_PARTIAL[1]


def test_hook_workspace_parses_posix_and_windows_shapes():
    from atlas_plus.setup_cmd import _hook_workspace as h, _merge
    run = "python -m atlas_plus.hooks.run --env "
    assert h({"command": run + "ATLAS_WORKSPACE_ROOT=/tmp/x/ws stop"}) == "/tmp/x/ws"
    assert h({"command": run + "ATLAS_WORKSPACE_ROOT=/tmp/x/ws --env ATLAS_SESSION_AUTONOMOUS=on stop"}) == "/tmp/x/ws"
    assert h({"command": run + "'ATLAS_WORKSPACE_ROOT=E:\\my ws' stop"}) == "e:\\my ws"
    assert h({"command": run + '"ATLAS_WORKSPACE_ROOT=E:\\my ws" stop'}) == "e:\\my ws"
    old = {"command": run + "ATLAS_WORKSPACE_ROOT=/tmp/x/ws --env ATLAS_SESSION_AUTONOMOUS=on stop"}
    new = {"command": run + "ATLAS_WORKSPACE_ROOT=/tmp/x/ws stop"}
    assert _merge([old], [new]) == [new]          # the same POSIX workspace is replaced, not duplicated (CI on Linux/macOS)

