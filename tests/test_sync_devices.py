"""Tests for atlas_plus.sync.devices -- pairing, auth, revocation."""
from __future__ import annotations

import json

import pytest

from atlas_plus.sync import devices


@pytest.fixture
def reg(tmp_path):
    return tmp_path / "devices.json"


class TestPair:
    def test_pair_returns_token_once_and_stores_hash_only(self, reg):
        device, token = devices.pair("laptop", path=reg)
        assert device.name == "laptop" and device.kind == "device"
        raw = reg.read_text(encoding="utf-8")
        assert token not in raw
        assert device.token_sha256 in raw
        assert len(token) >= 40

    def test_duplicate_active_name_rejected(self, reg):
        devices.pair("laptop", path=reg)
        with pytest.raises(ValueError, match="already exists"):
            devices.pair("laptop", path=reg)

    def test_revoked_name_can_be_repaired(self, reg):
        devices.pair("laptop", path=reg)
        devices.revoke("laptop", path=reg)
        device, _ = devices.pair("laptop", path=reg)
        assert device.revoked is False

    def test_peer_requires_prefix(self, reg):
        with pytest.raises(ValueError, match="allowlist prefix"):
            devices.pair("friend", kind="peer", path=reg)

    def test_unsafe_prefix_rejected(self, reg):
        with pytest.raises(ValueError, match="unsafe prefix"):
            devices.pair("friend", kind="peer", prefixes=("../up/",), path=reg)

    @pytest.mark.parametrize("kwargs", [
        {"kind": "robot"}, {"scope": "admin"},
    ])
    def test_invalid_enum_rejected(self, reg, kwargs):
        with pytest.raises(ValueError):
            devices.pair("x", path=reg, **kwargs)


class TestAuthenticate:
    def test_roundtrip(self, reg):
        _, token = devices.pair("laptop", path=reg)
        found = devices.authenticate(token, path=reg)
        assert found is not None and found.name == "laptop"

    def test_wrong_token_fails(self, reg):
        devices.pair("laptop", path=reg)
        assert devices.authenticate("nope", path=reg) is None

    def test_empty_and_giant_tokens_fail(self, reg):
        devices.pair("laptop", path=reg)
        assert devices.authenticate("", path=reg) is None
        assert devices.authenticate("x" * 1000, path=reg) is None

    def test_revoked_token_fails(self, reg):
        _, token = devices.pair("laptop", path=reg)
        devices.revoke("laptop", path=reg)
        assert devices.authenticate(token, path=reg) is None

    def test_corrupt_registry_fails_closed(self, reg):
        _, token = devices.pair("laptop", path=reg)
        reg.write_text("{not json", encoding="utf-8")
        assert devices.authenticate(token, path=reg) is None

    def test_missing_registry_fails_closed(self, reg):
        assert devices.authenticate("anything", path=reg) is None


class TestRevoke:
    def test_by_name_and_by_id(self, reg):
        device, _ = devices.pair("laptop", path=reg)
        assert devices.revoke(device.device_id, path=reg) is True
        assert devices.revoke("laptop", path=reg) is False  # already revoked

    def test_unknown_returns_false(self, reg):
        assert devices.revoke("ghost", path=reg) is False


class TestPrincipal:
    def test_mapping(self, reg):
        device, _ = devices.pair(
            "friend", kind="peer", scope="ro", prefixes=("reference/sip/",), path=reg
        )
        principal = device.principal()
        assert principal.kind == "peer" and principal.scope == "ro"
        assert principal.prefixes == ("reference/sip/",)

    def test_registry_survives_unknown_fields(self, reg):
        devices.pair("laptop", path=reg)
        data = json.loads(reg.read_text(encoding="utf-8"))
        data["devices"].append({"future_field": True, "name": "odd"})
        reg.write_text(json.dumps(data), encoding="utf-8")
        names = [d.name for d in devices.load_devices(reg)]
        assert "laptop" in names and "odd" not in names
