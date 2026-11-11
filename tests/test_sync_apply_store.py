"""Tests for atlas_plus.sync.apply (the only writer) and sync.store."""
from __future__ import annotations

import hashlib
import json

import pytest

from atlas_plus.sync import apply, store


class TestApplyChange:
    def test_writes_new_file(self, tmp_path):
        target = apply.apply_change(tmp_path, "reference/new.md", b"hello")
        assert target.read_bytes() == b"hello"

    def test_versions_before_overwrite(self, tmp_path):
        (tmp_path / "reference").mkdir(parents=True)
        (tmp_path / "reference" / "x.md").write_text("old", encoding="utf-8")
        apply.apply_change(tmp_path, "reference/x.md", b"new")
        assert (tmp_path / "reference" / "x.md").read_text(encoding="utf-8") == "new"
        versions = list((tmp_path / "versions" / "reference").glob("x_*_v1.md"))
        assert len(versions) == 1
        assert versions[0].read_text(encoding="utf-8") == "old"

    def test_integrity_mismatch_rejected(self, tmp_path):
        with pytest.raises(apply.SyncIntegrityError):
            apply.apply_change(tmp_path, "a.md", b"data", expected_sha256="0" * 64)

    def test_integrity_match_accepted(self, tmp_path):
        sha = hashlib.sha256(b"data").hexdigest()
        apply.apply_change(tmp_path, "a.md", b"data", expected_sha256=sha)
        assert (tmp_path / "a.md").exists()

    @pytest.mark.parametrize("rel", ["../up.md", ".env", "archive/x.md"])
    def test_policy_denied_paths_raise(self, tmp_path, rel):
        with pytest.raises(apply.SyncPolicyError):
            apply.apply_change(tmp_path, rel, b"x")
        assert not list(tmp_path.rglob("*"))

    def test_current_sha256(self, tmp_path):
        assert apply.current_sha256(tmp_path, "none.md") is None
        (tmp_path / "a.md").write_bytes(b"abc")
        assert apply.current_sha256(tmp_path, "a.md") == hashlib.sha256(b"abc").hexdigest()


class TestQuarantine:
    def test_parks_with_sidecar(self, tmp_path):
        parked = apply.quarantine(tmp_path, "reference/x.md", b"loser", "conflict test")
        assert parked.read_bytes() == b"loser"
        assert "_atlas" in parked.parts and "sync-conflicts" in parked.parts
        meta = json.loads(
            parked.with_name(parked.name + ".meta.json").read_text(encoding="utf-8")
        )
        assert meta["reason"] == "conflict test"
        assert meta["original_path"] == "reference/x.md"

    def test_collision_free(self, tmp_path):
        first = apply.quarantine(tmp_path, "a.md", b"1", "r")
        second = apply.quarantine(tmp_path, "a.md", b"2", "r")
        assert first != second and second.read_bytes() == b"2"

    def test_unsafe_relpath_still_parks(self, tmp_path):
        parked = apply.quarantine(tmp_path, "../../evil.md", b"x", "r")
        assert tmp_path in parked.parents  # never escapes the corpus


class TestInbox:
    def test_lands_as_proposed_with_provenance(self, tmp_path):
        landed = apply.inbox(tmp_path, "friend", "reference/sip/a.md", b"fact")
        assert "sync-inbox" in landed.parts and "friend" in landed.parts
        meta = json.loads(
            landed.with_name(landed.name + ".meta.json").read_text(encoding="utf-8")
        )
        assert meta["source_system"] == "friend"
        assert meta["status"] == "proposed"

    def test_invalid_path_rejected(self, tmp_path):
        with pytest.raises(apply.SyncPolicyError):
            apply.inbox(tmp_path, "friend", "../x.md", b"y")


class TestStore:
    def test_base_manifest_roundtrip(self, tmp_path):
        data = {"files": {"a.md": {"sha256": "1"}}, "manifest_hash": "h"}
        store.save_base_manifest("dev1", data, root=tmp_path)
        assert store.load_base_manifest("dev1", root=tmp_path) == data

    def test_missing_and_corrupt_base(self, tmp_path):
        assert store.load_base_manifest("ghost", root=tmp_path) is None
        path = store.base_manifest_path("bad", tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{oops", encoding="utf-8")
        assert store.load_base_manifest("bad", root=tmp_path) is None

    def test_principal_id_sanitized(self, tmp_path):
        path = store.base_manifest_path("../../evil", tmp_path)
        assert path.parent == tmp_path

    def test_checkin_roundtrip_and_truncation(self, tmp_path):
        store.record_checkin("dev1", {"host": "h" * 500, "manifest_hash": "m"}, root=tmp_path)
        table = store.load_checkins(root=tmp_path)
        assert table["dev1"]["manifest_hash"] == "m"
        assert len(table["dev1"]["host"]) == 200
        assert table["dev1"]["received_utc"]

    def test_audit_appends_jsonl(self, tmp_path):
        store.audit("test_event", root=tmp_path, device="laptop")
        store.audit("test_event2", root=tmp_path)
        lines = store.audit_path(tmp_path).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["event"] == "test_event" and first["device"] == "laptop"
