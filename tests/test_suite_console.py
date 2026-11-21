"""The suite behaves the same in a person's terminal as in CI.

Born 2026-11-21 from a by-hand install: `pytest tests -q` in a stock PowerShell window (console
code page 437) ended `1 failed, 352 passed, 12133 errors`. CI has no console and never saw it.
"""
from __future__ import annotations

import ctypes
import os

import pytest

from atlas_plus import setup_cmd as sc


@pytest.mark.skipif(os.name != "nt", reason="console code pages are a Windows matter")
@pytest.mark.parametrize("code_page", [437, 1252, 932])
def test_a_non_ascii_path_printed_in_a_code_page_console_is_still_readable(monkeypatch, tmp_path, capsys, code_page):
    """A CLI command that prints a non-ASCII path, run where the console reports a legacy code
    page: what capsys hands back is the text that was printed, and reading it does not raise."""
    monkeypatch.setattr(ctypes.windll.kernel32, "GetConsoleOutputCP", lambda: code_page)
    for k in list(os.environ):
        if k.startswith("ATLAS_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "My Wörk"))
    rc = sc.main(["--client", "codex"])
    out = capsys.readouterr()
    assert rc == 0, out.err
    assert "My Wörk" in out.out
