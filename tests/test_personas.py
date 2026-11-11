"""Persona pass, wave 1 (2026-11-10): three simulated humans installed the public build by hand
(a non-technical newcomer, an impatient hobbyist, a careful adopter). Every reproduced finding
is pinned here.

Newcomer: maintainer's workspace path shipped as an autodetect default; temporal build defaulted
to built-in fixture facts; no shell write command; backup prompted for an rclone password;
BOM in PowerShell-saved notes; import hint named a flag that does not exist.
Adopter: re-running a chat import doubled every conversation; report mode claimed imports;
search ignored the write-scope grammar; components ignored ATLAS_WEB_PORT; hooks reported a
clean CRLF repo as dirty (system git config dropped); capture route 500 on a form body.
Hobbyist: guessed commands dead-ended; umbrella --help swallowed; ATLAS_*_PORT=abc traceback;
sync hub had no /health; OLLAMA_HOST ignored; scaffold _README.md files in search results.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    (mem / "reference").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_PROJECTS_ROOT", str(tmp_path / "projects"))
    return mem


# ----------------------------------------------------------------- newcomer

def test_no_workspace_autodetect_of_any_folder(monkeypatch):
    from atlas_plus import paths
    monkeypatch.delenv("ATLAS_WORKSPACE_ROOT", raising=False)
    assert paths.workspace_root() == Path.home()


def test_temporal_build_defaults_to_the_corpus_not_fixtures(corpus, tmp_path):
    from atlas_plus.temporal_graph import cli as tcli
    r = CliRunner().invoke(tcli.app, ["build", "--graph-root", str(tmp_path / "graph")])
    assert r.exit_code == 0, r.output
    assert "fixtures" not in r.output.lower()
    assert "example-tunnel" not in r.output


def test_sextant_remember_then_search(corpus, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sextant"])
    from atlas_plus import cli
    from atlas_plus.mcp.memory_server import config, routing
    monkeypatch.setattr(config, "MEMORY_ROOT", corpus)
    monkeypatch.setattr(routing, "MEMORY_ROOT", corpus)
    r = CliRunner().invoke(cli.app, ["remember", "Dana takes her coffee black with one sugar.",
                                     "--scope", "reference:coffee"])
    assert r.exit_code == 0, r.output
    note = corpus / "reference" / "coffee.md"
    assert note.exists() and "one sugar" in note.read_text(encoding="utf-8")
    r = CliRunner().invoke(cli.app, ["add", "Second fact about coffee.", "--scope", "reference:coffee"])
    assert r.exit_code == 0, r.output


def test_import_hint_names_a_real_command():
    import inspect
    from atlas_plus import import_cmd, cli
    assert "atlas-fts-rebuild --force" not in inspect.getsource(import_cmd)
    assert "atlas-fts-rebuild --force" not in inspect.getsource(cli)


def test_backup_refuses_before_any_rclone_transfer_when_the_remote_is_missing(monkeypatch, tmp_path):
    from atlas_plus import backup as b
    calls = []

    class R:
        returncode = 1
        stdout = ""
        stderr = "no such remote"

    monkeypatch.setattr(b, "_rclone", lambda *a, **k: calls.append(a) or R())
    monkeypatch.setattr(b, "_log", lambda *a, **k: None)
    with pytest.raises(Exception):  # typer Exit
        b.now(dry_run=True, skip_archive=True)
    assert all(a[0] == "listremotes" for a in calls)


def test_rclone_never_prompts_for_a_config_password(monkeypatch):
    from atlas_plus import backup as b
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    monkeypatch.setattr(b.subprocess, "run", fake_run)
    b._rclone("version", capture=True)
    assert "--ask-password=false" in seen[0]


def test_bom_saved_note_reads_clean(corpus, monkeypatch):
    from atlas_plus.mcp.memory_server import config
    from atlas_plus.mcp.memory_server.tools import read
    monkeypatch.setattr(config, "MEMORY_ROOT", corpus)
    monkeypatch.setattr(read, "MEMORY_ROOT", corpus)
    (corpus / "reference" / "bom.md").write_bytes(b"\xef\xbb\xbf---\nid: bom\n---\n\nhello\n")
    r = read.memory_read("reference/bom.md")
    assert r["error"] is None and r["frontmatter"].get("id") == "bom" and "﻿" not in r["body"]


# ----------------------------------------------------------------- adopter

def _conv(title, ts, turns):
    mapping = {}
    for i, (role, text) in enumerate(turns):
        mapping[f"n{i}"] = {"message": {"author": {"role": role}, "create_time": ts + i,
                                        "content": {"parts": [text]}}}
    return {"title": title, "create_time": ts, "mapping": mapping}


def test_chat_imports_are_idempotent_and_report_mode_is_honest(corpus, tmp_path):
    from atlas_plus import import_cmd
    f = tmp_path / "conversations.json"
    f.write_text(json.dumps([_conv("Sourdough", 1735689600, [("user", "starter smells of acetone"),
                                                              ("assistant", "feed it")])]), encoding="utf-8")
    report = import_cmd.import_chatgpt(f, execute=False)
    assert report["imported"] == 0 and report["would_import"] == 1
    first = import_cmd.import_chatgpt(f, execute=True)
    assert first["imported"] == 1
    second = import_cmd.import_chatgpt(f, execute=True)
    assert second["imported"] == 0 and second["skipped_already_imported"] == 1
    assert len(list(corpus.rglob("*sourdough*.md"))) == 1
    c = tmp_path / "claude.json"
    c.write_text(json.dumps([{"name": "Vet", "created_at": "2026-01-02T00:00:00Z",
                              "chat_messages": [{"sender": "human", "text": "rex vet visit"}]}]), encoding="utf-8")
    assert import_cmd.import_claude(c, execute=True)["imported"] == 1
    again = import_cmd.import_claude(c, execute=True)
    assert again["imported"] == 0 and again["skipped_already_imported"] == 1
    assert len(list(corpus.rglob("*vet*.md"))) == 1


def test_cli_search_accepts_the_write_scope_grammar(corpus, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sextant"])
    from atlas_plus import cli
    from atlas_plus.mcp.memory_server import config, routing
    monkeypatch.setattr(config, "MEMORY_ROOT", corpus)
    monkeypatch.setattr(routing, "MEMORY_ROOT", corpus)
    assert CliRunner().invoke(cli.app, ["remember", "budget-app uses ruff for linting",
                                        "--scope", "project:budget-app"]).exit_code == 0
    with patch("atlas_plus.mcp.memory_server.tools.search.memory_search_hybrid",
               side_effect=lambda query, scope, limit: ([{"path": "projects/budget-app.md", "score": 0.9,
                                                          "snippet": "ruff"}] if scope == "projects/budget-app.md" else ([], "fts-only"))) as m:
        # the patched searcher returns a tuple only on the miss branch; emulate the real shape
        def fake(query, scope, limit):
            if scope == "projects/budget-app.md":
                return [{"path": "projects/budget-app.md", "score": 0.9, "snippet": "ruff"}], "fts-only"
            return [], "fts-only"
        m.side_effect = fake
        r = CliRunner().invoke(cli.app, ["search", "ruff", "--scope", "project:budget-app"])
        assert r.exit_code == 0 and "1 hit" in r.output and "confidence: high" in r.output
        r = CliRunner().invoke(cli.app, ["search", "ruff", "--scope", "projects/other.md"])
        assert "empty corpus" not in r.output.lower() and "No hits" in r.output


def test_components_honour_configured_ports(corpus, monkeypatch):
    monkeypatch.setenv("ATLAS_WEB_PORT", "47598")
    monkeypatch.setenv("ATLAS_SYNC_PORT", "59935")
    from atlas_plus import cli
    rows = {c["name"]: c for c in cli.gather_components(probe_ports=False)}
    assert ":47598" in rows["Web dashboard (Helm)"]["what"]
    assert ":59935" in rows["Device sync"]["what"]


def test_git_hardening_keeps_the_system_config():
    from atlas_plus import gitsafe
    assert "GIT_CONFIG_NOSYSTEM" not in gitsafe._env()


def test_capture_route_rejects_non_json_bodies(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "memory").mkdir()
    from fastapi.testclient import TestClient
    from atlas_plus.web import app as web_app
    monkeypatch.setattr(web_app, "API_KEY", "k" * 32)
    monkeypatch.setattr(web_app, "_ALLOW_NO_AUTH", False)
    web_app.AUTH_FAILURES.clear()
    c = TestClient(web_app.app, raise_server_exceptions=False)
    h = {"Authorization": "Bearer " + "k" * 32}
    r = c.post("/api/memory/capture", content=b"fact=hello", headers={**h, "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 400
    r = c.post("/api/memory/capture", content=b"[1]", headers={**h, "Content-Type": "application/json"})
    assert r.status_code == 400
    web_app.AUTH_FAILURES.clear()


# ----------------------------------------------------------------- hobbyist

def test_umbrella_passes_help_through_to_the_real_command(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sextant"])
    from atlas_plus import cli
    r = CliRunner().invoke(cli.app, ["import", "--help"])
    assert "--source" in r.output or "--path" in r.output, r.output


def test_guessed_dashboard_command_gives_a_hint(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["sextant"])
    from atlas_plus import cli
    for guess in ("dashboard", "serve"):
        r = CliRunner().invoke(cli.app, [guess])
        assert r.exit_code == 0 and "sextant-web" in r.output


@pytest.mark.parametrize("raw", ["abc", "70000", "0"])
def test_port_variables_are_validated_with_a_named_message(monkeypatch, raw):
    from atlas_plus import paths
    monkeypatch.setenv("ATLAS_WEB_PORT", raw)
    with pytest.raises(SystemExit, match="ATLAS_WEB_PORT"):
        paths.env_int("ATLAS_WEB_PORT", 8766)
    monkeypatch.delenv("ATLAS_WEB_PORT")
    assert paths.env_int("ATLAS_WEB_PORT", 8766) == 8766


def test_sync_hub_answers_on_health(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "mem").mkdir()
    c = TestClient(api.create_app())
    assert c.get("/health").status_code == 200 and c.get("/sync/v1/health").status_code == 200


def test_ollama_host_is_honoured_as_a_fallback(monkeypatch):
    from atlas_plus.llm.ollama_url import default_ollama_url
    monkeypatch.delenv("ATLAS_BRAIN_OLLAMA_URL", raising=False)
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:1")
    assert default_ollama_url() == "http://127.0.0.1:1"
    monkeypatch.setenv("ATLAS_BRAIN_OLLAMA_URL", "http://10.0.0.5:11434/")
    assert default_ollama_url() == "http://10.0.0.5:11434"


def test_scaffold_readmes_are_not_indexed(corpus, monkeypatch):
    from atlas_plus.mcp.memory_server import config, fts
    monkeypatch.setattr(config, "MEMORY_ROOT", corpus)
    monkeypatch.setattr(fts, "MEMORY_ROOT", corpus)
    monkeypatch.setattr(fts, "FTS_DB", corpus.parent / "state" / "search.sqlite")
    (corpus / "reference" / "_README.md").write_text("# Reference\n\nScaffold notes about dogs.\n", encoding="utf-8")
    (corpus / "reference" / "dog.md").write_text("My dog is named Rex.\n", encoding="utf-8")
    rels = {rel for rel, _ in fts._walk_memory()}
    assert "reference/dog.md" in rels and "reference/_README.md" not in rels


def test_fresh_device_with_no_base_never_pushes_over_a_diverged_hub(tmp_path, monkeypatch):
    """Guard for the hobbyist's reported overwrite: with no base manifest, two different copies
    of the same file are a conflict, never a push (his sequence had pulled the hub edit first)."""
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    hub = tmp_path / "hub"
    spoke = tmp_path / "spoke"
    (hub / "reference").mkdir(parents=True)
    (spoke / "reference").mkdir(parents=True)
    (hub / "reference" / "dog.md").write_text("HUB EDIT", encoding="utf-8")
    (spoke / "reference" / "dog.md").write_text("SPOKE EDIT", encoding="utf-8")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    _, token = devices.pair("fresh")
    client = SyncClient(token=token, http_client=TestClient(api.create_app(), raise_server_exceptions=False))
    r = client.sync_once(spoke)
    assert r.conflicts == ["reference/dog.md"] and r.pushed == []
    assert (hub / "reference" / "dog.md").read_text(encoding="utf-8") == "HUB EDIT"
    assert (spoke / "reference" / "dog.md").read_text(encoding="utf-8") == "SPOKE EDIT"


# =============================================================================
# Wave 2: Tomás (self-hoster on WSL), Noor (claims verifier)
# =============================================================================

def test_manifest_hash_is_content_identity_only():
    from atlas_plus.sync import manifest
    a = {"reference/x.md": {"sha256": "ab" * 32, "size": 10, "mtime": 1}}
    b = {"reference/x.md": {"sha256": "ab" * 32, "size": 10, "mtime": 999}}
    c = {"reference/x.md": {"sha256": "cd" * 32, "size": 10, "mtime": 1}}
    assert manifest.files_hash(a) == manifest.files_hash(b) != manifest.files_hash(c)


def test_atlas_subdirectories_never_sync_but_root_config_does():
    from atlas_plus.sync import policy
    assert policy.deny_reason("_atlas/routing.yaml") is None
    assert policy.deny_reason("_atlas/archive-conflicts/2026-11-10/reference/x.md") is not None
    assert policy.deny_reason("_atlas/intake/ledger.jsonl") is not None


def test_compose_brain_profile_names_the_variable_the_brain_reads():
    text = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "ATLAS_BRAIN_OLLAMA_URL: http://ollama:11434" in text
    assert "OLLAMA_BASE_URL: http://ollama" not in text
    assert "ATLAS_WEB_API_KEY: ${ATLAS_WEB_API_KEY:?" in text


def test_ollama_base_url_is_read_before_ollama_host(monkeypatch):
    from atlas_plus.llm.ollama_url import default_ollama_url
    monkeypatch.delenv("ATLAS_BRAIN_OLLAMA_URL", raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434/")
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:1")
    assert default_ollama_url() == "http://ollama:11434"


def test_forwarded_ip_is_trusted_only_from_loopback(monkeypatch):
    from atlas_plus.auth import forwarded_ip
    monkeypatch.delenv("ATLAS_FORWARDED_IP_HEADER", raising=False)
    h = {"Cf-Connecting-Ip": "203.0.113.9", "X-Forwarded-For": "198.51.100.7, 10.0.0.1"}
    assert forwarded_ip(h, "127.0.0.1") == "203.0.113.9"
    # the LAST non-loopback hop: an appending proxy leaves the first hop client-chosen (wave 4)
    assert forwarded_ip({"X-Forwarded-For": "198.51.100.7, 10.0.0.1"}, "127.0.0.1") == "10.0.0.1"
    assert forwarded_ip({"X-Forwarded-For": "198.51.100.7, 127.0.0.1"}, "127.0.0.1") == "198.51.100.7"
    assert forwarded_ip(h, "192.168.1.20") == ""
    monkeypatch.setenv("ATLAS_FORWARDED_IP_HEADER", "none")
    assert forwarded_ip(h, "127.0.0.1") == ""
    monkeypatch.setenv("ATLAS_FORWARDED_IP_HEADER", "X-Real-IP")
    assert forwarded_ip({"X-Real-IP": "203.0.113.5"}, "::1") == "203.0.113.5"


def test_capture_route_is_an_interactive_write(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "memory").mkdir()
    from fastapi.testclient import TestClient
    from atlas_plus.web import app as web_app
    monkeypatch.setattr(web_app, "API_KEY", "k" * 32)
    monkeypatch.setattr(web_app, "_ALLOW_NO_AUTH", False)
    seen = {}

    def fake_remember(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "file": "x"}

    import importlib
    rem = importlib.import_module("atlas_plus.mcp.memory_server.tools.remember")  # the module object: other tests re-pop the package graph
    monkeypatch.setattr(rem, "memory_remember", fake_remember)
    web_app.AUTH_FAILURES.clear()
    c = TestClient(web_app.app, raise_server_exceptions=False)
    r = c.post("/api/memory/capture", json={"fact": "hello", "scope": "reference:t"},
               headers={"Authorization": "Bearer " + "k" * 32})
    assert r.status_code == 200 and seen.get("interactive") is True
    web_app.AUTH_FAILURES.clear()


def test_status_probe_counts_429_as_degraded():
    import inspect
    from atlas_plus.web import app as web_app
    src = inspect.getsource(web_app)
    assert 'r.status_code == 429' in src


def test_mcp_lockout_spares_the_loopback_operator(monkeypatch):
    from fastapi.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from atlas_plus.mcp.memory_server import server as mcp_srv
    mcp_srv._AUTH_FAILURES.clear()
    mcp_srv._LOCKED_OUT.clear()
    app = Starlette(routes=[Route("/mcp", lambda r: PlainTextResponse("ok"), methods=["POST"])])
    app.add_middleware(mcp_srv._make_auth_middleware("t" * 32))
    c = TestClient(app, client=("127.0.0.1", 50000))  # a real loopback peer, not "testclient"
    for _ in range(12):
        assert c.post("/mcp", headers={"Authorization": "Bearer bad"}).status_code == 401
    assert c.post("/mcp", headers={"Authorization": "Bearer " + "t" * 32}).status_code == 200
    fwd = {"Cf-Connecting-Ip": "203.0.113.9"}
    codes = [c.post("/mcp", headers={"Authorization": "Bearer bad", **fwd}).status_code for _ in range(12)]
    assert 429 in codes
    assert c.post("/mcp", headers={"Authorization": "Bearer " + "t" * 32, **fwd}).status_code == 429
    mcp_srv._AUTH_FAILURES.clear()
    mcp_srv._LOCKED_OUT.clear()


def test_db_backup_commands_have_the_names_the_doctor_prints():
    from atlas_plus.db_backup import cli as db
    names = {c.name for c in db.app.registered_commands}
    assert {"snapshot", "restore", "prune"} <= names


def test_identity_excerpt_is_cut_at_a_line_break(tmp_path):
    from atlas_plus.hooks import session_start
    p = tmp_path / "user.md"
    p.write_text("---\nid: user\n---\n" + "\n".join(f"line {i} " + "x" * 40 for i in range(30)), encoding="utf-8")
    out = session_start._read_file(p, max_chars=600)
    assert out.endswith("\n...") and len(out) <= 600
    assert out[:-4].endswith("x" * 40)  # a whole line, never a cut word


def test_list_tools_survive_a_short_name_corpus_root(tmp_path, monkeypatch):
    """A corpus root spelled as an 8.3 name (or any alias) must not break memory_list."""
    corpus = tmp_path / "Long Name Dir" / "memory"
    (corpus / "reference").mkdir(parents=True)
    (corpus / "reference" / "a.md").write_text("---\nid: a\nlast_confirmed: 2099-01-01\n---\nbody", encoding="utf-8")
    alias = corpus
    if sys.platform == "win32":
        import ctypes
        buf = ctypes.create_unicode_buffer(512)
        if ctypes.windll.kernel32.GetShortPathNameW(str(corpus), buf, 512):
            alias = Path(buf.value)
    from atlas_plus.mcp.memory_server.tools import list as lst
    monkeypatch.setattr(lst, "MEMORY_ROOT", alias)
    lst._cache.clear() if hasattr(lst, "_cache") else None
    assert [r["path"] for r in lst.memory_list(prefix="reference/")] == ["reference/a.md"]
    assert [r["path"] for r in lst.memory_list_confirmed(since_days=36500)] == ["reference/a.md"]


def test_user_requested_decision_ticket_and_anti_are_interactive(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
    from atlas_plus.mcp.memory_server.tools import anti, decision
    with patch("atlas_plus.mcp.memory_server.tools.anti.ANTI_ROOT", tmp_path / "anti"):
        assert anti.anti_remember("libs", "never suggest lodash again please")["ok"] is False
        assert anti.anti_remember("libs", "never suggest lodash again please", interactive=True)["ok"] is True
    import inspect
    assert "interactive=True" in inspect.getsource(__import__("atlas_plus.mcp.memory_server.server", fromlist=["x"]).memory_anti_remember_tool)
    assert "interactive" in inspect.signature(decision.memory_remember_decision).parameters


def test_scope_kind_is_case_insensitive(tmp_path):
    from atlas_plus.mcp.memory_server import routing as mod
    (tmp_path / "reference").mkdir()
    with patch.object(mod, "MEMORY_ROOT", tmp_path):
        a, _ = mod.resolve_target("Reference:Coffee", for_read=True)
        b, _ = mod.resolve_target("reference:coffee", for_read=True)
    assert a == b


def test_stop_hook_reads_the_transcript_root_from_its_payload(tmp_path, monkeypatch):
    import io
    from atlas_plus.summarizer import hook_entry
    root = tmp_path / "claude-projects"
    (root / "proj").mkdir(parents=True)
    tp = root / "proj" / "session.jsonl"
    tp.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"transcript_path": str(tp)})))
    assert hook_entry._transcript_root_from_payload() == str(root.resolve())


# =============================================================================
# Rerun (Owen, Ravi, Mei) against 541124e
# =============================================================================

def test_template_default_frontmatter_is_not_a_fact(tmp_path):
    from atlas_plus.temporal_graph.sources import _FACT_PREDICATES, _TEMPLATE_DEFAULTS
    assert "type" not in _FACT_PREDICATES
    assert "active" in _TEMPLATE_DEFAULTS["status"] and "normal" in _TEMPLATE_DEFAULTS["priority"]


def test_short_keys_refuse_to_start(monkeypatch):
    from atlas_plus.web import app as web_app
    monkeypatch.setattr(web_app, "WEB_HOST", "127.0.0.1")
    monkeypatch.setattr(web_app, "API_KEY", "short")
    monkeypatch.setattr(web_app, "_ALLOW_NO_AUTH", False)
    monkeypatch.setattr(sys, "argv", ["sextant-web"])
    with pytest.raises(SystemExit):
        web_app.main()


def test_peer_proposal_is_not_re_sent_and_counts_as_current(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    hub = tmp_path / "hub"
    peer = tmp_path / "peer"
    (hub / "reference" / "shared").mkdir(parents=True)
    (peer / "reference" / "shared").mkdir(parents=True)
    (peer / "reference" / "shared" / "note.md").write_text("proposal", encoding="utf-8")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    _, token = devices.pair("friend", kind="peer", scope="rw", prefixes=("reference/shared/",))
    client = SyncClient(token=token, http_client=TestClient(api.create_app(), raise_server_exceptions=False))
    first = client.sync_once(peer, share=["reference/shared"])
    assert first.pushed == ["reference/shared/note.md"]
    second = client.sync_once(peer, share=["reference/shared"])
    assert second.pushed == [] and second.proposed == ["reference/shared/note.md"]
    plan, _l, _r = client.plan(peer, share=["reference/shared"])
    assert plan.to_push == [] and plan.proposed == ["reference/shared/note.md"]
    (peer / "reference" / "shared" / "note.md").write_text("proposal v2", encoding="utf-8")
    third = client.sync_once(peer, share=["reference/shared"])
    assert third.pushed == ["reference/shared/note.md"]


def test_sync_client_names_a_dead_hub_plainly(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.sync.client import SyncClient, SyncClientError
    with SyncClient(base_url="http://127.0.0.1:9", token="t", timeout=1.0) as c:
        with pytest.raises(SyncClientError, match="no hub answering"):
            c.remote_manifest()


def test_anti_scope_accepts_the_write_grammar(tmp_path):
    from atlas_plus.mcp.memory_server.tools.anti import _validate_scope
    assert _validate_scope("project:lantern") == "lantern"
    assert _validate_scope("libraries") == "libraries"


def test_decision_is_dated_locally(tmp_path, monkeypatch):
    from datetime import date
    from atlas_plus.mcp.memory_server.tools import decision
    monkeypatch.setattr(decision, "DECISIONS_ROOT", tmp_path / "decisions", raising=False)
    import inspect
    src = inspect.getsource(decision.memory_remember_decision)
    assert "date.today().isoformat()" in src


def test_doctor_strict_ignores_unconfigured_services(monkeypatch):
    import inspect
    from atlas_plus.doctor import cli as doc
    src = inspect.getsource(doc.main)
    assert "expected.get(" in src and '"MCP": bool(os.environ.get("ATLAS_MCP_TOKEN"))' in src


def test_stop_hook_says_why_it_skipped_the_summary(monkeypatch):
    from atlas_plus.summarizer import hook_entry
    monkeypatch.setenv("ATLAS_BRAIN_OLLAMA_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ATLAS_LOCAL_ONLY", "on")
    reason = hook_entry._summarizer_unavailable_reason()
    assert "no local model" in reason


# =============================================================================
# Rerun wave 4 (Ingrid Linux self-hoster, Sam upgrade, Farah claims verifier)
# =============================================================================

def test_servers_do_their_own_forwarded_header_handling():
    """uvicorn's ProxyHeadersMiddleware rewrote the peer before forwarded_ip() ran, so XFF
    always won, Cf-Connecting-Ip was ignored and ATLAS_FORWARDED_IP_HEADER=none half worked."""
    import inspect
    from atlas_plus.sync import api
    from atlas_plus.brain import server as brain
    from atlas_plus.web import app as web
    from atlas_plus.mcp.memory_server import server as mcp
    for mod in (api, brain, web, mcp):
        assert "proxy_headers=False" in inspect.getsource(mod), mod.__name__


def test_atlas_internals_are_never_indexed_or_primed(tmp_path, monkeypatch):
    from atlas_plus.mcp.memory_server import fts, config
    from atlas_plus.hooks import prime
    assert "_atlas" in config.EXCLUDED_DIRS and "_atlas" in prime._EXCLUDED_DIRS
    mem = tmp_path / "mem"
    (mem / "reference").mkdir(parents=True)
    (mem / "reference" / "a.md").write_text("---\nt: a\n---\nour broker is on cedar", encoding="utf-8")
    (mem / "_atlas" / "sync-inbox" / "friend" / "reference").mkdir(parents=True)
    (mem / "_atlas" / "sync-inbox" / "friend" / "reference" / "p.md").write_text("peer proposal cedar cedar", encoding="utf-8")
    (mem / "_atlas" / "sync-conflicts" / "2026-11-11").mkdir(parents=True)
    (mem / "_atlas" / "sync-conflicts" / "2026-11-11" / "x.md").write_text("parked copy cedar", encoding="utf-8")
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
    assert {rel for rel, _ in fts._walk_memory()} == {"reference/a.md"}
    scored = prime._score_memory_files(prime._tokenize("cedar broker"), mem)
    assert [p.name for _s, p in scored] == ["a.md"]


def test_push_conflict_parks_the_hubs_copy_in_the_same_sync(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    hub = tmp_path / "hub"
    spoke = tmp_path / "spoke"
    (hub / "reference").mkdir(parents=True)
    (spoke / "reference").mkdir(parents=True)
    (hub / "reference" / "infra.md").write_text("v1", encoding="utf-8")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    _, token = devices.pair("laptop")
    client = SyncClient(token=token, http_client=TestClient(api.create_app(), raise_server_exceptions=False))
    first = client.sync_once(spoke)
    assert first.pulled == ["reference/infra.md"]
    (hub / "reference" / "infra.md").write_text("hub v2", encoding="utf-8")
    (spoke / "reference" / "infra.md").write_text("spoke v2", encoding="utf-8")
    second = client.sync_once(spoke)
    assert second.conflicts == ["reference/infra.md"]
    parked = second.parked.get("reference/infra.md")
    assert parked and "sync-conflicts" in parked
    assert (spoke / "reference" / "infra.md").read_text(encoding="utf-8") == "spoke v2"
    assert Path(parked).read_text(encoding="utf-8") == "hub v2"


def test_bare_sync_command_lists_its_subcommands(capsys, monkeypatch):
    from atlas_plus.sync import cli as sync_cli
    monkeypatch.setattr(sys, "argv", ["sextant-sync"])
    with pytest.raises(SystemExit) as exc:
        sync_cli.main()
    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "serve" in out and "pair" in out and "{}" not in out


def test_doctor_report_creates_no_databases(tmp_path, monkeypatch):
    from atlas_plus.doctor import cli as doc
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(doc, "STATE_DIR", state)
    assert doc.check_pending_facts()["count"] == 0
    assert doc.check_sla_breaches()["count"] == 0
    assert doc.check_worker_detail()["queue_depth"] == 0
    assert list(state.iterdir()) == []


def test_temporal_rules_bump_retires_template_default_facts(tmp_path):
    from atlas_plus.temporal_graph import sources
    from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
    from atlas_plus.temporal_graph.models import TemporalFact
    root = tmp_path / "mem"
    (root / "projects").mkdir(parents=True)
    (root / "projects" / "coffee.md").write_text(
        "---\ntitle: Coffee\ntype: project\nstatus: active\npriority: normal\nupdated: 2026-05-16\n---\nbody\n",
        encoding="utf-8")
    provider = FileTemporalProvider(graph_root=tmp_path / "graph")
    src = sources.MemoryMarkdownSource(memory_root=root, _extra_allowed_roots=[root])
    manifest = sources.Manifest.load(provider._manifest_file)
    src.ingest(provider, manifest)
    # a graph built by the OLD rules: template defaults were facts, manifest stamped rules=1
    now = sources._now_iso()
    for pred, text in (("has_status", "Coffee status is active."), ("has_priority", "Coffee priority is normal.")):
        provider.add_fact(TemporalFact(
            fact_id=f"f:projects/coffee.md:{pred}", subject_id="memory:projects/coffee.md", predicate=pred,
            object_id="x", fact_text=text, valid_from=now, observed_at=now, ingested_at=now,
            last_verified_at=now, confidence=0.9, authority="memory", evidence_refs=[]))
    manifest.rules = 1
    manifest.save()
    old = sources.Manifest.load(provider._manifest_file)
    assert old.rules == 1
    result = src.ingest(provider, old)
    assert result["files_indexed"] == 1 and result["files_skipped"] == 0
    live = [fid for fid, fact in provider._facts.items() if not fact.invalid_at]
    assert not any(fid.endswith(":has_status") or fid.endswith(":has_priority") for fid in live), live
    assert sources.Manifest.load(provider._manifest_file).rules == sources.RULES_VERSION
    again = src.ingest(provider, sources.Manifest.load(provider._manifest_file))
    assert again["files_skipped"] == 1


def test_clip_inbox_hashes_the_bytes_it_writes():
    import inspect
    from atlas_plus.web import app as web
    assert 'path.write_bytes(payload.encode("utf-8"))' in inspect.getsource(web)


def test_brain_redaction_keeps_uvicorn_access_args():
    import logging
    from atlas_plus.brain.server import _RedactKeys
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("127.0.0.1:1", "GET", "/tasks?key=SECRET", "1.1", 401), None)
    assert _RedactKeys().filter(rec)
    assert rec.args[2] == "/tasks?key=REDACTED" and len(rec.args) == 5
    assert "SECRET" not in rec.getMessage()


def test_interactive_write_refusal_names_the_right_switch(monkeypatch):
    from atlas_plus import autonomous
    monkeypatch.setenv("ATLAS_INTERACTIVE_WRITES", "off")
    monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
    with pytest.raises(RuntimeError, match="ATLAS_INTERACTIVE_WRITES"):
        autonomous.require_write("capture", interactive=True)


def test_doctor_strict_names_why_a_row_counted():
    import inspect
    from atlas_plus.doctor import cli as doc
    src = inspect.getsource(doc.main)
    assert "expected because" in src and "ATLAS_MCP_TOKEN is set" in src


# =============================================================================
# Final install sweep 2026-11-11 (agentic PowerShell / Git Bash, package user, manual 3.11,
# WSL, Docker)
# =============================================================================

_REPO = Path(__file__).resolve().parents[1]


def test_doctor_report_creates_nothing_on_an_empty_workspace(tmp_path):
    """Three checks (temporal, exhausted tasks, spend) created state.sqlite and temporal_graph/
    on a pre-init doctor run while the report itself said 'state.sqlite not found'."""
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith("ATLAS_")}
    env["ATLAS_WORKSPACE_ROOT"] = str(tmp_path)
    env["ATLAS_BRAIN_OLLAMA_URL"] = "http://127.0.0.1:9"
    proc = subprocess.run([sys.executable, "-m", "atlas_plus.doctor.cli"], env=env,
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-800:]
    assert sorted(p.name for p in tmp_path.iterdir()) == [], list(tmp_path.iterdir())
    assert "state.sqlite not found" not in proc.stdout
    assert "no task database yet" in proc.stdout


def test_memory_health_always_names_its_root(tmp_path, monkeypatch):
    from atlas_plus.mcp.memory_server import server
    monkeypatch.setattr(server, "MEMORY_ROOT", tmp_path)
    assert server.memory_health()["memory_root"] == str(tmp_path)
    monkeypatch.setattr(server, "MEMORY_ROOT", tmp_path / "missing")
    bad = server.memory_health()
    assert bad["status"] == "degraded" and bad["memory_root"].endswith("missing")


def test_verify_is_the_packaged_roundtrip():
    from atlas_plus import roundtrip
    assert roundtrip.server_command("py.exe") == ["py.exe", "-m", roundtrip.SERVER_MODULE, "--stdio"]
    cmd = roundtrip.server_command(command="wsl -d Ubuntu -- /home/me/sextant/.venv/bin/python")
    assert cmd[:4] == ["wsl", "-d", "Ubuntu", "--"] and cmd[-3:] == ["-m", roundtrip.SERVER_MODULE, "--stdio"]
    from atlas_plus.cli import app
    res = CliRunner().invoke(app, ["verify", "--help"])
    assert res.exit_code == 0 and "--command" in res.output
    shim = (_REPO / "scripts" / "mcp_roundtrip.py").read_text(encoding="utf-8")
    assert "from atlas_plus.roundtrip import main" in shim


def test_setup_prints_a_registration_for_this_install(monkeypatch, tmp_path):
    from atlas_plus import setup_cmd
    for k in setup_cmd.PATH_VARS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    root = tmp_path / "a b"
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root / "memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(root / "state"))
    info = setup_cmd.describe()
    cmd = info["claude_code"]
    assert cmd.startswith("claude mcp add -s user sextant -e ")
    assert f'-e "ATLAS_MEMORY_ROOT={root / "memory"}"' in cmd
    assert f'-- "{sys.executable}" -m atlas_plus.mcp.memory_server.server --stdio' in cmd
    entry = info["mcp_servers"]["mcpServers"]["sextant"]
    assert entry["command"] == sys.executable and entry["env"]["ATLAS_STATE_DIR"] == str(root / "state")
    hook_cmd = json.loads(json.dumps(info["hooks"]))["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    # POSIX puts the variables on the command line in front; Windows cannot (hooks run via cmd)
    assert f'"{sys.executable}" -m atlas_plus.hooks.session_start' in hook_cmd
    assert hook_cmd.startswith('"') if os.name == "nt" else hook_cmd.startswith("ATLAS_MEMORY_ROOT=")
    assert "claude_code_from_windows" not in info
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    wsl = setup_cmd.describe()["claude_code_from_windows"]
    assert "-- wsl -d Ubuntu -- " in wsl and '"WSLENV=ATLAS_MEMORY_ROOT:ATLAS_STATE_DIR"' in wsl
    from atlas_plus.cli import app
    res = CliRunner().invoke(app, ["setup", "--json"])
    assert res.exit_code == 0 and json.loads(res.output)["verify"] == "sextant verify"
    res = CliRunner().invoke(app, ["setup", "--client", "claude-code"])
    assert res.exit_code == 0 and res.output.strip().startswith("claude mcp add -s user sextant")


def test_in_clone_corpus_is_gitignored_and_sdist_carries_snippets():
    gi = (_REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {"/memory/", "/state/", "/.memory/"} <= set(gi)
    assert "memory/" not in gi  # anchored only: src/atlas_plus/trading/memory/ stays tracked
    manifest = (_REPO / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include deploy *.json" in manifest and "include constraints.txt" in manifest


def test_clock_leads_with_the_local_zone_not_the_authors_city(monkeypatch):
    from atlas_plus.mcp.memory_server.tools import now_state as ns
    assert ns.HOOK_ZONES[0][0] == "local"
    monkeypatch.delenv("ATLAS_HOME_TZ", raising=False)
    line = ns.world_clock_line()
    assert line.startswith("Now: ") and "UTC" in line
    monkeypatch.setenv("ATLAS_HOME_TZ", "Europe/Berlin")
    assert "Berlin" in ns.world_clock_line()
    monkeypatch.setenv("ATLAS_HOME_TZ", "Not/AZone")
    assert ns.home_zone() == ("local", "local")


def test_init_points_at_setup_and_verify(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(sys, "argv", ["sextant-init"])
    from atlas_plus.init_cmd import main as init_main
    with pytest.raises(SystemExit):
        init_main()
    out = capsys.readouterr().out
    assert "sextant setup" in out and "sextant verify" in out and "deploy/" not in out


def test_remember_help_carries_no_internal_history():
    from atlas_plus.cli import app
    res = CliRunner().invoke(app, ["remember", "--help"])
    assert res.exit_code == 0 and "persona pass" not in res.output


def test_sweep6_docs_say_what_the_code_does():
    gs = (_REPO / "docs" / "GETTING-STARTED.md").read_text(encoding="utf-8")
    assert "`sextant-mcp --stdio`" in gs and "sextant setup" in gs and "ATLAS_HOME_TZ" in gs
    ai = (_REPO / "docs" / "AGENT-INSTALL.md").read_text(encoding="utf-8")
    assert "WSLENV" in ai and "-m atlas_plus setup" in ai and "-m atlas_plus verify" in ai
    assert "py --list" in ai and "always wins" in ai and "git-ignored" in ai
    readme = _REPO / "docs" / "README-PUBLIC.md"  # the public tree ships it as README.md
    rp = (readme if readme.exists() else _REPO / "README.md").read_text(encoding="utf-8")
    assert "read docs/AGENT-INSTALL.md in the clone" in rp
    dk = (_REPO / "docs" / "HOWTO-docker.md").read_text(encoding="utf-8")
    assert "set -a; . ./.env; set +a" in dk and "exec mcp sextant verify" in dk
    assert "ATLAS_BRAIN_API_KEY={secrets.token_urlsafe(32)}')\" >> .env" in dk
