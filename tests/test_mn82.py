"""MN82: after a hub restore, HOWTO-sync's recovery ("archive the parked folder and run sync: your files are
pushed") must actually push. On cbd4e02 the conflict lost its anchor in the restored run and came back on
every sync, on every device, so the newer copies never reached the hub."""
from __future__ import annotations

import shutil

import pytest


def _setup(tmp_path, monkeypatch):
    hub, laptop, desktop = tmp_path / "hub", tmp_path / "laptop", tmp_path / "desktop"
    for d in (hub, laptop, desktop):
        (d / "reference").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    _, t1 = devices.pair("laptop")
    _, t2 = devices.pair("desktop")
    app = api.create_app()
    mk = lambda t: SyncClient(token=t, http_client=TestClient(app, raise_server_exceptions=False))  # noqa: E731
    return hub, laptop, desktop, mk(t1), mk(t2)


_HUB_STATE = ("generation", "generation-marks", "generation-corpus", "devices.json")


def _backup(hub, tmp_path):
    from atlas_plus.sync import store
    shutil.copytree(hub, tmp_path / "bk" / "hub")
    (tmp_path / "bk" / "sync").mkdir()
    for name in _HUB_STATE:
        if (store.sync_dir() / name).exists():
            shutil.copy2(store.sync_dir() / name, tmp_path / "bk" / "sync" / name)


def _restore(hub, tmp_path):
    from atlas_plus.sync import store
    shutil.rmtree(hub)
    shutil.copytree(tmp_path / "bk" / "hub", hub)
    for name in _HUB_STATE:
        if (tmp_path / "bk" / "sync" / name).exists():
            shutil.copy2(tmp_path / "bk" / "sync" / name, store.sync_dir() / name)


def _archive_parked(root, tmp_path, tag):
    src = root / "_atlas" / "sync-conflicts"
    shutil.move(str(src), str(tmp_path / f"archived-{tag}"))


def _restored_with_conflict(tmp_path, monkeypatch):
    hub, laptop, desktop, lc, dc = _setup(tmp_path, monkeypatch)
    (laptop / "reference" / "a.md").write_text("v1\n", encoding="utf-8")
    assert lc.sync_once(laptop).pushed == ["reference/a.md"]
    assert dc.sync_once(desktop).pulled == ["reference/a.md"]
    _backup(hub, tmp_path)
    (laptop / "reference" / "a.md").write_text("v1\nv2 newer\n", encoding="utf-8")
    assert lc.sync_once(laptop).pushed == ["reference/a.md"]
    assert dc.sync_once(desktop).pulled == ["reference/a.md"]
    _restore(hub, tmp_path)
    r = lc.sync_once(laptop)
    assert r.conflicts == ["reference/a.md"] and any("restored from a backup" in e for e in r.errors)
    return hub, laptop, desktop, lc, dc


def test_archiving_the_parked_copy_after_a_restore_pushes_the_newer_file(tmp_path, monkeypatch):
    hub, laptop, desktop, lc, dc = _restored_with_conflict(tmp_path, monkeypatch)
    _archive_parked(laptop, tmp_path, "laptop")
    plan, _, _ = lc.plan(laptop)
    assert plan.to_push == ["reference/a.md"] and not plan.conflicts
    r = lc.sync_once(laptop)
    assert r.pushed == ["reference/a.md"] and not r.conflicts and not r.errors
    assert (hub / "reference" / "a.md").read_text(encoding="utf-8") == "v1\nv2 newer\n"
    r = lc.sync_once(laptop)
    assert not r.conflicts and not r.pushed and not r.pulled and not r.errors
    # the other device already holds the same bytes: in sync, nothing older pulled over it
    r = dc.sync_once(desktop)
    assert not r.pulled and not r.conflicts
    assert (desktop / "reference" / "a.md").read_text(encoding="utf-8") == "v1\nv2 newer\n"
    assert not dc.sync_once(desktop).conflicts


def test_while_the_parked_copy_is_still_there_nothing_is_pushed(tmp_path, monkeypatch):
    hub, laptop, desktop, lc, dc = _restored_with_conflict(tmp_path, monkeypatch)
    r = lc.sync_once(laptop)
    assert r.conflicts == ["reference/a.md"] and not r.pushed
    assert (hub / "reference" / "a.md").read_text(encoding="utf-8") == "v1\n"


def test_a_hub_copy_that_changed_after_the_restore_is_never_overwritten(tmp_path, monkeypatch):
    hub, laptop, desktop, lc, dc = _restored_with_conflict(tmp_path, monkeypatch)
    (hub / "reference" / "a.md").write_text("v1\nwritten on the hub after the restore\n", encoding="utf-8")
    _archive_parked(laptop, tmp_path, "laptop")
    r = lc.sync_once(laptop)
    assert "reference/a.md" in r.conflicts and "reference/a.md" not in r.pushed
    assert (hub / "reference" / "a.md").read_text(encoding="utf-8") == "v1\nwritten on the hub after the restore\n"
    assert (laptop / "reference" / "a.md").read_text(encoding="utf-8") == "v1\nv2 newer\n"


def test_a_local_edit_after_the_restore_is_pushed_too(tmp_path, monkeypatch):
    hub, laptop, desktop, lc, dc = _restored_with_conflict(tmp_path, monkeypatch)
    (laptop / "reference" / "a.md").write_text("v1\nv2 newer\nv3 merged by hand\n", encoding="utf-8")
    _archive_parked(laptop, tmp_path, "laptop")
    r = lc.sync_once(laptop)
    assert r.pushed == ["reference/a.md"]
    assert (hub / "reference" / "a.md").read_text(encoding="utf-8").endswith("v3 merged by hand\n")


def test_the_other_device_never_pushes_a_copy_that_drops_the_hubs_lines(tmp_path, monkeypatch):
    """The desktop also saw the newer hub; after the laptop's recovery it parks the hub's (now newest) copy.
    Archiving it without merging must not push the desktop's older copy over it."""
    hub, laptop, desktop, lc, dc = _restored_with_conflict(tmp_path, monkeypatch)
    (laptop / "reference" / "a.md").write_text("v1\nv2 newer\nv3 merged by hand\n", encoding="utf-8")
    _archive_parked(laptop, tmp_path, "laptop")
    assert lc.sync_once(laptop).pushed == ["reference/a.md"]
    r = dc.sync_once(desktop)
    assert "reference/a.md" in r.conflicts and not r.pushed
    _archive_parked(desktop, tmp_path, "desktop")
    r = dc.sync_once(desktop)
    assert "reference/a.md" not in r.pushed
    assert (hub / "reference" / "a.md").read_text(encoding="utf-8").endswith("v3 merged by hand\n")
    assert (desktop / "reference" / "a.md").read_text(encoding="utf-8") == "v1\nv2 newer\n"
    # the desktop takes the hub's copy as the doc says: converged
    (desktop / "reference" / "a.md").write_text("v1\nv2 newer\nv3 merged by hand\n", encoding="utf-8")
    shutil.rmtree(desktop / "_atlas" / "sync-conflicts", ignore_errors=True)
    r = dc.sync_once(desktop)
    assert not r.conflicts and not r.errors
    assert not lc.sync_once(laptop).conflicts


@pytest.mark.parametrize("raw,want", [
    (" http://127.0.0.1:9241", "use http://127.0.0.1:9241 "),
    ('"http://127.0.0.1:9241"', "use http://127.0.0.1:9241 "),
    ("http//127.0.0.1:9241", "use http://127.0.0.1:9241 "),
    ("https:/127.0.0.1:9241", "use https://127.0.0.1:9241 "),
    ("127.0.0.1:9241", "use http://127.0.0.1:9241 "),
])
def test_near_miss_hub_addresses_get_fitting_advice(raw, want):
    from atlas_plus.sync.client import SyncClientError, _check_hub_url
    with pytest.raises(SyncClientError) as e:
        _check_hub_url(raw)
    assert want in str(e.value) and "http://http" not in str(e.value) and "%20" not in str(e.value)


def test_good_hub_addresses_pass():
    from atlas_plus.sync.client import _check_hub_url
    for ok in ("http://127.0.0.1:9241", "https://hub.example-org.test", "HTTP://127.0.0.1:1"):
        _check_hub_url(ok)

