"""MN86 (sweep 25, paranoid lane): inbox fields reach the dashboard escaped, a captured link
is http(s) only, and the summarize worker never fetches a loopback or private address."""

from __future__ import annotations

import http.server
import threading

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

_IMG = "<img src=x onerror=" + "mn86()>"


def _client(tmp_path, monkeypatch):
    from atlas_plus.web import app as webapp
    from atlas_plus.inbox import store as inbox_store

    monkeypatch.setattr(webapp, "_ALLOW_NO_AUTH", True, raising=False)
    monkeypatch.setattr(webapp, "API_KEY", "", raising=False)
    root = tmp_path / "inbox"
    root.mkdir()
    monkeypatch.setattr(inbox_store, "INBOX_ROOT", root, raising=False)
    return TestClient(webapp.app), root


def _hand_written(root, name, **fields):
    """An inbox file as a synced device or a hand edit leaves it (no store validation)."""
    import yaml
    (root / f"{name}.md").write_text(
        "---\n" + yaml.safe_dump(fields, sort_keys=False) + "---\nbody\n", encoding="utf-8")


class TestInboxEscaping:
    def test_list_escapes_id_and_status(self, tmp_path, monkeypatch):
        client, root = _client(tmp_path, monkeypatch)
        _hand_written(root, "INB-0001", id=_IMG, source="web", status=_IMG, url="https://example-org.com/a")
        r = client.get("/api/inbox")
        assert r.status_code == 200
        assert _IMG not in r.text
        assert "&lt;img src=x onerror=mn86()&gt;" in r.text

    def test_detail_escapes_status_source_created(self, tmp_path, monkeypatch):
        client, root = _client(tmp_path, monkeypatch)
        _hand_written(root, "INB-0002", id="INB-0002", source=_IMG, status=_IMG, created=_IMG,
                      url="https://example-org.com/b")
        r = client.get("/api/inbox/INB-0002/detail")
        assert r.status_code == 200
        assert "Not found" not in r.text
        assert _IMG not in r.text

    def test_detail_never_links_a_javascript_url(self, tmp_path, monkeypatch):
        client, root = _client(tmp_path, monkeypatch)
        _hand_written(root, "INB-0003", id="INB-0003", source="web", status="new",
                      url="javascript:alert(document.cookie)")
        r = client.get("/api/inbox/INB-0003/detail")
        assert 'href="javascript:' not in r.text.lower()
        assert "javascript:alert" in r.text  # still shown, as text

    def test_detail_links_an_https_url(self, tmp_path, monkeypatch):
        client, root = _client(tmp_path, monkeypatch)
        _hand_written(root, "INB-0004", id="INB-0004", source="web", status="new",
                      url="https://example-org.com/ok")
        r = client.get("/api/inbox/INB-0004/detail")
        assert 'href="https://example-org.com/ok"' in r.text


class TestCaptureScheme:
    @pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///etc/passwd", "data:text/html,x"])
    def test_create_item_refuses_non_http(self, tmp_path, url):
        from atlas_plus.inbox.store import create_item
        with pytest.raises(ValueError, match="http"):
            create_item(source="api", url=url, inbox_root=tmp_path)
        assert not list(tmp_path.glob("*.md"))

    def test_create_item_keeps_https(self, tmp_path):
        from atlas_plus.inbox.store import create_item
        assert create_item(source="api", url="HTTPS://example-org.com/x", inbox_root=tmp_path).url


def _serve(handler):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class _Internal(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"INTERNAL-ONLY-MN86")

    def log_message(self, *a):
        pass


class TestSummarizeFetchGuard:
    def test_loopback_url_is_not_fetched(self, monkeypatch):
        from atlas_plus.inbox.summarize import _default_fetch_url
        monkeypatch.delenv("ATLAS_INBOX_FETCH_PRIVATE", raising=False)
        srv = _serve(_Internal)
        try:
            with pytest.raises(ValueError, match="non-public"):
                _default_fetch_url(f"http://127.0.0.1:{srv.server_port}/admin", timeout=5)
        finally:
            srv.shutdown()

    def test_redirect_to_loopback_is_not_followed(self, monkeypatch):
        """The public first hop is simulated by letting the check pass once."""
        from atlas_plus.inbox import summarize
        monkeypatch.delenv("ATLAS_INBOX_FETCH_PRIVATE", raising=False)
        internal = _serve(_Internal)

        class _Redir(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{internal.server_port}/keys")
                self.end_headers()

            def log_message(self, *a):
                pass

        redir = _serve(_Redir)
        real = summarize._refuse_private_target
        calls = []

        def first_hop_public(url):
            calls.append(url)
            if len(calls) == 1:
                return None  # stands in for an attacker's public page
            return real(url)

        monkeypatch.setattr(summarize, "_refuse_private_target", first_hop_public)
        try:
            with pytest.raises(ValueError, match="non-public"):
                summarize._default_fetch_url(f"http://127.0.0.1:{redir.server_port}/article", timeout=5)
            assert len(calls) == 2 and calls[1].endswith("/keys")
        finally:
            redir.shutdown()
            internal.shutdown()

    def test_summarize_item_falls_back_to_body_not_internal_text(self, tmp_path, monkeypatch):
        from atlas_plus.inbox.store import create_item
        from atlas_plus.inbox.summarize import summarize_item
        monkeypatch.delenv("ATLAS_INBOX_FETCH_PRIVATE", raising=False)
        srv = _serve(_Internal)
        try:
            it = create_item(source="api", url=f"http://127.0.0.1:{srv.server_port}/x", inbox_root=tmp_path)
            seen = {}

            def complete(prompt):
                seen["p"] = prompt
                return "S.\nTags: a"

            summarize_item(it.id, complete=complete, inbox_root=tmp_path)
            assert "INTERNAL-ONLY-MN86" not in seen["p"]
        finally:
            srv.shutdown()

    def test_opt_in_allows_private_targets(self, monkeypatch):
        from atlas_plus.inbox.summarize import _default_fetch_url
        monkeypatch.setenv("ATLAS_INBOX_FETCH_PRIVATE", "on")
        srv = _serve(_Internal)
        try:
            assert "INTERNAL-ONLY-MN86" in _default_fetch_url(f"http://127.0.0.1:{srv.server_port}/", timeout=5)
        finally:
            srv.shutdown()

    def test_non_http_scheme_refused(self):
        from atlas_plus.inbox.summarize import _refuse_private_target
        with pytest.raises(ValueError, match="http"):
            _refuse_private_target("file:///etc/passwd")
