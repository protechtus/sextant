"""Sync client: a local edit made after plan() is never overwritten by a pull.

Regression test for the 2026-11-09 data-safety audit row 20 (TOCTOU between
plan() and apply_change on pull).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atlas_plus.sync import api, devices
from atlas_plus.sync.client import SyncClient


@pytest.fixture
def world(tmp_path, monkeypatch):
    hub_root = tmp_path / "hub-mem"
    spoke_root = tmp_path / "spoke-mem"
    (hub_root / "reference").mkdir(parents=True)
    (hub_root / "reference" / "doc.md").write_text("v1", encoding="utf-8")
    spoke_root.mkdir()
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub_root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    _, token = devices.pair("laptop")
    http = TestClient(api.create_app(), raise_server_exceptions=False)
    return SyncClient(token=token, http_client=http), hub_root, spoke_root


def test_local_edit_between_plan_and_pull_is_quarantined_not_overwritten(world):
    client, hub_root, spoke_root = world
    client.sync_once(spoke_root)
    local = spoke_root / "reference" / "doc.md"
    assert local.read_text(encoding="utf-8") == "v1"

    (hub_root / "reference" / "doc.md").write_text("v2 from hub", encoding="utf-8")
    real_fetch = client.fetch_file

    def fetch_then_local_edit(rel):
        result = real_fetch(rel)
        # The operator saves the file after planning, before the write lands.
        local.write_text("v2 typed locally", encoding="utf-8")
        return result

    client.fetch_file = fetch_then_local_edit
    report = client.sync_once(spoke_root)

    assert local.read_text(encoding="utf-8") == "v2 typed locally"
    assert "reference/doc.md" in report.conflicts
    assert "reference/doc.md" not in report.pulled
    parked = list((spoke_root / "_atlas" / "sync-conflicts").rglob("doc.md"))
    assert parked and parked[0].read_text(encoding="utf-8") == "v2 from hub"


def test_new_local_file_created_after_plan_is_not_overwritten(world):
    client, hub_root, spoke_root = world
    client.sync_once(spoke_root)
    (hub_root / "reference" / "new.md").write_text("hub new", encoding="utf-8")
    target = spoke_root / "reference" / "new.md"
    real_fetch = client.fetch_file

    def fetch_then_create(rel):
        result = real_fetch(rel)
        target.write_text("local new", encoding="utf-8")
        return result

    client.fetch_file = fetch_then_create
    report = client.sync_once(spoke_root)
    assert target.read_text(encoding="utf-8") == "local new"
    assert "reference/new.md" in report.conflicts


def test_untouched_file_still_pulls(world):
    client, hub_root, spoke_root = world
    client.sync_once(spoke_root)
    (hub_root / "reference" / "doc.md").write_text("v2 from hub", encoding="utf-8")
    report = client.sync_once(spoke_root)
    assert report.pulled == ["reference/doc.md"] and not report.conflicts
    assert (spoke_root / "reference" / "doc.md").read_text(encoding="utf-8") == "v2 from hub"
