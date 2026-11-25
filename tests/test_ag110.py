"""AG110 (sweep 31): SEX-051 F3/F4/F5/F6/F8, SEX-043 item 5, SEX-048 item 9. In-process, no live service."""
from __future__ import annotations

import io
import os
import subprocess
import sys

import pytest

from atlas_plus import _next_step

WIN = os.name == "nt"
VAR = "ATLAS_AG110_KEY"


def _env_with_stub(tmp_path):
    """PATH with a `python` first that prints nothing and exits 9009, as the Microsoft Store stub does."""
    (tmp_path / "stub").mkdir(exist_ok=True)
    (tmp_path / "stub" / "python.cmd").write_text("@echo off\r\nexit /b 9009\r\n", encoding="ascii")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_")}
    env["PATH"] = str(tmp_path / "stub") + os.pathsep + env.get("PATH", "")
    return env


def _key_from(tmp_path, shell, line):
    if shell == "PowerShell:":
        from atlas_plus._proc import run_bounded
        script = tmp_path / "hint.ps1"
        script.write_text(line + "\r\n[Console]::Out.Write($env:" + VAR + ")\r\n", encoding="utf-8-sig")
        res = run_bounded(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                           str(script)], timeout=60, max_bytes=65536, env=_env_with_stub(tmp_path))
        return res.stdout.decode("utf-8", "replace").strip()
    res = subprocess.run(f'cmd /d /s /c "{line} >nul & set {VAR}"', env=_env_with_stub(tmp_path),  # as typed at a prompt
                         stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    out = res.stdout.decode("mbcs", "replace").splitlines()
    return next((ln.split("=", 1)[1].strip() for ln in out if ln.startswith(VAR + "=")), "")


@pytest.mark.skipif(not WIN, reason="the Store stub and these shells are Windows-only")
@pytest.mark.parametrize("spaced", [False, True])
def test_f4_windows_key_hints_give_a_key_with_the_store_stub_first(tmp_path, monkeypatch, spaced):
    monkeypatch.delenv(VAR, raising=False)
    if spaced:  # an interpreter path with spaces: a wrapper that forwards to this one
        (tmp_path / "Program Files" / "Py thon").mkdir(parents=True)
        wrapper = tmp_path / "Program Files" / "Py thon" / "python.cmd"
        wrapper.write_text(f'@"{sys.executable}" %*\r\n', encoding="ascii")
        monkeypatch.setattr(sys, "executable", str(wrapper))
    lines = _next_step.key_setup_lines(VAR).splitlines()
    for shell in ("PowerShell:", "cmd.exe:"):
        line = next(x for x in lines if x.strip().startswith(shell)).split(shell, 1)[1].strip()
        assert len(_key_from(tmp_path, shell, line)) >= 16, (shell, line)


def _run_main(monkeypatch, argv, func) -> int:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys.modules["__main__"], "__spec__", None, raising=False)
    try:
        func()
    except SystemExit as exc:
        return 0 if exc.code is None else (exc.code if isinstance(exc.code, int) else 1)
    return 0


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A fresh corpus and state dir in tmp_path, made by the real init; home in tmp_path too."""
    for k in [k for k in os.environ if k.startswith("ATLAS_")]:
        monkeypatch.delenv(k, raising=False)
    for k, v in (("ATLAS_WORKSPACE_ROOT", "ws"), ("ATLAS_MEMORY_ROOT", "ws/.memory"), ("ATLAS_STATE_DIR", "state"),
                 ("HOME", "home"), ("USERPROFILE", "home")):
        monkeypatch.setenv(k, str(tmp_path / v))
    (tmp_path / "home").mkdir()
    from atlas_plus import init_cmd
    init_cmd.run_init(dry_run=False)
    (tmp_path / "state" / "embeddings").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "embeddings" / "x.bin").write_bytes(b"x")
    return tmp_path


@pytest.mark.parametrize("form", ["--path", "positional"])
def test_f3_import_expands_a_leading_tilde(installed, monkeypatch, capsys, form):
    from atlas_plus import import_cmd
    vault = installed / "home" / "MyVault"
    vault.mkdir(parents=True)
    (vault / "one.md").write_text("# One\nhello vault\n", encoding="utf-8")
    arg = "~" + os.sep + "MyVault"  # what PowerShell hands a native program: the tilde unexpanded
    argv = ["sextant-import", "--source", "obsidian"] + (["--path", arg] if form == "--path" else [arg])
    assert _run_main(monkeypatch, argv, import_cmd.main) == 0
    cap = capsys.readouterr()
    assert "path not found" not in cap.err and "one.md" in cap.out


def test_f3_siblings_setup_out_and_confirm_path(installed, monkeypatch, capsys):
    from atlas_plus import init_cmd, setup_cmd, uninstall_cmd
    assert setup_cmd.main(["--client", "json", "--out", "~/c.json"]) == 0 and (installed / "home" / "c.json").is_file()
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(installed / "home" / "mem"))
    init_cmd.run_init(dry_run=False)
    gone = []
    monkeypatch.setattr(uninstall_cmd, "_rmtree", gone.append)  # record, remove nothing
    argv = ["sextant-uninstall", "--corpus", "--yes", "--confirm-path", "~/mem"]
    assert _run_main(monkeypatch, argv, uninstall_cmd.main) == 0, capsys.readouterr()
    assert gone and "did not match" not in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["empty-pipe", "devnull"])
@pytest.mark.parametrize("which, argv", [
    ("uninstall", ["sextant-uninstall", "--state", "--yes"]),
    ("reset", ["sextant-reset", "--all", "--yes"]),
])
def test_f6_no_prompt_without_a_terminal(installed, monkeypatch, capsys, which, argv, kind):
    from atlas_plus import reset_cmd, uninstall_cmd
    with (io.StringIO("") if kind == "empty-pipe" else open(os.devnull, encoding="utf-8")) as stream:  # NUL: isatty() True on Windows
        monkeypatch.setattr(sys, "stdin", stream)
        assert _run_main(monkeypatch, argv, uninstall_cmd.main if which == "uninstall" else reset_cmd.main) == 1
    cap = capsys.readouterr()
    assert "Type the FULL" not in cap.out + cap.err and "no interactive terminal" in cap.out + cap.err
    assert "--confirm-" in cap.out + cap.err and "Next:" in cap.err
    assert (installed / "state" / "embeddings" / "x.bin").exists() and (installed / "ws" / ".memory").exists()


def test_f6_a_piped_path_still_confirms(installed, monkeypatch, capsys):
    from atlas_plus import uninstall_cmd
    monkeypatch.setattr(sys, "stdin", io.StringIO(str(installed / "state") + "\n"))
    assert uninstall_cmd.confirm_state_removal(True, None) is None
    assert "Type the FULL" not in capsys.readouterr().out


def _stop(path):  # the OS stops the removal part way
    raise PermissionError(13, "The process cannot access the file", str(path))


@pytest.mark.parametrize("which, argv, flag", [
    ("reset", ["sextant-reset", "--state", "--yes"], "--state --dry-run"),
    ("uninstall", ["sextant-uninstall", "--state", "--yes", "--confirm-state-path", "{state}"], "--state --dry-run"),
    ("uninstall", ["sextant-uninstall", "--corpus", "--yes", "--confirm-path", "{corpus}"], "--corpus --dry-run"),
])
def test_f8_stopped_removal_next_line_keeps_the_area(installed, monkeypatch, capsys, which, argv, flag):
    from atlas_plus import reset_cmd, uninstall_cmd
    monkeypatch.setattr(uninstall_cmd, "_rmtree", _stop)
    argv = [a.format(state=installed / "state", corpus=installed / "ws" / ".memory") for a in argv]
    assert _run_main(monkeypatch, argv, reset_cmd.main if which == "reset" else uninstall_cmd.main) == 1
    nxt = next(ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("Next:"))
    assert flag in nxt, nxt


def test_f5_brain_and_dashboard_say_the_same_minimum(monkeypatch, capsys, caplog):
    from atlas_plus.brain import server as brain
    from atlas_plus.web import app as web
    n = _next_step.KEY_MIN_LEN
    monkeypatch.setattr(brain, "_API_KEY", "short")
    with caplog.at_level("ERROR"):
        assert _run_main(monkeypatch, ["sextant-brain"], brain.main) == 1
    assert f"at least {n}" in capsys.readouterr().err
    assert any(f"at least {n}" in r.getMessage() for r in caplog.records)  # the log line too
    monkeypatch.setattr(web, "API_KEY", "x" * (n - 1))  # one under the constant is refused
    monkeypatch.setattr(web, "_ALLOW_NO_AUTH", False)
    assert _run_main(monkeypatch, ["sextant-web"], web.main) == 1
    assert f"at least {n}" in capsys.readouterr().err


def test_setup_every_section_header_says_required_or_optional(installed, capsys):
    import re
    from atlas_plus import setup_cmd
    assert setup_cmd.main([]) == 0
    headers = [ln for ln in capsys.readouterr().out.splitlines() if re.match(r"^(\[[a-z][^\]]*\] )?\d[a-z]?\. \S", ln)]
    assert len(headers) >= 4 and all(ln.startswith(("[required", "[optional]")) for ln in headers), headers
    assert any(ln.startswith("[optional]") and "hooks" in ln for ln in headers)


def test_remember_shows_the_scope_as_stored(installed):
    from typer.testing import CliRunner
    from atlas_plus import cli
    r = CliRunner().invoke(cli.app, ["remember", "--scope", "Project:Alpha", "the alpha note stays"])
    assert r.exit_code == 0 and "project:alpha" in " ".join(r.output.split()) and "lowercase" in r.output, r.output
    r = CliRunner().invoke(cli.app, ["remember", "--scope", "project:alpha", "a second alpha fact"])
    assert r.exit_code == 0 and "lowercase" not in r.output
