"""MN85 (sweep 25): the Map tab's Model backend node probes the Ollama the Brain uses.

Before: /api/topology read OLLAMA_BASE_URL only, so with ATLAS_BRAIN_OLLAMA_URL (or OLLAMA_HOST)
pointing elsewhere the Map showed http://127.0.0.1:11434 -- another install's Ollama -- as "up"
while the status pill on the same page said "ollama: down" (the pill took default_ollama_url()
with MN25; the Map is its sibling).
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("ATLAS_WEB_API_KEY", "test-key-abc")
_TEST_KEY = os.environ["ATLAS_WEB_API_KEY"]

from fastapi.testclient import TestClient  # noqa: E402

import atlas_plus.web.app as webapp  # noqa: E402

client = TestClient(webapp.app, raise_server_exceptions=False)
AUTH = {"Authorization": f"Bearer {_TEST_KEY}"}


@pytest.fixture()
def probed(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))
    seen: list[str] = []

    async def fake_probe(client_, url):
        seen.append(url)
        return "up" if url.startswith("http://127.0.0.1:11434") else "down"

    monkeypatch.setattr(webapp, "_probe_health", fake_probe)
    for var in ("ATLAS_BRAIN_OLLAMA_URL", "OLLAMA_BASE_URL", "OLLAMA_HOST"):
        monkeypatch.delenv(var, raising=False)
    return seen


def _ollama_node():
    r = client.get("/api/topology", headers=AUTH)
    assert r.status_code == 200
    return {n["id"]: n for n in r.json()["nodes"]}["ollama"]


@pytest.mark.parametrize("var,value,expect", [
    ("ATLAS_BRAIN_OLLAMA_URL", "http://127.0.0.1:9439/", "http://127.0.0.1:9439"),
    ("OLLAMA_HOST", "127.0.0.1:9438", "http://127.0.0.1:9438"),
])
def test_map_probes_the_brains_ollama(probed, monkeypatch, var, value, expect):
    monkeypatch.setenv(var, value)
    node = _ollama_node()
    assert node["detail"] == expect
    assert f"{expect}/api/tags" in probed
    assert not any(u.startswith("http://127.0.0.1:11434") for u in probed)
    assert node["status"] == "down"  # the stub answers only on 11434


def test_map_still_honours_ollama_base_url(probed, monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9437")
    assert _ollama_node()["detail"] == "http://127.0.0.1:9437"


# --- Brain-call 503s name the fix (Run, Ask Claude, Quick Run, Retry, pending facts) ---------------

class _Refused:
    """Stands in for the Brain's HTTP client: every call is a refused connect with an empty message,
    as httpx raises it on Windows for a closed loopback port."""
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def _refuse(self, *a, **k):
        import httpx
        raise httpx.ConnectError("")

    get = post = patch = put = delete = request = _refuse


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/tasks", {"instruction": "say hello"}),
    ("post", "/api/tasks/claude", {"instruction": "hello"}),
    ("post", "/api/scheduler/trigger/suggest-facts", None),
    ("get", "/api/tasks/abc123", None),
])
def test_brain_down_503_names_the_fix(monkeypatch, method, path, body):
    monkeypatch.setattr(webapp, "_PeerClient", _Refused)
    kw = {"headers": AUTH}
    if body is not None:
        kw["json"] = body
    r = getattr(client, method)(path, **kw)
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "Brain not running or not reachable" in detail and "sextant-brain" in detail


def test_brain_http_error_keeps_its_own_text():
    import httpx
    exc = httpx.HTTPStatusError("401 Unauthorized", request=httpx.Request("POST", "http://x"),
                                response=httpx.Response(401))
    assert webapp._brain_error_detail(exc) == "401 Unauthorized"
