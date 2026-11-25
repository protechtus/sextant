"""AG104 (sweep 27, SEX-048, MN88's Linux walls): key hints a POSIX shell runs and "set but empty";
control characters in printed paths escaped; a refused write names the folder and the next step,
and doctor probes that the roots are writable. In-process, no console-script name, no service."""
from __future__ import annotations

import logging
import shlex
import sys
from pathlib import Path

import pytest

SPACED = "/opt/my env/bin/python3"
CODE = "import secrets; print(secrets.token_urlsafe(32))"


@pytest.fixture
def posix_hint(monkeypatch):
    from atlas_plus import _next_step
    monkeypatch.setattr(_next_step, "_windows", lambda: False)
    monkeypatch.setattr(sys, "executable", SPACED)
    return _next_step


def _runnable(text: str) -> bool:
    """The hint inside `text` is a command a POSIX shell splits back into the interpreter."""
    start = text.index(shlex.quote(SPACED))
    return shlex.split(text[start:].split(CODE, 1)[0] + CODE + '"') == [SPACED, "-c", CODE]


def _run_main(monkeypatch, argv, func) -> int:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys.modules["__main__"], "__spec__", None, raising=False)
    with pytest.raises(SystemExit) as exc:
        func()
    code = exc.value.code
    return 0 if code is None else (code if isinstance(code, int) else 1)


def test_keygen_command_and_key_missing(posix_hint, monkeypatch):
    assert shlex.split(posix_hint.keygen_command()) == [SPACED, "-c", CODE]
    monkeypatch.setattr(posix_hint, "_windows", lambda: True)
    assert posix_hint.keygen_command() == f'python -c "{CODE}"'  # Windows: unchanged
    monkeypatch.delenv("ATLAS_AG104_KEY", raising=False)
    assert posix_hint.key_missing("ATLAS_AG104_KEY") == "ATLAS_AG104_KEY is not set"
    monkeypatch.setenv("ATLAS_AG104_KEY", "")
    assert posix_hint.key_missing("ATLAS_AG104_KEY").startswith("ATLAS_AG104_KEY is set but empty")


def test_web_empty_and_short_key(monkeypatch, capsys, posix_hint):
    from atlas_plus.web import app as web
    monkeypatch.setenv("ATLAS_WEB_API_KEY", "")
    monkeypatch.setattr(web, "API_KEY", "")
    monkeypatch.setattr(web, "_ALLOW_NO_AUTH", False)
    assert _run_main(monkeypatch, ["sextant-web"], web.main) == 1
    err = capsys.readouterr().err
    assert "ATLAS_WEB_API_KEY is set but empty" in err and "$(python -c" not in err
    assert _runnable(next(ln for ln in err.splitlines() if "export ATLAS_WEB_API_KEY=" in ln))
    monkeypatch.setattr(web, "API_KEY", "short")
    assert _run_main(monkeypatch, ["sextant-web"], web.main) == 1
    assert _runnable(capsys.readouterr().err)


@pytest.mark.parametrize("modname, attr, var, argv", [
    ("atlas_plus.mcp.memory_server.server", "_MCP_TOKEN", "ATLAS_MCP_TOKEN", "sextant-mcp"),
    ("atlas_plus.brain.server", "_API_KEY", "ATLAS_BRAIN_API_KEY", "sextant-brain"),
])
def test_servers_with_an_empty_key_say_empty(monkeypatch, caplog, capsys, posix_hint, modname, attr, var, argv):
    import importlib
    mod = importlib.import_module(modname)
    monkeypatch.setenv(var, "")
    monkeypatch.setattr(mod, attr, "")
    with caplog.at_level(logging.ERROR):
        assert _run_main(monkeypatch, [argv], mod.main) == 1
    # the key lines go to stderr outside the log record since AG106 (a log record folds them)
    err = capsys.readouterr().err
    assert f"{var} is set but empty" in err
    assert _runnable(next(ln for ln in err.splitlines() if f"export {var}=" in ln))


def test_safe_escapes_control_characters():
    from atlas_plus._term import safe
    assert safe("notes/a\nb\x1b[31m.md") == "notes/a\\nb\\x1b[31m.md"
    assert safe("ok/\u65e5\u672c.md") == "ok/\u65e5\u672c.md"
    assert "\n" not in safe("x\u2028y\r\n")


def test_search_and_doctor_print_a_newline_in_a_path_as_an_escape(monkeypatch, tmp_path):
    from typer.testing import CliRunner
    from atlas_plus import cli
    from atlas_plus.doctor import cli as doctor
    from atlas_plus.mcp.memory_server.tools import search as search_tools
    (tmp_path / "m").mkdir()
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "m"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "s"))
    fake = "reference/evil\n  reference/fake.md  injected\x1b[2J.md"
    monkeypatch.setattr(search_tools, "memory_search_hybrid",
                        lambda **kw: ([{"path": fake, "snippet": "hello", "score": 0.9}], "fts-only"))
    res = CliRunner().invoke(cli.app, ["search", "hello"], prog_name="sextant")
    assert res.exit_code == 0 and "\x1b[2J" not in res.output
    hit = [ln for ln in res.output.splitlines() if "evil" in ln]
    assert len(hit) == 1 and "evil\\n" in hit[0] and "fake.md" in hit[0]
    out = doctor.render_text({"permissions": {"loose": [{"path": "/h/.memory\nFAKE", "mode": "0755", "fix": "x"}]}})
    assert "/h/.memory\\nFAKE" in out


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    from atlas_plus import cli
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / ".memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))
    for name in ("ATLAS_EMBED_URL", "ATLAS_OLLAMA_URL", "OLLAMA_HOST"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    assert _run_main(monkeypatch, ["sextant", "init"], cli.main) == 0
    return tmp_path / ".memory"


def _deny(*a, **k):
    raise PermissionError(13, "Permission denied", str(Path("C:/Projects/corpus/.memory/reference/notes.md")))


def test_refused_write_names_the_folder_and_next_step(monkeypatch, corpus, capsys):
    """CLI remember, and the MCP tool whose error `sextant verify` prints."""
    from typer.testing import CliRunner
    from atlas_plus import cli
    from atlas_plus.mcp.memory_server.tools import remember
    capsys.readouterr()
    monkeypatch.setattr(remember, "append_fact", _deny)
    res = CliRunner().invoke(cli.app, ["remember", "the sky is blue"], prog_name="sextant")
    text = " ".join(res.output.split())
    assert res.exit_code == 1 and "[Errno 13]" not in text
    assert "cannot write in" in text and "reference" in text and "Next:" in text and "doctor" in text
    out = remember.memory_remember("reference:notes", "the sky is blue", source="agent", interactive=True)
    assert out["ok"] is False and "cannot write in" in out["error"] and "doctor" in out["error"]


def test_doctor_reports_an_unwritable_root(monkeypatch, corpus):
    import tempfile
    from atlas_plus.doctor import cli as doctor
    monkeypatch.setattr(doctor, "MEMORY_ROOT", corpus)
    good = doctor.check_writable()
    assert good["ok"] is True and "corpus" in {r["what"] for r in good["roots"]}
    assert not [p for p in corpus.iterdir() if p.name.startswith(".sextant-write-probe-")]

    def refuse(*a, dir=None, **k):
        raise PermissionError(13, "Permission denied", str(Path(dir) / ".sextant-write-probe-x"))
    monkeypatch.setattr(tempfile, "mkstemp", refuse)
    bad = doctor.check_writable()
    out = doctor.render_text({"writable": bad})
    assert bad["ok"] is False and "FAIL" in out and "cannot be written" in out and "doctor" in out
