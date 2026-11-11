"""Tests for atlas_plus.brain.promotion -- the Inspiration Engine promotion gate.

All dependencies (write_fact / check_contradiction / record_anti) are injected, so no
test touches the live memory corpus or temporal graph.
"""

from __future__ import annotations

from atlas_plus.brain import promotion as promo
from atlas_plus.brain import inspiration as insp


# --- core promote / reject -------------------------------------------------

def test_promote_writes_when_no_contradiction():
    calls = []

    def fake_write(scope, fact, source, confidence):
        calls.append((scope, fact, source, confidence))
        return f"C:/Tools/.memory/{scope}/x.md"

    res = promo.promote_idea(
        "Aurora is a strong name", scope="ideas",
        write_fact=fake_write, check_contradiction=lambda t: None,
    )
    assert res.action == "promoted"
    assert res.memory_path and "x.md" in res.memory_path
    assert calls and calls[0][1] == "Aurora is a strong name"


def test_promote_demotes_on_contradiction_without_writing():
    calls = []
    res = promo.promote_idea(
        "X", scope="ideas",
        write_fact=lambda *a: calls.append(a),
        check_contradiction=lambda t: "matches rejected pattern: Y",
    )
    assert res.action == "demoted"
    assert "contradiction" in res.detail
    assert calls == []  # never wrote to memory


def test_promote_empty_idea_demoted():
    res = promo.promote_idea("   ", scope="ideas", write_fact=lambda *a: "p", check_contradiction=lambda t: None)
    assert res.action == "demoted"


def test_reject_records_anti():
    recorded = []
    res = promo.reject_idea(
        "bad idea", record_anti=lambda scope, pat: recorded.append((scope, pat)), reason="off-brand",
    )
    assert res.action == "rejected"
    assert recorded and "bad idea" in recorded[0][1] and "off-brand" in recorded[0][1]


# --- matches_rejected (pure contradiction matcher) -------------------------

def test_matches_rejected_equality_and_overlap():
    assert promo.matches_rejected("name it Aurora", ["name it Aurora"]) is not None
    assert promo.matches_rejected("call the project Aurora", ["call the project Aurora now"]) is not None
    assert promo.matches_rejected("totally different thing", ["name it Aurora"]) is None
    assert promo.matches_rejected("anything", []) is None


def test_default_check_contradiction_fails_open(monkeypatch):
    import atlas_plus.mcp.memory_server.tools.anti as anti_mod

    def _raise(*a, **k):
        raise RuntimeError("anti list unavailable")

    monkeypatch.setattr(anti_mod, "list_anti", _raise)
    assert promo.default_check_contradiction("anything") is None  # fail-open, no block


def test_default_check_contradiction_matches_existing(monkeypatch):
    import atlas_plus.mcp.memory_server.tools.anti as anti_mod
    monkeypatch.setattr(anti_mod, "list_anti", lambda *a, **k: [{"reject_pattern": "name it Aurora"}])
    reason = promo.default_check_contradiction("name it Aurora")
    assert reason is not None and "rejected" in reason


# --- quarantine-file helpers -----------------------------------------------

def _write_idea_file(tmp_path, name, text):
    f = tmp_path / name
    f.write_text(insp.idea_to_markdown(insp.Idea(text=text)), encoding="utf-8")
    return f


def test_read_idea_body_strips_frontmatter(tmp_path):
    f = _write_idea_file(tmp_path, "01.md", "Project name: Aurora")
    assert promo.read_idea_body(f) == "Project name: Aurora"


def test_promote_idea_file_marks_status_and_preserves_body(tmp_path):
    f = _write_idea_file(tmp_path, "01-aurora.md", "Project name: Aurora")
    res = promo.promote_idea_file(
        f, scope="ideas",
        write_fact=lambda scope, fact, src, conf: "C:/Tools/.memory/ideas/p.md",
        check_contradiction=lambda t: None,
    )
    assert res.action == "promoted"
    txt = f.read_text(encoding="utf-8")
    assert "status: promoted" in txt
    assert "memory_path:" in txt
    assert "Project name: Aurora" in txt  # body preserved


def test_promote_idea_file_demotes_on_contradiction(tmp_path):
    f = _write_idea_file(tmp_path, "03.md", "dup idea")
    res = promo.promote_idea_file(
        f, scope="ideas",
        write_fact=lambda *a: "should-not-be-called",
        check_contradiction=lambda t: "matches rejected pattern: dup",
    )
    assert res.action == "demoted"
    assert "status: demoted" in f.read_text(encoding="utf-8")


def test_reject_idea_file_marks_rejected_and_records_anti(tmp_path):
    f = _write_idea_file(tmp_path, "02-bad.md", "bad idea")
    recorded = []
    res = promo.reject_idea_file(f, reason="off-brand", record_anti=lambda s, p: recorded.append((s, p)))
    assert res.action == "rejected"
    assert "status: rejected" in f.read_text(encoding="utf-8")
    assert recorded
