"""AG115 (sweep 33, ticket SEX-052 items 3, 4, 5, 8). Each test runs the fixed path.

- Item 3: `sextant verify` printed raw JSON step lines by default; now one plain line per step, the
  raw lines under `--json-lines`, the `{ok, steps}` object under `--json`, exit codes unchanged.
- Item 4: `sextant components` offered `atlas-fts-rebuild` (an alias a public install never shows);
  the MCP search and the dashboard named `sextant fts-rebuild`, which is no command. `python -m
  atlas_plus.mcp.memory_server.fts` printed "Indexed N files" while the console script printed
  "FTS index rebuilt: N files": one line for both.
- Item 5: `sextant init` said "Edit identity/user.md" relative to a corpus the user may not be in.
- Item 8: the MCP remember tools did not say the scope was stored lowercased.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    for sub in ("reference", "projects", "feedback", "identity"):
        (root / sub).mkdir(parents=True)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SEMANTIC_SEARCH", "off")
    import importlib
    config = importlib.import_module("atlas_plus.mcp.memory_server.config")
    routing = importlib.import_module("atlas_plus.mcp.memory_server.routing")
    fts = importlib.import_module("atlas_plus.mcp.memory_server.fts")
    monkeypatch.setattr(config, "MEMORY_ROOT", root)
    monkeypatch.setattr(routing, "MEMORY_ROOT", root)
    monkeypatch.setattr(routing, "ROUTING_CONFIG", root / "_atlas" / "routing.yaml")
    monkeypatch.setattr(fts, "MEMORY_ROOT", root)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "fts.sqlite")
    (tmp_path / "state").mkdir(exist_ok=True)
    return root


def test_verify_default_prints_plain_step_lines_and_json_lines_keeps_the_old_shape(monkeypatch, capsys):
    import json

    from atlas_plus import roundtrip
    monkeypatch.setenv("ATLAS_VERIFY_TIMEOUT", "5")
    dead = "http://127.0.0.1:9/mcp"   # nothing listens: the error step and the verdict, no server needed

    assert roundtrip.main(["--url", dead]) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1] == "ROUND-TRIP: FAILED"
    assert not any(ln.lstrip().startswith("{") for ln in out), out
    assert out[0].startswith("error: error "), out

    assert roundtrip.main(["--url", dead, "--json-lines"]) == 1
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1] == "ROUND-TRIP: FAILED" and json.loads(out[0])["step"] == "error", out

    assert roundtrip.main(["--url", dead, "--json"]) == 1
    obj = json.loads(capsys.readouterr().out)
    assert obj["ok"] is False and obj["steps"][0]["step"] == "error", obj
    assert roundtrip._JSON_LINES is False   # the mode never leaks into the next call


def test_plain_lines_name_what_an_installer_checks():
    from atlas_plus import roundtrip
    root = str(Path("/home/user/.memory"))
    assert roundtrip._plain({"step": "memory_health", "result": {"status": "ok", "memory_root": root}}) == \
        f"memory_health: status ok, memory_root {root}"
    line = roundtrip._plain({"step": "memory_search_tool", "confidence": "high", "hits": 1,
                             "found_this_write": True, "first": {"path": "projects/demo.md"}})
    assert line == "memory_search_tool: confidence high, 1 hit(s), found this write, first projects/demo.md"
    assert "FAILED" in roundtrip._plain({"step": "memory_remember_tool", "result": {"ok": False, "error": "x"}})


def test_components_names_the_fts_rebuild_script_this_install_has(monkeypatch):
    from atlas_plus import cli
    monkeypatch.setattr(sys, "argv", [str(Path("/home/user/.venv/bin/sextant")), "components"])
    row = next(r for r in cli.gather_components(probe_ports=False) if "FTS" in r["name"])
    assert "sextant-fts-rebuild" in row["disable"] and "atlas-fts-rebuild" not in row["disable"], row
    monkeypatch.setattr(sys, "argv", [str(Path("/home/user/.venv/bin/atlas-plus")), "components"])
    row = next(r for r in cli.gather_components(probe_ports=False) if "FTS" in r["name"])
    assert "atlas-fts-rebuild" in row["disable"], row  # an atlas-* install keeps its own name


def test_python_m_fts_prints_the_console_scripts_line(tmp_path):
    from atlas_plus._proc import run_bounded
    root = tmp_path / "memory"
    (root / "projects").mkdir(parents=True)
    (root / "projects" / "demo.md").write_text("---\ntitle: demo\n---\nA note about rebuilding.\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_")}
    env.update(ATLAS_WORKSPACE_ROOT=str(tmp_path), ATLAS_MEMORY_ROOT=str(root),
               ATLAS_STATE_DIR=str(tmp_path / "state"), ATLAS_SEMANTIC_SEARCH="off",
               PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    res = run_bounded([sys.executable, "-m", "atlas_plus.mcp.memory_server.fts"], timeout=120,
                      max_bytes=1 << 20, env=env, cwd=str(tmp_path))
    out = res.stdout if isinstance(res.stdout, str) else res.stdout.decode("utf-8", "replace")
    assert res.returncode == 0, (out, res.stderr_tail)
    assert out.strip().startswith("FTS index rebuilt: 1 files indexed into "), out
    assert "Indexed " not in out


def test_index_error_names_a_command_that_exists(monkeypatch):
    from atlas_plus import _next_step
    monkeypatch.setattr(sys, "argv", [str(Path("/home/user/.venv/bin/sextant")), "search"])
    shown = _next_step.script("fts-rebuild")
    assert "sextant-fts-rebuild" in shown and "sextant fts-rebuild" not in shown, shown


def test_init_next_step_names_user_md_by_its_full_path(tmp_path, monkeypatch, capsys):
    root = tmp_path / "my corpus" / ".memory"
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "my corpus"))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "my corpus" / ".atlas"))
    monkeypatch.setattr(sys, "argv", ["sextant-init"])
    from atlas_plus import init_cmd
    try:
        init_cmd.main()
    except SystemExit as exc:
        assert not exc.code, exc
    out = capsys.readouterr().out
    want = str(root / "identity" / "user.md")
    assert (root / "identity" / "user.md").is_file()
    assert f'1. Edit "{want}" -- introduce yourself' in out, out


def test_mcp_remember_reply_names_the_scope_as_stored(corpus):
    from atlas_plus.mcp.memory_server import server
    w = server.memory_remember_tool("Project:Foo", "The Foo build runs nightly.")
    assert w.get("ok") is True, w
    assert w["scope"] == "project:foo" and "project:foo" in w["scope_note"], w
    assert Path(w["file"]).name == "foo.md"
    w2 = server.memory_remember_tool_v2("Reference:Bar", "Bar lives in the east rack.")
    assert w2.get("ok") is True and w2["scope"] == "reference:bar", w2
    same = server.memory_remember_tool("project:foo", "The Foo build also runs on Sundays.")
    assert "scope_note" not in same  # said only when the folding changed what was typed
