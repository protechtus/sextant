"""SEX-047 row 7: the audit of the public install path (2026-11-24). Each test runs the failing path.

- A failed `sextant verify` ended on a pointer to a section of docs/GETTING-STARTED.md that does not exist.
  Any section a failure message names must be a heading of that page.
- Three `sextant setup --out` failures said what went wrong and stopped there.
"""
from __future__ import annotations

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / ".memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))
    for var in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(var, str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)


def test_a_failed_verify_names_no_doc_section_that_is_missing(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / ".memory").mkdir()                                   # a corpus exists: the general failure line
    from atlas_plus import roundtrip
    assert roundtrip._say_next(1, []) == 1
    err = capsys.readouterr().err
    assert "doctor" in err and "Fix line" in err
    page = DOCS / "GETTING-STARTED.md"
    headings = [h.strip().lower() for h in re.findall(r"^#{1,4}\s+(.+)$", page.read_text(encoding="utf-8"), re.M)]
    for section in re.findall(r"GETTING-STARTED\.md,\s*([^).]+)", err):
        assert any(section.strip().lower() in h for h in headings), f"no heading for {section!r}"
    assert "Troubleshooting)" not in err                             # the pointer that led nowhere


def test_setup_out_onto_a_file_it_will_not_merge_says_what_to_do(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    from atlas_plus import setup_cmd
    out = tmp_path / "notes.txt"
    out.write_text("mine\n", encoding="utf-8")
    rc = setup_cmd.main(["--client", "claude-code", "--out", str(out)])
    err = capsys.readouterr().err
    assert rc == 1 and "nothing was written" in err
    assert "leave --out off" in err and "new file name" in err
    assert out.read_text(encoding="utf-8") == "mine\n"


def test_setup_out_that_cannot_be_written_ends_with_a_next_line(monkeypatch, tmp_path, capsys):
    _isolate(monkeypatch, tmp_path)
    from atlas_plus import setup_cmd
    blocker = tmp_path / "afile"
    blocker.write_text("x", encoding="utf-8")
    rc = setup_cmd.main(["--client", "json", "--out", str(blocker / "sub" / "mcp.json")])  # a file where a folder is needed
    err = capsys.readouterr().err
    assert rc == 1 and "cannot write" in err
    assert err.rstrip().splitlines()[-1].startswith("Next: ") and "leave --out off" in err
