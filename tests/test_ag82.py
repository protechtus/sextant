"""Sweep 22 AG82: the sync client's user experience.

Every test runs the real path (client, CLI, in-process hub, real sockets) and checks what the
operator sees: verify's URL hints in the sync client, a token that starts with `-`, progress on a
long first sync, `devices` hiding revoked rows, `pair` on an active name, and plan/sync agreeing on
a locally edited file the hub no longer holds.
"""
from __future__ import annotations

import http.server
import json
import socket
import sys
import threading

import httpx
import pytest


@pytest.fixture
def world(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    hub_root = tmp_path / "hub-mem"
    spoke_root = tmp_path / "spoke-mem"
    (hub_root / "notes").mkdir(parents=True)
    (spoke_root / "notes").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub_root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    monkeypatch.setattr(api, "_corpus_root", lambda: hub_root)  # the CLI under test reads ATLAS_MEMORY_ROOT as the spoke
    _, token = devices.pair("spoke")

    def client():
        return SyncClient(token=token, http_client=TestClient(api.create_app(), raise_server_exceptions=False))

    return client, hub_root, spoke_root, token


def _run_cli(monkeypatch, capsys, argv, memory_root=None):
    from atlas_plus.sync import cli
    if memory_root is not None:
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(memory_root))
    monkeypatch.setattr(sys, "argv", ["sextant-sync", *argv])
    with pytest.raises(SystemExit) as info:
        cli.main()
    out = capsys.readouterr()
    return info.value.code, out.out, out.err


def _route_cli_to(monkeypatch, make_client, seen_tokens=None):
    """cmd_plan/cmd_sync build SyncClient(hub, token): hand them the in-process hub instead."""
    from atlas_plus.sync import client as client_mod

    def factory(hub, token, *a, **k):
        if seen_tokens is not None:
            seen_tokens.append(token)
        return make_client()

    monkeypatch.setattr(client_mod, "SyncClient", factory)


# -- URL hints ----------------------------------------------------------------------------------

def _mock_client(base_url, handler):
    from atlas_plus.sync.client import SyncClient
    return SyncClient(base_url, "t0ken", transport=httpx.MockTransport(handler))


def test_a_redirect_to_https_names_the_https_address():
    from atlas_plus.sync.client import SyncClientError

    def handler(request):
        return httpx.Response(308, headers={"Location": "https://hub.example-org.test" + request.url.path})

    with pytest.raises(SyncClientError) as info:
        _mock_client("http://hub.example-org.test:8768", handler).remote_manifest()
    msg = str(info.value)
    assert "redirects plain HTTP to HTTPS" in msg and "https://hub.example-org.test" in msg
    assert "token was not sent" in msg
    assert "HTTP 308: HTTP 308" not in msg


@pytest.mark.parametrize("suffix", ["/sync", "/sync/v1", "/sync/v1/", "/mcp"])
def test_a_path_suffix_on_the_hub_url_names_the_root_address(suffix):
    from atlas_plus.sync.client import SyncClientError

    def handler(request):
        return httpx.Response(404, json={"detail": "Not Found"})

    with pytest.raises(SyncClientError) as info:
        _mock_client("http://hub.example-org.test:8768" + suffix, handler).remote_manifest()
    msg = str(info.value)
    assert "without a path" in msg and "use http://hub.example-org.test:8768;" in msg


def test_a_tls_proxys_400_for_plain_http_names_https():
    from atlas_plus.sync.client import SyncClientError

    def handler(request):
        return httpx.Response(400, text="Client sent an HTTP request to an HTTPS server.\n")

    with pytest.raises(SyncClientError) as info:
        _mock_client("http://hub.example-org.test:443", handler).remote_manifest()
    assert "use https://hub.example-org.test:443" in str(info.value)


@pytest.fixture
def plain_http_server():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_https_to_a_plain_http_hub_is_not_called_no_hub_answering(plain_http_server):
    from atlas_plus.sync.client import SyncClient, SyncClientError
    with pytest.raises(SyncClientError) as info:
        with SyncClient(f"https://127.0.0.1:{plain_http_server}", "t0ken", timeout=5) as c:
            c.remote_manifest()
    msg = str(info.value)
    assert "no hub answering" not in msg  # the hub IS answering, in plain HTTP
    assert "plain HTTP" in msg and "use http://" in msg


@pytest.fixture
def closing_server():
    """A port that accepts and closes without a reply: what a TLS-only port does to plain HTTP."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    stop = threading.Event()

    def loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            try:
                conn.recv(4096)
            except OSError:
                pass
            conn.close()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    yield srv.getsockname()[1]
    stop.set()
    t.join(2)
    srv.close()


def test_http_to_a_port_that_drops_plain_http_suggests_https(closing_server):
    from atlas_plus.sync.client import SyncClient, SyncClientError
    with pytest.raises(SyncClientError) as info:
        with SyncClient(f"http://127.0.0.1:{closing_server}", "t0ken", timeout=5) as c:
            c.remote_manifest()
    msg = str(info.value)
    assert f"use https://127.0.0.1:{closing_server}" in msg


def test_the_cli_shows_the_hint(monkeypatch, capsys, tmp_path):
    from atlas_plus.sync import client as client_mod
    real = client_mod.SyncClient

    def factory(hub, token, *a, **k):
        return real(hub, token, transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"detail": "Not Found"})))

    monkeypatch.setattr(client_mod, "SyncClient", factory)
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    code, out, err = _run_cli(monkeypatch, capsys, ["plan", "--hub", "http://hub.example-org.test:8768/mcp", "--token", "x"],
                              memory_root=tmp_path / "mem")
    assert code == 2 and "use http://hub.example-org.test:8768;" in err


# -- a token that starts with "-" -----------------------------------------------------------------

def test_pairing_never_issues_a_token_that_reads_as_a_flag(tmp_path, monkeypatch):
    from atlas_plus.sync import devices
    draws = iter(["-starts-with-a-dash-" + "a" * 23, "--two-dashes-" + "b" * 30, "Clean" + "c" * 38])
    monkeypatch.setattr(devices.secrets, "token_urlsafe", lambda n=32: next(draws))
    _, token = devices.pair("laptop", path=tmp_path / "devices.json")
    assert not token.startswith("-")
    assert devices.authenticate(token, path=tmp_path / "devices.json") is not None


@pytest.mark.parametrize("order", ["token-last", "token-first"])
def test_a_dash_token_from_an_older_pairing_still_parses(world, monkeypatch, capsys, order):
    make_client, _hub, spoke_root, _token = world
    seen: list[str] = []
    _route_cli_to(monkeypatch, make_client, seen)
    dash = "-" + "Q" * 42
    argv = ["plan", "--hub", "http://127.0.0.1:9", "--token", dash] if order == "token-last" \
        else ["plan", "--token", dash, "--hub", "http://127.0.0.1:9"]
    code, out, err = _run_cli(monkeypatch, capsys, argv, memory_root=spoke_root)
    assert "expected one argument" not in err
    assert seen == [dash] and code == 0


# -- progress on a long first sync -----------------------------------------------------------------

def test_a_long_first_sync_prints_progress(world, monkeypatch, capsys):
    make_client, hub_root, spoke_root, _ = world
    for i in range(260):
        (hub_root / "notes" / f"n{i:04d}.md").write_text(f"note {i}", encoding="utf-8")
    _route_cli_to(monkeypatch, make_client)
    code, out, err = _run_cli(monkeypatch, capsys, ["sync", "--hub", "http://127.0.0.1:9", "--token", "x"],
                              memory_root=spoke_root)
    assert code == 0 and "pulled 260" in out
    assert "syncing 260 files (260 to pull, 0 to push)" in err
    assert "260/260 files" in err
    assert "syncing" not in out  # stdout keeps the one summary line scripts read


def test_a_small_sync_stays_quiet(world, monkeypatch, capsys):
    make_client, hub_root, spoke_root, _ = world
    (hub_root / "notes" / "one.md").write_text("one", encoding="utf-8")
    _route_cli_to(monkeypatch, make_client)
    from atlas_plus.sync import cli
    assert cli.PROGRESS_MIN > 1
    code, out, err = _run_cli(monkeypatch, capsys, ["sync", "--hub", "http://127.0.0.1:9", "--token", "x"],
                              memory_root=spoke_root)
    assert code == 0 and "syncing" not in err


# -- devices / pair ---------------------------------------------------------------------------------

def test_devices_hides_revoked_rows_unless_all(tmp_path, monkeypatch, capsys):
    from atlas_plus.sync import devices
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    devices.pair("laptop")
    devices.pair("oldphone")
    devices.revoke("oldphone")
    code, out, _ = _run_cli(monkeypatch, capsys, ["devices"])
    assert code == 0 and "laptop" in out and "oldphone" not in out and "--all" in out
    code, out, _ = _run_cli(monkeypatch, capsys, ["devices", "--all"])
    assert "oldphone" in out and "REVOKED" in out
    code, out, _ = _run_cli(monkeypatch, capsys, ["devices", "--json"])
    assert [d["name"] for d in json.loads(out)] == ["laptop"]
    code, out, _ = _run_cli(monkeypatch, capsys, ["devices", "--json", "--all"])
    assert sorted(d["name"] for d in json.loads(out)) == ["laptop", "oldphone"]
    devices.revoke("laptop")
    code, out, _ = _run_cli(monkeypatch, capsys, ["devices"])
    assert "no active devices" in out and "2 revoked" in out


def test_pair_on_an_active_name_names_revoke_then_pair(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    code, out, _ = _run_cli(monkeypatch, capsys, ["pair", "laptop"])
    assert code == 0
    code, out, err = _run_cli(monkeypatch, capsys, ["pair", "laptop"])
    assert code == 1 and "already exists" in err
    assert "revoke laptop" in err and "pair laptop" in err
    code, out, _ = _run_cli(monkeypatch, capsys, ["revoke", "laptop"])
    code, out, _ = _run_cli(monkeypatch, capsys, ["pair", "laptop"])
    assert code == 0 and "token" in out


# -- plan and sync agree ------------------------------------------------------------------------------

def test_plan_and_sync_agree_on_a_local_edit_the_hub_lacks(world, monkeypatch, capsys):
    make_client, hub_root, spoke_root, _ = world
    (hub_root / "notes" / "a.md").write_text("base", encoding="utf-8")
    with make_client() as c:
        c.sync_once(spoke_root)
    (hub_root / "notes" / "a.md").unlink()  # archived away on the hub
    (spoke_root / "notes" / "a.md").write_text("edited here", encoding="utf-8")

    with make_client() as c:
        plan, _l, _r = c.plan(spoke_root)
        report = c.sync_once(spoke_root, dry_run=True)
    assert plan.conflicts == [] and report.conflicts == []
    assert plan.kept_local_edits == ["notes/a.md"] and "notes/a.md" in plan.remote_deleted

    _route_cli_to(monkeypatch, make_client)
    hub = ["--hub", "http://127.0.0.1:9", "--token", "x"]
    _, plan_out, _ = _run_cli(monkeypatch, capsys, ["plan", *hub], memory_root=spoke_root)
    _, dry_out, _ = _run_cli(monkeypatch, capsys, ["sync", "--dry-run", *hub], memory_root=spoke_root)
    _, sync_out, _ = _run_cli(monkeypatch, capsys, ["sync", *hub], memory_root=spoke_root)
    for out in (plan_out, dry_out, sync_out):
        assert "CONFLICT" not in out
        assert "edited here, deleted on the hub" in out and "notes/a.md" in out
    _, plan_again, _ = _run_cli(monkeypatch, capsys, ["plan", *hub], memory_root=spoke_root)
    assert "CONFLICT" not in plan_again
    assert (spoke_root / "notes" / "a.md").read_text(encoding="utf-8") == "edited here"

    # The edit's old anchor is kept: the hub restoring its old copy is a conflict, never a pull over it.
    (hub_root / "notes" / "a.md").write_text("base", encoding="utf-8")
    with make_client() as c:
        r = c.sync_once(spoke_root)
    assert r.pulled == [] and (spoke_root / "notes" / "a.md").read_text(encoding="utf-8") == "edited here"


def test_a_dry_run_never_claims_a_copy_was_parked(world, monkeypatch, capsys):
    make_client, hub_root, spoke_root, _ = world
    (hub_root / "notes" / "c.md").write_text("base", encoding="utf-8")
    with make_client() as c:
        c.sync_once(spoke_root)
    (hub_root / "notes" / "c.md").write_text("hub edit", encoding="utf-8")
    (spoke_root / "notes" / "c.md").write_text("spoke edit", encoding="utf-8")
    _route_cli_to(monkeypatch, make_client)
    _, out, _ = _run_cli(monkeypatch, capsys, ["sync", "--dry-run", "--hub", "http://127.0.0.1:9", "--token", "x"],
                         memory_root=spoke_root)
    assert "CONFLICT: notes/c.md" in out
    assert "is parked at" not in out and "would keep your copy" in out
    assert not (spoke_root / "_atlas" / "sync-conflicts").exists()
