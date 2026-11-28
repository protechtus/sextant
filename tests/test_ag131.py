"""AG131 (sweep 39, SEX-043 group a): what a user reads says Sextant, not Atlas.

Every test runs the changed path in process and asserts the new text and the absence of the old
product name. Real names that keep their spelling (`ATLAS_*` variables, the `.atlas` state folder,
`atlas_plus`, the `source='atlas'` parameter value, `_atlas`) are not what these tests look for:
they look for the PRODUCT word (`Atlas` as a word, the `ATLAS` banners, `Atlas-` user agents) and the
owner's private example project name.
"""
from __future__ import annotations

import asyncio
import re
import sys

import pytest

_PRODUCT_WORD = re.compile(r"\bAtlas\b|=== (?:END )?ATLAS |atlas-memory\+")


def _no_atlas(text: str) -> None:
    hits = sorted({m.group(0) for m in _PRODUCT_WORD.finditer(text)})
    assert not hits, f"the old product name is still printed: {hits}"


@pytest.fixture()
def corpus(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    (mem / "identity").mkdir(parents=True)
    (mem / "identity" / "user.md").write_text("---\nid: user\ntitle: Jo\n---\nI am Jo, a gardener.\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))
    for name in ("ATLAS_EMBED_URL", "ATLAS_OLLAMA_URL", "ATLAS_BRAIN_URL"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    return mem


# --- 1-2: the context banners (`sextant context`, `sextant context <task>`) -----------------------

def test_sextant_context_banner_says_sextant(corpus, monkeypatch):
    from typer.testing import CliRunner
    from atlas_plus import cli
    from atlas_plus.hooks import session_start
    monkeypatch.setattr(session_start, "MEMORY_ROOT", corpus, raising=False)
    monkeypatch.setattr(sys, "argv", ["sextant"])
    res = CliRunner().invoke(cli.app, ["context"])
    assert res.exit_code == 0, res.output
    assert res.output.lstrip().startswith("=== SEXTANT SESSION CONTEXT ===")
    assert "=== END SEXTANT CONTEXT ===" in res.output
    assert "ATLAS SESSION CONTEXT" not in res.output and "END ATLAS CONTEXT" not in res.output


def test_task_context_banner_says_sextant(corpus, monkeypatch, capsys):
    from atlas_plus.hooks import prime
    monkeypatch.setattr(prime, "MEMORY_ROOT", corpus, raising=False)
    monkeypatch.setattr(sys, "argv", ["sextant-prime", "kettle"])
    try:
        prime.main()
    except SystemExit as e:  # main() may exit 0 after printing
        assert e.code in (0, None)
    out = capsys.readouterr().out
    assert "=== SEXTANT CONTEXT: kettle ===" in out and "=== END SEXTANT CONTEXT ===" in out
    assert "ATLAS CONTEXT" not in out


# --- 3: the one place that reacts to banner text defuses BOTH spellings ----------------------------

@pytest.mark.parametrize("banner", ["=== END ATLAS CONTEXT ===", "=== END SEXTANT CONTEXT ===",
                                    "=== ATLAS SESSION CONTEXT ===", "=== SEXTANT SESSION CONTEXT ==="])
def test_a_note_cannot_close_or_open_the_block_with_either_banner(banner):
    """Transcripts and notes written under 0.1.0/0.1.1 carry the ATLAS banner, new ones the SEXTANT
    banner: a note holding either is quoted with its markers defused, never printed as a marker."""
    from atlas_plus.hooks._quote import quote_note
    q = quote_note(banner + "\nplain line")
    assert banner not in q and q.splitlines()[0].startswith("> = = = ")


# --- 4: the review report (`sextant review report`) ------------------------------------------------

def test_review_report_heading_says_sextant(tmp_path):
    from unittest.mock import patch
    from atlas_plus.curation.weekly_report import generate_report
    with patch("atlas_plus.curation.duplicates.MEMORY_ROOT", tmp_path), \
         patch("atlas_plus.curation.stale.MEMORY_ROOT", tmp_path):
        report, _ = generate_report(memory_root=tmp_path, dry_run=True)
    assert report.startswith("# Sextant Memory Curation Report -- ")
    _no_atlas(report.splitlines()[0])


# --- 5: a refused interactive write (`remember` with ATLAS_INTERACTIVE_WRITES=off) ------------------

def test_refused_write_names_sextant(monkeypatch):
    from atlas_plus import autonomous
    monkeypatch.setenv("ATLAS_INTERACTIVE_WRITES", "off")
    with pytest.raises(RuntimeError) as ei:
        autonomous.require_write("remember", interactive=True)
    assert str(ei.value).startswith("Sextant remember blocked:")
    _no_atlas(str(ei.value))


# --- 6-12: help texts of seven commands ------------------------------------------------------------

@pytest.mark.parametrize("module, attr, words", [
    ("atlas_plus.curation.cli", "app", "Sextant memory curation hygiene tools"),
    ("atlas_plus.tickets.cli", "app", "Sextant lightweight ticket system"),
    ("atlas_plus.temporal_graph.cli", "app", "Query the Sextant temporal knowledge graph."),
    ("atlas_plus.db_backup.cli", "app", "Sextant SQLite state DB backup / restore CLI"),
])
def test_typer_help_says_sextant(module, attr, words, monkeypatch):
    import importlib
    from typer.testing import CliRunner
    monkeypatch.setattr(sys, "argv", ["sextant-x"])
    app = getattr(importlib.import_module(module), attr)
    res = CliRunner().invoke(app, ["--help"], terminal_width=200)
    assert res.exit_code == 0, res.output
    assert words in " ".join(res.output.split())
    _no_atlas(res.output)


@pytest.mark.parametrize("module, words", [
    ("atlas_plus.budget.__main__", "Sextant token-budget preflight"),
    ("atlas_plus.curation.nightly_contradiction_check", "Nightly contradiction check for Sextant .memory/ files."),
    ("atlas_plus.curation.reflexion_loop", "recently written Sextant facts."),
])
def test_argparse_help_says_sextant(module, words, monkeypatch, capsys):
    import importlib
    monkeypatch.setattr(sys, "argv", ["sextant-x", "--help"])
    mod = importlib.import_module(module)
    with pytest.raises(SystemExit) as ei:
        mod.main()
    assert ei.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert words in out
    _no_atlas(out)


# --- 13: the MCP tool descriptions a model and a user read -----------------------------------------

def test_mcp_tool_descriptions_say_sextant():
    from atlas_plus.mcp.memory_server import server
    tools = asyncio.run(server.mcp.list_tools())
    by_name = {t.name: t.description or "" for t in tools}
    assert "Search the Sextant temporal graph" in by_name["temporal_search_tool"]
    assert "'sextant-temporal build --source atlas'" in by_name["temporal_search_tool"]
    assert "project:my-project" in by_name["memory_remember_tool"]
    for name, text in by_name.items():
        hits = sorted({m.group(0) for m in _PRODUCT_WORD.finditer(text)})
        assert not hits, (name, hits)


# --- 14: the dashboard's task export downloads as sextant-tasks.csv --------------------------------

def test_dashboard_export_name_is_sextant():
    from atlas_plus.web import app as webapp
    page = webapp._html("")
    assert "link.download='sextant-tasks.csv'" in page
    assert "atlas-tasks.csv" not in page
    assert webapp.app.title == "Sextant Web UI"


# --- 15: the user agent a remote site sees --------------------------------------------------------

def test_link_checker_user_agent_says_sextant(monkeypatch):
    import httpx
    from atlas_plus.curation import link_check
    seen = {}

    class _Resp:
        status_code = 200
        headers: dict = {}
        is_redirect = False
        next_request = None

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def head(self, url, headers=None, **k):
            seen.update(headers or {})
            return _Resp()

        get = head

    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr("atlas_plus.inbox.summarize._refuse_private_target", lambda *a, **k: None)
    link_check.check_url("https://example-org.example/page")
    assert seen.get("User-Agent", "").startswith("Sextant-LinkChecker/1.0")
    _no_atlas(seen["User-Agent"])


# --- 16: a hook's error line names the Sextant program ---------------------------------------------

def test_pre_compact_error_line_says_sextant(tmp_path, monkeypatch, capsys):
    """The hook entry (`python -m atlas_plus.hooks.pre_compact`) prints one prefixed line, never a traceback."""
    import runpy
    blocker = tmp_path / "state-is-a-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("ATLAS_STATE_DIR", str(blocker))
    monkeypatch.setattr(sys, "argv", ["pre_compact"])
    with pytest.raises(SystemExit) as ei:
        runpy.run_module("atlas_plus.hooks.pre_compact", run_name="__main__")
    assert ei.value.code == 0
    err = capsys.readouterr().err
    assert "[sextant-pre-compact] error: " in err and "[atlas-" not in err
