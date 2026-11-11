"""Tests for the Map tab's backend: /api/topology and /api/activity."""
from __future__ import annotations

import os

import pytest

# Same key literal as test_web_app.py: whichever module imports the app
# first wins the frozen API_KEY, so they must agree or suite order breaks.
os.environ.setdefault("ATLAS_WEB_API_KEY", "test-key-abc")
_TEST_KEY = os.environ["ATLAS_WEB_API_KEY"]

from fastapi.testclient import TestClient  # noqa: E402

from atlas_plus import activity  # noqa: E402
from atlas_plus.web.app import app  # noqa: E402
import atlas_plus.web.app as webapp  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)
AUTH = {"Authorization": f"Bearer {_TEST_KEY}"}


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))
    return tmp_path / ".atlas"


@pytest.fixture()
def probes_stubbed(monkeypatch):
    # Key on the module's own BRAIN_URL, not a port literal: test_web_app.py
    # sets ATLAS_BRAIN_URL before the app module is first imported, so the
    # frozen URL depends on module import order across the suite.
    async def fake_probe(client_, url):
        return "up" if url.startswith(webapp.BRAIN_URL) else "down"
    monkeypatch.setattr(webapp, "_probe_health", fake_probe)


class TestTopology:
    def test_requires_auth(self):
        assert client.get("/api/topology").status_code == 401

    def test_nodes_and_edges(self, state, tmp_path, monkeypatch, probes_stubbed):
        monkeypatch.setattr(webapp, "MEMORY_ROOT", tmp_path / "mem")
        r = client.get("/api/topology", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        ids = {n["id"] for n in body["nodes"]}
        assert {"clients", "helm", "mcp", "brain", "worker", "ollama",
                "sync", "corpus", "statedb", "temporal", "tickets"} <= ids
        # every edge endpoint must be a real node
        for e in body["edges"]:
            assert e["source"] in ids and e["target"] in ids
        by_id = {n["id"]: n for n in body["nodes"]}
        assert by_id["helm"]["status"] == "up"  # it answered this request
        assert by_id["brain"]["status"] == "up"  # stub: 8767 up
        assert by_id["ollama"]["status"] == "down"
        assert by_id["corpus"]["status"] == "missing"  # tmp corpus not created

    def test_store_presence_reflects_disk(self, state, tmp_path, monkeypatch, probes_stubbed):
        mem = tmp_path / "mem"
        mem.mkdir()
        monkeypatch.setattr(webapp, "MEMORY_ROOT", mem)
        (state / "temporal_graph").mkdir(parents=True)
        (state / "temporal_graph" / "facts.jsonl").write_text("{}\n", encoding="utf-8")
        body = client.get("/api/topology", headers=AUTH).json()
        by_id = {n["id"]: n for n in body["nodes"]}
        assert by_id["corpus"]["status"] == "present"
        assert by_id["temporal"]["status"] == "present"
        assert by_id["statedb"]["status"] == "missing"

    def test_worker_active_on_recent_brain_pulse(self, state, probes_stubbed, monkeypatch):
        activity.record("brain", "task_running", task_id="t-1")
        body = client.get("/api/topology", headers=AUTH).json()
        by_id = {n["id"]: n for n in body["nodes"]}
        assert by_id["worker"]["status"] == "active"


class TestMapPage:
    def test_page_carries_map_tab_and_js(self):
        html = client.get("/", headers=AUTH).text
        assert 'id="tab-map"' in html
        assert 'data-tab="map"' in html
        assert "function mapTabShown" in html
        assert 'id="map-ticker"' in html

    def test_tab_switching_is_data_driven(self):
        """The old parallel _TABS array was missing operator/watchtower,
        which misaligned bar highlighting from the third tab on and left
        those panels active when switching away. The bar and the panel set
        must both derive from data-tab now."""
        html = client.get("/", headers=AUTH).text
        assert "const _TABS=" not in html
        assert "querySelectorAll('.tab[data-tab]')" in html
        # every bar tab carries data-tab matching its switchTab argument
        import re as _re
        bar = _re.findall(r'class="tab[^"]*" data-tab="([a-z]+)" onclick="switchTab\(\'([a-z]+)\'\)"', html)
        # one bar tab per panel, whatever optional panels this build carries
        assert len(bar) == html.count('class="tab-content') and len(bar) >= 10
        for data_name, onclick_name in bar:
            assert data_name == onclick_name


class TestActivityEndpoint:
    def test_requires_auth(self):
        # Earlier page-render tests set the sextant_ui auth cookie on the
        # shared client (it is accepted on GET); clear it so this request
        # is genuinely anonymous.
        client.cookies.clear()
        assert client.get("/api/activity").status_code == 401

    def test_returns_tail_with_cursor(self, state):
        activity.record("mcp", "tool_called", tool="memory_search_tool", ok=True, ms=3.2)
        activity.record("tickets", "ticket_created", ticket_id="T-1", project="p")
        r = client.get("/api/activity", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert [e["event"] for e in body["events"]] == ["tool_called", "ticket_created"]
        assert "now" in body
        cursor = body["events"][0]["ts"]
        # params= URL-encodes the '+00:00' offset; a raw query string would
        # decode '+' as a space and break the cursor compare (the Map tab's
        # JS uses encodeURIComponent for the same reason).
        later = client.get(
            "/api/activity", params={"since": cursor}, headers=AUTH
        ).json()
        assert [e["event"] for e in later["events"]] == ["ticket_created"]

    def test_limit_is_clamped(self, state):
        for i in range(5):
            activity.record("brain", "task_done", task_id=f"t-{i}")
        body = client.get("/api/activity?limit=99999", headers=AUTH).json()
        assert len(body["events"]) == 5
        body = client.get("/api/activity?limit=2", headers=AUTH).json()
        assert len(body["events"]) == 2
