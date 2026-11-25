"""AG106 (sweep 28; SEX-048, SEX-050 item 9, SEX-047): the key hint of sextant-mcp and sextant-brain
is one copyable command per line outside the log record (a log record folded it at 80 columns);
the dashboard's start says where to open it and how the key is given; a removal the OS stops part
way in uninstall and reset ends with an outcome line and a next step, not a traceback.
In-process, no console-script name, no live service, every child bounded."""
from __future__ import annotations

import importlib
import shlex
import subprocess
import sys

import pytest

CODE = "import secrets; print(secrets.token_urlsafe(32))"


def _run_main(monkeypatch, argv, func) -> int:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys.modules["__main__"], "__spec__", None, raising=False)
    with pytest.raises(SystemExit) as exc:
        func()
    code = exc.value.code
    return 0 if code is None else (code if isinstance(code, int) else 1)


@pytest.mark.parametrize("modname, attr, var, argv", [
    ("atlas_plus.mcp.memory_server.server", "_MCP_TOKEN", "ATLAS_MCP_TOKEN", "sextant-mcp"),
    ("atlas_plus.brain.server", "_API_KEY", "ATLAS_BRAIN_API_KEY", "sextant-brain"),
])
@pytest.mark.parametrize("unset", [True, False])
def test_server_key_hint_is_one_runnable_line(monkeypatch, capsys, modname, attr, var, argv, unset):
    from atlas_plus import _next_step
    mod = importlib.import_module(modname)
    monkeypatch.setattr(_next_step, "_windows", lambda: False)  # the POSIX line runs on every OS
    if unset:
        monkeypatch.delenv(var, raising=False)
    else:
        monkeypatch.setenv(var, "")
    monkeypatch.setattr(mod, attr, "")
    assert _run_main(monkeypatch, [argv], mod.main) == 1
    err = capsys.readouterr().err
    assert (f"{var} is set but empty" in err) is (not unset)  # the set-but-empty wording is kept
    line = next(ln for ln in err.splitlines() if f"export {var}=$(" in ln)
    inner = line.split(f"export {var}=$(", 1)[1].rsplit(")", 1)[0]
    argv_ = shlex.split(inner)
    assert argv_[1:] == ["-c", CODE]
    r = subprocess.run(argv_, capture_output=True, text=True, encoding="utf-8", timeout=60,
                       stdin=subprocess.DEVNULL)
    assert r.returncode == 0 and len(r.stdout.strip()) >= 16  # the printed line makes a real key


@pytest.mark.parametrize("modname, attr, var, argv", [
    ("atlas_plus.mcp.memory_server.server", "_MCP_TOKEN", "ATLAS_MCP_TOKEN", "sextant-mcp"),
    ("atlas_plus.brain.server", "_API_KEY", "ATLAS_BRAIN_API_KEY", "sextant-brain"),
])
def test_server_key_hint_powershell_line_is_whole(monkeypatch, capsys, modname, attr, var, argv):
    from atlas_plus import _next_step
    mod = importlib.import_module(modname)
    monkeypatch.setattr(_next_step, "_windows", lambda: True)
    monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(mod, attr, "")
    assert _run_main(monkeypatch, [argv], mod.main) == 1
    lines = capsys.readouterr().err.splitlines()
    assert any(ln.strip() == f'PowerShell:  $env:{var} = python -c "{CODE}"' for ln in lines)


def test_dashboard_start_says_where_to_open_it(monkeypatch):
    import uvicorn
    from typer.testing import CliRunner
    from atlas_plus import cli
    from atlas_plus.web import app as webapp
    key = "k" * 12 + "AG106" + "z" * 12
    ran = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: ran.append(k.get("port")))
    monkeypatch.setattr(webapp, "API_KEY", key)
    monkeypatch.setattr(webapp, "_ALLOW_NO_AUTH", False)
    monkeypatch.setattr(webapp, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(webapp, "WEB_PORT", 9604)  # what ATLAS_WEB_PORT=9604 sets at import
    monkeypatch.setattr(webapp, "_port_free", lambda h, p: True)
    r = CliRunner().invoke(cli.app, ["web"])
    assert r.exit_code == 0, r.output
    assert ran == [9604]
    lines = [ln for ln in r.output.splitlines() if ln.strip() and "[atlas" not in ln]
    assert "http://127.0.0.1:9604/" in lines[-1] and "Bearer" in lines[-1]
    assert "127.0.0.1:9604" in lines[0]  # the banner names this start's bind, not only the default
    assert key not in r.output


def test_open_line_brackets_ipv6():
    from atlas_plus.web import app as webapp
    assert "http://[::1]:9605/" in webapp.open_line("::", 9605)
    assert "http://127.0.0.1:9605/" in webapp.open_line("0.0.0.0", 9605, keyed=False)


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A fresh corpus and state dir in tmp_path, made by the real init."""
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "ws" / ".memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus import init_cmd
    init_cmd.run_init(dry_run=False)
    (tmp_path / "state" / "embeddings").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "embeddings" / "x.bin").write_bytes(b"x")
    return tmp_path


def _held(monkeypatch):
    """The OS stops the removal part way (a file held open on Windows, a read-only folder on POSIX)."""
    from atlas_plus import uninstall_cmd

    def _stop(path):
        raise PermissionError(13, "The process cannot access the file", str(path))
    monkeypatch.setattr(uninstall_cmd, "_rmtree", _stop)


@pytest.mark.parametrize("which, argv", [
    ("reset", ["sextant-reset", "--state", "--yes"]),
    ("uninstall-state", ["sextant-uninstall", "--state", "--yes", "--confirm-state-path", "{state}"]),
    ("uninstall-corpus", ["sextant-uninstall", "--corpus", "--yes", "--confirm-path", "{corpus}"]),
])
def test_a_stopped_removal_names_a_next_step(installed, monkeypatch, capsys, which, argv):
    from atlas_plus import reset_cmd, uninstall_cmd
    _held(monkeypatch)
    argv = [a.format(state=installed / "state", corpus=installed / "ws" / ".memory") for a in argv]
    func = reset_cmd.main if which == "reset" else uninstall_cmd.main
    assert _run_main(monkeypatch, argv, func) == 1  # not a PermissionError traceback
    cap = capsys.readouterr()
    assert "failed:" in cap.out
    assert "Next:" in cap.err


def test_setup_failed_write_names_a_next_step(tmp_path, monkeypatch, capsys):
    from atlas_plus import setup_cmd
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "afile").write_text("x", encoding="utf-8")
    assert setup_cmd.main(["--client", "json", "--out", str(tmp_path / "afile" / "sub" / "c.json")]) == 1
    assert "Next:" in capsys.readouterr().err


@pytest.mark.parametrize("modname, attr, var, argv", [
    ("atlas_plus.mcp.memory_server.server", "_MCP_TOKEN", "ATLAS_MCP_TOKEN", "sextant-mcp"),
    ("atlas_plus.brain.server", "_API_KEY", "ATLAS_BRAIN_API_KEY", "sextant-brain"),
])
def test_short_key_gets_the_same_copyable_lines(monkeypatch, capsys, modname, attr, var, argv):
    """The sibling refusal (a key under 16 characters) folded the same command in its log record."""
    from atlas_plus import _next_step
    mod = importlib.import_module(modname)
    monkeypatch.setattr(_next_step, "_windows", lambda: False)
    monkeypatch.setattr(mod, attr, "short")
    assert _run_main(monkeypatch, [argv], mod.main) == 1
    err = capsys.readouterr().err
    assert "16 characters" in err and "short" not in err.replace("characters", "")
    assert any(ln.startswith(f"  bash/zsh:    export {var}=$(") for ln in err.splitlines())


def test_remember_says_where_a_preference_lands(installed):
    from typer.testing import CliRunner
    from atlas_plus import cli
    r = CliRunner().invoke(cli.app, ["remember", "--scope", "preference:testing", "run the fast tests first"])
    assert r.exit_code == 0, r.output
    flat = " ".join(r.output.split())
    assert "feedback" in flat and "preference:<topic> is stored in the feedback folder" in flat
    r = CliRunner().invoke(cli.app, ["remember", "--scope", "project:demo", "the demo note stays"])
    assert r.exit_code == 0, r.output
    assert "feedback folder" not in r.output
