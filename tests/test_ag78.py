"""AG78 (sweep 21): a spoke's three-way base belongs to ONE pairing.

The base was always `base-hub.json`, whatever hub or token the spoke used. A hub restored from
an older corpus-only backup (the documented restore) has no pairing registry, so the device
is re-paired; the old base then claimed the device's newer note was "unchanged since the last
sync" and the hub's OLDER copy was pulled over it, silently. A base recorded under another
credential must not anchor the diff: both sides differ, so it is a conflict (nothing is
overwritten).
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from atlas_plus.sync import api, devices, store
from atlas_plus.sync.client import SyncClient


@pytest.fixture
def world(tmp_path, monkeypatch):
    hub_root = tmp_path / "hub-mem"
    spoke_root = tmp_path / "spoke-mem"
    (hub_root / "projects").mkdir(parents=True)
    (hub_root / "projects" / "orca.md").write_text("old fact\n", encoding="utf-8")
    spoke_root.mkdir()
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub_root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    return hub_root, spoke_root


def _client(name):
    _, token = devices.pair(name)
    return SyncClient(token=token, http_client=TestClient(api.create_app(), raise_server_exceptions=False))


def test_repaired_device_against_restored_hub_conflicts_instead_of_reverting(world):
    hub_root, spoke_root = world
    first = _client("laptop")
    first.sync_once(spoke_root)
    newer = "old fact\nnewer fact written after the backup\n"
    (spoke_root / "projects" / "orca.md").write_text(newer, encoding="utf-8")
    assert first.sync_once(spoke_root).pushed == ["projects/orca.md"]
    # The hub is restored from the older backup (corpus only: the pairing registry is gone)
    # and the device is paired again with a new token.
    (hub_root / "projects" / "orca.md").write_text("old fact\n", encoding="utf-8")
    report = _client("laptop-again").sync_once(spoke_root)
    assert (spoke_root / "projects" / "orca.md").read_text(encoding="utf-8") == newer
    assert report.pulled == []
    assert report.conflicts == ["projects/orca.md"]


def test_same_pairing_keeps_its_base_and_legacy_base_is_adopted(world):
    hub_root, spoke_root = world
    client = _client("laptop")
    client.sync_once(spoke_root)
    base_path = store.base_manifest_path("hub")
    data = json.loads(base_path.read_text(encoding="utf-8"))
    data.pop("credential", None)  # a base written by an older build carries no stamp
    base_path.write_text(json.dumps(data), encoding="utf-8")
    (hub_root / "projects" / "orca.md").write_text("hub edit\n", encoding="utf-8")
    report = client.sync_once(spoke_root)
    assert report.pulled == ["projects/orca.md"] and not report.conflicts
    assert json.loads(base_path.read_text(encoding="utf-8")).get("credential")


def test_stop_hook_with_a_moved_workspace_writes_nothing_at_the_old_path(tmp_path, monkeypatch, capsys):
    """The session-start hook said the corpus was missing; the Stop hook wrote a new day log and state
    dir at the old path (AG78: restore to a new drive letter with the hook command left as it was)."""
    import io

    from atlas_plus.summarizer import hook_entry as he

    gone = tmp_path / "old-place" / "ws"
    transcripts = tmp_path / "cc" / "projects" / "slug"
    transcripts.mkdir(parents=True)
    (transcripts / "sess-M.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(gone))
    for var in ("ATLAS_MEMORY_ROOT", "ATLAS_STATE_DIR", "ATLAS_CLAUDE_CODE_ROOT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    payload = {"session_id": "sess-M", "transcript_path": str(transcripts / "sess-M.jsonl")}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(he, "_summarizer_unavailable_reason", lambda: "")
    spawned = []

    class _P:
        def __init__(self, args, **kw):
            spawned.append(args)

    monkeypatch.setattr(he.subprocess, "Popen", _P)
    he._SESSION["id"] = ""
    assert he.main() == 0
    assert spawned == []
    assert not gone.exists()
    assert "missing" in capsys.readouterr().err
