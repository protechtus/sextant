"""`python -m atlas_plus <subcommand> --help` names a program that exists.

`sextant setup` prints the `python -m atlas_plus` form, so a stranger types it. The umbrella hands
guessable subcommands (`review`, `ingest`, ...) to their own apps by rewriting argv[0], and Click, seeing
a `python -m` start, glued that name onto the package: the usage line read `python -m
atlas_plus.sextant review` and `python -m atlas_plus.sextant-transcripts ingest`. Neither module
exists; copying the line gave "No module named". Found on 2026-11-25 while walking `ingest --help`.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"


def _help(tmp_path, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_")}
    env.update({
        "PYTHONPATH": str(_SRC), "PYTHONUTF8": "1", "COLUMNS": "200", "NO_COLOR": "1", "TERM": "dumb",
        "ATLAS_WORKSPACE_ROOT": str(tmp_path / "ws"), "ATLAS_MEMORY_ROOT": str(tmp_path / "ws" / ".memory"),
        "ATLAS_STATE_DIR": str(tmp_path / "ws" / ".atlas"),
    })
    res = subprocess.run([sys.executable, "-m", "atlas_plus", *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", env=env, cwd=str(tmp_path), timeout=120,
                         stdin=subprocess.DEVNULL)
    return res.returncode, res.stdout + res.stderr


def _usage_line(out: str) -> str:
    for line in out.splitlines():
        if "usage:" in line.lower():
            return line.strip()
    return ""


@pytest.mark.parametrize("args", [("review", "--help"), ("ingest", "--help"), ("review", "stale", "--help")])
def test_the_usage_line_names_no_module_that_does_not_exist(tmp_path, args):
    code, out = _help(tmp_path, *args)
    assert code == 0, out
    usage = _usage_line(out)
    assert usage, out
    for dotted in re.findall(r"-m (atlas_plus[\w.\-]*)", usage):
        assert dotted == "atlas_plus", f"the usage line names `python -m {dotted}`: {usage}"
    assert "atlas_plus.sextant" not in out


def test_review_help_keeps_the_form_the_user_typed(tmp_path):
    _code, out = _help(tmp_path, "review", "--help")
    assert "python -m atlas_plus review" in _usage_line(out)


def test_the_helper_leaves_other_starts_alone(monkeypatch):
    from atlas_plus import _typer
    # under the test runner `__main__` is not atlas_plus: nothing to correct
    monkeypatch.setattr(sys, "argv", ["sextant review"])
    assert _typer._delegated_prog_name() is None

    class _Main:
        __package__ = "atlas_plus"
    monkeypatch.setitem(sys.modules, "__main__", _Main)
    assert _typer._delegated_prog_name() == "python -m atlas_plus review"
    monkeypatch.setattr(sys, "argv", ["sextant-transcripts", "ingest"])
    assert _typer._delegated_prog_name() == "sextant-transcripts"
    monkeypatch.setattr(sys, "argv", [__file__])   # a real path: the umbrella itself
    assert _typer._delegated_prog_name() is None
