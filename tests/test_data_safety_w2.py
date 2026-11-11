"""Regression tests for the 2026-11-09 data-safety audit, worker-2 rows.

Rows covered: 5, 6 (anti-memory confinement + YAML-safe propagation),
12 (config toggle scope), 13 (session cookie, ?key= only on GET /, no key in
page), 14 (web memory-file write confinement), 15 (decision collisions),
16 (project_state resolve + git hardening), 17a (lock key), 25 (':' in
scopes), 26 (confirm gate + version copy), 29 (forwarded-header trust),
30 (non-ASCII token is a clean 401).
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

_TEST_KEY = "test-key-abc"
os.environ.setdefault("ATLAS_WEB_API_KEY", _TEST_KEY)


# ---------------------------------------------------------------------------
# Shared: point every module-level corpus root at a temp corpus
# ---------------------------------------------------------------------------

@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "mem"
    (root / "_atlas").mkdir(parents=True)
    from atlas_plus.mcp.memory_server import routing, versioning
    from atlas_plus.mcp.memory_server.tools import anti, read
    monkeypatch.setattr(routing, "MEMORY_ROOT", root)
    monkeypatch.setattr(routing, "ROUTING_CONFIG", root / "_atlas" / "routing.yaml")
    monkeypatch.setattr(versioning, "MEMORY_ROOT", root)
    monkeypatch.setattr(read, "MEMORY_ROOT", root)
    monkeypatch.setattr(anti, "_MEMORY_ROOT", root)
    monkeypatch.setattr(anti, "ANTI_ROOT", root / "anti")
    return root


def _fm(text: str) -> dict:
    assert text.startswith("---")
    end = text.find("\n---", 3)
    return yaml.safe_load(text[3:end]) or {}


# ---------------------------------------------------------------------------
# Row 25: ':' in scope names
# ---------------------------------------------------------------------------

class TestScopeColon:
    @pytest.mark.parametrize("scope", [
        "project:c:secret", "project:notes:hidden", "reference:a:b",
        "project:../x", "project:a/b", "project:a\\b",
    ])
    def test_rejected(self, corpus, scope):
        from atlas_plus.mcp.memory_server.routing import resolve_target
        with pytest.raises(ValueError):
            resolve_target(scope)
        assert not (corpus / "secret.md").exists()
        assert not (corpus / "projects" / "notes").exists()

    def test_plain_scope_still_works(self, corpus):
        from atlas_plus.mcp.memory_server.routing import resolve_target
        target, _ = resolve_target("project:fine-name")
        assert target == corpus / "projects" / "fine-name.md"


# ---------------------------------------------------------------------------
# Row 17(a): lock key + locked_atomic_rewrite helper
# ---------------------------------------------------------------------------

class TestLockKey:
    def test_different_spellings_share_a_key(self, corpus):
        from atlas_plus.mcp.memory_server.routing import _lock_key
        (corpus / "reference").mkdir()
        a = corpus / "reference" / "x.md"
        b = corpus / "reference" / ".." / "reference" / "X.md"
        assert _lock_key(a) == _lock_key(b)
        assert _lock_key(a) == "rel:reference/x.md"

    def test_root_spelling_does_not_matter(self, corpus, monkeypatch):
        from atlas_plus.mcp.memory_server import routing
        target = corpus / "identity" / "user.md"
        k1 = routing._lock_key(target)
        # Same corpus spelled with a redundant component, as a second
        # process configured differently would.
        monkeypatch.setattr(routing, "MEMORY_ROOT", corpus / "_atlas" / "..")
        assert routing._lock_key(target) == k1

    def test_junction_spelling_shares_key(self, corpus, tmp_path, monkeypatch):
        from atlas_plus.mcp.memory_server import routing
        link = tmp_path / "corpus-link"
        try:
            if os.name == "nt":
                import subprocess
                r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(corpus)],
                                   capture_output=True)
                if r.returncode != 0:
                    pytest.skip("cannot create junction")
            else:
                link.symlink_to(corpus, target_is_directory=True)
        except OSError:
            pytest.skip("cannot create link")
        k_real = routing._lock_key(corpus / "reference" / "a.md")
        monkeypatch.setattr(routing, "MEMORY_ROOT", link)
        assert routing._lock_key(link / "reference" / "a.md") == k_real

    def test_locked_atomic_rewrite(self, corpus):
        from atlas_plus.mcp.memory_server.routing import locked_atomic_rewrite
        target = corpus / "reference" / "n.md"
        locked_atomic_rewrite(target, lambda t: t + "one\n")
        locked_atomic_rewrite(target, lambda t: t + "two\n")
        assert target.read_text(encoding="utf-8") == "one\ntwo\n"
        before = target.stat().st_mtime_ns
        locked_atomic_rewrite(target, lambda t: t)  # unchanged: no write
        assert target.stat().st_mtime_ns == before
        assert not list(target.parent.glob("*.w-tmp"))


# ---------------------------------------------------------------------------
# Rows 5 + 6: anti-memory
# ---------------------------------------------------------------------------

class TestAntiConfined:
    @pytest.mark.parametrize("scope", [
        "../identity/user", "..\\identity\\user", "/abs/evil", "C:\\evil", "c:evil",
        "a:b", "con", ".hidden",
    ])
    def test_escape_scopes_refused(self, corpus, scope):
        from atlas_plus.mcp.memory_server.tools.anti import anti_remember
        (corpus / "identity").mkdir()
        user = corpus / "identity" / "user.md"
        user.write_text("---\nid: user\n---\nbody\n", encoding="utf-8")
        before = user.read_bytes()
        result = anti_remember(scope=scope, reject_pattern="never use redis caching layers")
        assert result["ok"] is False
        assert user.read_bytes() == before
        assert not list(corpus.parent.glob("*.md"))

    def test_normal_scope_writes_inside_anti(self, corpus):
        from atlas_plus.mcp.memory_server.tools.anti import anti_remember
        r = anti_remember(scope="Libraries", reject_pattern="never use leftpad anywhere")
        assert r["ok"] is True
        assert Path(r["file"]) == corpus / "anti" / "libraries.md"
        r2 = anti_remember(scope="libraries", reject_pattern="avoid moment timezone handling")
        assert r2["ok"] is True
        text = (corpus / "anti" / "libraries.md").read_text(encoding="utf-8")
        assert "leftpad" in text and "moment" in text


class TestAntiPropagationSafe:
    PATTERN = 'use redis caching in D:\\Users\\temp "quoted" it\'s\nstatus: archived'

    def _seed(self, corpus):
        body = "We use redis caching for sessions under users temp dirs.\n"
        live = corpus / "reference" / "cache.md"
        live.parent.mkdir(parents=True)
        live.write_text(f"---\nid: cache\nstatus: active\n---\n{body}", encoding="utf-8")
        hist = corpus / "versions" / "reference" / "cache_2026-01-01_v1.md"
        hist.parent.mkdir(parents=True)
        hist.write_text(f"---\nid: cache\nstatus: active\n---\n{body}", encoding="utf-8")
        arch = corpus / "archive" / "cache-old.md"
        arch.parent.mkdir(parents=True)
        arch.write_text(f"---\nid: old\nstatus: active\n---\n{body}", encoding="utf-8")
        return live, hist, arch

    def test_yaml_safe_and_skips_history(self, corpus):
        from atlas_plus.mcp.memory_server.tools.anti import propagate_anti_rejection
        live, hist, arch = self._seed(corpus)
        hist_before, arch_before = hist.read_bytes(), arch.read_bytes()
        res = propagate_anti_rejection(self.PATTERN, "caching", memory_root=corpus)
        assert res["flagged"] == 1
        fm = _fm(live.read_text(encoding="utf-8"))
        assert fm["status"] == "active"           # newline did not inject a key
        assert fm["anti_flagged"] is True
        assert "D:\\Users\\temp" in fm["anti_reason"]
        assert "\n" not in fm["anti_reason"]
        assert hist.read_bytes() == hist_before
        assert arch.read_bytes() == arch_before
        # A version copy of the pre-flag file exists.
        copies = list((corpus / "versions" / "reference").glob("cache_*_v*.md"))
        assert any("anti_flagged" not in c.read_text(encoding="utf-8") for c in copies
                   if c != hist)
        assert len(copies) >= 2


# ---------------------------------------------------------------------------
# Row 15: decisions never silently overwrite
# ---------------------------------------------------------------------------

class TestDecisionCollisions:
    @pytest.fixture
    def ddir(self, tmp_path, monkeypatch):
        from atlas_plus.mcp.memory_server import routing
        from atlas_plus.mcp.memory_server.tools import decision
        monkeypatch.setattr(decision, "MEMORY_ROOT", tmp_path)
        monkeypatch.setattr(routing, "MEMORY_ROOT", tmp_path)
        return tmp_path / "decisions"

    def test_same_title_twice_keeps_both(self, ddir):
        from atlas_plus.mcp.memory_server.tools.decision import memory_remember_decision
        a = memory_remember_decision(title="Use FAISS", decision="first")
        b = memory_remember_decision(title="Use FAISS", decision="second")
        assert a["slug"] == "use-faiss" and b["slug"] == "use-faiss-2"
        assert a["replaced"] is False and b["replaced"] is False
        assert "first" in (ddir / "use-faiss.md").read_text(encoding="utf-8")
        assert "second" in (ddir / "use-faiss-2.md").read_text(encoding="utf-8")
        assert b["path"] == str(ddir / "use-faiss-2.md")

    def test_hand_written_file_untouched(self, ddir):
        from atlas_plus.mcp.memory_server.tools.decision import memory_remember_decision
        ddir.mkdir(parents=True)
        (ddir / "foo.md").write_text("hand written", encoding="utf-8")
        r = memory_remember_decision(title="foo", decision="x")
        assert (ddir / "foo.md").read_text(encoding="utf-8") == "hand written"
        assert r["slug"] == "foo-2"

    def test_non_latin_title_gets_hash_slug(self, ddir):
        from atlas_plus.mcp.memory_server.tools.decision import memory_remember_decision
        a = memory_remember_decision(title="データベース", decision="x")
        b = memory_remember_decision(title="мой выбор", decision="y")
        assert a["slug"].startswith("decision-") and b["slug"].startswith("decision-")
        assert a["slug"] != b["slug"]
        assert not (ddir / ".md").exists()


# ---------------------------------------------------------------------------
# Row 16: project_state confinement + git hardening
# ---------------------------------------------------------------------------

class TestProjectStateHardening:
    def test_dotdot_absolute_path_rejected(self, tmp_path, monkeypatch):
        from atlas_plus.mcp.memory_server.tools import project_state as ps
        root = tmp_path / "projects"
        root.mkdir()
        outside = tmp_path / "Downloads" / "evil-repo"
        outside.mkdir(parents=True)
        monkeypatch.setattr(ps, "_PROJECTS_ROOT", root)
        sneaky = str(root) + os.sep + ".." + os.sep + "Downloads" + os.sep + "evil-repo"
        with pytest.raises(ValueError):
            ps._resolve(sneaky)
        inside = root / "good"
        inside.mkdir()
        assert ps._resolve(str(inside))[0] == inside.resolve()

    def test_every_git_call_is_hardened(self, tmp_path):
        from atlas_plus.project_state import git as g
        calls = []

        def fake_run(args, **kw):
            calls.append(args)
            return SimpleNamespace(stdout="", returncode=0)

        with patch.object(g.subprocess, "run", fake_run):
            g.git_state(tmp_path)
        assert calls
        for args in calls:
            assert args[0] == "git"
            assert "core.fsmonitor=" in args
            assert f"core.hooksPath={os.devnull}" in args
            assert args.index("core.fsmonitor=") < len(args) - 1


# ---------------------------------------------------------------------------
# Row 26: memory_confirm gate + version copy
# ---------------------------------------------------------------------------

class TestConfirmGate:
    def _seed(self, corpus):
        f = corpus / "reference" / "acme.md"
        f.parent.mkdir(parents=True)
        f.write_text("---\nid: acme\nupdated: 2020-01-01\n---\nbody\n", encoding="utf-8")
        return f

    def test_interactive_off_blocks_confirm(self, corpus, monkeypatch):
        from atlas_plus.mcp.memory_server.tools.remember import memory_confirm
        f = self._seed(corpus)
        before = f.read_bytes()
        monkeypatch.setenv("ATLAS_INTERACTIVE_WRITES", "off")
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        r = memory_confirm("reference/acme.md", interactive=True)
        assert r["ok"] is False
        assert f.read_bytes() == before

    def test_confirm_versions_first(self, corpus, monkeypatch):
        from atlas_plus.mcp.memory_server.tools.remember import memory_confirm
        f = self._seed(corpus)
        before = f.read_text(encoding="utf-8")
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        r = memory_confirm("reference/acme.md", interactive=True)
        assert r["ok"] is True
        copies = list((corpus / "versions" / "reference").glob("acme_*_v*.md"))
        assert copies and copies[0].read_text(encoding="utf-8") == before
        assert "last_confirmed" in f.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Row 30: non-ASCII token
# ---------------------------------------------------------------------------

class TestNonAsciiToken:
    def test_constant_time_equals_never_raises(self):
        from atlas_plus.auth import constant_time_equals
        assert constant_time_equals("ключ", "key") is False
        assert constant_time_equals("\udcff", "key") is False
        assert constant_time_equals("key", "key") is True


# ---------------------------------------------------------------------------
# Row 29: forwarded headers trusted only from loopback
# ---------------------------------------------------------------------------

def _req(peer: str, headers: dict):
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


class TestForwardedTrust:
    def test_mcp_server(self):
        from atlas_plus.mcp.memory_server.server import _client_ip
        h = {"Cf-Connecting-Ip": "9.9.9.9", "X-Forwarded-For": "8.8.8.8"}
        assert _client_ip(_req("192.168.1.50", h)) == "192.168.1.50"
        assert _client_ip(_req("127.0.0.1", h)) == "9.9.9.9"
        assert _client_ip(_req("127.0.0.1", {"X-Forwarded-For": "8.8.8.8, 1.1.1.1"})) == "1.1.1.1"  # last non-loopback hop (wave 4)
        assert _client_ip(_req("::1", {})) == "::1"

    def test_web_app(self):
        from atlas_plus.web.app import _client_ip
        h = {"Cf-Connecting-Ip": "9.9.9.9"}
        assert _client_ip(_req("192.168.1.50", h)) == ("192.168.1.50", "")
        assert _client_ip(_req("127.0.0.1", h)) == ("127.0.0.1", "9.9.9.9")


# ---------------------------------------------------------------------------
# Web UI: rows 12, 13, 14, 30
# ---------------------------------------------------------------------------

@pytest.fixture
def web(monkeypatch):
    from fastapi.testclient import TestClient
    import atlas_plus.web.app as web_app
    monkeypatch.setattr(web_app, "API_KEY", _TEST_KEY)
    monkeypatch.setattr(web_app, "BRAIN_URL", "http://127.0.0.1:9")
    web_app.AUTH_FAILURES.clear()
    c = TestClient(web_app.app, raise_server_exceptions=False)
    yield c, web_app
    web_app.AUTH_FAILURES.clear()


AUTH = {"Authorization": f"Bearer {_TEST_KEY}"}
LOCAL_ORIGIN = {"Origin": "http://testserver", "X-Sextant-Request": "1"}  # own host:port + the page header


def _session(c) -> str:
    r = c.get(f"/?key={_TEST_KEY}")
    assert r.status_code == 200
    tok = r.cookies.get("sextant_ui")
    c.cookies.clear()
    assert tok
    return tok


class TestWebSession:
    def test_page_html_contains_no_api_key(self, web):
        c, _ = web
        r = c.get("/", headers=AUTH)
        assert r.status_code == 200
        assert _TEST_KEY not in r.text
        # the only hx-headers left is the static CSRF marker on <body>; none carries a key
        assert r.text.count("hx-headers") == 1 and "X-Sextant-Request" in r.text
        assert "Authorization" not in r.text.split("<body")[1]
        assert r.cookies.get("sextant_ui") not in (None, "", _TEST_KEY)

    def test_session_cookie_post_with_local_origin_passes_auth(self, web):
        c, _ = web
        tok = _session(c)
        r = c.post("/api/config", json={"var": "NOPE"}, cookies={"sextant_ui": tok},
                   headers=LOCAL_ORIGIN)
        assert r.status_code == 400  # past auth, rejected by the allowlist

    def test_session_cookie_post_without_origin_rejected(self, web):
        c, _ = web
        tok = _session(c)
        r = c.post("/api/config", json={"var": "NOPE"}, cookies={"sextant_ui": tok})
        assert r.status_code == 401

    def test_session_cookie_post_cross_origin_rejected(self, web):
        c, _ = web
        tok = _session(c)
        r = c.post("/api/config", json={"var": "NOPE"}, cookies={"sextant_ui": tok},
                   headers={"Origin": "https://evil.example"})
        assert r.status_code == 401

    def test_raw_key_as_cookie_rejected_on_post(self, web):
        c, _ = web
        c.cookies.clear()
        r = c.post("/api/config", json={"var": "NOPE"}, cookies={"sextant_ui": _TEST_KEY},
                   headers=LOCAL_ORIGIN)
        assert r.status_code == 401

    def test_query_key_rejected_on_post(self, web):
        c, _ = web
        c.cookies.clear()
        r = c.post(f"/api/config?key={_TEST_KEY}", json={"var": "NOPE"})
        assert r.status_code == 401

    def test_query_key_rejected_on_api_get(self, web):
        c, _ = web
        c.cookies.clear()
        assert c.get(f"/api/config?key={_TEST_KEY}").status_code == 401

    def test_bearer_still_works(self, web):
        c, _ = web
        c.cookies.clear()
        assert c.post("/api/config", json={"var": "NOPE"}, headers=AUTH).status_code == 400

    def test_non_ascii_bearer_is_clean_401(self, web):
        c, _ = web
        c.cookies.clear()
        r = c.get("/api/config", headers={"Authorization": "Bearer \u00e9\u00e9".encode("latin-1")})
        assert r.status_code == 401

    def test_websocket_query_key_refused(self, web):
        from starlette.websockets import WebSocketDisconnect
        c, _ = web
        c.cookies.clear()
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect(f"/ws/chat/s?key={_TEST_KEY}") as ws:
                ws.receive_text()

    def test_uvicorn_access_log_disabled(self):
        import inspect
        import atlas_plus.web.app as web_app
        assert "access_log=False" in inspect.getsource(web_app.main)


class TestConfigToggleScope:
    def test_toggle_reports_web_process_only(self, web, monkeypatch):
        c, _ = web
        monkeypatch.setenv("ATLAS_SMART_ROUTING", "off")
        r = c.post("/api/config", json={"var": "ATLAS_SMART_ROUTING"}, headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["scope"] == "web-process-only"
        assert body["affects_other_services"] is False
        html = c.get("/api/config", headers=AUTH).text
        assert "web process only" in html
        assert "do not change Brain, MCP" in html


class TestMemoryFileWriteConfined:
    @pytest.fixture
    def mem(self, web, tmp_path, monkeypatch):
        c, web_app = web
        from atlas_plus.mcp.memory_server import routing
        root = tmp_path / "mem"
        for rel in ("reference/ok.md", "versions/reference/ok_2026-01-01_v1.md",
                    "archive/old.md", "_atlas/routing.yaml", ".git/config", "_sync/x.md"):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("original", encoding="utf-8")
        monkeypatch.setattr(web_app, "MEMORY_ROOT", root)
        monkeypatch.setattr(routing, "MEMORY_ROOT", root)
        return c, root

    @pytest.mark.parametrize("rel", [
        "versions/reference/ok_2026-01-01_v1.md", "archive/old.md",
        "_atlas/routing.yaml", ".git/config", "_sync/x.md", "C:/x.md",
        "reference%2F..%2F.git%2Fconfig",
    ])
    def test_excluded_paths_refused(self, mem, rel):
        c, root = mem
        r = c.post("/api/memory/file", json={"path": rel, "content": "pwned"}, headers=AUTH)
        assert r.status_code in (400, 404)
        for p in root.rglob("*"):
            if p.is_file():
                assert p.read_text(encoding="utf-8") == "original"

    def test_normal_write_versions_and_lands(self, mem):
        c, root = mem
        r = c.post("/api/memory/file", json={"path": "reference/ok.md", "content": "new"},
                   headers=AUTH)
        assert r.status_code == 200
        assert (root / "reference" / "ok.md").read_text(encoding="utf-8") == "new"
        copies = [p for p in (root / "versions" / "reference").iterdir()
                  if p.read_text(encoding="utf-8") == "original" and "ok_" in p.name]
        assert copies

    def test_version_failure_aborts_write(self, mem):
        c, root = mem

        def boom(*a, **k):
            raise OSError("disk full")

        with patch("atlas_plus.mcp.memory_server.versioning.version_before_write", boom):
            r = c.post("/api/memory/file", json={"path": "reference/ok.md", "content": "new"},
                       headers=AUTH)
        assert r.status_code == 500
        assert (root / "reference" / "ok.md").read_text(encoding="utf-8") == "original"
