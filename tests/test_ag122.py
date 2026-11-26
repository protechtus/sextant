"""AG122 (SEX-048): note paths printed by `sextant review` and `sextant versions` go through
_term.safe (a U+202E in a file name cannot reorder the line) and, for rich, markup is escaped AFTER
it (a `[bold]` in a file name prints as written). In process; no console-script name."""
from __future__ import annotations

import unicodedata

import pytest
from typer.testing import CliRunner

RLO = "‮"
NAMES = ["plain-a.md", "rlo-" + RLO + "gnp.md", "tag-[bold]x.md", "zwsp-​hid.md"]


def _raw(text: str) -> set[str]:
    return {hex(ord(c)) for c in text
            if (unicodedata.category(c) == "Cf" and c not in "‌‍")
            or (unicodedata.category(c) == "Cc" and c not in "\n\r\t")}


@pytest.fixture()
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "mem"
    ref = root / "reference"
    ref.mkdir(parents=True)
    vd = root / "versions" / "reference"
    vd.mkdir(parents=True)
    for i, n in enumerate(NAMES):
        try:
            (ref / n).write_text(f"---\ntype: reference\nupdated: 2020-01-0{i + 1}\n---\n# n{i}\nbody {i}\n",
                                 encoding="utf-8")
            (vd / f"{n[:-3]}_2020-01-01_v1.md").write_text("old\n", encoding="utf-8")
        except OSError:
            pytest.skip("this file system refuses the test file names")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root))
    monkeypatch.setenv("COLUMNS", "200")
    from atlas_plus.curation import cli, stale
    for mod in (cli, stale):
        if hasattr(mod, "MEMORY_ROOT"):
            monkeypatch.setattr(mod, "MEMORY_ROOT", root)
    return root


def test_review_stale_escapes_format_chars_and_markup(corpus):
    from atlas_plus.curation.cli import app
    res = CliRunner().invoke(app, ["stale"])
    assert res.exit_code == 0, res.output
    assert "plain-a.md" in res.output
    assert not _raw(res.output), _raw(res.output)
    assert "\\u202e" in res.output
    assert "tag-[bold]x.md" in res.output  # escape() last: the tag is text, not style


def test_versions_report_and_prune_plan_escape(corpus, capsys):
    from atlas_plus import versions_cmd
    assert versions_cmd.main([]) == 0
    assert versions_cmd.main(["--prune", "--older-than", "0", "--keep-last", "0"]) == 0
    out = capsys.readouterr().out
    assert "rlo-" in out and "\\u202e" in out
    assert not _raw(out), _raw(out)


def test_p_order_escape_after_safe():
    from atlas_plus.curation.cli import _p
    from rich.console import Console
    con = Console(record=True, width=200, color_system=None)
    con.print(_p("a/" + RLO + "[bold]b.md"))
    shown = con.export_text()
    assert "a/\\u202e[bold]b.md" in shown
    assert RLO not in shown


def test_archive_closed_stdin_names_yes(corpus):
    from atlas_plus.curation.cli import app
    res = CliRunner().invoke(app, ["archive", "reference/plain-a.md"], input="")
    assert res.exit_code == 1
    assert "--yes" in res.output
    assert (corpus / "reference" / "plain-a.md").exists()
