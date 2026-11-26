"""AG123 (sweep 37, SEX-053 items 11, 12, 15, 14): panel counts that name their caps, a Memory Quality line
that adds up, the exit codes with a parked sync conflict, and an interrupted 0.1.0 index healed by the head.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

_KEY = "ag123-" + "panel-key"


# ---------------------------------------------------------------------------
# Item 11 (MN93 F4): the curation panel showed its cap as a total; next-actions said "good shape" beside it
# ---------------------------------------------------------------------------

@pytest.fixture
def web(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    (mem / "reference").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_WEB_API_KEY", _KEY)
    monkeypatch.setenv("ATLAS_BRAIN_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ATLAS_WEB_REPORT_SUBPROCESS", "off")
    monkeypatch.setenv("ATLAS_WEB_PANEL_CACHE", "off")
    from fastapi.testclient import TestClient
    from atlas_plus.web import app as app_mod
    monkeypatch.setattr(app_mod, "_check_auth", lambda request: None)
    return mem, TestClient(app_mod.app, raise_server_exceptions=False)


def test_a_capped_duplicate_scan_is_named_on_the_panel(web, monkeypatch):
    mem, client = web
    words = "alpha bravo charlie delta echo foxtrot golf hotel".split()
    for i in range(10):  # ten near-copies: 45 candidate pairs, far past a cap of 3
        lines = [" ".join(words[(i + j) % 8:] + words[:(i + j) % 8]) for j in range(3)]
        (mem / "reference" / f"n{i:02d}.md").write_text(
            f"---\nid: n{i:02d}\n---\n# Note\n\n" + "\n".join(f"- {s}." for s in lines) + "\n", encoding="utf-8")
    from atlas_plus.curation import duplicates, weekly_report
    monkeypatch.setattr(weekly_report, "MEMORY_ROOT", mem)
    monkeypatch.setattr(duplicates, "_MAX_FILE_PAIRS", 3)
    r = client.get("/api/memory/curation-report-panel")
    assert r.status_code == 200
    text = re.sub(r"<[^>]+>", "", r.text)
    m = re.search(r"Duplicates: (\d+)", text)
    assert m, text
    assert "or more" in text, f"the cap read as a total: {text!r}"
    assert "3 note pairs" in text, text
    assert re.search(r"Curation scan: \d+ or more item\(s\)", text), text


def test_an_uncapped_scan_says_no_cap(web, monkeypatch):
    _mem, client = web
    fake = ("## Repeated facts (2 found: 2 exact, 0 near)\n## Potential Duplicates (1 found, threshold=0.4)\n"
            "## Stale Files (0 found, older than 6 months)\n")
    with patch("atlas_plus.curation.weekly_report.generate_report", return_value=(fake, None)):
        r = client.get("/api/memory/curation-report-panel")
    text = re.sub(r"<[^>]+>", "", r.text)
    assert "Duplicates: 3" in text and "or more" not in text, text


def test_next_actions_says_what_it_counts(web):
    _mem, client = web
    with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]), \
         patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
         patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]):
        r = client.get("/api/memory/next-actions-panel")
    assert r.status_code == 200
    assert "good shape" not in r.text, "a whole-memory verdict beside a panel that lists duplicates (MN93 F4)"
    for counted in ("decayed", "orphan", "gaps", "curation scan"):
        assert counted in r.text, r.text


# ---------------------------------------------------------------------------
# Item 12 (MN93 F5): "254 files ... confidence: high:3 medium:1 none:235" summed to 239
# ---------------------------------------------------------------------------

def test_the_confidence_line_adds_up_to_the_files_it_names(tmp_path):
    from atlas_plus.doctor import cli
    mem = tmp_path / ".memory"
    notes = {
        "high.md": "---\nconfidence: high\n---\nx\n",
        "medium.md": "---\nconfidence: medium\n---\nx\n",
        "low.md": "---\nconfidence: low\n---\nx\n",
        "nokey.md": "---\ntitle: t\n---\nx\n",
        "numeric.md": "---\nconfidence: 0.9\n---\nx\n",
        "word.md": "---\nconfidence: certain\n---\nx\n",
        "plain.md": "# no frontmatter at all\n",
        "textblock.md": "---\nA top block that is a sentence.\n---\nbody\n",
        "unterminated.md": "---\nconfidence: high\nno closing rule\n",
    }
    for rel, text in notes.items():
        p = mem / "reference" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    q = cli.check_memory_quality(cli.scan_memory_files(mem))
    dist = q["confidence_dist"]
    assert q["total_files"] == len(notes)
    assert sum(dist.values()) == q["total_files"], dist
    assert dist["high"] == 1 and dist["medium"] == 1 and dist["low"] == 1
    assert dist["other"] == 2, dist          # 0.9 and "certain" are no level search ranks by
    assert dist["no-frontmatter"] == 2, dist  # no block, an unterminated block
    assert dist["none"] == 2, dist            # no key; a top block that is text loads as no keys (AG119)
    out = cli.render_text({"memory_quality": q})
    line = next(ln for ln in out.splitlines() if "confidence:" in ln)
    assert sum(int(n) for n in re.findall(r":(\d+)", line)) == len(notes), line
    assert "other:2" in line and "no-frontmatter:2" in line, line


# ---------------------------------------------------------------------------
# Item 15: exit codes with a parked conflict (docs/HOWTO-sync.md "Conflict handling"); passes on H
# ---------------------------------------------------------------------------

class _Hub(httpx.BaseTransport):
    def __init__(self, files):
        self.files = dict(files)

    def table(self):
        return {rel: {"sha256": hashlib.sha256(b).hexdigest(), "size": len(b)} for rel, b in self.files.items()}

    def handle_request(self, request):
        from atlas_plus.sync import manifest
        path = request.url.path
        if path.endswith("/manifest"):
            return httpx.Response(200, json={"files": self.table(), "generation": 1, "generation_mark": "m1",
                                             "principal": {"name": "devB", "kind": "device", "scope": "rw",
                                                           "prefixes": []}})
        if path.endswith("/file"):
            rel = request.url.params.get("path")
            if rel not in self.files:
                return httpx.Response(404, json={"detail": "not found"})
            body = self.files[rel]
            return httpx.Response(200, content=body, headers={"X-Sextant-Sha256": hashlib.sha256(body).hexdigest()})
        if path.endswith("/push"):
            results = [{"path": ch["path"], "result": "applied"} for ch in json.loads(request.content)["changes"]]
            return httpx.Response(200, json={"results": results, "generation": 2, "generation_mark": "m1"})
        if path.endswith("/checkin"):
            return httpx.Response(200, json={"server_manifest_hash": manifest.files_hash(self.table())})
        return httpx.Response(404)


class _Args(argparse.Namespace):
    def __getattr__(self, name):  # every flag the parser would default
        return None


def test_a_parked_conflict_exit_codes_match_the_howto(tmp_path, monkeypatch, capsys):
    for key in ("ATLAS_SYNC_HUB", "ATLAS_SYNC_DEVICE_TOKEN", "ATLAS_SYNC_SHARE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    root = tmp_path / "mem"
    root.mkdir()
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root))
    from atlas_plus.sync import cli
    from atlas_plus.sync import client as sc
    hub = _Hub({"a.md": b"base\n"})
    dead = "http://127.0.0.1:9"
    with sc.SyncClient(dead, "tok-" + "ag123", transport=hub) as c:
        c.sync_once(root)
    (root / "a.md").write_bytes(b"mine\n")
    hub.files["a.md"] = b"theirs\n"
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real(*a, **{**k, "transport": hub}))
    args = _Args(hub=dead, token="tok-" + "ag123", share=None, json=False, dry_run=False)
    assert cli.cmd_plan(args) == 0
    assert "CONFLICT: a.md" in capsys.readouterr().out
    assert cli.cmd_sync(args) == 0
    capsys.readouterr()
    assert (root / "a.md").read_bytes() == b"mine\n"   # your copy stays live
    assert cli.cmd_status(args) == 0
    assert "UNRESOLVED CONFLICTS: 1" in capsys.readouterr().out
    assert cli.cmd_checkin(args) == 1                  # the corpora differ while it is unresolved


# ---------------------------------------------------------------------------
# Item 14: an interrupted 0.1.0 build (some notes indexed, no parse stamp) resumed by the head; passes on H
# ---------------------------------------------------------------------------

def test_an_interrupted_old_build_is_finished_and_healed_once(tmp_path, monkeypatch):
    mem, state = tmp_path / "ws" / ".memory", tmp_path / "ws" / ".atlas"
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
    monkeypatch.setenv("ATLAS_EMBED_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ATLAS_OLLAMA_URL", "http://127.0.0.1:9")
    from atlas_plus.mcp.memory_server import fts
    notes = {
        "rules/rule-0.md": "---\nThis top block is a sentence about the wombatrule.\n---\nBody.\n",
        "rules/rule-1.md": "---\nAnother sentence names the ibexrule.\n---\nBody.\n",
        "notes/fm.md": "---\ntitle: Router\n---\nThe router firmware is 4.2.\n",
    }
    for i in range(5):
        notes[f"notes/plain-{i}.md"] = f"# Plain {i}\n\nThe staging box {i} reboots on Fridays.\n"
    for rel, text in notes.items():
        p = mem / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "FTS_DB", state / "ag123-fts.sqlite")
    fts.mark_dirty()
    cur = fts._parse_md

    def old_parse(raw):  # 0.1.0: a top block that is not key/value pairs was dropped from the body
        fm_json, body, ts = cur(raw)
        if fm_json == "{}" and raw.startswith("---"):
            end = raw.find("\n---", 3)
            if end != -1:
                body = raw[end + 4:].strip()
        return fm_json, body, ts
    with monkeypatch.context() as m:
        m.setattr(fts, "_parse_md", old_parse)
        fts.rebuild(force=True)
    missing = ["rules/rule-1.md", "notes/plain-3.md", "notes/plain-4.md"]  # the build stopped before these
    conn = sqlite3.connect(str(fts.FTS_DB))
    try:
        for rel in missing:
            conn.execute("DELETE FROM memory_fts WHERE path=?", (rel,))
            conn.execute("DELETE FROM memory_files WHERE path=?", (rel,))
        conn.execute("DELETE FROM atlas_meta WHERE key='parse_revision'")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == len(notes) - len(missing)
    finally:
        conn.close()
    fts.mark_dirty()
    written = fts.update_incremental()
    assert written == len(missing) + 1, written   # the three never indexed, plus rule-0 healed
    conn = sqlite3.connect(str(fts.FTS_DB))
    try:
        assert sorted(r[0] for r in conn.execute("SELECT path FROM memory_fts")) == sorted(notes)
        stamp = conn.execute("SELECT value FROM atlas_meta WHERE key='parse_revision'").fetchone()
    finally:
        conn.close()
    assert stamp is not None and str(stamp[0]).split("@")[0] == str(fts.PARSE_REVISION)  # "<rev>@<last_indexed>"
    assert [h["path"] for h in fts.search("wombatrule")] == ["rules/rule-0.md"]
    assert [h["path"] for h in fts.search("ibexrule")] == ["rules/rule-1.md"]
    assert len(fts.search("staging")) == 5
    assert fts.update_incremental() == 0           # a second pass writes nothing
