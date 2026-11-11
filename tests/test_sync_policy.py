"""Tests for atlas_plus.sync.policy -- the fence everything else trusts."""
from __future__ import annotations

import pytest

from atlas_plus.sync import policy


class TestSafeRelpath:
    @pytest.mark.parametrize("rel", [
        "identity/user.md", "reference/a/b/c.md", "top.md", "_atlas/routing.yaml",
    ])
    def test_accepts_normal_paths(self, rel):
        assert policy.is_safe_relpath(rel) is True

    @pytest.mark.parametrize("rel", [
        "", "/abs/path.md", "C:/x.md", "c:\\x.md", "a\\b.md",
        "../escape.md", "a/../b.md", "a/./b.md", "a//b.md", "a/\x00b",
    ])
    def test_rejects_escapes(self, rel):
        assert policy.is_safe_relpath(rel) is False

    def test_rejects_very_long_paths(self):
        assert policy.is_safe_relpath("a/" * 600 + "x.md") is False


class TestSecretFence:
    @pytest.mark.parametrize("rel", [
        ".env", ".env.local", "creds/.env", "keys/server.key", "x.pem",
        "vault.key", "sub/dir/api.secret", "state.sqlite", "db.sqlite-wal",
        "notes.db", ".crypt-passphrase",
    ])
    def test_secrets_never_sync(self, rel):
        assert policy.deny_reason(rel) == "secret (tier 3) never syncs"

    def test_fence_is_superset_of_backup_fence(self):
        from atlas_plus.backup import _SECRET_PATTERNS
        for pattern in _SECRET_PATTERNS:
            assert pattern in policy.SECRET_PATTERNS

    def test_normal_markdown_passes(self):
        assert policy.deny_reason("reference/tokens-explained.md") is None

    @pytest.mark.parametrize("rel", [
        ".ENV", ".Env.Local", "keys/SERVER.KEY", "X.PEM", "Vault.Key",
        "sub/API.Secret", "STATE.SQLITE", "notes.DB",
    ])
    def test_fence_is_case_insensitive_on_every_os(self, rel):
        """normcase lowercases only on Windows -- the fence must still
        catch .ENV / server.KEY on POSIX, where public self-hosters
        actually deploy (hardening, quality loop run 32)."""
        assert policy.deny_reason(rel) == "secret (tier 3) never syncs"


class TestExclusions:
    @pytest.mark.parametrize("rel", [
        "archive/old.md", "versions/x/y.md", "_sync/manifest.json",
        ".git/config", "a/__pycache__/x.pyc", "deep/archive/nested.md",
    ])
    def test_excluded_dirs(self, rel):
        assert policy.deny_reason(rel) == "excluded directory"

    @pytest.mark.parametrize("rel", [
        "_atlas/sync-conflicts/2026-08-02/x.md",
        "_atlas/sync-inbox/peer1/a.md",
    ])
    def test_sync_internal_paths(self, rel):
        assert policy.deny_reason(rel) == "sync-internal path"

    def test_atlas_routing_still_syncs(self):
        assert policy.deny_reason("_atlas/routing.yaml") is None


class TestPrincipals:
    def test_device_default_sees_everything_allowed(self):
        p = policy.Principal(name="laptop")
        assert policy.deny_reason("identity/user.md", p) is None

    def test_device_prefix_restriction(self):
        p = policy.Principal(name="worklaptop", prefixes=("reference/",))
        assert policy.deny_reason("reference/x.md", p) is None
        assert policy.deny_reason("identity/user.md", p) == "outside device scope"

    def test_peer_default_deny(self):
        p = policy.Principal(name="friend", kind="peer")
        assert policy.deny_reason("reference/x.md", p) == "not in peer allowlist"

    def test_peer_allowlist(self):
        p = policy.Principal(name="friend", kind="peer", prefixes=("reference/sip/",))
        assert policy.deny_reason("reference/sip/glossary.md", p) is None
        assert policy.deny_reason("reference/other.md", p) == "not in peer allowlist"
        assert policy.deny_reason("identity/user.md", p) == "not in peer allowlist"

    def test_peer_prefix_no_partial_dirname_match(self):
        p = policy.Principal(name="friend", kind="peer", prefixes=("reference/sip",))
        assert policy.deny_reason("reference/sip-secrets/x.md", p) == "not in peer allowlist"
        assert policy.deny_reason("reference/sip/x.md", p) is None

    def test_readonly_principal_cannot_write(self):
        p = policy.Principal(name="viewer", scope="ro")
        assert policy.deny_reason("reference/x.md", p, "write") == "read-only principal"
        assert policy.deny_reason("reference/x.md", p, "read") is None

    def test_secret_fence_beats_allowlist(self):
        p = policy.Principal(name="friend", kind="peer", prefixes=("creds/",))
        assert policy.deny_reason("creds/.env", p) == "secret (tier 3) never syncs"


class TestFilterManifest:
    def test_drops_denied_entries(self):
        files = {
            "reference/ok.md": {"sha256": "a"},
            "archive/no.md": {"sha256": "b"},
            ".env": {"sha256": "c"},
        }
        p = policy.Principal(name="laptop")
        assert set(policy.filter_manifest(files, p)) == {"reference/ok.md"}

    def test_peer_sees_only_allowlist(self):
        files = {
            "reference/sip/a.md": {"sha256": "a"},
            "reference/b.md": {"sha256": "b"},
            "identity/user.md": {"sha256": "c"},
        }
        p = policy.Principal(name="friend", kind="peer", prefixes=("reference/sip/",))
        assert set(policy.filter_manifest(files, p)) == {"reference/sip/a.md"}
