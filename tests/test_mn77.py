"""MN77: a note the hub wrote itself, pulled by a device, then a hub restore from a backup taken before that note."""
from __future__ import annotations

import shutil


def _hub_and_laptop(tmp_path, monkeypatch):
    hub, laptop = tmp_path / "hub", tmp_path / "laptop"
    for d in (hub, laptop):
        (d / "reference").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    _, token = devices.pair("laptop")
    app = api.create_app()
    return hub, laptop, SyncClient(token=token, http_client=TestClient(app, raise_server_exceptions=False))


_HUB_STATE = ("generation", "generation-marks", "generation-corpus", "devices.json")


def _backup(hub, tmp_path):
    from atlas_plus.sync import store
    shutil.copytree(hub, tmp_path / "bk" / "hub")
    (tmp_path / "bk" / "sync").mkdir()
    for name in _HUB_STATE:  # the hub's own sync state (the device's base shares this state dir in the test)
        if (store.sync_dir() / name).exists():
            shutil.copy2(store.sync_dir() / name, tmp_path / "bk" / "sync" / name)


def _restore(hub, tmp_path):
    from atlas_plus.sync import store
    shutil.rmtree(hub)
    shutil.copytree(tmp_path / "bk" / "hub", hub)
    for name in _HUB_STATE:
        if (tmp_path / "bk" / "sync" / name).exists():
            shutil.copy2(tmp_path / "bk" / "sync" / name, store.sync_dir() / name)


def test_a_restore_after_a_hub_local_write_does_not_pull_the_older_copy_over_the_device(tmp_path, monkeypatch):
    hub, laptop, lc = _hub_and_laptop(tmp_path, monkeypatch)
    (laptop / "reference" / "a.md").write_text("v1\n", encoding="utf-8")
    assert lc.sync_once(laptop).pushed == ["reference/a.md"]
    _backup(hub, tmp_path)
    (hub / "reference" / "a.md").write_text("v1\nhub-local fact\n", encoding="utf-8")  # the hub's own assistant wrote it
    assert lc.sync_once(laptop).pulled == ["reference/a.md"]
    _restore(hub, tmp_path)
    r = lc.sync_once(laptop)
    assert (laptop / "reference" / "a.md").read_text(encoding="utf-8") == "v1\nhub-local fact\n"
    assert "reference/a.md" not in r.pulled
    assert any("restored from a backup" in e for e in r.errors)


def test_a_new_hub_local_note_is_not_deleted_silently_either(tmp_path, monkeypatch):
    hub, laptop, lc = _hub_and_laptop(tmp_path, monkeypatch)
    (laptop / "reference" / "a.md").write_text("v1\n", encoding="utf-8")
    lc.sync_once(laptop)
    _backup(hub, tmp_path)
    (hub / "reference" / "b.md").write_text("written on the hub\n", encoding="utf-8")
    assert lc.sync_once(laptop).pulled == ["reference/b.md"]
    _restore(hub, tmp_path)
    r = lc.sync_once(laptop)
    assert any("restored from a backup" in e for e in r.errors)
    assert (laptop / "reference" / "b.md").read_text(encoding="utf-8") == "written on the hub\n"


def test_ordinary_hub_local_writes_and_pushes_raise_no_restore_sentence(tmp_path, monkeypatch):
    hub, laptop, lc = _hub_and_laptop(tmp_path, monkeypatch)
    (laptop / "reference" / "a.md").write_text("v1\n", encoding="utf-8")
    for i in range(3):
        (hub / "reference" / f"h{i}.md").write_text(f"hub {i}\n", encoding="utf-8")
        r = lc.sync_once(laptop)
        assert not r.errors and f"reference/h{i}.md" in r.pulled
        (laptop / "reference" / "a.md").write_text(f"v{i + 2}\n", encoding="utf-8")
        r = lc.sync_once(laptop)
        assert not r.errors and r.pushed == ["reference/a.md"]
    r = lc.sync_once(laptop)
    assert not r.errors and not r.pulled and not r.pushed and not r.conflicts
