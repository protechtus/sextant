"""Tests for curation.reverify -- autonomous re-verification (action half of decay-as-behavior).
The model is always injected; no real LLM call. reset_clock is exercised on temp files."""

from __future__ import annotations

from pathlib import Path

from dataclasses import dataclass

from atlas_plus.curation.reverify import (
    ReverifyOutcome, parse_outcome, reset_clock, reverify_fact, run_reverification,
)


@dataclass
class _Cand:
    path: str
    title: str = "T"
    updated: str = "2025-01-01"
    days_overdue: int = 40
    half_life_days: int = 30


# --- parse_outcome --------------------------------------------------------

def test_parse_outcome_recognises_each_token():
    assert parse_outcome("STILL_VALID - obviously true") == ReverifyOutcome.STILL_VALID
    assert parse_outcome("OUTDATED, the manager left") == ReverifyOutcome.OUTDATED
    assert parse_outcome("NEEDS_HUMAN, can't tell") == ReverifyOutcome.NEEDS_HUMAN


def test_parse_outcome_ambiguous_defaults_to_needs_human():
    assert parse_outcome("I am not sure about this") == ReverifyOutcome.NEEDS_HUMAN
    assert parse_outcome("") == ReverifyOutcome.NEEDS_HUMAN


def test_parse_outcome_strips_think_block():
    raw = "<think>maybe still valid, maybe outdated</think>\nOUTDATED because superseded"
    assert parse_outcome(raw) == ReverifyOutcome.OUTDATED


def test_parse_outcome_first_token_wins():
    # If the model rambles and names two, the earliest token is the verdict.
    assert parse_outcome("STILL_VALID. It is not OUTDATED.") == ReverifyOutcome.STILL_VALID


# --- reverify_fact (model injected) --------------------------------------

def _gen(value):
    return lambda prompt: value


def test_reverify_still_valid_prescribes_reset():
    v = reverify_fact(path="reference/x.md", title="X", content="Alex uses Python 3.12",
                      generate=_gen("STILL_VALID\nstill the current setup"))
    assert v.outcome == ReverifyOutcome.STILL_VALID
    assert v.action == "reset_clock"
    assert v.resets_clock is True
    assert "current setup" in v.reasoning


def test_reverify_outdated_prescribes_quarantine():
    v = reverify_fact(path="reference/x.md", title="X", content="...",
                      generate=_gen("OUTDATED\nthe project was renamed"))
    assert v.outcome == ReverifyOutcome.OUTDATED
    assert v.action == "quarantine"
    assert v.resets_clock is False


def test_reverify_needs_human_prescribes_review():
    v = reverify_fact(path="reference/x.md", title="X", content="...",
                      generate=_gen("NEEDS_HUMAN\nonly Alex knows"))
    assert v.outcome == ReverifyOutcome.NEEDS_HUMAN
    assert v.action == "human_review"


def test_reverify_model_failure_defers_to_human():
    def boom(prompt):
        raise RuntimeError("ollama down")

    v = reverify_fact(path="reference/x.md", title="X", content="...", generate=boom)
    assert v.outcome == ReverifyOutcome.NEEDS_HUMAN
    assert v.action == "human_review"
    assert "failed" in v.reasoning


def test_reverify_passes_metadata_into_prompt():
    captured = {}

    def cap(prompt):
        captured["prompt"] = prompt
        return "STILL_VALID ok"

    reverify_fact(path="p.md", title="My Fact", content="body text",
                  updated="2025-01-01", age_days=120, half_life_days=30, generate=cap)
    assert "My Fact" in captured["prompt"]
    assert "120 days ago" in captured["prompt"]
    assert "body text" in captured["prompt"]


# --- reset_clock (the one mutation) --------------------------------------

def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_reset_clock_updates_updated_and_adds_last_verified(tmp_path):
    f = tmp_path / "m.md"
    _write(f, "---\ntitle: T\ntype: reference\nupdated: 2025-01-01\nhalf_life: 30\n---\n\nBody here.")
    changed = reset_clock(f, today="2026-06-05", now_iso="2026-06-05T12:00:00+00:00")
    assert changed is True
    out = f.read_text(encoding="utf-8")
    assert "updated: 2026-06-05" in out
    assert "last_verified_at: 2026-06-05T12:00:00+00:00" in out
    assert "Body here." in out          # body preserved
    assert "title: T" in out            # other frontmatter preserved
    assert "half_life: 30" in out


def test_reset_clock_replaces_existing_last_verified(tmp_path):
    f = tmp_path / "m.md"
    _write(f, "---\ntitle: T\nupdated: 2025-01-01\nlast_verified_at: 2025-01-01T00:00:00+00:00\n---\nBody")
    reset_clock(f, today="2026-06-05", now_iso="2026-06-05T12:00:00+00:00")
    out = f.read_text(encoding="utf-8")
    assert out.count("last_verified_at:") == 1
    assert "2026-06-05T12:00:00+00:00" in out


def test_reset_clock_noop_on_file_without_frontmatter(tmp_path):
    f = tmp_path / "plain.md"
    _write(f, "no frontmatter here")
    assert reset_clock(f) is False
    assert f.read_text(encoding="utf-8") == "no frontmatter here"


def test_reset_clock_body_with_horizontal_rules_preserved(tmp_path):
    # A body containing '---' must not confuse the frontmatter boundary.
    f = tmp_path / "m.md"
    _write(f, "---\ntitle: T\nupdated: 2025-01-01\n---\n\nIntro\n\n---\n\nSection two")
    reset_clock(f, today="2026-06-05", now_iso="2026-06-05T12:00:00+00:00")
    out = f.read_text(encoding="utf-8")
    assert "updated: 2026-06-05" in out
    assert "Section two" in out
    assert "Intro" in out


# --- run_reverification (the autonomous loop) ----------------------------

def _verdict_by_path(mapping):
    """generate() that returns a verdict token based on which path is in the prompt."""
    def gen(prompt):
        for needle, token in mapping.items():
            if needle in prompt:
                return token
        return "NEEDS_HUMAN"
    return gen


def test_run_reverification_buckets_by_outcome():
    cands = [_Cand("a.md", title="AAA"), _Cand("b.md", title="BBB"), _Cand("c.md", title="CCC")]
    gen = _verdict_by_path({"AAA": "STILL_VALID ok", "BBB": "OUTDATED gone", "CCC": "NEEDS_HUMAN ask"})
    reset_calls = []
    run = run_reverification(
        cands, autonomous=True, generate=gen,
        load_content=lambda c: "body", reset=lambda c: reset_calls.append(c.path) or True,
    )
    assert run.checked == 3
    assert run.reset == ["a.md"]
    assert run.quarantine == ["b.md"]
    assert run.human == ["c.md"]
    assert reset_calls == ["a.md"]  # only the still-valid one was reset


def test_run_reverification_gate_off_does_not_mutate():
    cands = [_Cand("a.md", title="AAA")]
    reset_calls = []
    run = run_reverification(
        cands, autonomous=False, generate=_verdict_by_path({"AAA": "STILL_VALID ok"}),
        load_content=lambda c: "body", reset=lambda c: reset_calls.append(c.path) or True,
    )
    assert run.reset == []
    assert run.would_reset == ["a.md"]   # recorded, not mutated
    assert reset_calls == []             # reset never called when autonomous is off


def test_run_reverification_respects_limit():
    cands = [_Cand(f"{i}.md") for i in range(20)]
    run = run_reverification(
        cands, limit=5, generate=lambda p: "NEEDS_HUMAN",
        load_content=lambda c: "x", reset=lambda c: True,
    )
    assert run.checked == 5


def test_run_reverification_summary_has_counts():
    cands = [_Cand("a.md", title="AAA")]
    run = run_reverification(
        cands, autonomous=True, generate=_verdict_by_path({"AAA": "STILL_VALID ok"}),
        load_content=lambda c: "body", reset=lambda c: True,
    )
    md = run.summary_md()
    assert "checked: 1" in md
    assert "a.md" in md
