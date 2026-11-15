"""MN67 (sweep 21 manual wave, the expert's thirteenth sitting).

1. A device that syncs with two hubs (its own and a federation peer's, `--hub --token`) kept ONE
   base manifest: each sync stamped it with that pairing, so the other hub's next sync found a
   foreign base and every one-sided edit became a conflict. Each pairing now has its own base.
2. The text doctor printed neither the AG74 crash-leftover hint nor the AG78 conflict-copy hint
   (both were JSON-only), and it ignored `.tmp` leftovers that sync refuses.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atlas_plus.sync import api, devices
from atlas_plus.sync.client import SyncClient


@pytest.fixture
def two_hubs(tmp_path, monkeypatch):
    a, b, spoke = tmp_path / "hub-a", tmp_path / "hub-b", tmp_path / "spoke"
    (a / "projects").mkdir(parents=True)
    (b / "projects").mkdir(parents=True)
    (a / "projects" / "mine.md").write_text("mine v1\n", encoding="utf-8")
    (b / "projects" / "theirs.md").write_text("theirs v1\n", encoding="utf-8")
    spoke.mkdir()
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    return a, b, spoke, monkeypatch


def _client(name):
    _, token = devices.pair(name)
    return SyncClient(token=token, http_client=TestClient(api.create_app(), raise_server_exceptions=False))


def test_one_sided_edit_on_either_hub_is_pulled_after_syncing_the_other(two_hubs):
    a, b, spoke, mp = two_hubs
    ca, cb = _client("laptop-a"), _client("laptop-b")

    def on(root, client):
        mp.setenv("ATLAS_MEMORY_ROOT", str(root))
        return client.sync_once(spoke)

    on(a, ca); on(b, cb); on(a, ca); on(b, cb)          # both hubs and the device agree on both files
    (a / "projects" / "mine.md").write_text("mine v2 (edited on hub A)\n", encoding="utf-8")
    r = on(a, ca)
    assert r.conflicts == [] and r.pulled == ["projects/mine.md"]
    on(b, cb)                                           # carries v2 to hub B
    (b / "projects" / "theirs.md").write_text("theirs v2 (edited on hub B)\n", encoding="utf-8")
    r = on(b, cb)
    assert r.conflicts == [] and r.pulled == ["projects/theirs.md"]
    assert (spoke / "projects" / "theirs.md").read_text(encoding="utf-8").startswith("theirs v2")


def test_text_doctor_names_crash_leftovers_and_conflict_copies(tmp_path, monkeypatch):
    root = tmp_path / ".memory"
    (root / "projects").mkdir(parents=True)
    (root / "projects" / "orca.md").write_text("---\ntitle: orca\n---\n- a fact\n", encoding="utf-8")
    (root / "projects" / "orca.md.77-1.w-tmp").write_text("x", encoding="utf-8")
    (root / "projects" / "tmpab12cd.TMP").write_text("x", encoding="utf-8")
    (root / "projects" / "orca (1).md").write_text("---\ntitle: orca\n---\n- a fact\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.doctor import cli as doc
    monkeypatch.setattr(doc, "MEMORY_ROOT", root)  # a module constant: another test may have imported doctor first
    mc = doc.check_memory_corpus()
    assert "projects/tmpab12cd.TMP" in mc["crash_leftovers"]   # sync refuses `.tmp` in any case; doctor named only three suffixes
    text = doc._plain(doc.render_text({"memory_corpus": mc}))
    assert "left by an interrupted write" in text and "orca.md.77-1.w-tmp" in text
    assert "conflict copies" in text and "orca (1).md" in text
