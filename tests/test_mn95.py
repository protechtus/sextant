"""MN95: a peer's run with a narrower --share must not forget its proposals (re-sent after review)."""
from __future__ import annotations

import pytest


@pytest.fixture
def peer(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    hub_root = tmp_path / "hub-mem"
    spoke_root = tmp_path / "spoke-mem"
    (hub_root / "reference" / "shared").mkdir(parents=True)
    (hub_root / "reference" / "shared" / "hub.md").write_text("hub side", encoding="utf-8")
    (spoke_root / "reference" / "shared").mkdir(parents=True)
    (spoke_root / "reference" / "shared" / "idea.md").write_text("a peer proposal", encoding="utf-8")
    (spoke_root / "notes").mkdir()
    (spoke_root / "notes" / "mine.md").write_text("outside the hub's allowlist", encoding="utf-8")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub_root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    _, token = devices.pair("mn95-peer", kind="peer", scope="rw", prefixes=("reference/shared/",))
    client = SyncClient(token=token, http_client=TestClient(api.create_app(), raise_server_exceptions=False))
    sent: list[str] = []
    original = client.push

    def recording_push(changes, *args, **kwargs):
        sent.extend(c.get("path", "?") for c in changes)
        return original(changes, *args, **kwargs)

    client.push = recording_push  # type: ignore[method-assign]
    return client, hub_root, spoke_root, sent


def _inbox(hub_root):
    return sorted(p.name for p in (hub_root / "_atlas" / "sync-inbox").rglob("idea.md"))


@pytest.mark.parametrize("other_share", [["notes"], ["reference/elsewhere"]])
def test_narrower_share_run_keeps_the_proposal_memory(peer, other_share):
    client, hub_root, spoke_root, sent = peer
    first = client.sync_once(spoke_root, share=["reference/shared"])
    assert first.errors == [] and sent == ["reference/shared/idea.md"]
    assert _inbox(hub_root) == ["idea.md"]
    again = client.sync_once(spoke_root, share=["reference/shared"])
    assert again.errors == [] and sent == ["reference/shared/idea.md"]  # unchanged: not sent twice
    # the hub's reviewer handles the proposal and archives it out of the inbox
    reviewed = hub_root / "_atlas" / "reviewed"
    (hub_root / "_atlas" / "sync-inbox").rename(reviewed)
    narrowed = client.sync_once(spoke_root, share=other_share)
    assert narrowed.errors == []
    after = client.sync_once(spoke_root, share=["reference/shared"])
    assert after.errors == []
    assert sent == ["reference/shared/idea.md"], "an unchanged, already reviewed proposal was sent again"
    assert _inbox(hub_root) == []
    assert after.proposed == ["reference/shared/idea.md"]


def test_an_edited_proposal_is_still_proposed_again(peer):
    client, hub_root, spoke_root, sent = peer
    client.sync_once(spoke_root, share=["reference/shared"])
    client.sync_once(spoke_root, share=["notes"])
    (spoke_root / "reference" / "shared" / "idea.md").write_text("a peer proposal, edited", encoding="utf-8")
    client.sync_once(spoke_root, share=["reference/shared"])
    assert sent == ["reference/shared/idea.md", "reference/shared/idea.md"]
    # the hub keeps each revision beside the first (idea.md, idea_v1.md)
    texts = {p.read_text(encoding="utf-8") for p in (hub_root / "_atlas" / "sync-inbox").rglob("idea*.md")}
    assert texts == {"a peer proposal", "a peer proposal, edited"}


def test_accept_restore_names_the_files_the_hub_no_longer_holds(monkeypatch, capsys, tmp_path):
    import argparse
    from atlas_plus.sync import cli as sync_cli
    (tmp_path / "projects").mkdir()
    (tmp_path / "projects" / "kept.md").write_text("restored copy", encoding="utf-8")
    monkeypatch.setattr(sync_cli.atlas_paths, "memory_root", lambda: tmp_path)
    seen = {}

    def accept(files):
        seen.update(files)
        return ["projects/gone.md", "projects/kept.md"]

    monkeypatch.setattr(sync_cli.store, "accept_current", accept)
    assert sync_cli.cmd_accept_restore(argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert "projects/kept.md" in seen and "projects/gone.md" not in seen  # the real manifest of the corpus
    pull_part, _, gone_part = out.partition("no longer holds")
    assert "accepted 1 file(s) as the hub's newest copies" in pull_part and "projects/kept.md" in pull_part
    assert "projects/gone.md" not in pull_part, "a file the hub lost was listed as one the devices pull"
    assert "projects/gone.md" in gone_part and "KEEP" in gone_part


def test_hub_gone_mid_batch_does_not_claim_nothing_arrived(capsys):
    from atlas_plus.sync import cli as sync_cli
    show = sync_cli._progress_printer()
    show("start", 0, 300, 0, 300)
    show("end", 0, 300, 0, 300)
    err = capsys.readouterr().err
    assert "before any file was transferred" not in err and "confirmed no file (0 of 300" in err
