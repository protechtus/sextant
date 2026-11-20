"""Helm Tools tab (2026-11-19): local tool UIs as iframe panes, read from the
operator's <state dir>/web/tool-panes.json (never from the source), and a CSP
that frames exactly those origins and nothing else."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

PANES = [
    {"key": "alpha", "label": "Alpha: Board", "url": "http://127.0.0.1:9101/board"},
    {"key": "alpha-home", "label": "Alpha", "url": "http://127.0.0.1:9101/"},
    {"key": "beta", "label": "Beta", "url": "http://127.0.0.1:9102/"},
]


def _client(monkeypatch, panes):
    from atlas_plus.web import app as webapp

    monkeypatch.setattr(webapp, "_ALLOW_NO_AUTH", True, raising=False)
    monkeypatch.setattr(webapp, "API_KEY", "", raising=False)
    monkeypatch.setattr(webapp, "_TOOL_PANES", panes)
    monkeypatch.setattr(webapp, "_CSP", webapp._csp_for(panes))
    return webapp, TestClient(webapp.app)


def _frame_src(client):
    csp = client.get("/").headers["content-security-policy"]
    return csp, next(d for d in csp.split(";") if d.strip().startswith("frame-src")).split()[1:]


def _write(tmp_path, data):
    p = tmp_path / "tool-panes.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return p


def test_tools_tab_renders_one_pane_per_tool(monkeypatch, tmp_path):
    from atlas_plus.web import app as webapp

    panes = webapp._load_tool_panes(_write(tmp_path, PANES))
    assert panes == PANES
    webapp, client = _client(monkeypatch, panes)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert 'id="tabbtn-tools"' in body and 'id="tab-tools"' in body
    for pane in PANES:
        assert f'id="pane-{pane["key"]}"' in body
        assert f'src="{pane["url"]}"' in body
    assert body.count('class="card tool-pane"') == len(PANES)


def test_csp_frames_only_the_listed_tool_origins(monkeypatch):
    webapp, client = _client(monkeypatch, PANES)
    csp, frame_src = _frame_src(client)
    assert sorted(frame_src) == ["http://127.0.0.1:9101", "http://127.0.0.1:9102"]
    assert "frame-ancestors 'none'" in csp                      # Helm itself still cannot be framed


def test_no_panes_file_means_no_tab_and_no_framing(monkeypatch, tmp_path):
    from atlas_plus.web import app as webapp

    assert webapp._load_tool_panes(tmp_path / "absent.json") == []
    webapp, client = _client(monkeypatch, [])
    body = client.get("/").text
    assert 'id="tabbtn-tools"' not in body and 'id="tab-tools"' not in body
    assert "<iframe" not in body
    csp, frame_src = _frame_src(client)
    assert frame_src == ["'none'"]


@pytest.mark.parametrize("bad", [
    "{not json",
    {"key": "a", "label": "A", "url": "http://127.0.0.1:9101/"},                    # not a list
    [{"key": "a", "label": "A", "url": "https://example.com/"}],                    # not loopback
    [{"key": "a", "label": "A", "url": "http://127.0.0.1:9101.example.com/"}],      # look-alike host
    [{"key": "a", "label": "A", "url": "http://127.0.0.1:9101/x y"}],               # space would split the CSP
    [{"key": "a", "label": "A", "url": "http://127.0.0.1:9101/;script-src *"}],     # CSP directive injection
    [{"key": "a", "label": "A", "url": "http://127.0.0.1:99999/"}],                 # port out of range
    [{"key": "a", "label": "A", "url": "http://localhost:9101/"}],                  # one spelling only
    [{"key": "A b", "label": "A", "url": "http://127.0.0.1:9101/"}],                # key is a slug
    [{"key": "a", "label": "", "url": "http://127.0.0.1:9101/"}],
    [{"key": "a", "label": "A", "url": "http://127.0.0.1:9101/"}, "junk"],          # one bad row refuses all
    [{"key": "a", "label": "A", "url": "http://127.0.0.1:9101/"},
     {"key": "a", "label": "B", "url": "http://127.0.0.1:9102/"}],                  # duplicate key
    [{"key": f"k{i}", "label": "A", "url": "http://127.0.0.1:9101/"} for i in range(13)],
])
def test_a_bad_panes_file_is_refused_whole(tmp_path, bad):
    from atlas_plus.web import app as webapp

    assert webapp._load_tool_panes(_write(tmp_path, bad)) == []


def test_panes_file_with_a_bom_loads(tmp_path):
    from atlas_plus.web import app as webapp

    p = tmp_path / "tool-panes.json"
    p.write_text(json.dumps(PANES[:1]), encoding="utf-8-sig")
    assert webapp._load_tool_panes(p) == PANES[:1]


def test_origin_strips_the_path():
    from atlas_plus.web.app import _origin

    assert _origin("http://127.0.0.1:8771/deals?x=1") == "http://127.0.0.1:8771"
