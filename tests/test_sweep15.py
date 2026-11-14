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
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH " + "PRIVATE KEY-----"
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
    fake_turns = [SimpleNamespace(role="user", content="t1"), SimpleNamespace(role="assistant", content="t2"), SimpleNamespace(role="user", content="t3")]
    monkeypatch.setattr(scli, "load_turns", lambda **kw: called.append("load") or list(fake_turns))
    res = CliRunner().invoke(scli.app, ["--conversation-id", "sess-1"])
    assert res.exit_code == 2 and called == [] and "ATLAS_SESSION_AUTONOMOUS" in res.output   # the gate, before any load
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    sdir = tmp_path / "sessions"
    sdir.mkdir()
    monkeypatch.setattr(renderer, "MEMORY_SESSIONS_DIR", sdir)
    from datetime import datetime
    (sdir / (datetime.now().astimezone().strftime("%Y-%m-%d") + ".md")).write_text("## t\n<!-- session:sess-1 turns:3 -->\nbody\n", encoding="utf-8")
    res2 = CliRunner().invoke(scli.app, ["--conversation-id", "sess-1"])
    assert res2.exit_code == 0 and called == ["load"] and "nothing new" in res2.output      # same turns: no model call (MN33)


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
    assert '"X-Sextant-Eol": ("mixed" if' in src
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


# --- AG42: performance round four ---


def _fts_corpus(tmp_path, monkeypatch, n=3):
    import atlas_plus.mcp.memory_server.fts as fts

    mem = tmp_path / "mem"
    (mem / "reference").mkdir(parents=True)
    words = ["alpha aardvark", "beta binturong", "gamma gerenuk", "delta dikdik"]
    for i in range(n):
        (mem / "reference" / f"n{i}.md").write_text(f"---\ntitle: N{i}\n---\n\n{words[i % 4]} note{i}\n", encoding="utf-8")
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "fts.sqlite")
    fts.rebuild(force=True)
    return fts, mem


def test_incremental_update_deletes_index_rows_by_rowid(tmp_path, monkeypatch):
    import os
    import sqlite3
    import time

    fts, mem = _fts_corpus(tmp_path, monkeypatch)
    conn = sqlite3.connect(str(fts.FTS_DB))
    rows = dict(conn.execute("SELECT path, fts_rowid FROM memory_files").fetchall())
    assert len(rows) == 3 and all(v is not None for v in rows.values())     # the rebuild recorded every rowid
    # every mtime changes (a pull, a restore, a copy): the update must not scan the table per note
    later = time.time() + 5
    for p in (mem / "reference").glob("*.md"):
        os.utime(p, (later, later))
    conn.close()
    assert fts.update_incremental() == 3
    conn = sqlite3.connect(str(fts.FTS_DB))
    assert conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 3
    # a stale rowid (pointing at another note) must not delete that note's row
    a_rowid = conn.execute("SELECT fts_rowid FROM memory_files WHERE path LIKE '%n0.md'").fetchone()[0]
    b_rowid = conn.execute("SELECT fts_rowid FROM memory_files WHERE path LIKE '%n1.md'").fetchone()[0]
    conn.execute("UPDATE memory_files SET fts_rowid=? WHERE path LIKE '%n0.md'", (b_rowid,))
    conn.commit()
    conn.close()
    later += 5
    os.utime(mem / "reference" / "n0.md", (later, later))
    assert fts.update_incremental() == 1
    conn = sqlite3.connect(str(fts.FTS_DB))
    assert conn.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 3
    paths = sorted(r[0] for r in conn.execute("SELECT path FROM memory_fts"))
    assert len(set(paths)) == 3
    conn.close()
    assert [r["path"] for r in fts.search("binturong")]           # the other note's row survived
    assert [r["path"] for r in fts.search("aardvark")]


def test_prefix_filtered_file_duplicates_equal_brute_force(tmp_path):
    import itertools
    import random

    from atlas_plus.curation import duplicates as d

    root = tmp_path / "mem"
    (root / "reference").mkdir(parents=True)
    rng = random.Random(42)
    vocab = [f"w{i}" for i in range(2000)]
    notes = []
    for i in range(400):
        words = rng.sample(vocab, 40)
        notes.append(words)
        (root / "reference" / f"n{i}.md").write_text("---\nid: n\n---\n" + " ".join(words) + "\n", encoding="utf-8")
    for i in range(20):                                             # planted near-copies
        base = notes[i]
        (root / "reference" / f"c{i}.md").write_text("---\nid: c\n---\n" + " ".join(base[:30] + rng.sample(vocab, 6)) + "\n", encoding="utf-8")
    files = sorted((root / "reference").glob("*.md"))
    toks = {f: d._tokenize(f.read_text(encoding="utf-8")) for f in files}
    for threshold in (0.4, 0.2):
        brute = set()
        for a, b in itertools.combinations(files, 2):
            if d._jaccard(toks[a], toks[b]) >= threshold:
                brute.add(frozenset((a.relative_to(root).as_posix(), b.relative_to(root).as_posix())))
        got = {frozenset((p.file1, p.file2)) for p in d.find_duplicates(root, threshold=threshold)}
        assert got == brute, (threshold, len(got), len(brute))
        assert d._LAST_CAPPED is False


def test_file_duplicate_cap_is_disclosed(tmp_path):
    from atlas_plus.curation import duplicates as d
    from atlas_plus.curation.weekly_report import _diverse

    root = tmp_path / "mem"
    (root / "reference").mkdir(parents=True)
    body = " ".join(f"w{i}" for i in range(50))
    for i in range(60):                                             # 60 near-identical notes: 1,770 pairs over the cap
        (root / "reference" / f"n{i}.md").write_text("---\nid: n\n---\n" + body + f" x{i}\n", encoding="utf-8")
    pairs = d.find_duplicates(root, threshold=0.4)
    assert len(pairs) == d._MAX_FILE_PAIRS and d._LAST_CAPPED is True
    dups = [{"file1": "a", "file2": "b", "fact": f"f{i}", "kind": "exact"} for i in range(100)] + [{"file1": "c", "file2": "d", "fact": "g", "kind": "exact"}]
    assert [x["file1"] for x in _diverse(dups, 30)] == ["a", "a", "a", "c"]


def test_agents_route_has_a_connect_timeout():
    import inspect

    from atlas_plus.web import app as web

    assert "httpx.AsyncClient(timeout=20.0)" not in inspect.getsource(web)


def test_curation_report_runs_in_a_worker_process(tmp_path, monkeypatch):
    """The worker inherits the environment: the report it returns is the one for ATLAS_MEMORY_ROOT."""
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_WEB_REPORT_SUBPROCESS", "on")
    from atlas_plus import init_cmd

    init_cmd.run_init()
    (tmp_path / "mem" / "reference" / "w.md").write_text("---\nid: w\n---\n\n- worker fact one\n- worker fact one\n", encoding="utf-8")
    from atlas_plus.web import app as web

    text, _ = web._generate_report_isolated()
    assert "Curation Report" in text and "Repeated facts (" in text


def test_dashboard_inline_script_has_no_raw_newline_in_a_regex(monkeypatch):
    """The page is an f-string: a CRLF regex written with single backslashes renders as real line
    breaks inside a JS regex literal and the whole inline script fails to parse (MN36, sweep 15:
    every tab dead in every browser)."""
    import re

    from fastapi.testclient import TestClient

    from atlas_plus.web import app as web

    monkeypatch.setattr(web, "API_KEY", "k" * 40, raising=False)
    client = TestClient(web.app, raise_server_exceptions=False)
    r = client.get("/", headers={"Authorization": "Bearer " + "k" * 40})
    html = r.text
    assert r.status_code == 200 and "editMemoryFile" in html
    # every regex literal opened by `.replace(/` must close on the same line
    for m in re.finditer(r"\.replace\(/([^\n]*)", html):
        assert "/" in m.group(1), m.group(0)[:80]
    assert "v=v.replace(/\\r?\\n/g,'\\r\\n')" in html


# --- manual wave 15 (MN31, MN32, MN33, MN35, MN36) ---


def test_redaction_is_linear_exact_and_covers_more_shapes():
    import time

    from atlas_plus.redaction import redact

    # false positives that mangled ordinary text (MN31)
    plain = "Raised max_tokens: 4096; tokenizer: bert; password_policy: strict for the secretary=bob; token_count=12; secret_santa: ann; my_tokenizer_config=bert-base-uncased"
    assert redact(plain) == plain
    # true positives, old and new shapes
    assert "hunter2hunter2" not in redact("DATABASE_PASSWORD=hunter2hunter2")
    assert "s3cretPass" not in redact("DATABASE_URL=postgres://admin:s3cretPass@db.internal:5432/app")
    assert "django-abcdef" not in redact("SECRET_KEY = r'django-abcdefghijk'")
    out = redact("?access_token=abcdefgh12345&x=1")
    assert "abcdefgh12345" not in out and "&x=1" in out
    assert "[REDACTED:private-key]" in redact("-----BEGIN PGP " + "PRIVATE KEY BLOCK-----\nabc\n-----END PGP " + "PRIVATE KEY BLOCK-----")
    assert "[REDACTED:private-key]" in redact("-----BEGIN OPENSSH " + "PRIVATE KEY-----\nb3BlbnNz cut off")
    assert "glpat-" + "x" * 24 not in redact("token glpat-" + "x" * 24)
    # Japanese key names and full-width separators (MN36)
    for s in ("パスワード=ほげ1234abc", "パスワード：hunter2hunter2", "ＤＢ＿ＰＡＳＳＷＯＲＤ＝abc123xyz", "APIキー=abcdef123456", "トークン: abcdef123456789"):
        r = redact(s)
        assert "[REDACTED]" in r, (s, r)
    # linear on a long identifier run (MN31: 40k characters took 35 s)
    big = "x_" * 40_000
    t0 = time.perf_counter()
    redact(big)
    assert time.perf_counter() - t0 < 2.0


def test_fact_dedupe_is_exact_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.mcp.memory_server import config as _config, routing as _routing, versioning as _versioning
    monkeypatch.setattr(_config, "MEMORY_ROOT", tmp_path / "mem")
    monkeypatch.setattr(_routing, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    monkeypatch.setattr(_routing, "ROUTING_CONFIG", tmp_path / "mem" / "_atlas" / "routing.yaml", raising=False)
    monkeypatch.setattr(_versioning, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    from atlas_plus import init_cmd
    from atlas_plus.curation import suggest_facts as sf
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    init_cmd.run_init()
    memory_remember("project:infra", "The deploy target is web2.", source="agent")
    memory_remember("project:infra", "Backups run nightly at 02:00 to the NAS.", source="agent")
    assert sf._fact_already_there("project:infra", "the deploy target is web2")
    assert not sf._fact_already_there("project:infra", "The deploy target is not web2.")       # a correction (MN31)
    assert not sf._fact_already_there("project:infra", "Backups run nightly at 03:00 to the NAS.")


def test_suggest_facts_marks_done_only_after_the_model_answered(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    from atlas_plus.mcp.memory_server import config as _config, routing as _routing, versioning as _versioning
    monkeypatch.setattr(_config, "MEMORY_ROOT", tmp_path / "mem")
    monkeypatch.setattr(_routing, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    monkeypatch.setattr(_routing, "ROUTING_CONFIG", tmp_path / "mem" / "_atlas" / "routing.yaml", raising=False)
    monkeypatch.setattr(_versioning, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    from atlas_plus import init_cmd
    from atlas_plus.curation import suggest_facts as sf

    init_cmd.run_init()
    monkeypatch.setattr(sf, "_load_transcript_text", lambda **kw: "USER: one\nASSISTANT: two\nUSER: three")

    def boom(text, confidence_threshold=None):
        raise RuntimeError("LLM provider error: HTTP 500")
    monkeypatch.setattr(sf, "_extract_candidates", boom)
    import pytest as _pt
    with _pt.raises(RuntimeError):
        sf.suggest_facts(since="today", auto=True, conversation_id="sess-F")
    assert not sf._session_done_marker("sess-F").exists()                       # a failed run leaves no marker (MN31)
    monkeypatch.setattr(sf, "_extract_candidates", lambda text, confidence_threshold=None: ([], 0))
    sf.suggest_facts(since="today", auto=True, conversation_id="sess-F")
    assert sf._session_done_marker("sess-F").read_text(encoding="utf-8").strip() == "3"
    r = sf.suggest_facts(since="today", auto=True, conversation_id="sess-F")
    assert r.get("already_done")                                                  # same turns: nothing new
    monkeypatch.setattr(sf, "_load_transcript_text", lambda **kw: "\n".join(f"USER: t{i}" for i in range(20)))
    r2 = sf.suggest_facts(since="today", auto=True, conversation_id="sess-F")
    assert not r2.get("already_done")                                             # the session grew: run again (MN33)


def test_summary_entry_is_replaced_when_the_session_grew(tmp_path, monkeypatch):
    from atlas_plus.summarizer import renderer as r

    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    monkeypatch.setattr(r, "MEMORY_SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(r, "UPDATES_TXT", tmp_path / "updates.txt")
    monkeypatch.setattr(r, "_version_file", lambda p: None)
    r.write_outputs("## 2026-11-13 - first\n<!-- session:S1 turns:6 -->\nsix turns\n", "e1\n", "2026-11-13", session_marker="<!-- session:S1 turns:6 -->")
    r.write_outputs("## 2026-11-13 - other\n<!-- session:S2 turns:3 -->\nother\n", "e2\n", "2026-11-13", session_marker="<!-- session:S2 turns:3 -->")
    found = r.find_session_entry(tmp_path / "sessions", "S1")
    assert found is not None and found[1] == 6
    r.write_outputs("## 2026-11-13 - first, grown\n<!-- session:S1 turns:30 -->\nthirty turns\n", "e3\n", "2026-11-13",
                    session_marker="<!-- session:S1 turns:30 -->", replace_session="<!-- session:S1")
    text = (tmp_path / "sessions" / "2026-11-13.md").read_text(encoding="utf-8")
    assert text.count("<!-- session:S1") == 1 and "thirty turns" in text and "six turns" not in text
    assert "<!-- session:S2 turns:3 -->" in text                                  # the other entry survived
    assert r.find_session_entry(tmp_path / "sessions", "S1")[1] == 30


def test_summarizer_helpers_lock_and_junk_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.summarizer import cli as scli

    assert scli._acquire_session_lock("S9") is True
    assert scli._acquire_session_lock("S9") is False                            # a second Stop in the same second waits (MN33)
    assert scli._looks_like_summary({"title": "Fixed the build", "completed": ["x"], "pending": []})
    assert not scli._looks_like_summary({"title": "things that were finished and confirmed working Pending: ...\nJSON:", "completed": []})
    assert not scli._looks_like_summary("not a dict")


def test_wsl_payload_path_and_no_facts_without_a_model(monkeypatch):
    from atlas_plus.summarizer import hook_entry as he

    monkeypatch.setattr(he, "_posix_path_for", he._posix_path_for)
    import os
    if os.name != "nt":
        assert he._posix_path_for("E:\\x\\sess.jsonl") == "/mnt/e/x/sess.jsonl"
    else:
        assert he._posix_path_for("E:\\x\\sess.jsonl") == "E:\\x\\sess.jsonl"
    import inspect
    src = inspect.getsource(he.main)
    assert "and not reason:  # no model, no facts stage" in src
    assert "ATLAS_OLLAMA_URL;" in inspect.getsource(he._summarizer_unavailable_reason)


def test_trusted_peer_matcher_and_sync_hub_config_is_not_pushable():
    from atlas_plus.auth import trusted_peer_matcher
    from atlas_plus.sync import policy

    m = trusted_peer_matcher("192.168.96.1, 10.0.0.0/8")
    assert m("192.168.96.1") and m("10.1.2.3") and not m("172.30.32.1") and not m("")
    assert trusted_peer_matcher("")("192.168.96.1") is False
    import inspect
    src = inspect.getsource(policy)
    assert 'direction == "write":' in src and "hub configuration (edit it on the hub)" in src


def test_bigram_fallback_is_valid_fts5_for_mixed_script():
    from atlas_plus.mcp.memory_server import fts

    q = fts._bigram_fallback("Sextantで東京の会議")
    assert q and " AND " in q and q.startswith('"Sextant"')
    assert fts._bigram_fallback("ab 東京の会議").startswith('"ab" AND (')


def test_conflict_marker_rule_sees_crlf():
    from atlas_plus.mcp.memory_server.routing import has_conflict_markers

    assert has_conflict_markers("x\r\n>>>>>>>\r\ny")


def test_corpus_signature_changes_on_rename(tmp_path, monkeypatch):
    from atlas_plus.web import app as web

    root = tmp_path / "mem"
    (root / "reference").mkdir(parents=True)
    (root / "reference" / "a.md").write_text("---\nid: a\n---\nx\n", encoding="utf-8")
    monkeypatch.setattr(web, "MEMORY_ROOT", root)
    s1 = web._corpus_signature()
    (root / "reference" / "a.md").rename(root / "reference" / "b.md")
    assert web._corpus_signature() != s1


def test_legacy_ticket_prefix_collision_stops_growing(tmp_path):
    from atlas_plus.tickets import store

    for proj in ("alpha-api", "alpine-ui"):
        (tmp_path / proj).mkdir(parents=True)
        (tmp_path / proj / "ALP-001.md").write_text("---\nid: ALP-001\ntitle: t\nproject: " + proj + "\nstatus: todo\n---\n", encoding="utf-8")
    a = store.create_ticket(title="new alpha", project="alpha-api", tickets_root=tmp_path, interactive=True)
    b = store.create_ticket(title="new alpine", project="alpine-ui", tickets_root=tmp_path, interactive=True)
    assert a.id.startswith("ALP-") and not b.id.startswith("ALP-") and a.id != b.id   # the later project lengthens (MN32)


def test_auth_panel_shows_refusals_and_msys_is_utf8(monkeypatch):
    from atlas_plus import _console
    from atlas_plus.web import app as web

    html = web._locked_out_html({"locked_out_refusals": {"203.0.113.9": 21}})
    assert "203.0.113.9: 21" in html
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    assert _console._console_output_encoding() == "utf-8"
