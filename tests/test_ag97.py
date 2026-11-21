"""AG97 (sweep 25, second wave): the stranger's first fifteen minutes.

Each test runs the fixed path and checks the outcome; every one fails on ede0693.
  3  the exact same fact remembered twice is written once ("Already remembered")
  4  `<!-- source:... -->` provenance tags never reach a search snippet (fts, dashboard)
  8  `sextant setup` section 4 honours ATLAS_MCP_PORT
  9  `sextant context` matching notes work for Cyrillic and Japanese tasks
 10  `sync --dry-run` says what a sync WOULD pull and push
  2  the status bar: a stdio install's MCP is a neutral pill; never-configured features have none
  6  the Services tab offers no start/stop for another install's Atlas-* task (and the endpoint refuses)
  7  Brain chat with no Brain: the not-running sentence comes first, not "thinking"
  5  the dashboard ships a keyboard-shortcuts list (palette entry and `?`)
"""
from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("ATLAS_WEB_API_KEY", "test-key-abc")

from fastapi.testclient import TestClient  # noqa: E402

import atlas_plus.web.app as webapp  # noqa: E402

client = TestClient(webapp.app, raise_server_exceptions=False)
AUTH = {"Authorization": f"Bearer {webapp.API_KEY}"}
CLOSED = "http://127.0.0.1:{}"   # ports 9465-9469 are this lane's block; nothing listens there


def _pin_corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.mcp.memory_server import config as _config, fts, routing as _routing, versioning as _versioning
    monkeypatch.setattr(_config, "MEMORY_ROOT", tmp_path / "mem")
    monkeypatch.setattr(_routing, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    monkeypatch.setattr(_routing, "ROUTING_CONFIG", tmp_path / "mem" / "_atlas" / "routing.yaml", raising=False)
    monkeypatch.setattr(_versioning, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "search.sqlite", raising=False)
    monkeypatch.setattr(fts, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    from atlas_plus import init_cmd
    init_cmd.run_init()
    return tmp_path / "mem"


# ---------------------------------------------------------------- 3: remember twice
def test_the_same_fact_twice_is_written_once(tmp_path, monkeypatch):
    mem = _pin_corpus(tmp_path, monkeypatch)
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    fact = "The deploy target for widgets is the blue cluster."
    first = memory_remember("reference:notes", fact, source="human-direct", interactive=True)
    second = memory_remember("reference:notes", fact, source="human-direct", interactive=True)
    assert first["ok"] and not first["already_remembered"]
    assert second["ok"] and second["already_remembered"] and "Already remembered" in second["note"]
    assert second["version_copy"] is None and not second["possible_duplicate"]
    assert (mem / "reference" / "notes.md").read_text(encoding="utf-8").count(fact) == 1
    # a different fact still writes; a fact that only CONTAINS the old one is not "already remembered"
    third = memory_remember("reference:notes", fact + " Since March.", source="human-direct", interactive=True)
    assert third["ok"] and not third["already_remembered"]


def test_cli_remember_twice_says_already_remembered(tmp_path, monkeypatch):
    mem = _pin_corpus(tmp_path, monkeypatch)
    from typer.testing import CliRunner
    from atlas_plus.cli import app

    fact = "Kettle brand is Zojirushi."
    r1 = CliRunner().invoke(app, ["remember", fact])
    r2 = CliRunner().invoke(app, ["remember", fact])
    assert r1.exit_code == 0 and r2.exit_code == 0, (r1.output, r2.output)
    assert "Already remembered" in " ".join(r2.output.split())
    assert (mem / "reference" / "notes.md").read_text(encoding="utf-8").count(fact) == 1


# ---------------------------------------------------------------- 4: snippets
def test_search_snippets_carry_no_provenance_comment(tmp_path, monkeypatch):
    _pin_corpus(tmp_path, monkeypatch)
    from atlas_plus.mcp.memory_server import fts
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    memory_remember("reference:kettle", "Kettle brand is Zojirushi.", source="human-direct", interactive=True)
    hits = fts.search("Zojirushi", limit=5)   # a hit beside the tag
    assert hits
    for h in hits:
        assert "<!--" not in h["snippet"] and "-->" not in h["snippet"], h["snippet"]
    assert fts.search("human", limit=5) == []  # the tag is not indexed at all since AG99 (sweep 26)
    assert "Zojirushi" in fts.search("Zojirushi", limit=1)[0]["snippet"]


def test_snippet_comment_strip_handles_cut_comments_and_keeps_arrows():
    from atlas_plus.mcp.memory_server.fts import strip_snippet_comments as s

    assert s("cluster.  <!-- source:human-dir...") == "cluster. ..."
    assert s("...human-direct session:abc -->  next fact") == "... next fact"
    assert s("a <!-- note --> b") == "a b"
    assert s("build --> deploy") == "build --> deploy"


def test_dashboard_memory_search_shows_no_provenance_comment(tmp_path, monkeypatch):
    mem = _pin_corpus(tmp_path, monkeypatch)
    monkeypatch.setattr(webapp, "MEMORY_ROOT", mem)
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    memory_remember("reference:kettle", "Kettle brand is Zojirushi.", source="human-direct", interactive=True)
    r = client.get("/api/memory/search", params={"q": "Zojirushi"}, headers=AUTH)
    assert r.status_code == 200 and "Zojirushi" in r.text
    assert "&lt;!--" not in r.text and "source:human" not in r.text


# ---------------------------------------------------------------- 8: setup port
def test_setup_section_four_uses_atlas_mcp_port():
    from atlas_plus import setup_cmd

    env = dict(os.environ)
    env["ATLAS_MCP_PORT"] = "9465"
    text = setup_cmd.render(setup_cmd.describe(env), "all")
    sect = text[text.index("4. No Claude Code"):]
    assert "127.0.0.1:9465" in sect and "-p 127.0.0.1:9465:9465" in sect
    assert "http://<host>:9465/mcp" in sect and "http://<host>:9465/health" in sect
    assert "8765" not in sect
    env.pop("ATLAS_MCP_PORT")
    assert "127.0.0.1:8765" in setup_cmd.render(setup_cmd.describe(env), "all")   # the default is unchanged


# ---------------------------------------------------------------- 9: prime tokenizer
@pytest.mark.parametrize("note,task", [
    ("Сервер базы данных стоит в Москве, резервные копии каждую ночь.", "сервер базы данных в Москве"),
    ("本番サーバーのデプロイは金曜日に禁止です。", "本番サーバーのデプロイ"),
    ("Kontoret i Århus har den nye printer.", "aarhus"),   # the digraph spelling, as search folds it
])
def test_context_matching_notes_in_every_script(tmp_path, monkeypatch, note, task):
    from atlas_plus.hooks import prime

    monkeypatch.setattr(prime, "_related_facts", lambda *a, **k: [])
    root = tmp_path / ".memory"
    (root / "reference").mkdir(parents=True)
    (root / "reference" / "the-note.md").write_text(note + "\n", encoding="utf-8")
    out = prime.build_prime_context(task, memory_root=root)
    assert "## Matching notes" in out and "reference/the-note.md" in out, out


# ---------------------------------------------------------------- 10: sync --dry-run
def test_sync_dry_run_says_what_it_would_move(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    (hub / "reference").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    app = api.create_app()
    clients = {}
    for name in ("laptop", "desktop"):
        (tmp_path / name / "reference").mkdir(parents=True)
        _, token = devices.pair(name)
        clients[name] = SyncClient(token=token, http_client=TestClient(app, raise_server_exceptions=False))
    laptop, desktop = tmp_path / "laptop", tmp_path / "desktop"
    (laptop / "reference" / "a.md").write_text("a\n", encoding="utf-8")
    (desktop / "reference" / "d.md").write_text("d\n", encoding="utf-8")
    assert clients["laptop"].sync_once(laptop).pushed == ["reference/a.md"]
    dry = clients["desktop"].sync_once(desktop, dry_run=True)
    assert dry.pulled == [] and not (desktop / "reference" / "a.md").exists()
    assert "would pull 1, would push 1" in dry.summary() and "pulled 0" not in dry.summary()
    real = clients["desktop"].sync_once(desktop)
    assert real.pulled == ["reference/a.md"] and real.pushed == ["reference/d.md"]
    assert real.summary().startswith("pulled 1, pushed 1")


# ---------------------------------------------------------------- 2: status pills
@pytest.fixture()
def nothing_running(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / ".atlas"))
    monkeypatch.setenv("ATLAS_MCP_PORT", "9465")
    monkeypatch.setenv("ATLAS_BRAIN_OLLAMA_URL", CLOSED.format(9466))
    monkeypatch.delenv("ATLAS_MCP_URL", raising=False)
    monkeypatch.setattr(webapp, "BRAIN_URL", CLOSED.format(9467))


def test_status_bar_on_a_fresh_stdio_install(nothing_running, monkeypatch):
    r = client.get("/api/status", headers=AUTH)
    assert r.status_code == 200
    assert "mcp: stdio" in r.text and "mcp: down" not in r.text
    for never_configured in ("maint:", "sec:", "loops:"):
        assert never_configured not in r.text, never_configured
    assert "brain: down" in r.text                     # a missing Brain is still news
    monkeypatch.setenv("ATLAS_MCP_URL", CLOSED.format(9465))   # an HTTP MCP the user configured: down is red
    assert '<span class="text-red-400">mcp: down</span>' in client.get("/api/status", headers=AUTH).text


# ---------------------------------------------------------------- 6: services tab
def test_services_offer_buttons_only_for_this_installs_tasks(monkeypatch):
    ours = f'"{sys.executable}" -m atlas_plus.brain.server'
    foreign = r'powershell.exe -File "D:\other\sextant\deploy\run-brain.ps1"'
    seen: list[str] = []

    def fake_ps(cmd, timeout=25.0):
        seen.append(cmd)
        if "Get-ScheduledTaskInfo" in cmd:   # the list
            return 0, f"Atlas-Brain\tRunning\t0\t{foreign}\nAtlas-MCP\tReady\t0\t{ours}\n"
        if "Start-ScheduledTask" in cmd or "Stop-ScheduledTask" in cmd:
            return 0, ""
        return 0, foreign if "'Atlas-Brain'" in cmd else ours   # the per-task command-line look-up

    monkeypatch.setattr(webapp, "_run_powershell", fake_ps)
    webapp._TTL_CACHE.pop("services-ps", None)
    html = client.get("/api/services", headers=AUTH).text
    assert "Windows Scheduled Tasks found by fixed name" in html
    assert 'svcAction(&quot;Atlas-Brain&quot;' not in html and "another install" in html
    assert 'svcAction(&quot;Atlas-MCP&quot;' in html
    r = client.post("/api/services/Atlas-Brain/stop", headers=AUTH)
    assert r.status_code == 409 and "not this install" in r.json()["detail"]
    assert not any("Stop-ScheduledTask" in c for c in seen)
    r = client.post("/api/services/Atlas-MCP/start", headers=AUTH)
    assert r.status_code == 200 and any("Start-ScheduledTask -TaskName 'Atlas-MCP'" in c for c in seen)
    webapp._TTL_CACHE.pop("services-ps", None)


# ---------------------------------------------------------------- 7: chat without a Brain
def test_chat_without_brain_says_so_first(nothing_running):
    with client.websocket_connect("/ws/chat/ag97", headers=AUTH) as ws:
        ws.send_text("hello")
        first = ws.receive_json()
    assert first["type"] == "error", first
    assert "Brain not running" in first["detail"] and "sextant-brain" in first["detail"]


# ---------------------------------------------------------------- 5: keyboard help
def test_dashboard_ships_a_keyboard_shortcut_list():
    r = client.get("/", headers=AUTH)
    assert r.status_code == 200
    page = r.text
    start = page.index('id="kbd-help"')
    dialog = page[start:page.index("</div>\n</div>", start)]
    for key in ("Ctrl+K", "/", "g d", "?", "Esc"):
        assert key in dialog, key
    assert "label:'Keyboard shortcuts'" in page and "showShortcuts()" in page
