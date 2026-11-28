"""AG133 at the fold: a command's usage line names what was typed on every supported Python.

Python 3.14's argparse builds its default program name from how `__main__` was started. Under it an installed
command on Windows printed `usage: python.exe C:\\...\\Scripts\\sextant-doctor [-h]` and a command the umbrella
delegates to printed the interpreter's name. `_next_step.ArgumentParser` names the program from `sys.argv[0]`.
"""
from __future__ import annotations

import sys

import pytest

from atlas_plus import _next_step


@pytest.mark.parametrize("arg0, expected", [
    (r"D:\\apps\\venv\\Scripts\\sextant-doctor.exe", "sextant-doctor"),
    (r"D:\\apps\\venv\\Scripts\\sextant-doctor", "sextant-doctor"),
    ("/home/someone/.venv/bin/sextant-doctor", "sextant-doctor"),
    ("sextant dashboard", "sextant dashboard"),
    ("sextant-web-script", "sextant-web"),
])
def test_usage_names_the_installed_command_or_the_words_typed(monkeypatch, capsys, arg0, expected):
    monkeypatch.setattr(sys, "argv", [arg0, "--help"])
    parser = _next_step.ArgumentParser(description="d")
    assert parser.prog == expected
    with pytest.raises(SystemExit):
        parser.parse_args()
    out = capsys.readouterr().out
    assert out.startswith(f"usage: {expected} "), out[:80]
    assert "python" not in out.splitlines()[0].lower() and ":" not in out.splitlines()[0].replace("usage:", "")


@pytest.mark.parametrize("arg0", ["/x/site-packages/atlas_plus/__main__.py", "tool.py", "-c", ""])
def test_a_module_or_file_start_keeps_argparses_own_name(monkeypatch, arg0):
    monkeypatch.setattr(sys, "argv", [arg0])
    assert _next_step._typed_prog() is None
    assert _next_step.ArgumentParser(description="d").prog  # argparse answered; nothing was forced


def test_a_caller_that_names_the_program_is_obeyed(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/venv/bin/sextant-doctor"])
    assert _next_step.ArgumentParser(prog="sextant doctor").prog == "sextant doctor"
    sub = _next_step.ArgumentParser().add_subparsers().add_parser("build")
    assert sub.prog == "sextant-doctor build"  # subparsers still build on the parent's name


def test_the_umbrellas_dashboard_help_names_what_was_typed_on_this_python():
    from typer.testing import CliRunner

    from atlas_plus import cli

    for typed in ("dashboard", "web"):
        res = CliRunner().invoke(cli.app, [typed, "--help"], prog_name="sextant")
        assert res.exit_code == 0, res.output
        assert res.output.startswith(f"usage: sextant {typed} "), res.output[:80]
