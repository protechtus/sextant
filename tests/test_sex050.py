"""SEX-050: leftovers of the PyPI everyman pass (MN89, sweep 27). Each test runs the path.

5. A fact with inner double quotes typed in PowerShell 5.1 arrives split into words, and a word such as
   `-q` reads as an option: the error must name the stdin form that works.
8. The uninstall report said "Unhook the client first." without the command.
"""
from __future__ import annotations

import sys

import pytest


def _run_cli(monkeypatch, tmp_path, argv):
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / ".memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))
    from atlas_plus import cli
    monkeypatch.setattr(sys, "argv", ["sextant", *argv])
    with pytest.raises(SystemExit) as exc:
        cli.app(args=argv, prog_name="sextant")
    return exc.value.code


@pytest.mark.parametrize("command", ["remember", "add"])
def test_a_fact_the_shell_split_at_a_dash_is_pointed_at_stdin(monkeypatch, tmp_path, capsys, command):
    # what `sextant remember 'Always run "pytest -q" before a push'` becomes in PowerShell 5.1
    code = _run_cli(monkeypatch, tmp_path, [command, "Always run pytest", "-q", "before a push"])
    err = capsys.readouterr().err
    assert code == 2
    assert f"| sextant {command} -`" in err, err
    assert "pipe the fact in" in err
    assert f"`sextant {command} --help`" in err                      # the usual next step is still there
    assert not (tmp_path / ".memory").exists()                       # nothing was stored


def test_a_fact_the_shell_split_into_words_is_pointed_at_stdin(monkeypatch, tmp_path, capsys):
    code = _run_cli(monkeypatch, tmp_path, ["remember", "Deploys", "happen on Tuesday"])
    err = capsys.readouterr().err
    assert code == 2 and "| sextant remember -`" in err, err


def test_a_bad_option_elsewhere_does_not_mention_facts(monkeypatch, tmp_path, capsys):
    code = _run_cli(monkeypatch, tmp_path, ["search", "words", "--no-such-flag"])
    err = capsys.readouterr().err
    assert code == 2 and "pipe the fact in" not in err and "`sextant search --help`" in err


def test_the_uninstall_report_names_the_unhook_command(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / ".memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))
    for var in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(var, str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    from atlas_plus import uninstall_cmd
    monkeypatch.setattr(sys, "argv", ["sextant-uninstall"])
    try:
        uninstall_cmd.main()
    except SystemExit as exc:
        assert exc.code in (0, None)
    out = capsys.readouterr().out
    assert "Report only. Nothing was changed." in out
    tail = out[out.index("Report only."):]
    assert "Unhook the client first" in tail
    assert "`claude mcp remove -s user sextant`" in tail and "`codex mcp remove sextant`" in tail
