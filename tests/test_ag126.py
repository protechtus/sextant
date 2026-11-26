"""AG126 (lesson LES-082, ticket SEX-048): one console check before any question.

Without a console no question is printed; a piped line still answers; an empty or closed stdin
ends with a next step (never a hang, never an EOFError traceback). On Windows the NUL device passes
isatty(), so the console check is the console mode, in `atlas_plus._prompt.is_terminal`.
"""
from __future__ import annotations

import io
import json
import sys

import pytest
from typer.testing import CliRunner


class _FakeNul(io.StringIO):
    """What `< NUL` looks like to Python on Windows: isatty() is True, and it reads as empty."""

    def isatty(self):
        return True

    def fileno(self):
        raise OSError("not a console")


# --- the helper --------------------------------------------------------------------------------

def test_is_terminal_refuses_a_nul_like_stream():
    from atlas_plus import _prompt
    assert _prompt.is_terminal(_FakeNul("")) is False
    assert _prompt.is_terminal(io.StringIO("")) is False
    assert _prompt.is_terminal(None) is False


def test_uninstall_uses_the_same_check():
    from atlas_plus import _prompt, uninstall_cmd
    assert uninstall_cmd._is_terminal is _prompt.is_terminal


@pytest.mark.parametrize("line,expected", [("y\n", True), ("yes\n", True), ("Y\r\n", True),
                                           ("n\n", False), ("maybe\n", False), ("\n", False)])
def test_confirm_reads_a_piped_line_without_printing_the_question(monkeypatch, capsys, line, expected):
    from atlas_plus import _prompt
    monkeypatch.setattr(sys, "stdin", io.StringIO(line))
    assert _prompt.confirm("QUESTION-7421?", rerun="`x --yes`") is expected
    out = capsys.readouterr()
    assert "QUESTION-7421" not in out.out + out.err


@pytest.mark.parametrize("stdin", [io.StringIO(""), _FakeNul(""), None])
def test_confirm_on_no_answer_names_the_rerun_and_exits_1(monkeypatch, capsys, stdin):
    import typer
    from atlas_plus import _prompt
    monkeypatch.setattr(sys, "stdin", stdin)
    with pytest.raises(typer.Exit) as ei:
        _prompt.confirm("QUESTION-7421?", rerun="`tool thing --yes`", nothing="nothing was thinged",
                        without="to thing without it")
    assert ei.value.exit_code == 1
    out = capsys.readouterr()
    assert "QUESTION-7421" not in out.out + out.err
    assert "Aborted: nothing was thinged. Next: answer y at the question, or run `tool thing --yes` " \
           "to thing without it." in out.err


# --- the three confirmation sites: no question text without a console (LES-082) -----------------

def _archive_corpus(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    mem = root / ".memory"
    (mem / "reference").mkdir(parents=True)
    (mem / "reference" / "a.md").write_text("# a\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(root / ".atlas"))
    return mem


def test_review_archive_closed_stdin_prints_no_question(tmp_path, monkeypatch):
    from atlas_plus.curation.cli import app
    mem = _archive_corpus(tmp_path, monkeypatch)
    res = CliRunner().invoke(app, ["archive", "reference/a.md"], input="")
    assert res.exit_code == 1
    assert "Continue?" not in res.output
    assert "nothing was archived" in res.output and "--yes" in res.output
    assert (mem / "reference" / "a.md").exists()


def test_review_archive_piped_n_prints_no_question_and_keeps_the_file(tmp_path, monkeypatch):
    from atlas_plus.curation.cli import app
    mem = _archive_corpus(tmp_path, monkeypatch)
    res = CliRunner().invoke(app, ["archive", "reference/a.md"], input="n\n")
    assert res.exit_code == 1
    assert "Continue?" not in res.output
    assert "nothing was archived" in res.output
    assert (mem / "reference" / "a.md").exists()


def test_temporal_reset_closed_stdin_prints_no_question(tmp_path, monkeypatch):
    from atlas_plus.temporal_graph import cli
    called = []
    monkeypatch.setattr(cli, "_get_provider", lambda root: called.append(root))
    res = CliRunner().invoke(cli.app, ["reset", "--graph-root", str(tmp_path / "g")], input="")
    assert res.exit_code == 1
    assert "Reset all derived" not in res.output
    assert "reset --yes" in res.output and not called


def test_temporal_reset_piped_y_still_resets(tmp_path, monkeypatch):
    from atlas_plus.temporal_graph import cli

    class _P:
        def reset_derived_state(self):
            called.append(True)
    called = []
    monkeypatch.setattr(cli, "_get_provider", lambda root: _P())
    res = CliRunner().invoke(cli.app, ["reset", "--graph-root", str(tmp_path / "g")], input="y\n")
    assert res.exit_code == 0, res.output
    assert called == [True]


def test_db_restore_closed_stdin_prints_no_question(tmp_path, monkeypatch):
    from atlas_plus.db_backup import cli
    snap = tmp_path / "snap.sqlite"
    snap.write_bytes(b"x")
    called = []
    monkeypatch.setattr(cli, "restore", lambda **kw: called.append(kw))
    res = CliRunner().invoke(cli.app, ["restore", str(snap), "--dest", str(tmp_path / "d.sqlite")], input="")
    assert res.exit_code == 1
    assert "Proceed with restore?" not in res.output
    assert "restore <snapshot> --yes" in res.output and not called


# --- the input() sites: a next step, not a hang or an EOFError traceback -----------------------

def _one_candidate():
    return [{"scope": "reference:test", "title": "t", "fact": "a fact worth keeping", "confidence": 0.5}]


def test_suggest_facts_review_on_a_closed_stdin_names_the_next_step(monkeypatch, capsys):
    from atlas_plus.curation import suggest_facts as sf
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    writes = []
    monkeypatch.setattr(sf, "_write_fact", lambda *a, **k: writes.append(a))
    stats = sf._review_candidates(_one_candidate(), corpus=[])
    out = capsys.readouterr()
    assert stats["written"] == 0 and not writes
    assert "[a]pprove" not in out.out + out.err, "the question was printed to nobody"
    assert "Next:" in out.err and "--auto" in out.err


def test_suggest_facts_review_piped_answer_still_works(monkeypatch, capsys):
    from atlas_plus.curation import suggest_facts as sf
    monkeypatch.setattr(sys, "stdin", io.StringIO("s\n"))
    monkeypatch.setattr(sf, "_write_fact", lambda *a, **k: None)
    stats = sf._review_candidates(_one_candidate(), corpus=[])
    assert stats["skipped"] == 1


def test_suggest_facts_edit_then_closed_stdin_ends_with_the_next_step(monkeypatch, capsys):
    from atlas_plus.curation import suggest_facts as sf
    monkeypatch.setattr(sys, "stdin", io.StringIO("e\n"))
    monkeypatch.setattr(sf, "_write_fact", lambda *a, **k: None)
    stats = sf._review_candidates(_one_candidate(), corpus=[])
    assert stats["written"] == 0
    assert "Next:" in capsys.readouterr().err


def test_summary_review_on_a_closed_stdin_is_a_refusal_not_a_traceback(monkeypatch, capsys):
    from atlas_plus.summarizer import review as rv
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    accepted, _s, _u = rv.review("LOG", "UPD")  # EOFError on H
    assert accepted is False
    err = capsys.readouterr().err
    assert "nothing was written" in err and "--auto-commit" in err


def test_summary_review_piped_a_accepts(monkeypatch):
    from atlas_plus.summarizer import review as rv
    monkeypatch.setattr(sys, "stdin", io.StringIO("a\n"))
    assert rv.review("LOG", "UPD")[0] is True


# --- SEX-048 last rows -------------------------------------------------------------------------

@pytest.mark.parametrize("windows", [True, False])
def test_setup_key_hint_names_this_interpreter_never_a_bare_python(monkeypatch, windows):
    from atlas_plus import _next_step, setup_cmd
    exe = "C:\\Projects\\My Env\\Scripts\\python.exe" if windows else "/home/user/my env/bin/python"
    monkeypatch.setattr(_next_step, "_windows", lambda: windows)
    monkeypatch.setattr(_next_step.sys, "executable", exe)
    hint = setup_cmd._keygen()
    assert "My Env" in hint or "my env" in hint
    assert not hint.lstrip("& '\"").startswith("python -c"), hint
    assert " -c " in hint
    if windows:
        for shell in ("powershell", "cmd", "posix"):
            assert not _next_step.keygen_command(shell).startswith("python "), shell


def test_doctor_json_keeps_the_true_path_and_the_human_line_escapes_it(monkeypatch, capsys):
    from atlas_plus.doctor import cli
    rlo = chr(0x202E)
    raw = "reference/old" + rlo + "dm.md"
    data = {"orphans": {"candidates": 1, "top": [{"path": raw, "age_days": 400, "score": 0.1}]}}
    monkeypatch.setattr(cli, "collect", lambda: data)
    monkeypatch.setattr(sys, "argv", ["doctor", "--json"])
    cli.main()
    printed = json.loads(capsys.readouterr().out)
    # JSON is data, not display: the TRUE path, unescaped, so a script can open the file
    assert printed["orphans"]["top"][0]["path"] == raw
    human = cli.render_text(data)
    assert rlo not in human
    assert "\\u202e" in human
