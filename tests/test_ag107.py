"""Sweep 29, lane AG107: small leftovers of SEX-043/047/048/050. Every test runs the fixed path
in-process (no console-script names, no live service; service URLs pin to a dead port)."""
from __future__ import annotations

import importlib.util
import io
import os
import re
import shlex
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def ws(tmp_path, monkeypatch):
    for var in list(os.environ):
        if var.startswith("ATLAS_"):
            monkeypatch.delenv(var, raising=False)
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(root))
    for var in ("ATLAS_BRAIN_URL", "ATLAS_MCP_URL", "ATLAS_OLLAMA_URL", "ATLAS_EMBED_URL"):
        monkeypatch.setenv(var, "http://127.0.0.1:9")
    return root


# ------------------------------------------------------------ 1f. one verify form in init and setup

def test_init_and_setup_name_verify_in_the_one_form_this_install_types(ws, capsys, monkeypatch):
    from atlas_plus import _next_step, init_cmd, setup_cmd
    monkeypatch.setattr(_next_step, "how_to_run", lambda name: "./venv/bin/" + name)
    monkeypatch.setattr(_next_step, "umbrella", lambda: "sextant")
    monkeypatch.setattr(sys, "argv", ["sextant-init"])
    with pytest.raises(SystemExit):
        init_cmd.main()
    init_out = capsys.readouterr().out
    assert "`./venv/bin/sextant verify`" in init_out and "`sextant verify`" not in init_out
    assert "`./venv/bin/sextant setup`" in init_out
    setup_cmd.main([])
    setup_out = capsys.readouterr().out
    assert "Verify: `./venv/bin/sextant verify`" in setup_out
    assert "`sextant verify`" not in setup_out


# ------------------------------------------------------------ 2a. brain /save says where preference: went

def test_brain_save_names_the_feedback_folder_for_a_preference_scope(ws, monkeypatch):
    from rich.console import Console
    from atlas_plus.brain import cli as brain_cli
    from atlas_plus.mcp.memory_server.tools import remember as rem

    monkeypatch.setattr(rem, "memory_remember",
                        lambda **kw: {"ok": True, "file": "feedback/tabs.md"})
    buf = io.StringIO()
    monkeypatch.setattr(brain_cli, "console", Console(file=buf, width=200, color_system=None))
    brain_cli._save_fact("preference:tabs Use four spaces.")
    out = buf.getvalue()
    assert "Saved to feedback/tabs.md" in out
    assert "stored in the feedback folder" in out
    buf.truncate(0)
    buf.seek(0)
    brain_cli._save_fact("project:demo A plain project fact.")
    assert "feedback folder" not in buf.getvalue()  # only the scope whose folder differs gets the clause


# ------------------------------------------------------------ 2c. setup --out on an unparsable file

@pytest.mark.parametrize("body", ["{not json", "[1, 2]"])
def test_setup_out_refusal_on_an_unparsable_file_names_the_next_command(ws, tmp_path, capsys, body):
    from atlas_plus import setup_cmd
    target = tmp_path / "client.json"
    target.write_text(body, encoding="utf-8")
    rc = setup_cmd.main(["--client", "json", "--out", str(target)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "Next:" in err and "setup --client json" in err and "without --out" in err
    assert target.read_text(encoding="utf-8") == body  # nothing written


# ------------------------------------------------------------ 3a. context before init

def test_context_before_init_names_init(ws, capsys):
    from atlas_plus import cli
    from atlas_plus import paths

    assert not paths.memory_root().exists()

    class Ctx:
        args = ["hello", "world"]

    try:
        cli.cmd_context(Ctx())
    except SystemExit:
        pass
    err = capsys.readouterr().err
    assert "No corpus" in err and "Next:" in err and "init" in err


# ------------------------------------------------------------ 3b. reset never creates a corpus

@pytest.mark.parametrize("flags", [["--corpus"], ["--corpus", "--yes"], ["--all", "--yes"]])
def test_reset_corpus_refuses_when_there_is_no_corpus(ws, capsys, monkeypatch, flags):
    from atlas_plus import paths, reset_cmd
    monkeypatch.setattr(sys, "argv", ["sextant-reset", *flags])
    with pytest.raises(SystemExit) as exc:
        reset_cmd.main()
    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert not paths.memory_root().exists()  # nothing created
    assert "no corpus" in err and "Next:" in err and "init" in err


# ------------------------------------------------------------ 3c. empty search says what to try (MCP and dashboard)

def _one_note(ws):
    from atlas_plus import paths
    root = paths.memory_root()
    (root / "reference").mkdir(parents=True)
    (root / "reference" / "a.md").write_text("# A\n\nThe kettle is blue.\n", encoding="utf-8")
    return root


def test_mcp_search_tool_empty_result_carries_a_next_step(ws):
    _one_note(ws)
    from atlas_plus._next_step import EMPTY_SEARCH
    from atlas_plus.mcp.memory_server import server
    res = server.memory_search_tool("zqxjvw")
    assert res["results"] == [] and res["hint"] == EMPTY_SEARCH


def test_dashboard_search_box_empty_result_says_what_to_try(ws, monkeypatch):
    pytest.importorskip("fastapi")
    import asyncio
    root = _one_note(ws)
    from atlas_plus.web import app as web
    monkeypatch.setattr(web, "_check_auth", lambda *a, **k: None)
    monkeypatch.setattr(web, "MEMORY_ROOT", root)
    body = asyncio.run(web.memory_search(None, q="zqxjvw")).body.decode("utf-8")
    assert ("No hits for" in body or "No matches for" in body) and "Try fewer or other words" in body


# ------------------------------------------------------------ 5. docker-compose key message

def test_compose_key_message_points_at_a_page_whose_command_makes_a_key():
    from atlas_plus._proc import run_bounded
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    for var in ("ATLAS_MCP_TOKEN", "ATLAS_WEB_API_KEY"):
        msg = re.search(r"\$\{" + var + r":\?([^}]*)\}", compose).group(1)
        page = re.search(r"docs/[A-Za-z-]+\.md", msg).group(0)
        text = (REPO / page).read_text(encoding="utf-8")
        line = next(ln for ln in text.splitlines() if ln.startswith("python3 -c ") and var in ln)
        code = shlex.split(line.split(">")[0])[2]
        res = run_bounded([sys.executable, "-c", code], timeout=60, max_bytes=65536)
        values = dict(ln.split("=", 1) for ln in res.stdout.decode("ascii").split())
        assert res.returncode == 0 and len(values[var]) >= 32


# ------------------------------------------------------------ 6. mode tests skip on a mount that ignores chmod

def _ag98():
    spec = importlib.util.spec_from_file_location("_ag98_for_ag107", REPO / "tests" / "test_ag98.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_mode_tests_skip_only_on_a_mount_that_ignores_modes():
    mod = _ag98()
    drvfs = "C:\\134 /mnt/c 9p rw,relatime,aname=drvfs;path=C:\\ 0 0\n"
    drvfs_meta = "C:\\134 /mnt/c 9p rw,relatime,aname=drvfs;path=C:\\;metadata 0 0\n"
    ext4 = "/dev/sdc / ext4 rw,relatime 0 0\n"
    vfat = "/dev/sdb1 /media/usb vfat rw 0 0\n"
    assert mod.modes_ignored("/mnt/c/tmp/x", drvfs, windows=False) is True
    assert mod.modes_ignored("/media/usb/tmp", vfat, windows=False) is True
    assert mod.modes_ignored("/mnt/c/tmp/x", drvfs_meta, windows=False) is False
    assert mod.modes_ignored("/tmp/pytest-1/x", ext4, windows=False) is False
    assert mod.modes_ignored("/mnt/c/tmp/x", drvfs, windows=True) is False  # Windows: the posix_only mark decides
