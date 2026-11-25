"""AG103 (sweep 27, SEX-047, LES-054): failures that named no next step, or printed it on stdout.

Every test runs the fixed path in-process (no console-script names, no child interpreter, no
live service)."""
from __future__ import annotations

import io
import sys

import pytest
from typer.testing import CliRunner


@pytest.fixture()
def sandbox_env(tmp_path, monkeypatch):
    for k in list(__import__("os").environ):
        if k.upper().startswith("ATLAS_"):
            monkeypatch.delenv(k, raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(ws))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(ws / ".memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(ws / ".atlas"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")
    for k in ("ATLAS_BRAIN_URL", "ATLAS_MCP_URL", "ATLAS_OLLAMA_URL", "ATLAS_EMBED_URL"):
        monkeypatch.setenv(k, "http://127.0.0.1:9")
    return ws


# ---- row 1: `sextant update` names the upgrade line that fits this install ----------------------

@pytest.mark.parametrize("kind, want, not_want", [
    ("pypi", "pip install -U sextant-ai", "--force-reinstall"),
    ("git", "--force-reinstall --no-deps git+https://example-org.test/fork/sextant", "pip install -U sextant-ai"),
    ("editable", "git pull", "--force-reinstall"),
])
def test_update_redirect_prints_the_line_for_this_install(monkeypatch, kind, want, not_want):
    from atlas_plus import _next_step, cli
    monkeypatch.setattr(_next_step, "install_kind", lambda: kind)
    # a git install upgrades from the address it was installed from (a fork stays on the fork)
    monkeypatch.setattr(_next_step, "install_source", lambda: "https://example-org.test/fork/sextant")
    res = CliRunner().invoke(cli.app, ["update"])
    text = " ".join(res.output.split())
    assert res.exit_code == 2
    assert want in text, text
    if not_want:
        assert not_want not in text, text
    if kind == "pypi":  # a release install is never sent to a git build; source installs get the page
        assert "git+" not in text and "docs/GETTING-STARTED.md" in text


def test_a_git_install_with_no_recorded_address_still_gets_a_line(monkeypatch):
    from atlas_plus import _next_step
    monkeypatch.setattr(_next_step, "_direct_url", lambda: {"vcs_info": {"vcs": "git"}})
    assert _next_step.install_kind() == "git" and _next_step.install_source() == ""
    assert "git+<the address you installed from>" in _next_step.upgrade_line()
    monkeypatch.setattr(_next_step, "_direct_url", lambda: {"url": "https://example-org.test/x", "vcs_info": {}})
    assert "git+https://example-org.test/x`" in _next_step.upgrade_line()


def test_install_kind_reads_direct_url(monkeypatch):
    import importlib.metadata as md
    from atlas_plus import _next_step

    class _Dist:
        def __init__(self, raw):
            self.raw = raw

        def read_text(self, name):
            return self.raw if name == "direct_url.json" else None

    cases = {'{"url": "https://github.com/x", "vcs_info": {"vcs": "git", "commit_id": "abc"}}': "git",
             '{"url": "file:///C:/Projects/sextant", "dir_info": {"editable": true}}': "editable",
             '{"url": "file:///C:/Projects/sextant", "dir_info": {}}': "folder",
             '{"url": "file:///C:/Projects/sextant", "dir_info": {"editable": false}}': "folder",
             None: "pypi", "not json": "pypi"}
    for raw, kind in cases.items():
        monkeypatch.setattr(md, "distribution", lambda _n, _r=raw: _Dist(_r))
        assert _next_step.install_kind() == kind, raw

    def _missing(_n):
        raise md.PackageNotFoundError(_n)
    monkeypatch.setattr(md, "distribution", _missing)
    assert _next_step.install_kind() == "pypi"


def test_getting_started_shows_the_pypi_upgrade_line():
    from pathlib import Path
    doc = (Path(__file__).resolve().parents[1] / "docs" / "GETTING-STARTED.md").read_text(encoding="utf-8")
    para = doc[doc.index("does not update with `-U`") - 400:doc.index("does not update with `-U`") + 200]
    assert "pip install -U sextant-ai" in para


# ---- row 2: `sextant versions` errors go to stderr, exit 1, end with Next -----------------------

def test_versions_error_goes_to_stderr_with_next(sandbox_env, capsys):
    from atlas_plus import versions_cmd
    mem = sandbox_env / ".memory"
    mem.mkdir()
    (mem / "versions").write_text("not a dir\n", encoding="utf-8")
    rc = versions_cmd.main([])
    out, err = capsys.readouterr()
    assert rc == 1
    assert out.strip() == "", out
    assert "is a file, not a directory" in err
    assert err.strip().splitlines()[-1].startswith("Next:") and "versions --help" in err
    rc = versions_cmd.main(["--json"])
    out, err = capsys.readouterr()
    assert rc == 1 and '"error"' in out and "Next:" in err


# ---- row 3: reset / uninstall refusals go to stderr and name --dry-run and --help ---------------

def _run_main(module_main, argv, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit) as exc:
        module_main()
    out, err = capsys.readouterr()
    return exc.value.code, out, err


@pytest.mark.parametrize("which", ["reset", "uninstall"])
def test_refusal_on_stderr_names_dry_run_and_help(sandbox_env, monkeypatch, capsys, which):
    from atlas_plus import init_cmd, reset_cmd, uninstall_cmd
    init_cmd.run_init(dry_run=False)
    note = sandbox_env / ".memory" / "keep.md"
    note.write_text("keep me\n", encoding="utf-8")
    main = reset_cmd.main if which == "reset" else uninstall_cmd.main
    code, out, err = _run_main(main, [f"sextant-{which}", "--corpus"], monkeypatch, capsys)
    assert code == 1
    assert "refused" not in out.lower(), out
    assert "refused" in err.lower(), err
    nxt = err.strip().splitlines()[-1]
    assert nxt.startswith("Next:") and "--dry-run" in nxt and "--help" in nxt, nxt
    assert "--yes" not in nxt, nxt  # never a destructive command as the next step
    assert note.exists()


def test_reset_dry_run_is_a_safe_preview(sandbox_env, monkeypatch, capsys):
    from atlas_plus import init_cmd, reset_cmd
    init_cmd.run_init(dry_run=False)
    note = sandbox_env / ".memory" / "keep.md"
    note.write_text("keep me\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["sextant-reset", "--all", "--yes", "--dry-run"])
    assert reset_cmd.main() is None
    out = capsys.readouterr().out
    assert "--dry-run: nothing was changed" in out
    assert note.read_text(encoding="utf-8") == "keep me\n"


# ---- row 4: `sextant context` before init: a person gets a pointer, a hook gets the same bytes ----

class _TtyText(io.TextIOWrapper):
    def isatty(self):
        return True


def _bytes_out(tty: bool):
    cls = _TtyText if tty else io.TextIOWrapper
    return cls(io.BytesIO(), encoding="utf-8", write_through=True)


def _session_start(monkeypatch, capsys_unused, sandbox_env, stdout):
    from atlas_plus.hooks import session_start
    monkeypatch.setattr(session_start, "MEMORY_ROOT", sandbox_env / ".memory")
    monkeypatch.setattr(sys, "argv", ["sextant-session-start"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b""), encoding="utf-8"))
    err = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", err)
    rc = session_start.main()
    monkeypatch.undo()
    stdout.flush()
    return rc, stdout.buffer.getvalue().decode("utf-8"), err.getvalue()


def test_context_before_init_tty_gets_a_pointer_and_pipe_is_unchanged(sandbox_env, monkeypatch, capsys):
    rc_tty, out_tty, err_tty = _session_start(monkeypatch, capsys, sandbox_env, _bytes_out(True))
    # monkeypatch.undo() inside cleared the env: set it again for the second run
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(sandbox_env))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(sandbox_env / ".memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(sandbox_env / ".atlas"))
    rc_pipe, out_pipe, err_pipe = _session_start(monkeypatch, capsys, sandbox_env, _bytes_out(False))
    assert rc_tty is None and rc_pipe is None
    assert "(no corpus yet: run `" in err_tty and "init`)" in err_tty, err_tty
    assert "no corpus yet" not in err_pipe and "no corpus yet" not in out_pipe
    strip = lambda s: [ln for ln in s.splitlines() if not ln.startswith(("Now:", "Time:", "Clock:"))]  # noqa: E731
    assert len(strip(out_tty)) == len(strip(out_pipe))  # same context text either way


# ---- row 5: `sextant search ""` backticks the command -------------------------------------------

def test_empty_search_backticks_the_command(sandbox_env):
    from atlas_plus import cli
    res = CliRunner().invoke(cli.app, ["search", ""])
    assert res.exit_code == 2
    assert '`sextant search "<words>"`' in res.output, res.output


# ---- row 6 (found by the wider G8): `atlas-pre-compact --help` ran the hook and printed nothing ----

def test_pre_compact_help_prints_usage_and_writes_nothing(sandbox_env, monkeypatch, capsys, tmp_path):
    from atlas_plus.hooks import pre_compact
    state = tmp_path / "state-probe"
    monkeypatch.setattr(pre_compact, "STATE_DIR", state)
    monkeypatch.setattr(sys, "argv", ["atlas-pre-compact", "--help"])
    assert pre_compact.main() is None
    assert "Usage: atlas-pre-compact" in capsys.readouterr().out
    assert not state.exists()


def test_a_copy_installed_from_a_folder_gets_an_upgrade_line_that_works(monkeypatch):
    """MN93 F2: `pip install .` in a clone records a file: address with an empty dir_info; `sextant upgrade`
    printed `pip install -U sextant-ai`, which answers "Requirement already satisfied" for such a copy. Fails
    before: the line named PyPI."""
    import importlib.metadata as md

    from atlas_plus import _next_step

    class _Dist:
        def __init__(self, raw):
            self.raw = raw

        def read_text(self, name):
            return self.raw if name == "direct_url.json" else None

    monkeypatch.setattr(md, "distribution", lambda _n: _Dist('{"url": "file:///srv/src/sextant", "dir_info": {}}'))
    line = _next_step.upgrade_line()
    assert _next_step.install_kind() == "folder"
    assert "git pull" in line and "pip install -U ." in line and "sextant" in line.split("`git pull` in ", 1)[1]
    assert _next_step.PYPI_UPGRADE not in line
    monkeypatch.setattr(md, "distribution", lambda _n: _Dist('{"url": "not-a-file-address", "dir_info": {}}'))
    assert "in the folder you installed from" in _next_step.upgrade_line()
