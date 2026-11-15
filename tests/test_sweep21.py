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
    monkeypatch.setenv("ATLAS_DUPLICATES_BUDGET_SECONDS", "0.000001")
    d.find_duplicates(memory_root=tmp_path)
    assert d._LAST_PARTIAL[1] == 300 and d._LAST_PARTIAL[0] < 300           # stopped early and said where
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

