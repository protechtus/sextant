"""SEX-046: the orphan scan walks the corpus once, builds no short tokens and scores a note only when needed.

Measured at 20,000 notes, warm: 12.2 s -> 7.7 s for the dashboard panel's call, results identical. Each test
runs the scan:
  a  the results equal a plain reference scan (one boundary regex per stem against every note, every note's
     health computed from its file) on a corpus with referenced and unreferenced notes, names with spaces,
     a CJK name, short names, a note without frontmatter, an unterminated one, skipped folders and files;
     for the panel's call, the report's call, a scope, a higher reference allowance and no limit;
  b  with a small `limit` only the notes the loop reaches are scored (fails before the change: every note's
     frontmatter was parsed up front), and a referenced note is never scored;
  c  the kept frontmatter block scores exactly as the whole text does, on every frontmatter shape;
  d  the corpus is walked once when there is no scope (fails before the change: twice).
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from atlas_plus.curation import health, orphan
from atlas_plus.curation.scan import is_excluded_mirror

OLD = (date.today() - timedelta(days=400)).isoformat()
NEW = (date.today() - timedelta(days=3)).isoformat()


def _note(path: Path, front: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((f"---\n{front}---\n" if front else "") + body, encoding="utf-8")


@pytest.fixture
def corpus(tmp_path) -> Path:
    mem = tmp_path / "mem"
    for i in range(40):
        front = f"title: note {i}\nupdated: {OLD if i % 3 else NEW}\n"
        if i % 5 == 0:
            front += "tags: [a]\nconfidence: high\nlast_confirmed: " + NEW + "\n"
        mention = f" see fact-{(i + 7) % 40:03d} for more" if i % 4 == 0 else ""
        _note(mem / ("reference" if i % 2 else "decisions") / f"fact-{i:03d}.md", front,
              f"The staging cluster note {i}{mention}.\n")
    _note(mem / "reference" / "Meeting Notes.md", f"title: m\nupdated: {OLD}\n", "minutes\n")
    _note(mem / "reference" / "Weekly Review 2026.md", f"title: w\nupdated: {OLD}\n", "review\n")
    _note(mem / "decisions" / "pointer.md", f"title: p\nupdated: {OLD}\n",
          "As the Meeting Notes say, and fact-003 too. Not weekly review 2025.\n")
    _note(mem / "reference" / "会議メモ.md", f"title: c\nupdated: {OLD}\n", "cjk\n")
    _note(mem / "decisions" / "cjk-pointer.md", f"title: cp\nupdated: {OLD}\n", "see 会議メモ here\n")
    _note(mem / "reference" / "abc.md", f"title: short\nupdated: {OLD}\n", "abc abc\n")
    _note(mem / "reference" / "plain-note.md", "", "no frontmatter, mentions abc\n")
    (mem / "reference" / "open-ended.md").write_text("---\ntitle: never closed\nbody\n", encoding="utf-8")
    (mem / "reference" / "empty-note.md").write_text("", encoding="utf-8")
    _note(mem / "archive" / "old-fact.md", f"title: a\nupdated: {OLD}\n", "fact-001 fact-002\n")
    _note(mem / "reference" / "_index.md", f"title: i\nupdated: {OLD}\n", "fact-005\n")
    return mem


def _reference(mem: Path, scope: str = "", min_age_days: int = 180, max_health: float = 0.5,
               max_inbound_refs: int = 0, limit: int = 200) -> list[dict]:
    """The scan as its docstring defines it, the slow and obvious way."""
    today = date.today()

    def scanned(f: Path) -> bool:
        parts = f.relative_to(mem).parts
        return not (any(p in orphan._SKIP_DIRS for p in parts) or is_excluded_mirror(parts)
                    or f.name.startswith("_"))

    files = [f for f in mem.rglob("*.md") if scanned(f)]
    texts = {f: f.read_text(encoding="utf-8", errors="replace") for f in files}
    inbound = {}
    for stem in {f.stem for f in files}:
        if len(stem) < 4:
            inbound[stem] = 0
            continue
        pat = re.compile(r"(?<![a-zA-Z0-9_-])" + re.escape(stem) + r"(?![a-zA-Z0-9_-])", re.IGNORECASE)
        inbound[stem] = sum(1 for f, t in texts.items() if f.stem != stem and pat.search(t))
    out = []
    for f in sorted((mem / scope if scope else mem).rglob("*.md")):
        if not scanned(f):
            continue
        h = health.compute_file_health(f, today)
        fm = h.get("frontmatter") or {}
        age = orphan._file_age_days(f, fm, today)
        if h["score"] > max_health or inbound[f.stem] > max_inbound_refs or age is None or age < min_age_days:
            continue
        out.append({
            "path": str(f.relative_to(mem)).replace("\\", "/"), "title": str(fm.get("title", f.stem))[:60],
            "score": h["score"], "inbound_refs": inbound[f.stem], "age_days": age, "issues": h["issues"],
            "orphan_score": round((1.0 - h["score"]) + min(1.0, age / 730) + (1.0 if inbound[f.stem] == 0 else 0.0), 3),
        })
        if len(out) >= limit:
            break
    out.sort(key=lambda r: r["orphan_score"], reverse=True)
    return out


@pytest.mark.parametrize("kwargs", [
    dict(min_age_days=90, max_health=0.5, limit=5),
    dict(min_age_days=180, max_health=0.5, limit=200),
    dict(min_age_days=0, max_health=1.0, limit=100000),
    dict(min_age_days=0, max_health=1.0, max_inbound_refs=2, limit=100000),
    dict(scope="decisions", min_age_days=0, max_health=1.0, limit=100000),
    dict(scope="reference", min_age_days=90, max_health=0.5, limit=3),
    dict(scope="no-such-folder", min_age_days=0, max_health=1.0, limit=10),
])
def test_a_results_equal_the_plain_reference_scan(corpus, kwargs):
    got = orphan.find_orphan_candidates(corpus, **kwargs)
    want = _reference(corpus, **kwargs)
    assert got == want
    if kwargs.get("limit", 0) > 1000 and not kwargs.get("scope"):
        assert got, "the corpus must produce candidates, or the comparison proves nothing"


def test_a_names_with_spaces_and_cjk_names_count_their_references(corpus):
    refs = orphan._build_inbound_refs(corpus)
    assert refs["Meeting Notes"] == 1 and refs["Weekly Review 2026"] == 0
    assert refs["会議メモ"] == 1 and refs["abc"] == 0 and refs["fact-003"] >= 1


def test_b_a_small_limit_scores_only_the_notes_it_reaches(corpus, monkeypatch):
    scored = []
    real = health.health_from_text

    def counting(text, today=None):
        scored.append(text)
        return real(text, today)

    monkeypatch.setattr(health, "health_from_text", counting)
    total = len(orphan._scan_files(corpus))
    got = orphan.find_orphan_candidates(corpus, min_age_days=0, max_health=1.0, limit=2)
    assert len(got) == 2
    assert len(scored) < total / 2, f"{len(scored)} of {total} notes were scored for a limit of 2"

    scored.clear()
    everything = orphan.find_orphan_candidates(corpus, min_age_days=0, max_health=1.0, limit=100000)
    referenced = {s for s, n in orphan._build_inbound_refs(corpus).items() if n > 0}
    assert referenced, "the corpus must hold referenced notes"
    assert len(scored) == total - len(referenced)  # a referenced note is never scored
    assert not {Path(r["path"]).stem for r in everything} & referenced


@pytest.mark.parametrize("text", [
    "", "plain text\n", "---\ntitle: never closed\nbody\n", "---\n---\nbody\n",
    f"---\ntitle: t\ntags: [a]\nupdated: {OLD}\nconfidence: high\nlast_confirmed: {NEW}\n---\nbody\n---\nmore\n",
    "---\ntitle: [unclosed\n---\nbody\n", "---\r\ntitle: crlf\r\nupdated: 2020-01-01\r\n---\r\nbody\r\n",
    "--- not a block\ntext\n---\n", "---\ntitle: t\n--- trailing words\nbody\n", "﻿---\ntitle: bom\n---\n",
])
def test_c_the_kept_frontmatter_block_scores_as_the_whole_text(text):
    today = date.today()
    assert health.health_from_text(orphan._frontmatter_head(text), today) == health.health_from_text(text, today)


def test_d_the_corpus_is_walked_once_without_a_scope(corpus, monkeypatch):
    walks = []
    real = Path.rglob

    def counting(self, pattern, **kw):
        walks.append(self)
        return real(self, pattern, **kw)

    monkeypatch.setattr(Path, "rglob", counting)
    orphan.find_orphan_candidates(corpus, min_age_days=0, max_health=1.0, limit=5)
    assert walks == [corpus]


@pytest.mark.parametrize("top", ["Just a sentence under a rule", "- one\n- two", "42", "true", "[a, b]"])
def test_e_a_top_block_that_is_not_key_value_pairs_is_text_not_frontmatter(corpus, top):
    """Found while writing test c: a note that opens with a rule line and a sentence (or a list) made the
    health scoring raise AttributeError, and with it the orphan scan and the health scan (fails before)."""
    note = corpus / "reference" / "rule-then-text.md"
    note.write_text(f"---\n{top}\n---\nbody of the note\n", encoding="utf-8")
    scored = health.compute_file_health(note, date.today())
    assert scored["score"] == 0.0 and scored["issues"] == ["no_frontmatter"] and scored["frontmatter"] == {}
    kwargs = dict(min_age_days=0, max_health=1.0, limit=100000)
    got = orphan.find_orphan_candidates(corpus, **kwargs)
    assert got == _reference(corpus, **kwargs)
    assert "reference/rule-then-text.md" in {r["path"] for r in got}
    assert any(r["path"] == "reference/rule-then-text.md" for r in health.scan_memory_health(corpus))
