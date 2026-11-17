"""AG94 (SEX-017): a hub corpus restored from a backup WITHOUT its state dir (over the live one) is detected per file.

Before: the generation and its marks live in the state dir, so they were untouched; the corpus-hash bump (MN77) made the
older corpus look like a new hub edit, and every device pulled the older copies over its newer ones with rc 0.
"""
from __future__ import annotations

import shutil


def _setup(tmp_path, monkeypatch, *names):
    hub = tmp_path / "hub"
    (hub / "reference").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    app = api.create_app()
    out = [hub]
    for name in names:
        d = tmp_path / name
        (d / "reference").mkdir(parents=True)
        _, token = devices.pair(name)
        out += [d, SyncClient(token=token, http_client=TestClient(app, raise_server_exceptions=False))]
    return out


def _w(root, rel, text):
    (root / rel).write_text(text, encoding="utf-8", newline="\n")


def _r(root, rel):
    p = root / rel
    return p.read_text(encoding="utf-8") if p.is_file() else None


def _restore_corpus_only(hub, backup):
    shutil.rmtree(hub)
    shutil.copytree(backup, hub)  # the state dir (generation, marks, pairings) is left as it is


def _day_two(tmp_path, monkeypatch):
    """laptop and desktop synced; backup of the corpus; laptop edits a.md and writes c.md; the hub writes b.md
    itself; both devices pull everything; then the corpus alone is restored. Returns the newest of every file."""
    hub, laptop, lc, desktop, dc = _setup(tmp_path, monkeypatch, "laptop", "desktop")
    _w(laptop, "reference/a.md", "a v1\n")
    _w(laptop, "reference/b.md", "b v1\n")
    assert sorted(lc.sync_once(laptop).pushed) == ["reference/a.md", "reference/b.md"]
    assert len(dc.sync_once(desktop).pulled) == 2
    shutil.copytree(hub, tmp_path / "backup")
    _w(laptop, "reference/a.md", "a v1\na v2 from laptop\n")
    _w(laptop, "reference/c.md", "c born after the backup\n")
    assert sorted(lc.sync_once(laptop).pushed) == ["reference/a.md", "reference/c.md"]
    _w(hub, "reference/b.md", "b v1\nb v2 written on the hub\n")
    assert len(dc.sync_once(desktop).pulled) == 3
    assert lc.sync_once(laptop).pulled == ["reference/b.md"]
    newest = {rel: _r(laptop, rel) for rel in ("reference/a.md", "reference/b.md", "reference/c.md")}
    _restore_corpus_only(hub, tmp_path / "backup")
    return hub, laptop, lc, desktop, dc, newest


def test_a_corpus_restored_without_its_state_dir_is_not_pulled_over_newer_device_copies(tmp_path, monkeypatch):
    hub, laptop, lc, desktop, dc, newest = _day_two(tmp_path, monkeypatch)
    plan, _l, _r_ = dc.plan(desktop)
    assert plan.to_pull == []
    assert plan.hub_older == ["reference/a.md", "reference/b.md", "reference/c.md"]
    assert not plan.clean
    dry = dc.sync_once(desktop, dry_run=True)
    assert dry.hub_older == plan.hub_older and dry.pulled == []
    for client, root in ((dc, desktop), (lc, laptop), (lc, laptop)):  # a second sync refuses again
        report = client.sync_once(root)
        assert report.pulled == []
        assert any("OLDER" in e for e in report.errors)
        assert report.hub_older == ["reference/a.md", "reference/b.md", "reference/c.md"]
    for root in (laptop, desktop):  # the ledger: every newest edit is still live on every device
        for rel, text in newest.items():
            assert _r(root, rel) == text, (root.name, rel)


def test_push_newer_puts_every_newest_edit_back_on_the_hub(tmp_path, monkeypatch):
    hub, laptop, lc, desktop, dc, newest = _day_two(tmp_path, monkeypatch)
    report = dc.sync_once(desktop, on_older="push")
    assert sorted(report.pushed) == sorted(newest) and not report.errors and not report.conflicts
    for rel, text in newest.items():
        assert _r(hub, rel) == text
    again = lc.sync_once(laptop)
    assert again.pulled == [] and again.pushed == [] and not again.errors and not again.hub_older
    assert dc.plan(desktop)[0].clean


def test_pull_older_is_the_explicit_way_to_take_the_hub_copies(tmp_path, monkeypatch):
    hub, laptop, lc, desktop, dc, newest = _day_two(tmp_path, monkeypatch)
    report = dc.sync_once(desktop, on_older="pull")
    assert sorted(report.pulled) == ["reference/a.md", "reference/b.md"]
    assert _r(desktop, "reference/a.md") == "a v1\n"
    assert _r(desktop, "reference/c.md") == newest["reference/c.md"]  # a file the hub lost is never deleted here
    assert not dc.sync_once(desktop).hub_older  # the device now holds the hub's copies: nothing older to refuse


def test_checkin_says_the_hub_is_older_instead_of_different_state(tmp_path, monkeypatch, capsys):
    hub, laptop, lc, desktop, dc, newest = _day_two(tmp_path, monkeypatch)
    from atlas_plus.sync import cli
    from atlas_plus.sync import client as client_mod

    class _Injected:
        def __init__(self, *_a, **_k):
            pass

        def __enter__(self):
            return dc

        def __exit__(self, *exc):
            return False

    from atlas_plus.sync import api
    monkeypatch.setattr(client_mod, "SyncClient", _Injected)
    monkeypatch.setattr(api, "_corpus_root", lambda: hub)  # the in-process hub keeps its corpus
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(desktop))  # the CLI reads the device's
    import argparse
    rc = cli.cmd_checkin(argparse.Namespace(hub="http://127.0.0.1:9401", token="t", share=None))
    out = capsys.readouterr().out
    assert rc == 0 and "OLDER copies of 3 file(s)" in out and "different corpus state" not in out
    rc = cli.cmd_plan(argparse.Namespace(hub="http://127.0.0.1:9401", token="t", share=None, json=False,
                                         pull_older=False, push_newer=False))
    out = capsys.readouterr().out
    assert rc == 0 and "WARNING" in out and "not pulled (yours kept): reference/a.md" in out
    assert "pull: reference/a.md" not in out


def test_a_pushed_revert_is_new_content_not_a_restore(tmp_path, monkeypatch):
    hub, laptop, lc, desktop, dc = _setup(tmp_path, monkeypatch, "laptop", "desktop")
    _w(laptop, "reference/a.md", "v1\n")
    lc.sync_once(laptop)
    dc.sync_once(desktop)
    _w(laptop, "reference/a.md", "v2\n")
    lc.sync_once(laptop)
    dc.sync_once(desktop)
    _w(laptop, "reference/a.md", "v1\n")  # the laptop reverts on purpose and pushes it
    assert lc.sync_once(laptop).pushed == ["reference/a.md"]
    report = dc.sync_once(desktop)
    assert report.pulled == ["reference/a.md"] and not report.hub_older and not report.errors
    assert _r(desktop, "reference/a.md") == "v1\n"


def test_accept_restore_on_the_hub_lets_devices_take_a_deliberate_rollback(tmp_path, monkeypatch):
    hub, laptop, lc, desktop, dc, newest = _day_two(tmp_path, monkeypatch)
    from atlas_plus.sync import manifest, store
    assert dc.plan(desktop)[0].hub_older
    accepted = store.accept_current(manifest.build_manifest(hub)["files"])
    assert accepted == ["reference/a.md", "reference/b.md", "reference/c.md"]
    report = dc.sync_once(desktop)
    assert sorted(report.pulled) == ["reference/a.md", "reference/b.md"] and not report.hub_older
    assert store.accept_current(manifest.build_manifest(hub)["files"]) == []


def test_a_plain_delete_on_the_hub_is_still_a_reported_deletion(tmp_path, monkeypatch):
    hub, laptop, lc = _setup(tmp_path, monkeypatch, "laptop")
    _w(laptop, "reference/a.md", "a\n")
    _w(laptop, "reference/b.md", "b\n")
    lc.sync_once(laptop)
    (hub / "reference" / "b.md").unlink()
    report = lc.sync_once(laptop)
    assert report.remote_deleted == ["reference/b.md"] and not report.hub_older and not report.errors
    assert _r(laptop, "reference/b.md") == "b\n"
