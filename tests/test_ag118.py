"""AG118 (MN92 F1-F6): a hub that stops answering mid-run, the counts after it, and the next steps.

Every test runs the real sync client (SyncClient.sync_once / plan, cmd_checkin) against a fake hub: an
httpx transport that answers like the hub and then raises the transport errors a dead hub causes, or a
local HTTP server thread that drops the connection. No real hub process, no sleeps.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import threading
from pathlib import Path

import httpx
import pytest

from atlas_plus.sync import client as sc
from atlas_plus.sync import manifest

DEAD = "http://127.0.0.1:9"  # never contacted: the transport answers


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    for key in ("ATLAS_SYNC_HUB", "ATLAS_SYNC_DEVICE_TOKEN", "ATLAS_SYNC_SHARE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    (tmp_path / "mem").mkdir()
    return tmp_path


class FakeHub(httpx.BaseTransport):
    """Answers manifest/file/push/checkin like the hub; `die_after` answered requests later every request
    raises `dead` (a refused connect by default), as a stopped hub does."""

    def __init__(self, files: dict[str, bytes] | None = None, die_after: int | None = None,
                 dead=httpx.ConnectError, status: dict[str, int] | None = None, push_dies_at: int | None = None):
        self.files = dict(files or {})
        self.die_after = die_after
        self.dead = dead
        self.status = status or {}
        self.push_dies_at = push_dies_at  # the n-th push request (1-based) is lost
        self.answered = 0
        self.calls: list[str] = []
        self.pushes = 0

    def table(self) -> dict:
        return {rel: {"sha256": hashlib.sha256(b).hexdigest(), "size": len(b)} for rel, b in self.files.items()}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self.die_after is not None and self.answered >= self.die_after:
            raise self.dead("[WinError 10061] No connection could be made", request=request)
        if path.endswith("/push"):
            self.pushes += 1
            if self.push_dies_at is not None and self.pushes >= self.push_dies_at:
                raise httpx.ReadError("[WinError 10054] An existing connection was forcibly closed", request=request)
        self.answered += 1
        if path.endswith("/manifest"):
            return httpx.Response(200, json={"files": self.table(), "generation": 1, "generation_mark": "m1",
                                             "principal": {"name": "devB", "kind": "device", "scope": "rw",
                                                           "prefixes": []}})
        if path.endswith("/file"):
            rel = request.url.params.get("path")
            if rel in self.status:
                return httpx.Response(self.status[rel], json={"detail": "nope"})
            if rel not in self.files:
                return httpx.Response(404, json={"detail": "not found"})
            body = self.files[rel]
            return httpx.Response(200, content=body, headers={"X-Sextant-Sha256": hashlib.sha256(body).hexdigest()})
        if path.endswith("/push"):
            results = []
            for ch in json.loads(request.content)["changes"]:
                self.files[ch["path"]] = base64.b64decode(ch["content_b64"])
                results.append({"path": ch["path"], "result": "applied"})
            return httpx.Response(200, json={"results": results, "generation": 2, "generation_mark": "m1"})
        if path.endswith("/checkin"):
            return httpx.Response(200, json={"server_manifest_hash": manifest.files_hash(self.table())})
        return httpx.Response(404)


def _notes(n: int, prefix: str = "n") -> dict[str, bytes]:
    return {f"bulk/{prefix}{i:04d}.md": f"# note {i}\n".encode() for i in range(n)}


def _client(hub: FakeHub) -> sc.SyncClient:
    return sc.SyncClient(DEAD, "tok-" + "ag118", transport=hub)


def test_pull_stops_at_the_first_refused_connect_and_says_so_once(_sandbox):
    """F1: 30 files to pull, the hub dies after 5: ONE more request, not 25, and one line with the next step."""
    hub = FakeHub(_notes(30), die_after=1 + 5)  # the manifest, then five files
    root = _sandbox / "mem"
    with _client(hub) as c:
        report = c.sync_once(root)
    file_calls = [p for p in hub.calls if p.endswith("/file")]
    assert len(file_calls) == 6, f"kept asking a dead hub: {len(file_calls)} file requests"
    assert len(report.pulled) == 5 and len(report.not_transferred) == 25
    assert len(report.errors) == 1, report.errors
    line = report.errors[0]
    assert "stopped answering" in line and "25 not transferred" in line and "5 transferred" in line
    assert " serve`" in line and " sync` again" in line
    assert "TLS proxy" not in line
    assert not any(p.endswith("/checkin") for p in hub.calls[7:]), "checked in with a dead hub"
    # the next run resumes: the files not transferred are pulled, nothing is pulled twice
    hub.die_after = None
    with _client(hub) as c:
        again = c.sync_once(root)
    assert sorted(again.pulled) == sorted(report.not_transferred) and not again.errors


def test_an_http_answer_for_one_file_does_not_stop_the_batch(_sandbox):
    """F1 boundary: a 413/404 for one file is an answer, not a dead hub: the loop goes on as before."""
    files = _notes(6)
    hub = FakeHub(files, status={"bulk/n0002.md": 413})
    with _client(hub) as c:
        report = c.sync_once(_sandbox / "mem")
    assert len(report.pulled) == 5 and not report.hub_gone and not report.not_transferred
    assert len(report.errors) == 1 and "n0002" in report.errors[0]


def test_push_hub_lost_mid_batch_reports_unknown_not_zero(_sandbox):
    """F2 + F1 on push: 501 files (2 requests), the 1st request's answer is lost: 500 unknown (the hub may
    hold them), 1 never sent, pushed 0 is true, one line, and the 2nd request is never made."""
    root = _sandbox / "mem"
    for rel, body in _notes(501, "p").items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(body)
    hub = FakeHub({}, push_dies_at=1)
    with _client(hub) as c:
        report = c.sync_once(root, progress=lambda *a: None)
    assert hub.pushes == 1, f"{hub.pushes} push requests to a dead hub"
    assert len(report.pushed) == 0 and len(report.unconfirmed) == 500 and len(report.not_transferred) == 1
    assert len(report.errors) == 1 and "500 unknown (the hub did not confirm: run `" in report.errors[0]
    assert "unknown 500, not transferred 1" in report.summary()


def test_single_push_request_lost_is_unknown(_sandbox):
    """F2: one request carried every file and its answer was lost: not "pushed 0, errors 3"."""
    root = _sandbox / "mem"
    for rel, body in _notes(3, "q").items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(body)
    hub = FakeHub({}, push_dies_at=1)
    with _client(hub) as c:
        report = c.sync_once(root)
    assert len(report.unconfirmed) == 3 and len(report.errors) == 1
    assert "TLS proxy" not in report.errors[0]


class _DropAfter(http.server.BaseHTTPRequestHandler):
    """A real socket: answers `budget` requests like the hub, then drops every connection unanswered."""

    budget = 0
    seen: list = []
    files = _notes(4)

    def do_GET(self):  # noqa: N802
        type(self).seen.append(self.path)
        if len(type(self).seen) > type(self).budget:
            self.close_connection = True
            self.connection.close()
            return
        if self.path.startswith("/sync/v1/manifest"):
            table = {r: {"sha256": hashlib.sha256(b).hexdigest(), "size": len(b)} for r, b in self.files.items()}
            body = json.dumps({"files": table, "generation": 1, "principal": {
                "name": "d", "kind": "device", "scope": "rw", "prefixes": []}}).encode()
        else:
            from urllib.parse import parse_qs, urlsplit
            body = self.files[parse_qs(urlsplit(self.path).query)["path"][0]]
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve(budget: int):
    _DropAfter.budget, _DropAfter.seen = budget, []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DropAfter)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_a_hub_that_answered_then_dropped_is_not_a_tls_proxy(_sandbox):
    """F3: a real local server answers the manifest and one file, then drops connections: one
    "stopped answering" line, no TLS hint, and no request after the first dropped one."""
    srv = _serve(2)
    try:
        with sc.SyncClient(f"http://127.0.0.1:{srv.server_address[1]}", "tok-" + "ag118") as c:
            report = c.sync_once(_sandbox / "mem")
    finally:
        srv.shutdown(); srv.server_close()
    assert len(_DropAfter.seen) == 3, _DropAfter.seen
    assert len(report.errors) == 1 and "stopped answering" in report.errors[0]
    assert not any("TLS proxy" in e for e in report.errors)


def test_the_tls_hint_stays_for_a_first_request_that_is_dropped(_sandbox):
    """F3 boundary: the hint is kept where it can be true (the first request of the run)."""
    srv = _serve(0)
    try:
        with sc.SyncClient(f"http://127.0.0.1:{srv.server_address[1]}", "tok-" + "ag118") as c:
            with pytest.raises(sc.SyncClientError) as err:
                c.plan(_sandbox / "mem")
    finally:
        srv.shutdown(); srv.server_close()
    assert "TLS proxy" in str(err.value) and not isinstance(err.value, sc.SyncHubGoneError)


def _real_commands() -> set[str]:
    from importlib import metadata
    try:
        return {ep.name for ep in metadata.entry_points(group="console_scripts")}
    except Exception:  # noqa: BLE001
        return set()


def test_a_rejected_token_names_the_next_step(_sandbox):
    """F6: the 401 at the manifest and mid-run both say revoke/pair on the hub and the device variable."""
    import re

    class _Gone(FakeHub):
        def handle_request(self, request):
            if self.answered >= self.die_after:
                self.calls.append(request.url.path)
                return httpx.Response(401, json={"detail": "bad token"})
            return super().handle_request(request)

    with _client(_Gone(_notes(2), die_after=0)) as c:
        with pytest.raises(sc.SyncClientError) as err:
            c.plan(_sandbox / "mem")
    mid = _Gone(_notes(3), die_after=2)
    with _client(mid) as c:
        report = c.sync_once(_sandbox / "mem")
    for text in (str(err.value), report.errors[-1]):
        assert "token rejected by hub" in text or "rejected this device's token" in text
        assert "ATLAS_SYNC_DEVICE_TOKEN" in text
        cmds = re.findall(r"`((?:sextant|atlas)-sync) (revoke|pair) <name>`", text)
        assert {sub for _c, sub in cmds} == {"revoke", "pair"}, text
        installed = _real_commands()
        if installed:  # the private tree is not pip-installed: then the name shape is the check
            assert all(c in installed for c, _s in cmds), cmds


def test_checkin_names_the_differences_sync_will_not_act_on(_sandbox, monkeypatch, capsys):
    """F5: after a delete on the hub `checkin` still exits 1 (the corpora differ) but says why."""
    from atlas_plus.sync import cli

    root = _sandbox / "mem"
    hub = FakeHub({"a.md": b"a\n", "gone.md": b"g\n"})
    with _client(hub) as c:
        c.sync_once(root)
    assert (root / "gone.md").is_file()
    del hub.files["gone.md"]  # the hub's operator removed it: a delete is reported, never propagated
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(*a, **{**k, "transport": hub}))
    args = argparse.Namespace(hub=DEAD, token="tok-" + "ag118", share=None)
    rc = cli.cmd_checkin(args)
    out = capsys.readouterr().out
    assert rc == 1
    assert "only differences sync will not act on: 1 deleted remotely" in out, out
