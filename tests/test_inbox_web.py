"""ATL-026 step 4 tests -- Helm web UI inbox endpoints (list/detail/promote/archive)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    from atlas_plus.web import app as webapp
    from atlas_plus.inbox import store as inbox_store

    # auth off for the test; point the inbox store at a tmp root
    monkeypatch.setattr(webapp, "_ALLOW_NO_AUTH", True, raising=False)
    monkeypatch.setattr(webapp, "API_KEY", "", raising=False)
    root = tmp_path / "inbox"
    root.mkdir()
    monkeypatch.setattr(inbox_store, "INBOX_ROOT", root, raising=False)
    return TestClient(webapp.app), inbox_store, root


class TestInboxWeb:
    def test_empty_list(self, tmp_path, monkeypatch):
        client, _store, _root = _client(tmp_path, monkeypatch)
        r = client.get("/api/inbox")
        assert r.status_code == 200
        assert "Inbox empty" in r.text

    def test_list_and_detail(self, tmp_path, monkeypatch):
        client, store, root = _client(tmp_path, monkeypatch)
        it = store.create_item(url="https://example.com/x", title="Test item", inbox_root=root)
        r = client.get("/api/inbox")
        assert it.id in r.text and "Test item" in r.text
        d = client.get(f"/api/inbox/{it.id}/detail")
        assert "Test item" in d.text and "Promote to ticket" in d.text

    def test_archive(self, tmp_path, monkeypatch):
        client, store, root = _client(tmp_path, monkeypatch)
        it = store.create_item(text="blurb", inbox_root=root)
        r = client.post(f"/api/inbox/{it.id}/archive")
        assert r.status_code == 200
        assert store.get_item(it.id, inbox_root=root).status == "archived"

    def test_promote(self, tmp_path, monkeypatch):
        client, store, root = _client(tmp_path, monkeypatch)
        from atlas_plus.tickets import store as tstore

        troot = tmp_path / "tickets"
        troot.mkdir()
        monkeypatch.setattr(tstore, "TICKETS_ROOT", troot, raising=False)
        it = store.create_item(url="https://example.com/y", title="Promote me", inbox_root=root)
        r = client.post(f"/api/inbox/{it.id}/promote", json={"project": "myproject"})
        assert r.status_code == 201, r.text
        assert r.json()["ticket_id"] == "MYP-001"
        assert store.get_item(it.id, inbox_root=root).status == "promoted"

    def test_detail_not_found(self, tmp_path, monkeypatch):
        client, _store, _root = _client(tmp_path, monkeypatch)
        r = client.get("/api/inbox/INB-9999/detail")
        assert r.status_code == 200 and "Not found" in r.text
