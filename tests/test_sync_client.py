"""End-to-end: SyncClient (spoke) against the real hub app, in process.

Two corpora on disk: the hub's (served by api.create_app via env) and the
spoke's (passed to sync_once). One shared state dir is fine in-process:
registry/checkins belong to the hub role, base-hub.json to the spoke role,
and the filenames never collide.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atlas_plus.sync import api, devices, store
from atlas_plus.sync.client import SyncClient, SyncClientError


@pytest.fixture
def world(tmp_path, monkeypatch):
    hub_root = tmp_path / "hub-mem"
    spoke_root = tmp_path / "spoke-mem"
    (hub_root / "reference").mkdir(parents=True)
    (hub_root / "reference" / "hub-only.md").write_text("from hub", encoding="utf-8")
    (spoke_root / "projects").mkdir(parents=True)
    (spoke_root / "projects" / "spoke-note.md").write_text("from spoke", encoding="utf-8")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub_root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    # These tests edit the hub corpus directly on disk between syncs; disable
    # the server-side manifest cache so every plan sees the live tree.
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    _, token = devices.pair("laptop")
    http = TestClient(api.create_app(), raise_server_exceptions=False)
    client = SyncClient(token=token, http_client=http)
    return client, hub_root, spoke_root


class TestEndToEnd:
    def test_first_sync_converges_both_sides(self, world):
        client, hub_root, spoke_root = world
        report = client.sync_once(spoke_root)
        assert report.pulled == ["reference/hub-only.md"]
        assert report.pushed == ["projects/spoke-note.md"]
        assert not report.conflicts and not report.errors
        assert (spoke_root / "reference" / "hub-only.md").read_text(
            encoding="utf-8"
        ) == "from hub"
        assert (hub_root / "projects" / "spoke-note.md").read_text(
            encoding="utf-8"
        ) == "from spoke"

    def test_second_sync_is_clean(self, world):
        client, _, spoke_root = world
        client.sync_once(spoke_root)
        report = client.sync_once(spoke_root)
        assert not report.pulled and not report.pushed and not report.conflicts

    def test_spoke_edit_propagates_with_base_precondition(self, world):
        client, hub_root, spoke_root = world
        client.sync_once(spoke_root)
        (spoke_root / "reference" / "hub-only.md").write_text("v2 from spoke", encoding="utf-8")
        report = client.sync_once(spoke_root)
        assert report.pushed == ["reference/hub-only.md"]
        assert (hub_root / "reference" / "hub-only.md").read_text(
            encoding="utf-8"
        ) == "v2 from spoke"
        # hub versioned its previous copy before accepting the change
        assert list((hub_root / "versions" / "reference").glob("hub-only_*_v1.md"))

    def test_divergent_edits_conflict_and_quarantine(self, world):
        client, hub_root, spoke_root = world
        client.sync_once(spoke_root)
        (hub_root / "reference" / "hub-only.md").write_text("hub v2", encoding="utf-8")
        (spoke_root / "reference" / "hub-only.md").write_text("spoke v2", encoding="utf-8")
        report = client.sync_once(spoke_root)
        assert report.conflicts == ["reference/hub-only.md"]
        # local copy stays live, hub copy stays live, hub's version parked locally
        assert (spoke_root / "reference" / "hub-only.md").read_text(
            encoding="utf-8"
        ) == "spoke v2"
        assert (hub_root / "reference" / "hub-only.md").read_text(
            encoding="utf-8"
        ) == "hub v2"
        parked = list((spoke_root / "_atlas" / "sync-conflicts").rglob("hub-only.md"))
        assert parked and parked[0].read_text(encoding="utf-8") == "hub v2"

    def test_dry_run_changes_nothing(self, world):
        client, hub_root, spoke_root = world
        report = client.sync_once(spoke_root, dry_run=True)
        assert not report.pulled and not report.pushed
        assert not (spoke_root / "reference" / "hub-only.md").exists()
        assert not (hub_root / "projects" / "spoke-note.md").exists()

    def test_checkin_reports_server_state(self, world):
        client, _, spoke_root = world
        client.sync_once(spoke_root)
        result = client.checkin({"host": "spoke-01", "manifest_hash": "x"})
        assert result["acknowledged"] is True
        assert store.load_checkins()

    def test_bad_token_is_a_clear_error(self, world, tmp_path):
        _, _, spoke_root = world
        http = TestClient(api.create_app(), raise_server_exceptions=False)
        bad = SyncClient(token="wrong", http_client=http)
        with pytest.raises(SyncClientError, match="unauthorized"):
            bad.remote_manifest()

    def test_plan_matches_reality(self, world):
        client, _, spoke_root = world
        plan, local, remote = client.plan(spoke_root)
        assert plan.to_pull == ["reference/hub-only.md"]
        assert plan.to_push == ["projects/spoke-note.md"]
        assert remote["manifest_hash"] != local["manifest_hash"]
