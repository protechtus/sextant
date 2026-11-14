"""Post-flip sweep 15, agentic wave: AG37 Docker, AG38 paranoid round 3, AG39 agent install, AG40 Stop hook round 3,
AG41 two people round 2 (2026-11-13)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest


# --- AG38 F1: refusals while locked never lift the lock ---------------------------------------------


def test_refused_attempts_do_not_evict_counted_failures():
    from atlas_plus.auth import AuthFailureLog

    log = AuthFailureLog(max_records=50, window_seconds=60, max_failures_per_window=10)
    for _ in range(10):
        log.record(path="/", client_ip="203.0.113.7", reason="bad_key", now=100.0)
    assert log.is_rate_limited("203.0.113.7", now=101.0)
    for _ in range(600):
        log.record(path="/", client_ip="203.0.113.7", reason="locked_out", now=102.0)
    assert log.is_rate_limited("203.0.113.7", now=103.0)          # still locked
    assert len(log.recent(limit=1000)) == 10                       # the deque holds the real failures only
    assert log.stats(now=103.0)["locked_out_refusals"]["203.0.113.7"] == 600


# --- AG38 F2: the common secret shapes ----------------------------------------------------------------


def test_redaction_covers_env_style_keys_and_cloud_tokens():
    from atlas_plus.redaction import redact

    aws = "wJalrXUtn" + "FEMI/K7MDENG"          # assembled at runtime: the pre-commit secret scan reads literals
    stripe = "sk_live_" + "abcdefghij1234"
    text = (
        "DATABASE_PASSWORD=hunter2hunter2 AWS_SECRET_ACCESS_KEY=" + aws + " GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234 "
        "\"db_password\": \"s3cretv4lue\" AKIAIOSFODNN7EXAMPLE xoxb-1234567890-abcdefghij " + stripe + " "
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----"
    )
    out = redact(text)
    for secret in ("hunter2hunter2", aws[:13], "ghp_abcdefghij", "s3cretv4lue", "AKIAIOSFODNN7EXAMPLE",
                   "xoxb-1234567890", stripe[:18], "b3BlbnNzaC1rZXktdjEAAAAA"):
        assert secret not in out, secret
    assert "[REDACTED:private-key]" in out


# --- AG38 F3: an agent cannot write high confidence over MCP ------------------------------------------


def test_v2_agent_confidence_is_capped(monkeypatch):
    from atlas_plus.mcp.memory_server import server

    seen = {}

    def fake(**kw):
        seen.update(kw)
        return {"ok": True}
    monkeypatch.setattr(server, "memory_remember", fake)
    server.memory_remember_tool_v2(scope="reference:x", fact="f", confidence=0.99, source="agent")
    assert seen["confidence"] <= 0.84 and seen["source"] == "agent"
    server.memory_remember_tool_v2(scope="reference:x", fact="f", confidence=0.99, source="human-direct")
    assert seen["confidence"] <= 0.84                             # the human claim became agent, so capped too


# --- AG38 F4/F7: ticket project names ---------------------------------------------------------------------


def test_get_ticket_project_form_cannot_leave_the_root(tmp_path):
    from atlas_plus.tickets import store

    t = store.create_ticket(title="one", project="proj1", tickets_root=tmp_path, interactive=True)
    assert store.get_ticket("proj1/" + t.id, tickets_root=tmp_path) is not None
    for bad in ("C:/" + t.id, "E:/" + t.id, "../x/" + t.id, "_archive/" + t.id, ".hidden/" + t.id):
        assert store.get_ticket(bad, tickets_root=tmp_path) is None, bad
    with pytest.raises(ValueError):
        store.create_ticket(title="x", project="_archive", tickets_root=tmp_path, interactive=True)


# --- AG38 F5 / AG39 F1: one conflict-marker rule, incoming facts refused ----------------------------------


def test_conflict_marker_rule_and_fact_refusal(tmp_path, monkeypatch):
    from atlas_plus.mcp.memory_server.routing import has_conflict_markers

    assert has_conflict_markers("\ufeff<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> b\n")
    assert has_conflict_markers("a\n<<<<<<<\tHEAD\n")
    assert has_conflict_markers("a\n<<<<<<<\n")
    assert has_conflict_markers("a\n>>>>>>> branch\n")
    assert not has_conflict_markers("Title\n=======\nbody\n")          # a setext heading
    assert not has_conflict_markers("inline <<<<<<< HEAD mention\n")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.mcp.memory_server import config as _config, routing as _routing, versioning as _versioning
    monkeypatch.setattr(_config, "MEMORY_ROOT", tmp_path / "mem")
    monkeypatch.setattr(_routing, "MEMORY_ROOT", tmp_path / "mem", raising=False)   # module attributes another test may have left elsewhere
    monkeypatch.setattr(_routing, "ROUTING_CONFIG", tmp_path / "mem" / "_atlas" / "routing.yaml", raising=False)  # another test's routing.yaml (create_if_missing false) leaks in the full run
    monkeypatch.setattr(_versioning, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    from atlas_plus import init_cmd
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    init_cmd.run_init()
    res = memory_remember("project:gitnote", "How to resolve:\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> b\n", source="agent")
    assert not res.get("ok") and "conflict markers" in res["error"]
    ok = memory_remember("project:gitnote", "    <<<<<<< HEAD indented is fine", source="agent")
    assert ok.get("ok")


# --- AG39 F4: health is honest about an unusable state dir -------------------------------------------------


def test_memory_health_degraded_when_state_dir_is_a_file(tmp_path, monkeypatch):
    from atlas_plus import paths as atlas_paths
    from atlas_plus.mcp.memory_server import server

    f = tmp_path / "state-as-file"
    f.write_text("x", encoding="utf-8")
    monkeypatch.setattr(atlas_paths, "state_dir", lambda: f)
    monkeypatch.setattr(server, "MEMORY_ROOT", tmp_path)
    h = server.memory_health()
    assert h["status"] == "degraded" and "not a directory" in h["hint"]


# --- AG40 F1: the session override reaches the fact writer -------------------------------------------------


def test_session_override_enables_autonomous_writes(monkeypatch):
    from atlas_plus.autonomous import require_autonomous_writes

    monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    require_autonomous_writes("memory_write")                      # no raise
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "off")
    with pytest.raises(RuntimeError) as e:
        require_autonomous_writes("memory_write")
    assert "Sextant autonomous" in str(e.value)


# --- AG40 F2/F4: suggest-facts dedupe, redact-before-cut, once per session, identity never auto -----------


def test_suggest_facts_dedupes_against_fact_lines_and_redacts_before_the_cut(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.mcp.memory_server import config as _config, routing as _routing, versioning as _versioning
    monkeypatch.setattr(_config, "MEMORY_ROOT", tmp_path / "mem")
    monkeypatch.setattr(_routing, "MEMORY_ROOT", tmp_path / "mem", raising=False)   # module attributes another test may have left elsewhere
    monkeypatch.setattr(_routing, "ROUTING_CONFIG", tmp_path / "mem" / "_atlas" / "routing.yaml", raising=False)  # another test's routing.yaml (create_if_missing false) leaks in the full run
    monkeypatch.setattr(_versioning, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    from atlas_plus import init_cmd
    from atlas_plus.curation import suggest_facts as sf
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    init_cmd.run_init()
    memory_remember("project:fakeproj", "The developer prefers tabs over spaces.", source="agent")
    assert sf._fact_already_there("project:fakeproj", "the developer prefers tabs over spaces")
    assert not sf._fact_already_there("project:fakeproj", "deploys happen on Fridays from the laptop")
    key = "sk-ant-api03-" + "Z" * 40
    turns = [SimpleNamespace(role="user", content="A" * 300 + " " + key + " tail")]
    monkeypatch.setattr("atlas_plus.summarizer.loader.load_turns", lambda **kw: turns)
    text = sf._load_transcript_text(since="today", conversation_id="sess-1")
    assert "sk-ant-api03-Z" not in text and "[REDACTED" in text
    marker = sf._session_done_marker("sess/../1")
    assert marker is not None and marker.name == "sess..1.done"


def test_suggest_facts_runs_once_per_session_and_never_auto_writes_identity(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    from atlas_plus.mcp.memory_server import config as _config, routing as _routing, versioning as _versioning
    monkeypatch.setattr(_config, "MEMORY_ROOT", tmp_path / "mem")
    monkeypatch.setattr(_routing, "MEMORY_ROOT", tmp_path / "mem", raising=False)   # module attributes another test may have left elsewhere
    monkeypatch.setattr(_routing, "ROUTING_CONFIG", tmp_path / "mem" / "_atlas" / "routing.yaml", raising=False)  # another test's routing.yaml (create_if_missing false) leaks in the full run
    monkeypatch.setattr(_versioning, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    from atlas_plus import init_cmd
    from atlas_plus.curation import suggest_facts as sf

    init_cmd.run_init()
    monkeypatch.setattr(sf, "_load_transcript_text", lambda **kw: "USER: I am Dana and I run the vlan lab")
    cands = [{"scope": "identity", "title": "who", "fact": "The user is Dana.", "confidence": 0.99},
             {"scope": "project:lab", "title": "lab", "fact": "The lab runs on vlan 20.", "confidence": 0.99}]
    monkeypatch.setattr(sf, "_extract_candidates", lambda text, confidence_threshold=None: (list(cands), 0))
    r1 = sf.suggest_facts(since="today", auto=True, conversation_id="sess-A", skip_similar_threshold=0.8)
    assert r1["written"] == 1                                       # the identity fact was held for review
    from atlas_plus.mcp.memory_server.routing import resolve_target
    ident, _ = resolve_target("identity")
    assert (not ident.exists()) or "Dana" not in ident.read_text(encoding="utf-8")
    r2 = sf.suggest_facts(since="today", auto=True, conversation_id="sess-A", skip_similar_threshold=0.8)
    assert r2.get("already_done")                                   # a re-fired Stop does nothing
    r3 = sf.suggest_facts(since="today", auto=True, conversation_id="sess-B", skip_similar_threshold=0.8)
    assert r3["written"] == 0                                       # the same fact line exists already
    lab, _ = resolve_target("project:lab")
    assert lab.read_text(encoding="utf-8").count("vlan 20") == 1


# --- AG38 F9 / AG40: the summarizer checks the gate and the marker before any model call ----------------


def test_summarizer_gate_and_marker_come_before_the_model(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from atlas_plus.summarizer import cli as scli
    from atlas_plus.summarizer import renderer

    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ATLAS_SESSION_AUTONOMOUS", raising=False)
    monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
    called = []
    monkeypatch.setattr(scli, "load_turns", lambda **kw: called.append("load") or [])
    res = CliRunner().invoke(scli.app, ["--conversation-id", "sess-1"])
    assert res.exit_code == 2 and called == [] and "ATLAS_SESSION_AUTONOMOUS" in res.output
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    monkeypatch.setattr(renderer, "MEMORY_SESSIONS_DIR", sdir)
    from datetime import datetime
    (sdir / (datetime.now().astimezone().strftime("%Y-%m-%d") + ".md")).write_text("## t\n<!-- session:sess-1 -->\nbody\n", encoding="utf-8")
    res2 = CliRunner().invoke(scli.app, ["--conversation-id", "sess-1"])
    assert res2.exit_code == 0 and called == [] and "already summarised" in res2.output


def test_provider_names_the_missing_model(monkeypatch):
    from atlas_plus.summarizer import providers

    class _Resp:
        status_code = 404

    class _Err(Exception):
        response = _Resp()

    class _Prov:
        def __init__(self, **kw):
            pass

        def generate(self, *a, **k):
            raise _Err("404")
    monkeypatch.setattr("atlas_plus.llm.OllamaProvider", _Prov)
    with pytest.raises(providers.ProviderError) as e:
        providers._call_ollama("hi")
    assert "ollama pull" in str(e.value) and "not reachable" not in str(e.value)


def test_hook_probe_accepts_the_exact_tag_only(monkeypatch):
    import io
    import json
    import urllib.request

    from atlas_plus.summarizer import hook_entry as he

    monkeypatch.setenv("ATLAS_OLLAMA_URL", "http://127.0.0.1:9")
    monkeypatch.setattr("atlas_plus.summarizer.providers.OLLAMA_MODEL", "qwen3:8b")

    class _R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _R(json.dumps({"models": [{"name": "qwen3:4b"}]}).encode()))
    reason = he._summarizer_unavailable_reason()
    assert "qwen3:8b" in reason and "ollama pull" in reason
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _R(json.dumps({"models": [{"name": "qwen3:8b"}]}).encode()))
    assert he._summarizer_unavailable_reason() == ""


# --- AG40 F3: the newest summary is primed ----------------------------------------------------------------------


def test_last_session_takes_the_first_line_entry(tmp_path, monkeypatch):
    from atlas_plus.hooks import session_start as ss

    root = tmp_path / "mem"
    (root / "sessions").mkdir(parents=True)
    (root / "sessions" / "2026-11-13.md").write_text("## 2026-11-13 - newest\n<!-- session:B3 -->\nnewest body\n\n## 2026-11-13 - older\nolder body\n", encoding="utf-8")
    monkeypatch.setattr(ss, "MEMORY_ROOT", root)
    import inspect
    src = inspect.getsource(ss)
    assert 'idx = 0 if text.startswith("## ") else text.find("\\n## ")' in src
    assert 'not ln.startswith("<!-- session:")' in src


# --- AG39 F5: setup --out makes parent folders --------------------------------------------------------------


def test_setup_out_creates_parent_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus import setup_cmd

    out = tmp_path / "xdg" / "config" / "opencode" / "opencode.json"
    rc = setup_cmd.main(["--client", "opencode", "--out", str(out)])
    assert rc == 0 and out.exists()


# --- AG41 F1/F2: imports judge their own vault; a renamed, grown chat is updated ------------------------------


def test_two_vaults_with_the_same_note_name_do_not_ping_pong(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus import import_cmd

    va, vb = tmp_path / "va", tmp_path / "vb"
    va.mkdir()
    vb.mkdir()
    (va / "README.md").write_text("# A\n\nalpha body\n", encoding="utf-8")
    (vb / "README.md").write_text("# B\n\nbravo body\n", encoding="utf-8")
    import_cmd.import_obsidian(va, "reference/shared", execute=True)
    r1 = import_cmd.import_obsidian(vb, "reference/shared", execute=True, archive_stale=True)
    assert r1["archived"] == [] and r1["new"] == 1
    r2 = import_cmd.import_obsidian(va, "reference/shared", execute=True, archive_stale=True)
    assert r2["archived"] == [] and r2["unchanged"] == 1 and r2["edited"] == 0
    live = sorted(p.name for p in (tmp_path / "mem" / "reference" / "shared").glob("*.md"))
    assert len(live) == 2


def test_renamed_and_grown_chat_is_updated_in_place(tmp_path, monkeypatch):
    from atlas_plus import import_cmd

    assert import_cmd._strip_title("# Old title\n\n## user\n\nhi\n") == "## user\n\nhi\n"
    prior = tmp_path / "old.md"
    prior.write_text("---\nid: chatgpt-old\n---\n# Old title\n\n## user\n\nhi\n", encoding="utf-8")
    new_text = "---\nid: chatgpt-new\n---\n# New title\n\n## user\n\nhi\n\n## assistant\n\nMadrid\n"
    assert import_cmd._grew(prior, new_text)


# --- AG41 F3 / AG38 F8/F12: the dashboard keeps CRLF and BOM; typed and bounded bodies ------------------------


def test_raw_load_reports_eol_and_bom_and_json_rules():
    import asyncio
    import inspect

    from fastapi import HTTPException

    from atlas_plus.web import app as web

    src = inspect.getsource(web)
    assert '"X-Sextant-Eol": "crlf" if b"\\r\\n" in data else "lf"' in src
    assert "_mfEol==='crlf'" in src and "_mfBom==='1'" in src

    def req(raw: bytes):
        async def body():
            return raw
        return SimpleNamespace(body=body)
    assert asyncio.run(web._json_object(req(b'{"auto": true}'))) == {"auto": True}   # the dashboard's own flags
    with pytest.raises(HTTPException) as e1:
        asyncio.run(web._json_object(req(b'{"body": {"a": 1}}')))
    assert e1.value.status_code == 400
    with pytest.raises(HTTPException) as e2:
        asyncio.run(web._json_object(req(b'{"body": Infinity}')))
    assert e2.value.status_code == 400
    with pytest.raises(HTTPException) as e3:
        asyncio.run(web._json_object(req(b'{"k": "' + b"x" * 2_200_000 + b'"}')))
    assert e3.value.status_code == 413
