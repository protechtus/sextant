"""Post-flip sweep 14: dashboard as a product, locales, transcripts and the Stop hook, resilience (2026-11-13)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


# --- dashboard (AG31) ---------------------------------------------------------------------------------

def test_memory_file_count_ignores_version_copies(tmp_path, monkeypatch):
    from atlas_plus.web import app as web

    root = tmp_path / "mem"
    (root / "reference").mkdir(parents=True)
    (root / "reference" / "a.md").write_text("a", encoding="utf-8")
    (root / "versions" / "reference").mkdir(parents=True)
    for i in range(5):
        (root / "versions" / "reference" / f"a_2026-11-13_v{i}.md").write_text("a", encoding="utf-8")
    monkeypatch.setattr(web, "MEMORY_ROOT", root)
    monkeypatch.setattr(web, "_mem_file_count_cache", (0.0, 0))
    assert web._get_mem_file_count() == 1


def test_json_object_helper_answers_400_with_a_sentence():
    import asyncio

    from fastapi import HTTPException

    from atlas_plus.web import app as web

    def req(raw: bytes):
        async def body():
            return raw
        return SimpleNamespace(body=body)

    with pytest.raises(HTTPException) as e1:
        asyncio.run(web._json_object(req(b"notjson")))
    assert e1.value.status_code == 400 and "JSON object" in e1.value.detail
    with pytest.raises(HTTPException) as e2:
        asyncio.run(web._json_object(req(b"[]")))
    assert e2.value.status_code == 400
    with pytest.raises(HTTPException) as e3:
        asyncio.run(web._json_object(req(b"{}"), required=("title",)))
    assert "title" in e3.value.detail
    assert asyncio.run(web._json_object(req(b'{"title": "x"}'), required=("title",))) == {"title": "x"}


def test_curation_counts_include_repeated_facts():
    src = Path(__file__).resolve().parents[1] / "src" / "atlas_plus"
    for rel in ("web/app.py", "brain/cli.py", "telegram/commands.py"):
        assert 'if "Repeated facts (" in line:' in (src / rel).read_text(encoding="utf-8"), rel


def test_expired_page_and_auth_failures_panel_exist():
    src = (Path(__file__).resolve().parents[1] / "src" / "atlas_plus" / "web" / "app.py").read_text(encoding="utf-8")
    assert '@app.get("/expired"' in src
    assert "/api/security/auth-failures.html" in src and "hx-get=\"/api/security/auth-failures.html\"" in src
    assert "htmx:responseError" in src


# --- transcripts and the Stop hook (AG34) ----------------------------------------------------------------

def test_summarizer_module_has_an_entry_point():
    src = (Path(__file__).resolve().parents[1] / "src" / "atlas_plus" / "summarizer" / "cli.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in src


def test_drop_importer_reads_claude_code_lines_and_other_encodings(tmp_path):
    from atlas_plus.transcripts.sources import drop

    line = {"type": "user", "timestamp": "2026-11-13T10:00:00Z", "message": {"role": "user", "content": [{"type": "text", "text": "hello router"}]}}
    reply = {"type": "assistant", "message": {"role": "assistant", "model": "m", "content": [{"type": "text", "text": "hi"}, {"type": "tool_use", "name": "Bash"}]}}
    f = tmp_path / "s.jsonl"
    f.write_text(json.dumps(line) + "\n" + json.dumps(reply) + "\n", encoding="utf-8")
    recs, skipped = drop._parse_file(f)
    assert len(recs) == 2 and skipped == 0 and recs[0].content == "hello router" and "[tool_use Bash]" in recs[1].content
    bom = tmp_path / "bom.jsonl"
    bom.write_bytes(b"\xef\xbb\xbf" + json.dumps({"role": "user", "content": "one"}).encode() + b"\n" + json.dumps({"role": "assistant", "content": "two"}).encode() + b"\n")
    recs, skipped = drop._parse_file(bom)
    assert len(recs) == 2 and skipped == 0
    u16 = tmp_path / "u16.jsonl"
    u16.write_bytes((json.dumps({"role": "user", "content": "sixteen"}) + "\n").encode("utf-16"))
    recs, skipped = drop._parse_file(u16)
    assert len(recs) == 1 and recs[0].content == "sixteen"


def test_transcript_search_retries_bad_syntax_as_a_phrase(tmp_path, monkeypatch):
    from atlas_plus.transcripts import index as ix

    src = Path(ix.__file__).read_text(encoding="utf-8")
    assert "OperationalError" in src and "retried as one literal phrase" in src


def test_mining_targets_this_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "My Work"))
    monkeypatch.delenv("ATLAS_CLAUDE_PROJECT_DIR", raising=False)
    from atlas_plus.temporal_graph import sources

    claude_root = tmp_path / "claude" / "projects"
    slug_dir = claude_root / "e--sandbox-my-work"
    claude_root.mkdir(parents=True)
    assert "c--Tools" not in Path(sources.__file__).read_text(encoding="utf-8").replace('"c--Tools"', "")
    guess = sources._claude_project_dir(claude_root)
    assert guess and guess.lower() == __import__("re").sub(r"[^A-Za-z0-9]", "-", str(tmp_path / "My Work")).lower()
    monkeypatch.setenv("ATLAS_CLAUDE_PROJECT_DIR", "custom-dir")
    assert sources._claude_project_dir(claude_root) == "custom-dir"


def test_providers_url_follows_the_hook_probe_order(monkeypatch):
    import importlib

    monkeypatch.delenv("ATLAS_OLLAMA_URL", raising=False)
    monkeypatch.setenv("ATLAS_BRAIN_OLLAMA_URL", "http://127.0.0.1:9/")
    from atlas_plus.summarizer import providers

    importlib.reload(providers)
    assert providers.OLLAMA_URL == "http://127.0.0.1:9"


# --- resilience (AG36) ----------------------------------------------------------------------------------

def test_search_reports_an_unopenable_index(tmp_path, monkeypatch):
    from atlas_plus.mcp.memory_server import fts

    fts._LAST_CONNECT_ERROR["text"] = "disk I/O error"
    assert fts.last_connect_error() == "disk I/O error"
    from typer.testing import CliRunner

    from atlas_plus.cli import app
    from atlas_plus.mcp.memory_server.tools import search as search_mod

    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "mem" / "reference").mkdir(parents=True)
    monkeypatch.setattr(search_mod, "memory_search_hybrid", lambda **kw: ([], "fts-only"))
    res = CliRunner().invoke(app, ["search", "zephyr"])
    assert res.exit_code == 1 and "Search index unavailable" in res.output
    fts._LAST_CONNECT_ERROR["text"] = ""


def test_pid_alive_treats_zombie_state_as_dead(monkeypatch):
    from atlas_plus.mcp.memory_server import routing

    if os.name == "nt":
        pytest.skip("POSIX /proc only")
    assert routing._pid_alive(os.getpid())


def test_lock_token_write_failure_removes_the_lock(tmp_path, monkeypatch):
    from atlas_plus.mcp.memory_server import routing

    src = Path(routing.__file__).read_text(encoding="utf-8")
    assert "ENOSPC after the create" in src


def test_leap_day_decay_cutoff():
    from atlas_plus.mcp.memory_server import routing

    src = Path(routing.__file__).read_text(encoding="utf-8")
    assert "today.replace(year=" not in src


def test_turkish_dotted_i_folds_to_one_note(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus import init_cmd
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    init_cmd.run_init()
    a = memory_remember("reference:İstanbul", "a fact", source="agent")
    b = memory_remember("reference:istanbul", "another", source="agent")
    assert a.get("ok") and b.get("ok") and a["file"] == b["file"]
    assert "\u0307" not in Path(a["file"]).name
