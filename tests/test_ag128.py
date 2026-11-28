"""AG128 (SEX-048's rest): `review` tables escape note metadata; the HTTP verify line names `py -m`.

Functional: the review commands run in process (typer's CliRunner) on a tiny corpus whose frontmatter
carries rich markup and a U+202E; the setup text is rendered from `describe()` as a Windows client
would read it. No console-script name, no child process, no live service.
"""
from __future__ import annotations

import unicodedata

import pytest
from typer.testing import CliRunner

RLO = "\u202e"
BOLD = "[bold]"


def _raw_format_chars(text: str) -> list[str]:
    return [hex(ord(c)) for c in text if unicodedata.category(c) == "Cf"
            or (unicodedata.category(c) == "Cc" and c not in "\n\r\t")]


@pytest.fixture()
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "mem"
    (root / "reference").mkdir(parents=True)
    note = ("---\n"
            f"type: \"{BOLD}t{RLO}x\"\n"
            "half_life: 1\n"
            f"updated: \"2020-01-01{BOLD}u{RLO}\"\n"
            f"confidence: \"high\"\n"
            f"last_confirmed: \"2020-01-01{BOLD}c\"\n"
            "---\nbody\n")
    (root / "reference" / "ag128-note.md").write_text(note, encoding="utf-8")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root))
    monkeypatch.setenv("COLUMNS", "220")
    from atlas_plus.curation import cli as ccli
    from atlas_plus.curation import decay as cdecay
    for mod in (ccli, cdecay):
        monkeypatch.setattr(mod, "MEMORY_ROOT", root)
    from atlas_plus.mcp.memory_server import routing
    monkeypatch.setattr(routing, "MEMORY_ROOT", root, raising=False)
    return root


def test_decay_table_prints_type_and_updated_as_written(corpus):
    from atlas_plus.curation import cli as ccli
    res = CliRunner().invoke(ccli.app, ["decay"])
    assert res.exit_code == 0, res.output
    out = res.output
    assert "ag128-note" in out, out
    # rich markup in the frontmatter prints literally (it would vanish and style the row before AG128)
    assert BOLD + "t" in out, out    # `updated` is cut to 10 characters by the scanner: no markup reaches it
    assert not _raw_format_chars(out), _raw_format_chars(out)



def test_gaps_autoticket_prints_query_ticket_id_and_title_as_written(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from atlas_plus.curation import cli as ccli
    from atlas_plus.mcp.memory_server.tools import search_log
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COLUMNS", "220")
    miss = {"query": f"{BOLD}q{RLO}uery words", "miss_count": 3, "total": 4}
    monkeypatch.setattr(search_log, "top_miss_queries", lambda since_hours, limit: [miss])
    ticket = SimpleNamespace(id=f"{BOLD}T-1{RLO}", title=f"{BOLD}gap title{RLO}")
    monkeypatch.setattr(ccli, "_autofile_gap_tickets", lambda misses: [ticket])
    res = CliRunner().invoke(ccli.app, ["gaps", "--autoticket"])
    assert res.exit_code == 0, res.output
    out = res.output
    assert BOLD + "T-1" in out and BOLD + "gap title" in out, out     # ticket line (t.id, t.title)
    assert out.count(BOLD + "q") >= 2, out                           # the query cell and the suggestion cell
    assert not _raw_format_chars(out), _raw_format_chars(out)

def test_verify_http_line_names_the_py_launcher_form(monkeypatch):
    from atlas_plus import setup_cmd
    info = setup_cmd.describe()
    h = info["http"]
    assert h["verify_http_py"].startswith("py -m atlas_plus verify --url http://<host>:")
    assert h["verify_http_py"].endswith("--token $env:ATLAS_MCP_TOKEN")
    for windows in (True, False):  # POSIX setup text names it too: the CLIENT can be a Windows machine
        monkeypatch.setattr(setup_cmd, "_powershell", lambda w=windows: w)
        text = setup_cmd.render(setup_cmd.describe(), "all")
        assert "only the py launcher" in text and "types `py` in place of `python`" in text, text
        # the full PowerShell form where the text is read in PowerShell; POSIX text keeps no `$env:` (MN89)
        assert (h["verify_http_py"] in text) is windows


def test_py_launcher_form_only_rewrites_a_python_line():
    from atlas_plus import setup_cmd
    assert setup_cmd._py_launcher_form("python -m atlas_plus verify") == "py -m atlas_plus verify"
    assert setup_cmd._py_launcher_form("pythonw -m x") == "pythonw -m x"
    assert setup_cmd._py_launcher_form("/usr/bin/python3 -m x") == "/usr/bin/python3 -m x"
