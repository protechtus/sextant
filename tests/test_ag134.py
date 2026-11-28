"""AG134 (sweep 40, SEX-053 items 16, 9c, 9b and 4c).

Item 16: the dashboard's note viewer (GET /api/memory/file) read a note with
``read_text(encoding="utf-8", errors="replace")``, so a note saved by Notepad as "Unicode" (UTF-16 with a
byte-order mark) showed as replacement characters and NUL pairs, while search and `sextant import` read
it as text (MN96 gave the index `_readahead.read_note_text`). The viewer and its dashboard siblings (the
session viewers, the session search, the weekly narrative, the folder index) now read through it.

Item 9c: the sync client's `refused` map (a file the hub refused, e.g. too large) must survive a run that
cannot see the path (`--share` hides it), exactly as `proposed` does (lesson LES-098): otherwise the next
full run sends the refused file again.

Item 9b: a federation peer's edit of a shared hub file the hub never changed is a proposal in the hub's
inbox; the hub's copy stays. Item 4c: the hub goes away between the batches of a 600-file push (the
second request is refused, as a killed hub refuses it): the hub holds the 500 whole files of batch 1,
nothing partial, and the resume sends exactly the other 100. 9c, 9b and 4c pass on the base commit
(the behaviour was right, it had no test); 9c fails when the MN95 fix of `refused` is reverted.

Every assertion here is the same on Windows, Linux and macOS: the note bytes are written explicitly
(UTF-16 LE with BOM, CRLF line ends) and decoded by the product, no platform encoding is involved.
"""
from __future__ import annotations

import pytest

_FACT = "the backup key lives in the blue binder"


def _utf16_note(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe" + f"# Notepad note\r\n\r\n{_FACT}\r\n".encode("utf-16-le"))
    return path


def _web(monkeypatch, mem):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from atlas_plus.web import app as webapp
    monkeypatch.setattr(webapp, "_ALLOW_NO_AUTH", True, raising=False)
    monkeypatch.setattr(webapp, "API_KEY", "", raising=False)
    monkeypatch.setattr(webapp, "BRAIN_URL", "http://127.0.0.1:9", raising=False)
    monkeypatch.setattr(webapp, "MEMORY_ROOT", mem)
    monkeypatch.setattr(webapp, "_PANEL_CACHE", {}, raising=False)
    return webapp, TestClient(webapp.app)


def test_note_viewer_shows_a_utf16_note_as_text(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    _utf16_note(mem / "reference" / "notepad.md")
    _webapp, c = _web(monkeypatch, mem)
    r = c.get("/api/memory/file", params={"path": "reference/notepad.md"})
    assert r.status_code == 200
    assert _FACT in r.text, r.text[:400]
    assert "\ufffd" not in r.text and "\x00" not in r.text


def test_note_viewer_still_shows_a_utf8_note(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    (mem / "reference").mkdir(parents=True)
    (mem / "reference" / "plain.md").write_bytes(f"# Plain\n\n{_FACT} \u00e9\n".encode("utf-8"))
    _webapp, c = _web(monkeypatch, mem)
    r = c.get("/api/memory/file", params={"path": "reference/plain.md"})
    assert r.status_code == 200 and _FACT in r.text and "\u00e9" in r.text


def test_session_viewer_sibling_shows_a_utf16_session_as_text(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    _utf16_note(mem / "sessions" / "2026-11-28.md")
    _webapp, c = _web(monkeypatch, mem)
    r = c.get("/api/sessions/2026-11-28")
    assert r.status_code == 200
    assert _FACT in r.text and "\ufffd" not in r.text, r.text[:400]


def test_session_search_sibling_finds_a_fact_in_a_utf16_session(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    _utf16_note(mem / "sessions" / "2026-11-28.md")
    _webapp, c = _web(monkeypatch, mem)
    r = c.get("/api/sessions/search", params={"q": "blue binder"})
    assert r.status_code == 200
    assert "2026-11-28" in r.text, r.text[:400]


# -- item 9c: the `refused` map survives a run that cannot see the path (LES-098 sibling) -------------

@pytest.fixture
def older_hub(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    hub_root = tmp_path / "hub-mem"
    spoke_root = tmp_path / "spoke-mem"
    (hub_root / "notes").mkdir(parents=True)
    (hub_root / "notes" / "a.md").write_text("hub a\n", encoding="utf-8")
    (spoke_root / "reference").mkdir(parents=True)
    (spoke_root / "reference" / "big.md").write_text("z" * 150, encoding="utf-8")
    (spoke_root / "notes").mkdir()
    (spoke_root / "notes" / "mine.md").write_text("small\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(spoke_root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    monkeypatch.setattr(api, "_corpus_root", lambda: hub_root)
    monkeypatch.setattr(api, "MAX_FILE_BYTES", 100)
    # an older hub: it refuses the big file but does not advertise its limit, so only `refused` keeps it back
    _orig_m = SyncClient.remote_manifest
    monkeypatch.setattr(SyncClient, "remote_manifest",
                        lambda self: {k: v for k, v in _orig_m(self).items() if k != "limits"})
    sent: list = []
    _orig = SyncClient.push
    monkeypatch.setattr(SyncClient, "push",
                        lambda self, ch, on_batch=None: (sent.extend(c["path"] for c in ch), _orig(self, ch, on_batch))[1])
    _, token = devices.pair("ag134-laptop")
    client = SyncClient(token=token, http_client=TestClient(api.create_app(), raise_server_exceptions=False))
    return client, spoke_root, sent


def test_refused_file_is_not_resent_after_a_share_run_that_hides_it(older_hub):
    client, spoke_root, sent = older_hub
    first = client.sync_once(spoke_root)
    assert sent.count("reference/big.md") == 1
    assert any("big.md" in e for e in first.errors)
    n_first = len(sent)
    narrowed = client.sync_once(spoke_root, share=["notes"])   # this run cannot see reference/big.md
    assert "reference/big.md" not in sent[n_first:]
    assert not [e for e in narrowed.errors if "big.md" in e]
    full = client.sync_once(spoke_root)
    assert sent.count("reference/big.md") == 1, "the refused, unchanged file was sent again after a --share run"
    assert any("not sent again" in e for e in full.errors)


def test_refused_file_edited_meanwhile_is_tried_once_more(older_hub):
    client, spoke_root, sent = older_hub
    client.sync_once(spoke_root)
    client.sync_once(spoke_root, share=["notes"])
    (spoke_root / "reference" / "big.md").write_text("w" * 150, encoding="utf-8")
    client.sync_once(spoke_root)
    assert sent.count("reference/big.md") == 2   # an edit is sent once more (and refused again)


# -- item 9b: a federation peer edits a shared hub file the hub did not change ------------------------

def test_peer_edit_of_an_unchanged_hub_file_is_a_proposal_and_the_hub_copy_stays(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    hub_root = tmp_path / "hub-mem"
    spoke_root = tmp_path / "spoke-mem"
    (hub_root / "reference" / "shared").mkdir(parents=True)
    hub_file = hub_root / "reference" / "shared" / "hub.md"
    hub_file.write_bytes(b"hub side\n")
    spoke_root.mkdir()
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub_root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    _, token = devices.pair("ag134-peer", kind="peer", scope="rw", prefixes=("reference/shared/",))
    client = SyncClient(token=token, http_client=TestClient(api.create_app(), raise_server_exceptions=False))
    first = client.sync_once(spoke_root, share=["reference/shared"])
    assert first.errors == []
    local = spoke_root / "reference" / "shared" / "hub.md"
    assert local.read_bytes() == b"hub side\n"          # pulled
    local.write_bytes(b"hub side\npeer edit\n")         # the peer edits; the hub never changed it
    second = client.sync_once(spoke_root, share=["reference/shared"])
    assert second.errors == [], second.errors
    assert hub_file.read_bytes() == b"hub side\n", "a peer's edit overwrote the hub's copy"
    inbox = [p for p in (hub_root / "_atlas" / "sync-inbox").rglob("hub.md")]
    assert inbox, "the peer's edit is not in the hub's inbox"
    assert inbox[0].read_bytes().replace(b"\r\n", b"\n") == b"hub side\npeer edit\n"
    assert local.read_bytes() == b"hub side\npeer edit\n"   # the peer keeps its edit


# -- item 4c: the hub goes away between the batches of a push of more than 500 files -----------------

def test_hub_gone_between_batches_leaves_no_partial_and_the_resume_sends_exactly_the_rest(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    import httpx
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    hub_root = tmp_path / "hub-mem"
    spoke_root = tmp_path / "spoke-mem"
    hub_root.mkdir()
    d = spoke_root / "projects" / "walk"
    d.mkdir(parents=True)
    for i in range(600):
        (d / f"note-{i:04d}.md").write_bytes(f"# note {i}\n\nfact {i}\n".encode("utf-8"))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(spoke_root))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    monkeypatch.setattr(api, "_corpus_root", lambda: hub_root)
    _, token = devices.pair("ag134-batches")
    http = TestClient(api.create_app(), raise_server_exceptions=False)
    client = SyncClient(token=token, http_client=http)
    pushes = {"n": 0, "down": True}
    real_post = http.post

    def post(url, *a, **k):   # the hub process dies after answering the first batch
        if "/push" in str(url):
            pushes["n"] += 1
            if pushes["n"] >= 2 and pushes["down"]:
                raise httpx.ConnectError("[WinError 10061] connection refused")
        return real_post(url, *a, **k)

    http.post = post  # type: ignore[method-assign]
    sent: list = []
    _orig = SyncClient.push
    monkeypatch.setattr(SyncClient, "push",
                        lambda self, ch, on_batch=None: (sent.extend(c["path"] for c in ch), _orig(self, ch, on_batch))[1])
    first = client.sync_once(spoke_root)
    landed = sorted(p.name for p in hub_root.rglob("*") if p.is_file() and "_atlas" not in p.parts)
    assert len(landed) == 500 and all(n.endswith(".md") for n in landed), len(landed)   # no partial, no temp file
    for n in landed:   # every landed file is whole
        assert (hub_root / "projects" / "walk" / n).read_bytes() == (d / n).read_bytes()
    assert first.errors, "the run did not say the hub went away"
    pushes["down"] = False   # the hub is started again
    sent.clear()
    again = client.sync_once(spoke_root)
    assert again.errors == [], again.errors
    assert len(sent) == 100 and not set(n.rsplit("/", 1)[-1] for n in sent) & set(landed), len(sent)
    assert len([p for p in hub_root.rglob("*.md") if "_atlas" not in p.parts]) == 600
