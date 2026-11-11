"""Sync policy: deny checks compare normalized path components.

Regression tests for the 2026-11-09 data-safety audit row 4: the excluded-dir
and prefix checks were case-sensitive and blind to Win32 trailing-dot/space
trimming, so ".GIT/config", ".git./config", "Versions/..." and
"_ATLAS/sync-conflicts/..." crossed the boundary on Windows and macOS.
"""
from __future__ import annotations

import base64
import hashlib

import pytest

from atlas_plus.sync import apply, policy

ALIASES = [
    ".GIT/config", ".Git/HEAD", ".git./config", ".git /config", ".git. ./config",
    "Versions/x.md", "VERSIONS/a/b.md", "versions./x.md", "ARCHIVE/x.md", "Archive /x.md",
    "_SYNC/x.md", "_Sync./x.md", "notes/.GIT/config", "a/Versions/b.md",
    "_ATLAS/sync-inbox/x.md", "_atlas/SYNC-CONFLICTS/2026-11-09/x.md", "_Atlas/Sync-Inbox/peer/a.md",
    "__PYCACHE__/x.pyc", ".OBSIDIAN/workspace.json",
]

NON_PORTABLE = [
    "notes./x.md", "notes /x.md", "x.md.", "x.md ", "CON", "con.md", "nul.md", "NUL",
    "aux.txt", "prn", "COM1.md", "com9", "LPT1.md", "lpt9.txt", "dir/Com3/x.md",
    "con .md",
]


@pytest.mark.parametrize("rel", ALIASES)
@pytest.mark.parametrize("direction", ["read", "write"])
def test_excluded_aliases_denied_both_directions(rel, direction):
    assert policy.deny_reason(rel, None, direction) is not None
    rw = policy.Principal(name="laptop", kind="device", scope="rw")
    assert policy.deny_reason(rel, rw, direction) is not None


@pytest.mark.parametrize("rel", NON_PORTABLE)
def test_trailing_dot_space_and_device_names_denied(rel):
    assert policy.deny_reason(rel) == "invalid path"


@pytest.mark.parametrize("rel", [
    "identity/user.md", "_atlas/routing.yaml", "notes/console.md", "notes/com10.md",
    "notes/conference.md", "a.b/c.md", "projects/version-notes.md", "archived/x.md",
])
def test_ordinary_paths_still_sync(rel):
    assert policy.deny_reason(rel) is None


def test_normalize_component():
    assert policy.normalize_component(".GIT. ") == ".git"
    assert policy.normalize_component("Versions") == "versions"
    assert policy.is_portable_component("notes.md")
    assert not policy.is_portable_component("notes.md.")
    assert not policy.is_portable_component("LPT1.txt")


def test_apply_change_refuses_git_config_alias(tmp_path):
    root = tmp_path / "corpus"
    (root / ".git").mkdir(parents=True)
    cfg = root / ".git" / "config"
    cfg.write_bytes(b"[core]\n\tbare = false\n")
    payload = b"[core]\n\tfsmonitor = calc.exe\n"
    rw = policy.Principal(name="laptop", kind="device", scope="rw")
    for alias in (".GIT/config", ".git./config", ".git /config"):
        with pytest.raises(apply.SyncPolicyError):
            apply.apply_change(root, alias, payload, hashlib.sha256(payload).hexdigest(), rw)
    assert cfg.read_bytes() == b"[core]\n\tbare = false\n"


def test_hub_push_and_get_of_git_alias_denied(tmp_path, monkeypatch):
    """End to end through the hub: GET and push of `.GIT/config` are refused."""
    from fastapi.testclient import TestClient

    from atlas_plus.sync import api, devices

    corpus = tmp_path / "corpus"
    (corpus / ".git").mkdir(parents=True)
    cfg = corpus / ".git" / "config"
    cfg.write_bytes(b"[core]\n")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(corpus))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    _, token = devices.pair("laptop")
    client = TestClient(api.create_app(), raise_server_exceptions=False)
    auth = {"Authorization": f"Bearer {token}"}
    for alias in (".GIT/config", ".git./config", "Versions/x.md"):
        r = client.get("/sync/v1/file", params={"path": alias}, headers=auth)
        assert r.status_code in (403, 404), (alias, r.status_code)
        assert b"[core]" not in r.content
    payload = b"[core]\n\tfsmonitor = calc.exe\n"
    r = client.post("/sync/v1/push", headers=auth, json={"changes": [{
        "path": ".GIT/config", "sha256": hashlib.sha256(payload).hexdigest(), "base_sha256": None,
        "content_b64": base64.b64encode(payload).decode("ascii")}]})
    assert r.status_code == 200
    # api.py runs its base-hash conflict check before the policy gate, so a
    # push onto an EXISTING denied path is quarantined ("conflict") rather
    # than "denied". Either way the live file must be untouched.
    assert r.json()["results"][0]["result"] in ("denied", "conflict")
    assert cfg.read_bytes() == b"[core]\n"
    # A denied path that does not exist yet reaches the policy gate: denied, not created.
    r = client.post("/sync/v1/push", headers=auth, json={"changes": [{
        "path": ".GIT/hooks/post-checkout", "sha256": hashlib.sha256(payload).hexdigest(),
        "base_sha256": None, "content_b64": base64.b64encode(payload).decode("ascii")}]})
    assert r.json()["results"][0]["result"] == "denied"
    assert not (corpus / ".git" / "hooks").exists()
