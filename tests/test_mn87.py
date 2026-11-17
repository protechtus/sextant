"""MN87 (sweep 25, performance at scale, 20,000 notes). Each test runs the optimized path and counts the work it
does, or checks that the answer is the one the old code gave; none reads source text.

1. _recompose's Greek/stroke-letter check is one compiled character class, not a Python generator step per
   character of every note (40% of an index rebuild).
2. A NEW note (no memory_files row) no longer makes the search freshness update scan the whole FTS table to
   delete a row it cannot have (quadratic: 20,000 notes added beside an index of one took 22.7 minutes).
3. A loopback model URL with nothing listening is refused by a short TCP connect: the Stop hook spent 3.0 s
   (1.5 s per address of `localhost`) on every reply of a machine without Ollama; doctor's summarizer row too.
"""
from __future__ import annotations

import http.server
import json
import socket
import sys
import threading
import time

import pytest


# --- 1. _recompose -------------------------------------------------------------------------------

_OLD_SET = "ẞəƏđĐłŁøØðÐþÞħĦŧŦæÆœŒ"


def _old_needs_table(text: str) -> bool:
    return any("Ά" <= c <= "ώ" or c in _OLD_SET for c in text)


def test_recompose_same_answer_as_the_per_character_rule():
    from atlas_plus.mcp.memory_server import fts

    samples = ["plain ascii text", "Ελληνικά κείμενο", "Ålborg øre æble", "Łódź Đà Nẵng", "ħamrun ŧ",
               "cœur Æsir", "΅ Ϗ edges", "ẞ ə Ə", "東京 Привет", "", "x" * 1000 + "ά"]
    for ch in range(0x0370, 0x0400):
        samples.append(f"a{chr(ch)}b")
    for s in samples:
        expect = s.translate(fts._GREEK_ETC) if _old_needs_table(s) else s
        # the other folds of _recompose leave these samples alone, so the table decision is the whole difference
        assert fts._recompose(s) == expect, repr(s)


def test_recompose_does_not_step_a_python_generator_per_character():
    from atlas_plus.mcp.memory_server import fts

    text = "the deploy failed because the index was stale " * 2000  # 94,000 characters, nothing to fold
    steps = []

    def prof(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "<genexpr>" and frame.f_code.co_filename.endswith("fts.py"):
            steps.append(1)

    sys.setprofile(prof)
    try:
        out = fts._recompose(text)
    finally:
        sys.setprofile(None)
    assert out == text
    assert len(steps) < 10, len(steps)  # was one generator step per character (94,000)


# --- 2. new notes beside an existing index ---------------------------------------------------------

def _world(tmp_path, monkeypatch):
    from atlas_plus.mcp.memory_server import fts

    mem = tmp_path / ".memory"
    (mem / "reference").mkdir(parents=True)
    (mem / "reference" / "seed.md").write_text("---\nid: seed\n---\nseed note\n", encoding="utf-8")
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / ".atlas" / "search.sqlite")
    return fts, mem


def _counting(fts, monkeypatch):
    stmts: list[str] = []
    real = fts._connect

    def counting():
        c = real()
        c.set_trace_callback(stmts.append)
        return c

    monkeypatch.setattr(fts, "_connect", counting)
    return stmts


def test_new_notes_do_not_scan_the_fts_table(tmp_path, monkeypatch):
    fts, mem = _world(tmp_path, monkeypatch)
    assert fts.update_incremental() == 1  # the seed builds the index
    for i in range(60):
        p = mem / "projects" / f"g{i % 4}" / f"n{i}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntitle: t{i}\n---\nbody of note {i} walrus{i}\n", encoding="utf-8")
    stmts = _counting(fts, monkeypatch)
    assert fts.update_incremental() == 60
    path_deletes = [s for s in stmts if "DELETE FROM memory_fts WHERE path" in s]
    assert path_deletes == [], len(path_deletes)  # was one full-table scan per new note
    hits = {r["path"] for r in fts.search("walrus7")}
    assert hits == {"projects/g3/n7.md"}


def test_changed_deleted_and_new_notes_still_exact(tmp_path, monkeypatch):
    fts, mem = _world(tmp_path, monkeypatch)
    for i in range(10):
        (mem / "reference" / f"r{i}.md").write_text(f"old body heron{i}\n", encoding="utf-8")
    fts.update_incremental()
    (mem / "reference" / "r1.md").write_text("new body pelican\n", encoding="utf-8")
    (mem / "reference" / "r2.md").unlink()
    (mem / "reference" / "fresh.md").write_text("fresh pelican\n", encoding="utf-8")
    assert fts.update_incremental() == 3
    assert {r["path"] for r in fts.search("pelican")} == {"reference/r1.md", "reference/fresh.md"}
    assert fts.search("heron1") == [] and fts.search("heron2") == []
    import sqlite3
    c = sqlite3.connect(str(fts.FTS_DB))
    try:
        n_fts = c.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0]
        n_files = c.execute("SELECT COUNT(*) FROM memory_files").fetchone()[0]
    finally:
        c.close()
    assert n_fts == n_files == 11  # seed + r0..r9 - r2 + fresh: no duplicate and no orphan row


def test_a_legacy_row_without_rowid_is_still_replaced(tmp_path, monkeypatch):
    fts, mem = _world(tmp_path, monkeypatch)
    (mem / "reference" / "legacy.md").write_text("legacy osprey\n", encoding="utf-8")
    fts.update_incremental()
    import sqlite3
    c = sqlite3.connect(str(fts.FTS_DB))
    c.execute("UPDATE memory_files SET fts_rowid=NULL")  # an index from before AG42 recorded no rowid
    c.commit(); c.close()
    time.sleep(0.05)
    (mem / "reference" / "legacy.md").write_text("legacy kestrel changed\n", encoding="utf-8")
    fts.update_incremental()
    assert [r["path"] for r in fts.search("kestrel")] == ["reference/legacy.md"]
    assert fts.search("osprey") == []


# --- 3. the loopback model probe --------------------------------------------------------------------

def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_loopback_refused_answers():
    from atlas_plus.llm.ollama_url import loopback_refused

    port = _closed_port()
    assert loopback_refused(f"http://127.0.0.1:{port}") is True
    assert loopback_refused("http://203.0.113.9:11434") is False  # not loopback: the caller probes as before
    assert loopback_refused("not a url") is False
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        assert loopback_refused(f"http://127.0.0.1:{srv.getsockname()[1]}") is False
    finally:
        srv.close()


def test_stop_hook_probe_does_not_wait_on_a_closed_loopback_port(monkeypatch):
    import urllib.request

    from atlas_plus.summarizer import hook_entry as he

    port = _closed_port()
    monkeypatch.setenv("ATLAS_OLLAMA_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("ATLAS_LOCAL_ONLY", "on")
    calls = []
    real = urllib.request.urlopen
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    reason = he._summarizer_unavailable_reason()
    assert reason.startswith(f"no local model at http://127.0.0.1:{port}")  # the same sentence as before
    assert calls == []  # was an HTTP request with a 1.5 s timeout per address


class _Tags(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"models": [{"name": "nothing-like-it:1b"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_stop_hook_probe_still_asks_a_listening_server(monkeypatch):
    from atlas_plus.summarizer import hook_entry as he

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Tags)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        monkeypatch.setenv("ATLAS_OLLAMA_URL", f"http://127.0.0.1:{srv.server_address[1]}")
        reason = he._summarizer_unavailable_reason()
    finally:
        srv.shutdown()
        srv.server_close()
    assert "is not pulled" in reason and "nothing-like-it:1b" in reason


def test_doctor_stop_hook_row_does_not_wait_on_a_closed_loopback_port(monkeypatch):
    import urllib.request

    from atlas_plus.doctor import cli as dcli
    from atlas_plus.summarizer import providers

    port = _closed_port()
    monkeypatch.setattr(providers, "OLLAMA_URL", f"http://127.0.0.1:{port}")
    calls = []
    real = urllib.request.urlopen
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    res = dcli.check_stop_hook()
    assert res.get("reachable") is False
    assert calls == []
