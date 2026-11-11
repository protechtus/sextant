"""Tests for atlas_plus.sync.api -- the hub's HTTP surface, fail-closed."""
from __future__ import annotations

import base64
import hashlib

import pytest
from fastapi.testclient import TestClient

from atlas_plus.sync import api, devices, store


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A hub with an isolated corpus + state dir and one paired rw device."""
    corpus = tmp_path / "mem"
    (corpus / "reference").mkdir(parents=True)
    (corpus / "reference" / "a.md").write_text("alpha", encoding="utf-8")
    (corpus / "identity").mkdir()
    (corpus / "identity" / "user.md").write_text("me", encoding="utf-8")
    state = tmp_path / "state"
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(corpus))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
    _, token = devices.pair("laptop")
    client = TestClient(api.create_app(), raise_server_exceptions=False)
    return client, token, corpus


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


class TestAuth:
    def test_health_is_open(self, hub):
        client, _, _ = hub
        body = client.get("/sync/v1/health").json()
        assert body["status"] == "ok" and body["protocol"] == 1

    def test_manifest_requires_token(self, hub):
        client, _, _ = hub
        assert client.get("/sync/v1/manifest").status_code == 401
        assert client.get(
            "/sync/v1/manifest", headers=_auth("wrong")
        ).status_code == 401

    def test_auth_failure_audited(self, hub, tmp_path):
        client, _, _ = hub
        client.get("/sync/v1/manifest", headers=_auth("wrong"))
        audit = store.audit_path()
        assert audit.exists() and "auth_failure" in audit.read_text(encoding="utf-8")


class TestManifest:
    def test_lists_corpus(self, hub):
        client, token, _ = hub
        body = client.get("/sync/v1/manifest", headers=_auth(token)).json()
        assert set(body["files"]) == {"reference/a.md", "identity/user.md"}

    def test_peer_sees_only_allowlist(self, hub):
        client, _, _ = hub
        _, peer_token = devices.pair("friend", kind="peer", prefixes=("reference/",))
        body = client.get("/sync/v1/manifest", headers=_auth(peer_token)).json()
        assert set(body["files"]) == {"reference/a.md"}


class TestFile:
    def test_served_with_hash_header(self, hub):
        client, token, _ = hub
        response = client.get(
            "/sync/v1/file", params={"path": "reference/a.md"}, headers=_auth(token)
        )
        assert response.content == b"alpha"
        assert response.headers["X-Sextant-Sha256"] == hashlib.sha256(b"alpha").hexdigest()

    def test_denied_and_missing(self, hub):
        client, token, corpus = hub
        (corpus / ".env").write_text("SECRET=1", encoding="utf-8")
        assert client.get(
            "/sync/v1/file", params={"path": ".env"}, headers=_auth(token)
        ).status_code == 403
        assert client.get(
            "/sync/v1/file", params={"path": "../escape.md"}, headers=_auth(token)
        ).status_code == 403
        assert client.get(
            "/sync/v1/file", params={"path": "reference/none.md"}, headers=_auth(token)
        ).status_code == 404

    def test_unreadable_file_returns_clean_409(self, hub, monkeypatch):
        """A manifest-listed file that is unreadable by pull time must be a
        clean per-file 409, not an unhandled 500 (expert pass 2026-10-05
        finding 6; the digest cache can skip re-reading such files)."""
        client, token, _ = hub
        from pathlib import Path
        real = Path.read_bytes

        def denied(self):
            if self.name == "a.md":
                raise PermissionError(13, "denied", str(self))
            return real(self)

        monkeypatch.setattr(Path, "read_bytes", denied)
        r = client.get(
            "/sync/v1/file", params={"path": "reference/a.md"}, headers=_auth(token)
        )
        assert r.status_code == 409
        assert "unreadable" in r.json()["detail"]


def _change(rel, content: bytes, base_sha=None):
    return {
        "path": rel,
        "sha256": hashlib.sha256(content).hexdigest(),
        "base_sha256": base_sha,
        "content_b64": base64.b64encode(content).decode("ascii"),
    }


class TestPush:
    def test_new_file_applies(self, hub):
        client, token, corpus = hub
        body = client.post(
            "/sync/v1/push",
            json={"changes": [_change("projects/new.md", b"fresh")]},
            headers=_auth(token),
        ).json()
        assert body["results"][0]["result"] == "applied"
        assert (corpus / "projects" / "new.md").read_bytes() == b"fresh"

    def test_matching_base_applies_and_versions(self, hub):
        client, token, corpus = hub
        base = hashlib.sha256(b"alpha").hexdigest()
        body = client.post(
            "/sync/v1/push",
            json={"changes": [_change("reference/a.md", b"alpha v2", base)]},
            headers=_auth(token),
        ).json()
        assert body["results"][0]["result"] == "applied"
        assert (corpus / "reference" / "a.md").read_bytes() == b"alpha v2"
        assert list((corpus / "versions" / "reference").glob("a_*_v1.md"))

    def test_stale_base_conflicts_and_quarantines(self, hub):
        client, token, corpus = hub
        body = client.post(
            "/sync/v1/push",
            json={"changes": [_change("reference/a.md", b"divergent", "0" * 64)]},
            headers=_auth(token),
        ).json()
        result = body["results"][0]
        assert result["result"] == "conflict"
        parked = corpus / result["quarantined"]
        assert parked.read_bytes() == b"divergent"
        assert (corpus / "reference" / "a.md").read_bytes() == b"alpha"

    def test_denied_path(self, hub):
        client, token, corpus = hub
        body = client.post(
            "/sync/v1/push",
            json={"changes": [_change(".env", b"SECRET=1")]},
            headers=_auth(token),
        ).json()
        assert body["results"][0]["result"] == "denied"
        assert not (corpus / ".env").exists()

    def test_bad_hash_rejected(self, hub):
        client, token, _ = hub
        change = _change("projects/x.md", b"data")
        change["sha256"] = "f" * 64
        body = client.post(
            "/sync/v1/push", json={"changes": [change]}, headers=_auth(token)
        ).json()
        assert body["results"][0]["result"] == "error"

    def test_bad_base64_rejected(self, hub):
        client, token, _ = hub
        body = client.post(
            "/sync/v1/push",
            json={"changes": [{"path": "a.md", "content_b64": "!!!"}]},
            headers=_auth(token),
        ).json()
        assert body["results"][0]["result"] == "error"

    def test_readonly_device_cannot_push(self, hub):
        client, _, _ = hub
        _, ro_token = devices.pair("viewer", scope="ro")
        response = client.post(
            "/sync/v1/push",
            json={"changes": [_change("a.md", b"x")]},
            headers=_auth(ro_token),
        )
        assert response.status_code == 403

    def test_peer_push_lands_in_inbox(self, hub):
        client, _, corpus = hub
        _, peer_token = devices.pair("friend", kind="peer", prefixes=("reference/",))
        body = client.post(
            "/sync/v1/push",
            json={"changes": [_change("reference/shared.md", b"from friend")]},
            headers=_auth(peer_token),
        ).json()
        result = body["results"][0]
        assert result["result"] == "inbox"
        landed = corpus / result["landed"]
        assert landed.read_bytes() == b"from friend"
        assert not (corpus / "reference" / "shared.md").exists()

    def test_peer_push_outside_allowlist_denied(self, hub):
        client, _, corpus = hub
        _, peer_token = devices.pair("friend2", kind="peer", prefixes=("reference/",))
        body = client.post(
            "/sync/v1/push",
            json={"changes": [_change("identity/user.md", b"takeover")]},
            headers=_auth(peer_token),
        ).json()
        assert body["results"][0]["result"] == "denied"
        assert (corpus / "identity" / "user.md").read_text(encoding="utf-8") == "me"

    def test_empty_and_oversized_batches_rejected(self, hub):
        client, token, _ = hub
        assert client.post(
            "/sync/v1/push", json={"changes": []}, headers=_auth(token)
        ).status_code == 400
        too_many = [_change(f"p/{i}.md", b"x") for i in range(api.MAX_PUSH_CHANGES + 1)]
        assert client.post(
            "/sync/v1/push", json={"changes": too_many}, headers=_auth(token)
        ).status_code == 400


class TestCheckin:
    def test_recorded_and_answers_server_hash(self, hub):
        client, token, _ = hub
        body = client.post(
            "/sync/v1/checkin",
            json={"host": "laptop-01", "manifest_hash": "abc"},
            headers=_auth(token),
        ).json()
        assert body["acknowledged"] is True
        assert body["server_manifest_hash"]
        table = store.load_checkins()
        assert list(table.values())[0]["host"] == "laptop-01"


class TestManifestCache:
    def test_cache_serves_within_ttl_and_push_invalidates(self, hub, monkeypatch):
        client, token, corpus = hub
        monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "300")
        first = client.get("/sync/v1/manifest", headers=_auth(token)).json()
        # a direct disk write is invisible while cached...
        (corpus / "reference" / "sneaky.md").write_text("x", encoding="utf-8")
        cached = client.get("/sync/v1/manifest", headers=_auth(token)).json()
        assert "reference/sneaky.md" not in cached["files"]
        assert cached["manifest_hash"] == first["manifest_hash"]
        # ...but a push invalidates, and the next read sees everything current
        body = client.post(
            "/sync/v1/push",
            json={"changes": [_change("projects/p.md", b"new")]},
            headers=_auth(token),
        ).json()
        assert body["results"][0]["result"] == "applied"
        fresh = client.get("/sync/v1/manifest", headers=_auth(token)).json()
        assert "reference/sneaky.md" in fresh["files"]
        assert "projects/p.md" in fresh["files"]

    def test_ttl_zero_disables_cache(self, hub, monkeypatch):
        client, token, corpus = hub
        monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
        client.get("/sync/v1/manifest", headers=_auth(token))
        (corpus / "reference" / "live.md").write_text("y", encoding="utf-8")
        fresh = client.get("/sync/v1/manifest", headers=_auth(token)).json()
        assert "reference/live.md" in fresh["files"]

    def test_peer_filtering_applies_to_cached_view(self, hub, monkeypatch):
        client, _, _ = hub
        from atlas_plus.sync import devices
        monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "300")
        _, peer_token = devices.pair("cachefriend", kind="peer", prefixes=("reference/",))
        body = client.get("/sync/v1/manifest", headers=_auth(peer_token)).json()
        assert all(p.startswith("reference/") for p in body["files"])


class TestBindGuard:
    """Hardening (quality loop run 14): the docker doc PROMISES the hub
    refuses a non-loopback bind with an empty device registry -- the guard
    existed in api.main() but was never test-pinned, on either entry path."""

    def _run_main(self, monkeypatch, tmp_path, host, devices_present):
        import atlas_plus.sync.api as api
        import atlas_plus.sync.devices as devs

        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
        served = {}
        monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.update(kw))
        reg = tmp_path / "state" / "sync" / "devices.json"
        reg.parent.mkdir(parents=True, exist_ok=True)
        if devices_present == "active":
            devs.pair("laptop", path=reg)
        elif devices_present == "revoked":
            d, _tok = devs.pair("old", path=reg)
            devs.revoke(d.device_id, path=reg)
        # main()'s guard reads the DEFAULT registry path (env-resolved);
        # point it at ours.
        monkeypatch.setattr(devs, "registry_path", lambda: reg)
        monkeypatch.setattr("sys.argv", ["atlas-sync-hub", "--host", host])
        api.main()
        return served

    def test_nonloopback_empty_registry_refuses(self, monkeypatch, tmp_path):
        import pytest as _p
        with _p.raises(SystemExit, match="pair a device first"):
            self._run_main(monkeypatch, tmp_path, "0.0.0.0", devices_present=None)

    def test_nonloopback_revoked_only_refuses(self, monkeypatch, tmp_path):
        import pytest as _p
        with _p.raises(SystemExit, match="pair a device first"):
            self._run_main(monkeypatch, tmp_path, "0.0.0.0", devices_present="revoked")

    def test_loopback_empty_registry_serves(self, monkeypatch, tmp_path):
        served = self._run_main(monkeypatch, tmp_path, "127.0.0.1", devices_present=None)
        assert served.get("host") == "127.0.0.1"

    def test_nonloopback_with_active_device_serves(self, monkeypatch, tmp_path):
        served = self._run_main(monkeypatch, tmp_path, "0.0.0.0", devices_present="active")
        assert served.get("host") == "0.0.0.0"


class TestHealthDisclosure:
    def test_health_reveals_no_corpus_information(self, hub):
        """Unauthenticated liveness must stay liveness: no paths, counts,
        device names, or host details in the body."""
        client, _, _ = hub
        body = client.get("/sync/v1/health").json()
        assert set(body.keys()) <= {"status", "protocol", "server", "role"}
        import json
        text = json.dumps(body).lower()
        for needle in ("memory", "device", "c:\\", "/home", "path"):
            assert needle not in text, f"health leaks {needle!r}: {body}"
