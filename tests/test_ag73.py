"""AG73 (sweep 21 agentic wave): reflected ids, the palette Space, a 401 that stops the whole sync,
`--all` checking the corpus target first, the cheap snippet's bracket, and the keyword-line carry."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest


# ---------------------------------------------------------------- web
@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_WEB_API_KEY", "ag73-test-key-" + "0123456789abcdef")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_BRAIN_URL", "http://127.0.0.1:9")
    from fastapi.testclient import TestClient
    from atlas_plus.tickets import store as tstore
    from atlas_plus.web import app as webapp
    monkeypatch.setattr(tstore, "TICKETS_ROOT", tmp_path / "mem" / "tickets")
    monkeypatch.setattr(webapp, "API_KEY", "ag73-test-key-" + "0123456789abcdef", raising=False)
    d = tmp_path / "mem" / "tickets" / "alpha"
    d.mkdir(parents=True)
    (d / "ALP-001.md").write_text("---\nid: ALP-001\nproject: alpha\nstatus: todo\ntitle: t\ntype: ticket\n---\nbody\n", encoding="utf-8")
    c = TestClient(webapp.app, raise_server_exceptions=False)
    c.headers["Authorization"] = "Bearer ag73-test-key-" + "0123456789abcdef"
    return c


@pytest.mark.parametrize("path", ["/api/tickets/{p}/detail", "/api/tasks/{p}/detail", "/api/sessions/{p}"])
def test_not_found_fragments_escape_the_id(web, path):
    payload = "%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E"
    r = web.get(path.format(p=payload))
    assert "<img" not in r.text and "&lt;img" in r.text


def test_dispatch_accepts_a_lowercase_qualified_id_like_its_siblings(web):
    assert web.get("/api/tickets/alpha/alp-001/detail").text.count("ALP-001") >= 1
    r = web.post("/api/tickets/alpha/alp-001/dispatch", json={})
    assert r.status_code == 503  # found; the Brain is a closed port (was 404)


def test_kbdify_ignores_keys_that_bubble_from_inside():
    from atlas_plus.web import app as webapp
    import inspect
    page = inspect.getsource(webapp)
    i = page.index("function _kbdify")
    js = page[i:page.index("}});\n}}", i)]
    assert "ev.target!==el" in js.replace(" ", "")


# ---------------------------------------------------------------- sync
def test_a_token_revoked_mid_sync_stops_every_pass(tmp_path, monkeypatch):
    hub, spoke = tmp_path / "hub", tmp_path / "spoke"
    (hub / "reference").mkdir(parents=True)
    (spoke / "reference").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices, store
    from atlas_plus.sync.client import SyncClient
    _, tok = devices.pair("laptop")
    c = SyncClient(token=tok, http_client=TestClient(api.create_app(), raise_server_exceptions=False))
    for i in range(3):
        (hub / "reference" / f"c{i}.md").write_text("base", encoding="utf-8")
        (hub / "reference" / f"pull{i}.md").write_text("hub", encoding="utf-8")
    c.sync_once(spoke)
    for i in range(3):
        (hub / "reference" / f"c{i}.md").write_text("hub v2", encoding="utf-8")
        (spoke / "reference" / f"c{i}.md").write_text("spoke v2", encoding="utf-8")
        (hub / "reference" / f"pull{i}.md").write_text("hub v2", encoding="utf-8")
        (spoke / "reference" / f"push{i}.md").write_text("new", encoding="utf-8")
    before = dict(store.load_base_manifest("hub")["files"])
    orig = c.remote_manifest

    def _revoke_after_manifest():
        m = orig()
        devices.revoke("laptop")
        return m

    c.remote_manifest = _revoke_after_manifest
    r = c.sync_once(spoke)
    assert len(r.errors) == 1 and "sync stopped" in r.errors[0], r.errors
    assert not r.parked and not r.pushed
    assert store.load_base_manifest("hub")["files"] == before  # nothing adopted, nothing lost
    _, tok2 = devices.pair("laptop2")
    c2 = SyncClient(token=tok2, http_client=TestClient(api.create_app(), raise_server_exceptions=False))
    r2 = c2.sync_once(spoke)
    assert sorted(r2.pushed) == [f"reference/push{i}.md" for i in range(3)]
    # a new pairing does not trust the old pairing's base (AG78), so the hub's newer pulls are conflicts too: never a silent overwrite
    assert sorted(r2.conflicts) == sorted([f"reference/c{i}.md" for i in range(3)] + [f"reference/pull{i}.md" for i in range(3)])


# ---------------------------------------------------------------- reset / uninstall --all
@pytest.mark.skipif(os.name != "nt", reason="NTFS junction")
@pytest.mark.parametrize("argv", [["reset", "--all", "--yes"], ["uninstall", "--all", "--yes"]])
def test_all_refuses_a_junction_corpus_before_touching_state(tmp_path, argv):
    ws = tmp_path / "ws"
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_")}
    env["ATLAS_WORKSPACE_ROOT"] = str(ws)
    ws.mkdir()
    subprocess.run([sys.executable, "-c", "from atlas_plus import init_cmd; init_cmd.run_init(dry_run=False)"],
                   env=env, capture_output=True, check=True)
    real = ws / ".memory"
    assert real.is_dir()
    link = ws / "corp-link"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], capture_output=True, check=True)
    state = ws / ".atlas"
    state.mkdir(exist_ok=True)
    (state / ".sextant-state").write_text("", encoding="utf-8")
    sqlite3.connect(str(state / "search.sqlite")).close()
    env["ATLAS_MEMORY_ROOT"] = str(link)
    extra = ["--confirm-path", str(link)] + (["--confirm-state-path", str(state)] if argv[0] == "uninstall" else [])
    mod = "atlas_plus.reset_cmd" if argv[0] == "reset" else "atlas_plus.uninstall_cmd"
    p = subprocess.run([sys.executable, "-m", mod] + argv[1:] + extra, env=env, capture_output=True, text=True)
    assert p.returncode == 1
    assert (state / "search.sqlite").exists(), p.stdout  # the state step never ran
    assert "junction" in p.stdout + p.stderr  # reset's refusal moved to stderr with a Next line (AG103)
    if argv[0] == "reset":
        assert "python -m atlas_plus.reset_cmd" in p.stderr and "--dry-run" in p.stderr  # a command that exists
        assert "--yes" not in p.stderr.split("Next:", 1)[-1]


# ---------------------------------------------------------------- fts cheap snippet
def _snip(body: str, query: str) -> str:
    from atlas_plus.mcp.memory_server import fts
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memory_fts (body TEXT)")
    conn.execute("INSERT INTO memory_fts (rowid, body) VALUES (1, ?)", (fts._norm_index_text(body),))
    return fts._cheap_snippet(conn, 1, query)


def test_cheap_snippet_bracket_survives_length_changing_casefold():
    s = _snip("ǰ " * 400 + "filler " * 50 + "suchziel tail", "suchziel")
    assert "[suchziel]" in s


def test_cheap_snippet_brackets_a_cjk_term():
    s = _snip("filler " * 50 + "大阪城の歴史", "大阪城")
    assert "[" in s and "大" in s.replace("⁠", "")


# ---------------------------------------------------------------- keyword-line redaction
def test_word_and_digits_in_the_value_slot_is_masked_names_are_not():
    from atlas_plus.redaction import redact
    v = "Summer" + "2024"
    assert v not in redact("the root password on the box is " + v)
    assert ("Admin" + "1234") not in redact("das Passwort lautet " + "Admin" + "1234")
    assert redact("reset the password on Windows11 first") == "reset the password on Windows11 first"


@pytest.mark.parametrize("gap", ["\n\n", "\n```\n", "\n\n```\n"])
def test_carry_survives_a_blank_or_fence_line(gap):
    from atlas_plus.redaction import redact
    v = "Tr0ub4dor" + "&3x" + "Q"
    assert v not in redact("The root password is below:" + gap + v)


def test_an_already_redacted_line_does_not_carry():
    from atlas_plus.redaction import redact
    out = redact("password: " + "hunter2" + "Secret9\n" + "Build" + "Id7Xq")
    assert out.endswith("\nBuild" + "Id7Xq")
