"""AG135 (sweep 40, SEX-061): what the fold of AG127 left.

1. The embeddings client's urllib openers followed a 301/302/303 from the configured URL: the POST became a GET
   to wherever Location named, and that answer was returned as the note's vector. Both openers (loopback and
   proxy path) now refuse every redirect, the way roundtrip._NoRedirect does.
3. Two callers the fold changed and nobody drove: the intake's brand-kit validator and compose's taskkill.

Every server here binds 127.0.0.1 port 0 (the system picks a free port: two CI jobs can share a runner).
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import zipfile

import pytest

_SENTINEL = "ag135-" + "sentinel-" + "value"


class _Pair:
    """Port A answers every POST with the given 3xx to port B; port B records what reaches it."""

    def __init__(self, code: int):
        self.hits: list[dict] = []
        pair = self

        class Sink(http.server.BaseHTTPRequestHandler):
            def _any(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                pair.hits.append({"method": self.command, "body": len(body), "headers": sorted(self.headers.keys())})
                out = json.dumps({"embeddings": [[0.5, 0.5]]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = _any

            def log_message(self, *a):
                pass

        self.sink = http.server.HTTPServer(("127.0.0.1", 0), Sink)
        target = f"http://127.0.0.1:{self.sink.server_address[1]}/elsewhere"

        class Redir(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(code)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        self.redir = http.server.HTTPServer(("127.0.0.1", 0), Redir)
        self.url = f"http://127.0.0.1:{self.redir.server_address[1]}"
        for s in (self.sink, self.redir):
            threading.Thread(target=s.serve_forever, daemon=True).start()

    def close(self):
        for s in (self.sink, self.redir):
            s.shutdown()
            s.server_close()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("path", ["loopback", "proxy"])
def test_embed_never_follows_a_redirect_to_another_port(monkeypatch, code, path):
    from atlas_plus.mcp.memory_server import embeddings

    if path == "proxy":
        # the opener a remote ATLAS_EMBED_URL gets; no proxy is configured, so it still connects directly
        monkeypatch.setattr(embeddings, "_is_loopback_url", lambda url: False)
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    pair = _Pair(code)
    try:
        e = embeddings.OllamaEmbedder(base_url=pair.url, timeout=5)
        assert e.uses_proxy is (path == "proxy")
        with pytest.raises(urllib.error.HTTPError) as err:
            e.embed("a private note body")
        assert err.value.code == code
        assert pair.hits == [], f"the redirect target was fetched: {pair.hits}"
    finally:
        pair.close()


def test_a_redirected_embed_leaves_search_keyword_only(monkeypatch):
    """The degradation contract: a refused embed is an empty vector leg, never an answer from elsewhere."""
    from atlas_plus.mcp.memory_server import embeddings

    pair = _Pair(302)
    try:
        monkeypatch.setattr(embeddings, "_load_vectors", lambda: (["a.md"], [2], [[1.0, 0.0]], None))
        out = embeddings.vector_search("q", embedder=embeddings.OllamaEmbedder(base_url=pair.url, timeout=5))
        assert out == [] and pair.hits == []
    finally:
        pair.close()


# ---- item 3: the brand-kit validator -------------------------------------------------------------------------

_RECORDER = (
    "import json, os, sys\n"
    "from pathlib import Path\n"
    "Path(sys.argv[1], 'ENV_NAMES.json').write_text(json.dumps(sorted(os.environ)), encoding='utf-8')\n"
    "print('checked')\n"
)


def test_intake_brand_validator_gets_the_allowlisted_environment(tmp_path, monkeypatch):
    from atlas_plus.intake import engine

    ws = tmp_path / "ws"
    (ws / "brand" / "tools").mkdir(parents=True)
    (ws / "brand" / "tools" / "validate_brand_svgs.py").write_text(_RECORDER, encoding="utf-8")
    kit = tmp_path / "kit.zip"
    with zipfile.ZipFile(kit, "w") as zf:
        zf.writestr("logo.svg", "<svg xmlns='http://www.w3.org/2000/svg'/>")
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(ws))
    monkeypatch.setenv("SEXTANT_" + "MCP_TOKEN", _SENTINEL)
    monkeypatch.setenv("GITHUB_" + "TOKEN", _SENTINEL)
    monkeypatch.setenv("ATLAS_EMBED_MODEL", "not-for-a-workspace-script")

    summary = engine.brand_import_zip(kit, project="demo")
    found = list((ws / "brand").rglob("ENV_NAMES.json"))
    assert len(found) == 1, f"the validator did not run: {summary!r}"
    names = {n.upper() for n in json.loads(found[0].read_text(encoding="utf-8"))}
    assert "PATH" in names
    assert not {n for n in names if n.startswith(("ATLAS_", "SEXTANT_"))}, sorted(names)
    assert "GITHUB_TOKEN" not in names
    if os.name == "nt":
        assert "SYSTEMROOT" in names  # a Windows Python child cannot start its random source without it
    validation = next((ws / "brand").rglob("VALIDATION.txt")).read_text(encoding="utf-8")
    assert validation.startswith("exit=0") and "checked" in validation


# ---- item 3: compose's taskkill -------------------------------------------------------------------------------
# taskkill.exe lives in System32, which Windows searches BEFORE PATH for a bare program name: a fake on PATH
# cannot shadow it, so the real one runs here, against a child this test started. What is faked: nothing; what
# is recorded: the argv and the environment names compose hands to subprocess.run. Linux and macOS take the
# killpg branch (no child program at all): there the test checks the process group dies and nothing is run.


def test_compose_kill_tree_ends_the_child_and_hands_taskkill_no_secret(monkeypatch):
    from atlas_plus.project_state import compose

    monkeypatch.setenv("SEXTANT_" + "MCP_TOKEN", _SENTINEL)
    monkeypatch.setenv("GITHUB_" + "TOKEN", _SENTINEL)
    flags = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)
    calls: list[dict] = []
    real_run = subprocess.run

    def recording_run(args, **kw):
        calls.append({"args": list(args), "env": sorted((kw.get("env") or {}).keys()), "has_env": "env" in kw})
        return real_run(args, **kw)

    monkeypatch.setattr(compose.subprocess, "run", recording_run)
    try:
        compose._kill_tree(child)
        assert child.wait(timeout=20) is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
    if os.name == "nt":
        assert len(calls) == 1 and calls[0]["args"][0] == "taskkill" and calls[0]["has_env"]
        names = {n.upper() for n in calls[0]["env"]}
        assert "SYSTEMROOT" in names and "PATH" in names
        assert not {n for n in names if n.startswith(("ATLAS_", "SEXTANT_"))}
        assert "GITHUB_TOKEN" not in names
    else:
        assert calls == []          # killpg: no program is started on Linux or macOS
        assert child.returncode < 0  # ended by a signal


# ---- item 2: DNS rebinding between the public-address check and the client's own lookup -------------------------
# The guard's hook (inbox.summarize._getaddrinfo) answers a public address; socket.getaddrinfo, which the HTTP
# client's connect uses, answers 127.0.0.1 for the same name; any other lookup fails (no real DNS here). Linux,
# macOS and Windows answer the same: the connect path is httpcore's, not the system's.

_NAME = "rebind.example-org.test"


class _Loop:
    def __init__(self):
        self.hits: list[tuple] = []
        loop = self

        class H(http.server.BaseHTTPRequestHandler):
            def _any(self):
                loop.hits.append((self.command, self.headers.get("Host")))
                out = json.dumps({"AbstractText": "loopback answer", "RelatedTopics": []}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(out)

            do_GET = do_HEAD = _any

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def rebinding(monkeypatch):
    import socket

    from atlas_plus.inbox import summarize

    loop = _Loop()
    real = socket.getaddrinfo
    monkeypatch.setattr(summarize, "_getaddrinfo",
                        lambda host, port: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))])

    def second_answer(host, port, *a, **k):
        if host == _NAME:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", int(port)))]
        if host == "127.0.0.1":
            return real(host, port, *a, **k)
        raise socket.gaierror("no real DNS lookup in this test")

    monkeypatch.setattr(socket, "getaddrinfo", second_answer)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})  # httpx reads proxies through it (env, registry)
    for v in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(v, raising=False)
    try:
        yield loop, f"http://{_NAME}:{loop.port}/x"
    finally:
        loop.close()


def _inbox(url):
    from atlas_plus.inbox import summarize
    try:
        return summarize._default_fetch_url(url, timeout=5)
    except Exception as exc:  # noqa: BLE001 - a refused connect is the fixed outcome
        return repr(exc)


def _links(url):
    from atlas_plus.curation import link_check
    return link_check.check_url(url)


def _research(url, monkeypatch):
    from atlas_plus.brain import research
    monkeypatch.setattr(research, "_DDG_API", url)
    return research._web_search_duckduckgo("q")


@pytest.mark.parametrize("fetcher", ["inbox", "links", "research"])
def test_a_second_lookup_never_reaches_loopback(rebinding, monkeypatch, fetcher):
    loop, url = rebinding
    out = {"inbox": _inbox, "links": _links}.get(fetcher, lambda u: _research(u, monkeypatch))(url)
    assert loop.hits == [], f"{fetcher} fetched loopback after vetting a public address: {loop.hits} -> {out!r}"
    assert "loopback answer" not in str(out)


def test_the_pinned_connect_goes_to_the_vetted_address_and_keeps_the_host_name(rebinding, monkeypatch):
    """The positive side: the address the guard vetted is the one connected to; the Host header stays the name."""
    from atlas_plus.inbox import summarize

    loop, url = rebinding
    monkeypatch.setattr(summarize, "_refuse_private_target", lambda u, **k: "127.0.0.1")  # vetted = the listener
    monkeypatch.setattr(summarize, "_getaddrinfo", lambda host, port: [])
    import socket
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: real(host, port, *a, **k)
                        if host == "127.0.0.1" else (_ for _ in ()).throw(socket.gaierror("no DNS")))
    assert "loopback answer" in summarize._default_fetch_url(url, timeout=5)
    assert loop.hits == [("GET", f"{_NAME}:{loop.port}")]


def test_the_transport_still_has_the_hook_the_pin_uses():
    """An httpx release that moves it would leave the fetchers unpinned: fail here first."""
    import httpx
    assert hasattr(httpx.HTTPTransport()._pool, "_network_backend")
