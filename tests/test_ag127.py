"""AG127 (sweep 39): a child that runs another party's program gets an allowlisted environment (LES-079, LES-053).

A fake program written by the test dumps the NAMES of its environment to a file; secret-bearing settings are set
under both spellings (`ATLAS_*` and `SEXTANT_*`, which the package adopts as each other at import). Three different
callers that start another party's program must hand it no secret name. Values are sentinels, never real secrets.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from atlas_plus import _proc

# secret-looking names are assembled from pieces (no real secret anywhere)
_SECRETS = [
    "ATLAS_" + "MCP_" + "TOK" + "EN",
    "SEXTANT_" + "WEB_API_" + "KEY",
    "ATLAS_" + "TELEGRAM_BOT_" + "TOK" + "EN",
    "SEXTANT_" + "SYNC_DEVICE_" + "TOK" + "EN",
]
_SENTINEL = "sentinel-" + "not-a-value"


def _fake_program(tmp_path: Path) -> tuple[Path, Path]:
    """A program on disk that writes its environment's names (JSON) to a fixed file and prints `ok`."""
    out = tmp_path / "names.json"
    dump = tmp_path / "dump_env.py"
    dump.write_text(
        "import json, os, sys\n"
        f"open({str(out)!r}, 'w', encoding='utf-8').write(json.dumps(sorted(os.environ)))\n"
        "sys.stdout.write('ok')\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        prog = tmp_path / "fakeprog.cmd"
        prog.write_text(f'@"{sys.executable}" -I "{dump}"\r\n', encoding="utf-8")
    else:  # Linux and macOS: an executable shell script
        prog = tmp_path / "fakeprog"
        prog.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{dump}"\n', encoding="utf-8")
        prog.chmod(prog.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return prog, out


@pytest.fixture
def secrets_env(monkeypatch):
    for name in _SECRETS:
        monkeypatch.setenv(name, _SENTINEL)
    monkeypatch.setenv("ATLAS_" + "WORKSPACE_ROOT", os.path.join(os.sep, "home", "user", "ws"))
    return _SECRETS


def _names(out: Path) -> set[str]:
    assert out.is_file(), "the fake program did not run"
    return {n.upper() for n in json.loads(out.read_text(encoding="utf-8"))}


def _assert_clean(out: Path) -> None:
    names = _names(out)
    leaked = sorted(n for n in names if n in {s.upper() for s in _SECRETS})
    assert not leaked, f"secret-bearing names reached another party's program: {leaked}"
    assert "PATH" in names  # the allowlist keeps what a program needs to run


# ---- the helper ---------------------------------------------------------------------------------------------


def test_helper_keeps_the_allowlist_and_drops_both_spellings():
    base = {"PATH": "/usr/bin", "HOME": "/home/user", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TMPDIR": "/tmp",
            "ATLAS_WORKSPACE_ROOT": "/home/user/ws", "SEXTANT_STATE_DIR": "/home/user/st",
            "AWS_" + "SECRET_ACCESS_" + "KEY": _SENTINEL, "GIT_DIR": "/x", "RCLONE_CONFIG": "/y"}
    for name in _SECRETS:
        base[name] = _SENTINEL
    env = _proc.other_party_env(base=base)
    assert set(env) == {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}
    env = _proc.other_party_env(base=base, keep_prefixes=("GIT_",), extra={"PYTHONUTF8": "1"})
    assert set(env) == {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "GIT_DIR", "PYTHONUTF8"}
    env = _proc.other_party_env(base=base, settings=True)
    assert "ATLAS_WORKSPACE_ROOT" in env and "SEXTANT_STATE_DIR" in env  # a client that starts this package
    assert not set(_SECRETS) & set(env)  # never a secret, under either spelling
    assert "AWS_" + "SECRET_ACCESS_" + "KEY" not in env


def test_helper_drops_lowercase_spellings_too():
    env = _proc.other_party_env(base={"Path": "C:\\Windows", "atlas_mcp_" + "token": _SENTINEL}, settings=True)
    assert env == {"Path": "C:\\Windows"}  # Windows names are case-insensitive; a lowercase secret is still a secret


# ---- three callers that start another party's program ---------------------------------------------------------


def test_brain_agent_claude_cli_gets_no_secret(tmp_path, monkeypatch, secrets_env):
    from atlas_plus.brain import agents

    prog, out = _fake_program(tmp_path)
    monkeypatch.setattr(agents, "CLAUDE_CLI", str(prog))
    agent = agents.Agent(name="fake", model="m", agent_type="claude-cli", capabilities=[], description="d")
    reply = agents.dispatch(agent, "hello", session_id="s", timeout=60)
    assert "ok" in reply
    _assert_clean(out)
    assert "ATLAS_WORKSPACE_ROOT" in _names(out)  # a client program starts this package: settings stay


def test_brain_agent_opencode_cli_gets_no_secret(tmp_path, monkeypatch, secrets_env):
    from atlas_plus.brain import agents

    prog, out = _fake_program(tmp_path)
    monkeypatch.setattr(agents, "OPENCODE_CLI", str(prog))
    agent = agents.Agent(name="fake", model="m", agent_type="opencode-cli", capabilities=[], description="d")
    agents.dispatch(agent, "hello", session_id="s", timeout=60)
    _assert_clean(out)


def test_telegram_job_poller_gets_no_secret(tmp_path, monkeypatch, secrets_env):
    from types import SimpleNamespace

    from atlas_plus.telegram import job_poller

    prog, out = _fake_program(tmp_path)
    monkeypatch.setattr(job_poller, "CLAUDE_CLI", str(prog))
    ok, text = job_poller._run_claude(SimpleNamespace(id="T-1", title="t", body="b"))
    assert ok, text
    _assert_clean(out)


def test_run_bounded_with_the_helper_end_to_end(tmp_path, secrets_env):
    prog, out = _fake_program(tmp_path)
    res = _proc.run_bounded([str(prog)], timeout=60, max_bytes=1024, env=_proc.other_party_env())
    assert res.returncode == 0
    _assert_clean(out)


# ---- item 2: a fetch never follows a redirect unguarded (LES-044) --------------------------------------------

_REDIRECT_PORT, _TARGET_PORT = 9951, 9952  # the lane's block; loopback only


@pytest.fixture
def redirect_pair():
    """A 'public' server that redirects to a loopback one; records what the loopback one received."""
    import http.server
    import threading

    hits: list[str] = []

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{_TARGET_PORT}/private")
            self.end_headers()

        def log_message(self, *a):
            pass

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            body = b'{"AbstractText": "loopback answer", "RelatedTopics": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    servers = [http.server.HTTPServer(("127.0.0.1", _REDIRECT_PORT), Redirect),
               http.server.HTTPServer(("127.0.0.1", _TARGET_PORT), Target)]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    try:
        yield hits
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


def _pretend_redirector_is_public(monkeypatch):
    """The guard's resolver hook: the redirector's port reads as a public address; everything else is real."""
    import socket

    from atlas_plus.inbox import summarize

    real = socket.getaddrinfo

    def fake(host, port, *a, **k):
        if port == _REDIRECT_PORT:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]
        return real(host, port, *a, **k)

    monkeypatch.setattr(summarize, "_getaddrinfo", fake, raising=False)


def test_research_search_refuses_a_redirect_to_loopback(monkeypatch, redirect_pair):
    from atlas_plus.brain import research

    _pretend_redirector_is_public(monkeypatch)
    monkeypatch.setattr(research, "_DDG_API", f"http://127.0.0.1:{_REDIRECT_PORT}/")
    out = research._web_search_duckduckgo("anything")
    assert redirect_pair == [], "the search followed a redirect to a loopback address"
    assert out == []
