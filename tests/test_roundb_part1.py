"""Round B part-1 re-audit bypasses (2026-11-09): 8.3 short names, reparse points, cookie
origin port, state-marker ownership, derived-state inventory, anti skip list, header-only
weekly draft, ticket field rewrite, transcript torn tail."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


# ---------------- sync policy ----------------

@pytest.mark.parametrize("rel", ["GIT~1/config", "projects/CLAUDE~1/skills/x/SKILL.md",
                                 "OBSIDI~1/plugins/x/main.js", "_atlas/SYNC-C~1/x.md", "notes~1.md"])
def test_short_name_shapes_are_denied_both_ways(rel):
    from atlas_plus.sync import policy
    assert policy.deny_reason(rel, None, "read") is not None
    assert policy.deny_reason(rel, None, "write") is not None


def test_resolved_target_refuses_a_junction_inside_the_corpus(tmp_path):
    from atlas_plus.sync import policy
    root = tmp_path / "corpus"; (root / "projects").mkdir(parents=True)
    outside = tmp_path / "outside"; outside.mkdir()
    link = root / "projects" / "jump"
    if sys.platform == "win32":
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        if r.returncode != 0:
            pytest.skip("cannot create a junction here")
    else:
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("cannot create a symlink here")
    target, reason = policy.resolved_target(root, "projects/jump/x.md", None, "write")
    assert target is None and "symlink or junction" in reason
    ok, reason = policy.resolved_target(root, "projects/plain.md", None, "write")
    assert reason is None and ok == root / "projects" / "plain.md"


def test_apply_change_uses_the_resolved_check(tmp_path):
    from atlas_plus.sync import apply
    root = tmp_path / "corpus"; root.mkdir()
    with pytest.raises(apply.SyncPolicyError):
        apply.apply_change(root, "GIT~1/config", b"[core]\n")
    assert not (root / "GIT~1").exists() and not (root / ".git").exists()


# ---------------- intake ----------------

def test_intake_refuses_short_names_and_project_control_files(tmp_path):
    from atlas_plus.intake import rules
    landing, projects, memory = tmp_path / "intake", tmp_path / "projects", tmp_path / "memory"
    for d in (landing, projects, memory): d.mkdir()
    bad = [
        ("projects/app/CLAUDE~1/skills/x/SKILL.md", projects / "app" / "CLAUDE~1" / "skills" / "x" / "SKILL.md"),
        ("projects/app/GIT~1/hooks/pre-commit", projects / "app" / "GIT~1" / "hooks" / "pre-commit"),
        ("projects/app/CLAUDE.md", projects / "app" / "CLAUDE.md"),
        ("projects/app/AGENTS.md", projects / "app" / "AGENTS.md"),
        ("projects/app/requirements-dev.txt", projects / "app" / "requirements-dev.txt"),
        ("projects/app/docker-compose.prod.yml", projects / "app" / "docker-compose.prod.yml"),
        ("projects/app/pyproject.toml", projects / "app" / "pyproject.toml"),
    ]
    for rel, dest in bad:
        reason, _, _ = rules._check_destination(rel, dest, landing, projects, memory)
        assert reason, rel
    reason, _, replace = rules._check_destination("projects/app/notes/design.md",
                                                  projects / "app" / "notes" / "design.md",
                                                  landing, projects, memory)
    assert reason is None and replace is True


# ---------------- web cookie origin ----------------

class _Req:
    def __init__(self, url, headers):
        from starlette.datastructures import URL, Headers
        self.url = URL(url); self.headers = Headers(headers)


@pytest.mark.parametrize("origin,ok", [
    ("http://127.0.0.1:8766", True),
    ("http://127.0.0.1:3000", False),     # another loopback app
    ("http://localhost:8766", False),     # a different host spelling is a different origin
    ("http://127.0.0.1", False),          # port 80 != 8766
    ("https://evil.example", False),
])
def test_cookie_origin_must_match_host_and_port(origin, ok):
    from atlas_plus.web import app as web_app
    req = _Req("http://127.0.0.1:8766/api/x", {"origin": origin})
    assert web_app._cookie_origin_ok(req) is ok


def test_cookie_origin_behind_https_proxy():
    from atlas_plus.web import app as web_app
    req = _Req("http://helm.example.com/api/x", {"origin": "https://helm.example.com",
                                                  "x-forwarded-proto": "https"})
    assert web_app._cookie_origin_ok(req) is True


# ---------------- state marker ownership ----------------

def test_init_refuses_to_claim_a_directory_full_of_other_things(tmp_path, monkeypatch):
    from atlas_plus import init_cmd
    docs = tmp_path / "Documents"; docs.mkdir()
    (docs / "taxes-2025.xlsx").write_bytes(b"x"); (docs / "logs").mkdir(); (docs / "logs" / "a.txt").write_text("mine")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(docs))
    with pytest.raises(SystemExit) as exc:
        init_cmd.run_init(dry_run=False)
    assert "REFUSED" in str(exc.value)
    assert not (docs / ".sextant-state").exists()
    assert (docs / "logs" / "a.txt").read_text() == "mine"


def test_init_accepts_empty_new_and_pre_marker_sextant_dirs(tmp_path, monkeypatch):
    from atlas_plus import init_cmd
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "memory"))
    new = tmp_path / "new-state"
    monkeypatch.setenv("ATLAS_STATE_DIR", str(new))
    init_cmd.run_init(dry_run=False)
    assert (new / ".sextant-state").exists()
    old = tmp_path / "old-state"; old.mkdir(); (old / "state.sqlite").write_bytes(b""); (old / "transcripts").mkdir()
    monkeypatch.setenv("ATLAS_STATE_DIR", str(old))
    init_cmd.run_init(dry_run=False)
    assert (old / ".sextant-state").exists()


def test_reset_state_refuses_a_marker_only_directory(tmp_path, monkeypatch):
    from atlas_plus import uninstall_cmd
    state = tmp_path / "docs"; state.mkdir()
    (state / ".sextant-state").write_text("marker")
    (state / "logs").mkdir(); (state / "logs" / "mine.txt").write_text("user file")
    (state / "temporal").mkdir(); (state / "temporal" / "notes.md").write_text("user file")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
    out = uninstall_cmd.clear_derived_state(yes=True)
    assert out.startswith("REFUSED")
    assert (state / "logs" / "mine.txt").exists() and (state / "temporal" / "notes.md").exists()


def test_reset_state_keeps_logs_and_cli_history(tmp_path, monkeypatch):
    from atlas_plus import uninstall_cmd
    assert "logs" not in uninstall_cmd.DERIVED_STATE_ENTRIES
    assert "cli-history" not in uninstall_cmd.DERIVED_STATE_ENTRIES
    state = tmp_path / "state"; state.mkdir()
    (state / ".sextant-state").write_text("marker"); (state / "state.sqlite").write_bytes(b"")
    (state / "logs").mkdir(); (state / "logs" / "activity.ndjson").write_text("{}")
    (state / "temporal_graph").mkdir(); (state / "temporal_graph" / "g.json").write_text("{}")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
    out = uninstall_cmd.clear_derived_state(yes=True)
    assert out.startswith("cleared derived")
    assert (state / "logs" / "activity.ndjson").exists()
    assert not (state / "temporal_graph").exists()


# ---------------- anti propagation skip ----------------

@pytest.mark.parametrize("rel", ["Versions/x.md", "ARCHIVE/y.md", ".git/config.md", ".obsidian/a.md",
                                 ".trash/b.md", "projects/.hidden/c.md"])
def test_anti_propagation_skips_case_insensitively_and_dot_dirs(rel):
    from atlas_plus.mcp.memory_server.tools import anti
    assert anti._skip_propagation(rel)
    assert not anti._skip_propagation("projects/demo.md")


# ---------------- weekly synthesis ----------------

def test_header_only_weekly_draft_is_empty(tmp_path):
    from atlas_plus.curation import weekly_synthesis as ws
    draft = tmp_path / "weekly-draft.md"
    draft.write_text("# Weekly draft -- week of 2026-11-09\n", encoding="utf-8")
    called = []
    out = ws.run_weekly_synthesis(draft_file=draft, output_dir=tmp_path / "out",
                                  suggest_facts=False, _call_llm_fn=lambda *a, **k: called.append(1) or "junk")
    assert out["status"] == "empty_draft" and not called
    assert not list((tmp_path / "out").glob("*")) if (tmp_path / "out").exists() else True


# ---------------- tickets ----------------

def test_update_ticket_fields_keeps_unknown_keys_and_body(tmp_path, monkeypatch):
    from atlas_plus.tickets import store
    root = tmp_path / "tickets"; (root / "demo").mkdir(parents=True)
    t = root / "demo" / "DEMO-001.md"
    t.write_text("---\nid: DEMO-001\ntitle: A thing --- with dashes\nproject: demo\nstatus: todo\n"
                 "assignee: human\npriority: normal\neffort: M\nlabels: [a, b]\ndue: 2026-12-01\n"
                 "updated: 2026-11-01\n---\n\nBody line one.\n\n---\n\nBody keeps its own rule.\n", encoding="utf-8")
    monkeypatch.setattr(store, "TICKETS_ROOT", root, raising=False)
    import importlib
    monkeypatch.setattr(importlib.import_module("atlas_plus.mcp.memory_server.versioning"), "version_before_write", lambda p, **k: None)
    updated = store.update_ticket_fields("DEMO-001", {"priority": "high"}, tickets_root=root)
    text = t.read_text(encoding="utf-8")
    assert updated.priority == "high"
    assert "priority: high" in text and "labels: [a, b]" in text and "due: 2026-12-01" in text
    assert "Body keeps its own rule." in text and "title: A thing --- with dashes" in text
    with pytest.raises(ValueError):
        store.update_ticket_fields("DEMO-001", {"status": "done"}, tickets_root=root)


def test_ticket_rewrite_refuses_when_version_copy_fails(tmp_path, monkeypatch):
    from atlas_plus.tickets import store
    root = tmp_path / "tickets"; (root / "demo").mkdir(parents=True)
    t = root / "demo" / "DEMO-001.md"
    t.write_text("---\nid: DEMO-001\ntitle: x\nproject: demo\nstatus: todo\nupdated: 2026-11-01\n---\nbody\n", encoding="utf-8")
    before = t.read_text(encoding="utf-8")

    def boom(p, **k): raise OSError("disk full")
    import importlib
    monkeypatch.setattr(importlib.import_module("atlas_plus.mcp.memory_server.versioning"), "version_before_write", boom)
    with pytest.raises(RuntimeError):
        store.update_ticket_fields("DEMO-001", {"priority": "high"}, tickets_root=root)
    with pytest.raises(RuntimeError):
        store.update_ticket_status("DEMO-001", "done", tickets_root=root)
    assert t.read_text(encoding="utf-8") == before


# ---------------- transcripts torn tail ----------------

def test_torn_tail_after_journaled_head_is_set_aside(tmp_path, monkeypatch):
    from atlas_plus.transcripts.store import TranscriptStore, TurnRecord
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    s = TranscriptStore(root=tmp_path / "transcripts")
    r1 = s.append(TurnRecord(conversation_id="c1", source="test", role="user", content="one"))
    path = s._handles["c1"].path
    # the process dies mid-write: a torn fragment lands after the journaled head
    with path.open("ab") as f:
        f.write(b'{"turn_id": "x", "conversation_id": "c1", "cont')
    s2 = TranscriptStore(root=tmp_path / "transcripts")   # restart, no recovery run
    r2 = s2.append(TurnRecord(conversation_id="c1", source="test", role="assistant", content="two"))
    lines = path.read_bytes().splitlines()
    recs = [json.loads(l) for l in lines]
    assert [r["content"] for r in recs] == ["one", "two"]
    assert recs[1]["hash_prev"] == r1.hash
    torn = list(path.parent.glob(path.name + ".torn-*"))
    assert len(torn) == 1 and torn[0].read_bytes().startswith(b'{"turn_id": "x"')


def test_journal_behind_complete_lines_advances_instead_of_forking(tmp_path, monkeypatch):
    from atlas_plus.transcripts.store import TranscriptStore, TurnRecord
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    s = TranscriptStore(root=tmp_path / "transcripts")
    s.append(TurnRecord(conversation_id="c1", source="test", role="user", content="one"))
    h = s._handles["c1"]
    saved = h.journal_path.read_text(encoding="utf-8")
    r2 = s.append(TurnRecord(conversation_id="c1", source="test", role="assistant", content="two"))
    h.journal_path.write_text(saved, encoding="utf-8")   # journal lags one fsynced turn
    s2 = TranscriptStore(root=tmp_path / "transcripts")
    r3 = s2.append(TurnRecord(conversation_id="c1", source="test", role="user", content="three"))
    assert r3.hash_prev == r2.hash
    assert not list(h.path.parent.glob(h.path.name + ".torn-*"))
