"""AG88 (sweep 23): sync rough edges from MN77.

Every test drives the real client against the real hub app in process (TestClient), or the
real CLI entry point, and checks what the operator sees or what crossed the wire.
"""
from __future__ import annotations

import shutil
import sys

import pytest
from fastapi.testclient import TestClient

from atlas_plus.sync import api, cli, devices, store
from atlas_plus.sync import client as client_mod
from atlas_plus.sync.client import SyncClient, SyncClientError

_GEN_FILES = ("generation", "generation-marks", "generation-corpus")


@pytest.fixture
def world(tmp_path, monkeypatch):
    hub_root = tmp_path / "hub-mem"
    spoke_root = tmp_path / "spoke-mem"
    (hub_root / "notes").mkdir(parents=True)
    (hub_root / "notes" / "a.md").write_text("hub a\n", encoding="utf-8")
    spoke_root.mkdir()
    # The CLI reads memory_root() as the SPOKE corpus; the in-process hub serves hub_root.
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(spoke_root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    monkeypatch.setattr(api, "_corpus_root", lambda: hub_root)
    _, token = devices.pair("laptop")
    app = api.create_app()
    spoke = SyncClient(token=token, http_client=TestClient(app, raise_server_exceptions=False))

    def factory(*_a, **_k):  # what the CLI's `SyncClient(hub, token)` gets (it closes it after use)
        return SyncClient(token=token, http_client=TestClient(app, raise_server_exceptions=False))

    monkeypatch.setattr(client_mod, "SyncClient", factory)
    monkeypatch.setenv("ATLAS_SYNC_HUB", "http://127.0.0.1:9339")
    monkeypatch.setenv("ATLAS_SYNC_DEVICE_TOKEN", token)
    return spoke, hub_root, spoke_root, tmp_path


def _run_cli(monkeypatch, capsys, *argv) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["sextant-sync", *argv])
    with pytest.raises(SystemExit) as ex:
        cli.main()
    out, err = capsys.readouterr()
    return int(ex.value.code or 0), out, err


def _restore_hub(world_tuple, edit):
    """Back the hub up, let `edit` push something, then put the backup back."""
    spoke, hub_root, spoke_root, tmp = world_tuple
    sync = store.sync_dir()
    backup = tmp / "backup"
    shutil.copytree(hub_root, backup / "mem")
    for name in _GEN_FILES:
        if (sync / name).exists():
            shutil.copy2(sync / name, backup / name)
    edit()
    shutil.rmtree(hub_root)
    shutil.copytree(backup / "mem", hub_root)
    for name in _GEN_FILES:
        (sync / name).unlink(missing_ok=True)
        if (backup / name).exists():
            shutil.copy2(backup / name, sync / name)


# -- 1. a restored hub: plan, checkin and a dry run say so -------------------------------------

class TestRestoredHub:
    def _setup(self, world):
        spoke, hub_root, spoke_root, _ = world
        spoke.sync_once(spoke_root)

        def edit():
            (spoke_root / "notes" / "a.md").write_text("device a v2\n", encoding="utf-8")
            assert spoke.sync_once(spoke_root).pushed == ["notes/a.md"]
        _restore_hub(world, edit)

    def test_plan_flags_restore(self, world, monkeypatch, capsys):
        self._setup(world)
        plan, _l, _r = world[0].plan(world[2])
        assert plan.hub_restored is True
        rc, out, _err = _run_cli(monkeypatch, capsys, "plan")
        assert rc == 0
        assert "restored from a backup" in out
        rc, out, _err = _run_cli(monkeypatch, capsys, "plan", "--json")
        assert '"hub_restored": true' in out

    def test_checkin_flags_restore(self, world, monkeypatch, capsys):
        self._setup(world)
        rc, out, _err = _run_cli(monkeypatch, capsys, "checkin")
        assert "restored from a backup" in out
        assert "different corpus state" not in out

    def test_dry_run_flags_restore(self, world, monkeypatch, capsys):
        self._setup(world)
        rc, out, _err = _run_cli(monkeypatch, capsys, "sync", "--dry-run")
        assert "restored from a backup" in out

    def test_unrestored_hub_is_not_flagged(self, world, monkeypatch, capsys):
        spoke, _hub, spoke_root, _ = world
        spoke.sync_once(spoke_root)
        rc, out, _err = _run_cli(monkeypatch, capsys, "checkin")
        assert out.strip() == "in sync with hub"
        rc, out, _err = _run_cli(monkeypatch, capsys, "plan")
        assert "restored" not in out


# -- 2. push progress per request, counting only what landed -----------------------------------

class TestPushProgress:
    def test_progress_per_batch_and_honest_final_count(self, world, monkeypatch):
        spoke, hub_root, spoke_root, _ = world
        (spoke_root / "bulk").mkdir()
        for i in range(1100):
            (spoke_root / "bulk" / f"n{i:04d}.md").write_text(f"n {i}\n", encoding="utf-8")
        (spoke_root / "bulk" / "big.md").write_text("x" * 300, encoding="utf-8")
        monkeypatch.setattr(api, "MAX_FILE_BYTES", 200)
        # an older hub: it refuses big.md but does not advertise its limit, so the file is sent once
        _orig = SyncClient.remote_manifest
        monkeypatch.setattr(SyncClient, "remote_manifest",
                            lambda self: {k: v for k, v in _orig(self).items() if k != "limits"})
        calls = []
        report = spoke.sync_once(spoke_root, progress=lambda *a: calls.append(a))
        assert len(report.pushed) == 1100
        pushes = [c for c in calls if c[0] == "push"]
        assert len(pushes) >= 3, calls  # one line per request of 500, not one after the whole push
        assert [c[1] for c in pushes] == sorted(c[1] for c in pushes)
        end = [c for c in calls if c[0] == "end"]
        # 1 pull + 1100 pushes landed out of 1102 planned: the refused file is not counted as done
        assert end and end[-1][1] == 1101 and end[-1][2] == 1102

    def test_printer_says_how_many_did_not_land(self, capsys):
        show = cli._progress_printer()
        show("start", 0, 1001, 0, 1001)
        show("push", 500, 1001, 0, 1001)
        show("push", 1000, 1001, 0, 1001)
        show("end", 1000, 1001, 0, 1001)
        err = capsys.readouterr().err
        assert "1000/1001 files transferred (1 not: see below)" in err
        assert "1001/1001" not in err


# -- 3. a hub address without a scheme ----------------------------------------------------------

class TestHubAddress:
    @pytest.mark.parametrize("hub", ["127.0.0.1:9339", "localhost:9339", "ftp://127.0.0.1:9339"])
    def test_client_names_the_scheme(self, hub):
        with pytest.raises(SyncClientError) as ex:
            SyncClient(hub, "t")
        assert "needs http:// or https://" in str(ex.value)
        assert "http://" + hub.split("://")[-1] in str(ex.value)

    @pytest.mark.parametrize("verb", ["plan", "sync", "checkin"])
    def test_cli_names_the_scheme(self, verb, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
        (tmp_path / "mem").mkdir()
        rc, _out, err = _run_cli(monkeypatch, capsys, verb, "--hub", "localhost:9339", "--token", "t")
        assert rc == 2
        assert "needs http:// or https://" in err and "http://localhost:9339" in err
        assert "unsupported protocol" not in err.lower()

    def test_https_and_http_still_accepted(self):
        SyncClient("http://127.0.0.1:9339", "t").close()
        SyncClient("HTTPS://sync.example-org.invalid", "t").close()


# -- 4. files over the hub's limit: named, documented, not re-sent ------------------------------

class TestTooLarge:
    def test_hub_names_its_limit_and_audits(self, world, monkeypatch):
        spoke, _hub, spoke_root, _ = world
        monkeypatch.setattr(api, "MAX_FILE_BYTES", 2 * 1048576)
        import base64, hashlib
        raw = b"y" * (2 * 1048576 + 10)
        answer = spoke.push([{"path": "notes/big.md", "sha256": hashlib.sha256(raw).hexdigest(),
                              "base_sha256": None, "content_b64": base64.b64encode(raw).decode()}])
        detail = answer["results"][0]["detail"]
        assert "2 MB per file" in detail and f"{len(raw):,} bytes" in detail
        assert '"push_error"' in store.audit_path().read_text(encoding="utf-8")

    def test_advertised_limit_never_sends(self, world, monkeypatch, capsys):
        spoke, hub_root, spoke_root, _ = world
        monkeypatch.setattr(api, "MAX_FILE_BYTES", 100)
        (spoke_root / "notes").mkdir()
        (spoke_root / "notes" / "big.md").write_text("z" * 150, encoding="utf-8")
        sent = []
        _orig = SyncClient.push
        monkeypatch.setattr(SyncClient, "push", lambda self, ch, on_batch=None: (sent.extend(c["path"] for c in ch), _orig(self, ch, on_batch))[1])
        report = spoke.sync_once(spoke_root)
        assert "notes/big.md" not in sent
        assert report.too_large == ["notes/big.md"]
        assert any("over the hub's limit of 100 bytes per file" in e for e in report.errors)
        rc, out, _err = _run_cli(monkeypatch, capsys, "plan")
        assert "too large for the hub, not sent: notes/big.md" in out
        assert not (hub_root / "notes" / "big.md").exists()

    def test_older_hub_refusal_is_remembered(self, world, monkeypatch):
        spoke, hub_root, spoke_root, _ = world
        monkeypatch.setattr(api, "MAX_FILE_BYTES", 100)
        _orig_m = SyncClient.remote_manifest
        monkeypatch.setattr(SyncClient, "remote_manifest",
                            lambda self: {k: v for k, v in _orig_m(self).items() if k != "limits"})
        (spoke_root / "notes").mkdir()
        big = spoke_root / "notes" / "big.md"
        big.write_text("z" * 150, encoding="utf-8")
        sent = []
        _orig = SyncClient.push
        monkeypatch.setattr(SyncClient, "push", lambda self, ch, on_batch=None: (sent.extend(c["path"] for c in ch), _orig(self, ch, on_batch))[1])
        first = spoke.sync_once(spoke_root)
        assert sent.count("notes/big.md") == 1
        assert any("this hub accepts at most 100 bytes per file" in e for e in first.errors)
        second = spoke.sync_once(spoke_root)
        assert sent.count("notes/big.md") == 1  # unchanged and refused: not sent again
        assert any("not sent again" in e for e in second.errors)
        big.write_text("w" * 150, encoding="utf-8")  # an edit is tried once more
        spoke.sync_once(spoke_root)
        assert sent.count("notes/big.md") == 2

    def test_howto_names_the_limit(self):
        from pathlib import Path
        doc = Path(__file__).resolve().parents[1] / "docs" / "HOWTO-sync.md"
        if not doc.exists():
            pytest.skip("docs not shipped in this tree")
        text = doc.read_text(encoding="utf-8")
        assert "20 MB per file" in text


# -- 5. devices --all after a re-pair ------------------------------------------------------------

def test_devices_all_shows_ids_for_a_repaired_name(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    first, _ = devices.pair("laptop")
    assert devices.revoke("laptop")
    second, _ = devices.pair("laptop")
    devices.pair("phone")
    rc, out, _err = _run_cli(monkeypatch, capsys, "devices", "--all")
    rows = [line for line in out.splitlines() if "laptop" in line]
    assert len(rows) == 2
    assert first.device_id in rows[0] and second.device_id in rows[1]
    phone = next(line for line in out.splitlines() if "phone" in line)
    assert phone.split()[0] == "phone" and phone.split()[1] == "device"
