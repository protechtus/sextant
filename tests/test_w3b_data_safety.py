"""Regression tests for the 2026-11-09 data-safety audit (rows 8, 11, 18, 27, 28, 33, 34).

Row 19 (cloud fallback opt-in) is covered in test_summarizer_providers.py / test_llm.py.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
import yaml


def _fm(text: str) -> dict:
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n", 2)[1])


# -- row 8: weekly synthesis -------------------------------------------------

def test_weekly_synthesis_rerun_never_overwrites_and_archives_draft(tmp_path, monkeypatch):
    from atlas_plus.curation.weekly_synthesis import _week_str, run_weekly_synthesis

    monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
    out = tmp_path / "mem" / "sessions"
    draft = tmp_path / "state" / "weekly-draft.md"
    draft.parent.mkdir(parents=True)
    draft.write_text("# draft\nMonday: built the thing\n", encoding="utf-8")

    r1 = run_weekly_synthesis(draft_file=draft, output_dir=out, suggest_facts=False,
                              _call_llm_fn=lambda p: "FIRST SYNTHESIS")
    first = out / f"_week-{_week_str()}.md"
    assert r1["written"] and first.exists()
    # the draft was archived, not just truncated
    archived = draft.parent / f"weekly-draft-{_week_str()}.md"
    assert "built the thing" in archived.read_text(encoding="utf-8")

    draft.write_text("# draft\nTuesday: more\n", encoding="utf-8")
    r2 = run_weekly_synthesis(draft_file=draft, output_dir=out, suggest_facts=False,
                              _call_llm_fn=lambda p: "SECOND SYNTHESIS")
    assert "FIRST SYNTHESIS" in first.read_text(encoding="utf-8")
    second = out / f"_week-{_week_str()}-v2.md"
    assert r2["output_path"] == str(second)
    assert "SECOND SYNTHESIS" in second.read_text(encoding="utf-8")
    assert "more" in (draft.parent / f"weekly-draft-{_week_str()}-2.md").read_text(encoding="utf-8")


# -- row 27: stale.archive_file ---------------------------------------------

def test_archive_file_never_overwrites_previous_archive(tmp_path):
    from atlas_plus.curation.stale import archive_file

    for i in range(3):
        (tmp_path / "notes").mkdir(exist_ok=True)
        (tmp_path / "notes" / "a.md").write_text(f"version {i}", encoding="utf-8")
        archive_file("notes/a.md", memory_root=tmp_path)
    arch = tmp_path / "archive" / f"{date.today().isoformat()}-curation"
    contents = sorted(p.read_text(encoding="utf-8") for p in arch.iterdir())
    assert contents == ["version 0", "version 1", "version 2"]


# -- row 33: dream / research / inspiration ----------------------------------

def test_dream_same_day_rerun_does_not_overwrite(tmp_path, monkeypatch):
    import atlas_plus.curation.dream_mode as mod

    monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
    root = tmp_path / "mem"
    (root / "feedback").mkdir(parents=True)
    (root / "feedback" / "x.md").write_text("---\nid: x\ntitle: X\n---\nbody text here\n", encoding="utf-8")
    (root / "sessions").mkdir()
    monkeypatch.setattr(mod, "_date_str", lambda: "2026-05-17")
    r1 = mod.run_dream(memory_root=root, scopes=["feedback"], _call_llm_fn=lambda p: "ONE")
    (root / "feedback" / "x.md").write_text("---\nid: x\ntitle: X\n---\nchanged body text\n", encoding="utf-8")
    r2 = mod.run_dream(memory_root=root, scopes=["feedback"], _call_llm_fn=lambda p: "TWO")
    assert (root / "sessions" / "_dream-2026-05-17.md").read_text(encoding="utf-8").count("ONE") == 1
    assert "TWO" in (root / "sessions" / "_dream-2026-05-17-2.md").read_text(encoding="utf-8")
    assert r1["written"] and r2["written"]


def test_persist_ideas_same_day_rerun_does_not_overwrite(tmp_path):
    from atlas_plus.brain.inspiration import Idea, persist_ideas

    import inspect
    fields = {k for k in inspect.signature(Idea).parameters}
    kw = {"text": "an idea", "status": "quarantined", "lane": "a", "confidence": 0.5,
          "support": 1, "source": "t"}
    idea = Idea(**{k: v for k, v in kw.items() if k in fields})
    p1 = persist_ideas([idea], tmp_path)
    p2 = persist_ideas([idea], tmp_path)
    assert p1[0] != p2[0]
    assert p1[0].exists() and p2[0].exists()


# -- row 11: auto session log ------------------------------------------------

@pytest.mark.asyncio
async def test_auto_session_log_gated_by_autonomous_off(monkeypatch):
    from atlas_plus.brain import scheduler

    monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
    monkeypatch.delenv("ATLAS_AUTO_SESSION_LOG", raising=False)
    called = []
    with patch("atlas_plus.sprint_summary.write_session_log",
               lambda *a, **k: called.append(1) or {"ok": True}):
        await scheduler._auto_session_log()
    assert not called


def test_session_log_versions_and_writes_atomically(tmp_path, monkeypatch):
    from atlas_plus import sprint_summary as ss

    mem = tmp_path / "mem"
    (mem / "sessions").mkdir(parents=True)
    hist = mem / "sessions" / "_history.md"
    hist.write_text("OLD HISTORY\n", encoding="utf-8")
    upd = tmp_path / "state" / "updates.txt"
    upd.parent.mkdir()
    upd.write_text("OLD UPDATES\n", encoding="utf-8")

    ss._version_existing(hist, mem)
    ss._atomic_write(hist, "NEW\nOLD HISTORY\n")
    ss._version_existing(upd, None)
    ss._atomic_write(upd, "NEW\nOLD UPDATES\n")

    versions = list((mem / "versions" / "sessions").glob("_history_*"))
    assert versions and versions[0].read_text(encoding="utf-8") == "OLD HISTORY\n"
    uv = list((upd.parent / "versions").glob("updates_*"))
    assert uv and uv[0].read_text(encoding="utf-8") == "OLD UPDATES\n"
    assert hist.read_text(encoding="utf-8").startswith("NEW")
    assert not list(hist.parent.glob(".*.tmp"))


# -- row 18: updates file never touches a user's file ------------------------

def test_updates_file_defaults_into_state_dir(tmp_path, monkeypatch):
    from atlas_plus import paths

    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("ATLAS_STATE_DIR", raising=False)
    monkeypatch.delenv("ATLAS_UPDATES_FILE", raising=False)
    assert paths.updates_file() == paths.state_dir() / "updates.txt"
    assert paths.updates_file() != tmp_path / "updates.txt"
    mine = tmp_path / "my-updates.txt"
    monkeypatch.setenv("ATLAS_UPDATES_FILE", str(mine))
    assert paths.updates_file() == mine


def test_renderer_writes_atomically_and_versions_beside_state_file(tmp_path):
    from atlas_plus.summarizer import renderer

    state = tmp_path / "state"
    state.mkdir()
    updates = state / "updates.txt"
    updates.write_text("====\nold\n", encoding="utf-8")
    sessions = tmp_path / "mem" / "sessions"
    with patch.object(renderer, "UPDATES_TXT", updates), \
         patch.object(renderer, "MEMORY_SESSIONS_DIR", sessions), \
         patch.object(renderer, "VERSIONS_DIR", tmp_path / "mem" / "versions" / "sessions"), \
         patch.object(renderer, "is_session_autonomous", return_value=True):
        renderer.write_outputs("log", "entry\n", "2026-11-09")
    assert "entry" in updates.read_text(encoding="utf-8")
    assert list((state / "versions").glob("updates_*"))
    assert not list(state.glob(".*.tmp"))


# -- row 28: import scope + frontmatter --------------------------------------

@pytest.mark.parametrize("scope", ["/etc", "../outside", "a/../../b", "C:/x", "versions/x",
                                   "archive", "_atlas/y", ".git/hooks", ""])
def test_import_scope_rejected(scope):
    from atlas_plus.import_cmd import _safe_scope

    with pytest.raises(ValueError):
        _safe_scope(scope)


def test_import_scope_accepts_plain_relative():
    from atlas_plus.import_cmd import _safe_scope

    assert _safe_scope("reference/obsidian/") == "reference/obsidian"


def test_import_titles_cannot_break_frontmatter(tmp_path, monkeypatch):
    from atlas_plus import import_cmd

    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    nasty = 'He said: "hi"\nstatus: archived # x'
    fm = _fm(import_cmd._frontmatter_for(nasty, "obsidian", "C:\\a: b\\c.md"))
    assert fm["title"] == nasty
    assert fm["type"] == "imported" and "status" not in fm


def test_obsidian_note_with_existing_frontmatter_is_stamped_and_keeps_keys(tmp_path, monkeypatch):
    from atlas_plus import import_cmd

    mem = tmp_path / "mem"
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "n.md").write_text("---\ntitle: Mine\ntags: [a, b]\n---\n\nBody here\n", encoding="utf-8")
    res = import_cmd.import_obsidian(vault, "reference/obsidian", execute=True)
    assert res["imported"] == 1
    out = (mem / "reference" / "obsidian" / "n.md").read_text(encoding="utf-8")
    fm = _fm(out)
    assert fm["title"] == "Mine" and fm["tags"] == ["a", "b"]
    assert fm["imported_from"] == "obsidian" and "imported_at" in fm and "source" in fm
    assert out.rstrip().endswith("Body here")
    # idempotent re-import: no duplicate
    res2 = import_cmd.import_obsidian(vault, "reference/obsidian", execute=True)
    assert res2["imported"] == 0 and res2["skipped_already_imported"] == 1


def test_import_reports_why_conversation_skipped(tmp_path, monkeypatch):
    import json
    from atlas_plus import import_cmd

    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    f = tmp_path / "conversations.json"
    f.write_text(json.dumps([{"title": "Empty one", "mapping": {}}]), encoding="utf-8")
    res = import_cmd.import_chatgpt(f, execute=True)
    assert res["skipped_empty"] == 1
    assert "Empty one" in res["skipped_reasons"][0]
    assert "no user/assistant text" in res["skipped_reasons"][0]


def test_gap_stub_frontmatter_survives_nasty_query(tmp_path):
    from atlas_plus.curation.cli import _create_gap_stubs

    q = 'what is "x": y\nstatus: active'
    with patch("atlas_plus.mcp.memory_server.fts.search", return_value=[]):
        created = _create_gap_stubs(
            [{"query": q, "miss_count": 3, "total": 3}], tmp_path / "gaps", "2026-11-09")
    fm = _fm(created[0].read_text(encoding="utf-8"))
    assert fm["source_query"] == q and fm["status"] == "unfilled"


# -- row 34: compact lock ----------------------------------------------------

def test_compact_skips_when_another_compactor_holds_lock(tmp_path):
    from atlas_plus.temporal_graph.file_provider import FileTemporalProvider

    root = tmp_path / "tg"
    p = FileTemporalProvider(graph_root=root)
    (root / ".compact.lock").write_text("12345", encoding="ascii")
    rep = p.compact()
    assert rep["skipped"]["compacted"] is False
    assert (root / ".compact.lock").exists()  # not ours; left alone


def test_compact_releases_lock(tmp_path):
    from atlas_plus.temporal_graph.file_provider import FileTemporalProvider

    root = tmp_path / "tg"
    p = FileTemporalProvider(graph_root=root)
    p.compact()
    assert not (root / ".compact.lock").exists()
