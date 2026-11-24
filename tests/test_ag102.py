"""AG102 (sweep 26): every failure names the next step (LES-054). Each test runs the fixed path and
reads the LAST lines a first-timer sees. The tree-wide rule is G8 in tests/test_guardrails.py."""
from __future__ import annotations

import importlib
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _last(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


_ORIGINAL_NAMES = {"sextant": "atlas-plus", "sextant-mcp": "atlas-memory-mcp", "sextant-web": "atlas-web-ui"}


def _entry(name: str):
    """What the console script `name` runs, read from pyproject (the installed wrapper calls exactly this)."""
    scripts = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    if name not in scripts:  # a tree whose pyproject has only the atlas-* originals of the sextant-* names
        name = _ORIGINAL_NAMES.get(name, name.replace("sextant-", "atlas-", 1))
    target = scripts[name]
    mod, _, attr = target.partition(":")
    return getattr(importlib.import_module(mod), attr)


def _run(monkeypatch, argv: list[str], func) -> int:
    monkeypatch.setattr(sys, "argv", argv)
    # a console-script wrapper has no module spec; under `python -m pytest` Python 3.14's argparse would
    # name the program "python -m pytest" instead of argv[0]
    monkeypatch.setattr(sys.modules["__main__"], "__spec__", None, raising=False)
    with pytest.raises(SystemExit) as exc:
        rv = func()
        raise SystemExit(rv)
    code = exc.value.code
    return 0 if code is None else (code if isinstance(code, int) else 1)


@pytest.fixture
def no_corpus(tmp_path, monkeypatch):
    root = tmp_path / "nothing-here" / ".memory"
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "nothing-here" / ".atlas"))
    return root


def test_argparse_usage_error_ends_with_the_help_pointer(monkeypatch, capsys):
    rc = _run(monkeypatch, ["sextant-init", "--no-such-option"], _entry("sextant-init"))
    err = capsys.readouterr().err
    assert rc == 2
    assert "unrecognized arguments: --no-such-option" in err
    assert _last(err) == "Run `sextant-init --help` to see what it takes."


def test_argparse_subcommand_parser_inherits_the_pointer(monkeypatch, capsys):
    rc = _run(monkeypatch, ["sextant-sync", "status", "--no-such-option"], _entry("sextant-sync"))
    last = _last(capsys.readouterr().err)
    assert rc == 2 and last.startswith("Run `sextant-sync") and "--help`" in last


@pytest.mark.parametrize("argv, want", [
    (["sextant", "serch", "x"], "Run `sextant --help` to see every command and option."),
    (["sextant", "search"], "Run `sextant search --help` to see what it takes; `sextant --help` lists every command."),
    (["sextant", "remember", "--no-such-option"], "Run `sextant remember --help`"),
    (["sextant", "--no-such-option"], "Run `sextant --help`"),
])
def test_click_usage_errors_end_with_the_help_pointer(monkeypatch, capsys, argv, want):
    from atlas_plus import cli

    # prog_name pinned: under `python -m pytest` Click would name the program after pytest's __main__
    rc = _run(monkeypatch, argv, lambda: cli.app(args=argv[1:], prog_name="sextant"))
    err = capsys.readouterr().err
    assert rc == 2 and "Traceback" not in err
    assert _last(err).startswith(want)


def test_every_typer_app_gets_the_pointer(monkeypatch, capsys):
    """The sibling Typer apps (backup, transcripts, review, tickets ...) share the class."""
    rc = _run(monkeypatch, ["sextant-backup"], lambda: _entry("sextant-backup")(args=[], prog_name="sextant-backup"))
    err = capsys.readouterr().err
    assert rc == 2 and "Missing command" in err and _last(err) == "Run `sextant-backup --help` to see every command and option."


def test_a_delegated_command_exit_zero_stays_zero(monkeypatch, capsys, tmp_path):
    """The wrapper resolves its exception classes before any except clause: a missing attribute in
    one turned `sextant init`'s SystemExit(0) into a traceback while this lane built it."""
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "m"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "s"))
    from atlas_plus import cli

    assert _run(monkeypatch, ["sextant", "init", "--dry-run"], cli.main) == 0
    assert "Traceback" not in capsys.readouterr().err


def test_guessed_command_redirect_goes_to_stderr(monkeypatch, capsys):
    from atlas_plus import cli

    rc = _run(monkeypatch, ["sextant", "install"], cli.main)
    out, err = capsys.readouterr()
    assert rc == 2 and out.strip() == ""
    assert "No such command 'install'" in err and "`sextant init`" in err


def test_search_before_init_says_init_on_stderr(monkeypatch, capsys, no_corpus):
    from atlas_plus import cli

    rc = _run(monkeypatch, ["sextant", "search", "anything"], cli.main)
    out, err = capsys.readouterr()
    assert rc == 1 and "No corpus yet" in err and "No corpus yet" not in out
    assert "init` first" in _last(err) and not no_corpus.exists()


def test_verify_before_init_ends_with_init(monkeypatch, capsys, no_corpus):
    monkeypatch.setenv("ATLAS_VERIFY_TIMEOUT", "60")
    from atlas_plus import roundtrip

    rc = roundtrip.main(["--skip-remember"])
    out, err = capsys.readouterr()
    assert rc == 1 and _last(out) == "ROUND-TRIP: FAILED"  # the documented machine line stays
    assert _last(err).startswith("Next: no corpus yet at ") and "init`, then `" in _last(err)


def test_verify_failure_elsewhere_points_at_doctor(monkeypatch, capsys):
    from atlas_plus import roundtrip

    rc = roundtrip.main(["--url", "http://127.0.0.1:9/mcp"])
    err = capsys.readouterr().err
    assert rc == 1 and "doctor`" in _last(err)


def test_doctor_report_ends_with_the_fix(no_corpus):
    from atlas_plus.doctor.cli import render_text

    text = render_text({"ts": "2026-11-23T00:00:00", "memory_corpus": {
        "exists": False, "root": str(no_corpus), "hint": "memory root does not exist -- run sextant-init"}})
    assert _last(text) == "Next: memory root does not exist -- run sextant-init"
    assert _last(render_text({"ts": "x", "memory_corpus": {"exists": True, "md_files": 3, "root": "r"}})) == "Generated: x"


def test_narrative_help_prints_help_and_runs_nothing(monkeypatch, capsys):
    from atlas_plus import session_narrative

    def _boom(*a, **k):
        raise AssertionError("--help ran the report")

    monkeypatch.setattr(session_narrative, "run_narrative", _boom)
    assert _run(monkeypatch, ["atlas-narrative", "--help"], _entry("atlas-narrative")) == 0
    assert "--days" in capsys.readouterr().out


def test_narrative_written_report_exits_zero(monkeypatch, capsys):
    from atlas_plus import session_narrative

    monkeypatch.setattr(session_narrative, "run_narrative", lambda since_days=14: {"ok": True, "week": "w"})
    assert _run(monkeypatch, ["atlas-narrative"], _entry("atlas-narrative")) == 0


def test_fts_rebuild_before_init_creates_nothing(monkeypatch, capsys, tmp_path):
    from atlas_plus.mcp.memory_server import fts

    monkeypatch.setattr(fts, "MEMORY_ROOT", tmp_path / "none" / ".memory")
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "search.sqlite")
    rc = _run(monkeypatch, ["sextant-fts-rebuild"], fts.main)
    err = capsys.readouterr().err
    assert rc == 1 and "init` first" in _last(err)
    assert not (tmp_path / "state").exists()


def test_embed_index_before_init_names_init(monkeypatch, capsys, tmp_path):
    from atlas_plus.mcp.memory_server import config, embeddings

    monkeypatch.setattr(config, "MEMORY_ROOT", tmp_path / "none" / ".memory")
    calls = []
    monkeypatch.setattr(embeddings, "rebuild_index", lambda **k: calls.append(k) or {"failed": 0, "added": 0, "updated": 0})
    rc = _run(monkeypatch, ["sextant-embed-index"], embeddings.main)
    assert rc == 1 and "init` first" in _last(capsys.readouterr().err) and calls == []


def test_import_missing_path_says_what_to_give(monkeypatch, capsys):
    rc = _run(monkeypatch, ["sextant-import", r"C:\Projects\no-such-vault-ag102"], _entry("sextant-import"))
    err = capsys.readouterr().err
    assert rc == 1 and "path not found" in err and "`sextant-import --help`" in _last(err)


def test_mcp_http_without_token_points_at_setup(monkeypatch, capsys):
    from atlas_plus.mcp.memory_server import server

    monkeypatch.setattr(server, "_MCP_TOKEN", "")
    rc = _run(monkeypatch, ["sextant-mcp"], server.main)
    err = capsys.readouterr().err
    assert rc == 1 and "setup`" in _last(err) and "stdio" in _last(err)


def test_web_without_key_ends_with_the_docs_page(monkeypatch, capsys):
    from atlas_plus.web import app as webapp

    monkeypatch.setattr("atlas_plus.web.app.BRAIN_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(webapp, "API_KEY", "")
    monkeypatch.setattr(webapp, "_ALLOW_NO_AUTH", False)
    rc = _run(monkeypatch, ["sextant-web"], webapp.main)
    assert rc == 1 and "docs/GETTING-STARTED.md" in _last(capsys.readouterr().err)


def test_sync_without_subcommand_backticks_the_step(monkeypatch, capsys):
    rc = _run(monkeypatch, ["sextant-sync"], _entry("sextant-sync"))
    assert rc == 2 and "`sextant-sync status`" in _last(capsys.readouterr().err)


def test_prime_usage_ends_with_an_example(monkeypatch, capsys):
    rc = _run(monkeypatch, ["atlas-prime"], _entry("atlas-prime"))
    assert rc == 1 and _last(capsys.readouterr().err) == 'atlas-prime "fix the login bug"'


def test_root_help_ends_with_where_to_go_next():
    from typer.testing import CliRunner

    from atlas_plus.cli import app

    res = CliRunner().invoke(app, ["--help"])
    assert res.exit_code == 0
    tail = " ".join(res.output.split()[-40:])
    assert "`sextant doctor`" in tail and "docs/GETTING-STARTED.md" in tail


def test_python_m_atlas_plus_speaks_sextant_on_the_public_install(monkeypatch):
    from atlas_plus import _next_step, cli

    monkeypatch.setattr(sys, "argv", [str(Path(cli.__file__).with_name("__main__.py"))])
    monkeypatch.setattr(cli, "_dist_name", lambda: "sextant-ai")
    assert cli._brand() == ("Sextant", "sextant")
    monkeypatch.setattr(sys, "argv", ["atlas-plus"])
    assert cli._brand() == ("Keel", "atlas") and _next_step.umbrella() == "atlas-plus"
