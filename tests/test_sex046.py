"""SEX-046: the curation report at scale (AG100 leftovers, sweep 26).

Three changes, each checked by running the path and comparing with an oracle or counting calls; none reads
source text and none sleeps:

1. MinHash minima come from a walk over each hash column's tokens in ascending order instead of a minimum
   per note per column. The minima must be the ones the direct computation gives (so the candidates, and
   the report, are unchanged).
2. The repeated-facts scan stops at its cap: when exact repeats already fill the answer, no fact is
   tokenised; and the index build before the near-repeat walk has a time budget.
3. One report runs the confirmation-decay pass once.
"""
from __future__ import annotations

import hashlib
import random
import time
from array import array


def _oracle_minima(notes: list, k: int) -> list:
    """The computation this replaced: per note, the column-wise minimum over its tokens' hash rows."""
    table: dict = {}
    out = []
    for toks in notes:
        if not toks:
            out.append(None)
            continue
        rows = []
        for t in toks:
            h = table.get(t)
            if h is None:
                h = table[t] = array("I", hashlib.shake_128(t.encode("utf-8", "surrogatepass")).digest(4 * k))
            rows.append(h)
        out.append(list(map(min, zip(*rows))))
    return out


def _notes(seed: int, n: int, vocab: int, lo: int, hi: int) -> list:
    rng = random.Random(seed)
    words = [f"w{i}" for i in range(vocab)]
    notes = [sorted(set(rng.sample(words, rng.randint(lo, hi)))) for _ in range(n)]
    notes[3] = []                                        # an empty note is skipped, not signed
    notes[5] = ["rare-a", "rare-b"]                      # short notes of rare words: the direct tail
    notes[7] = ["rare-b", "rare-c", words[0]]
    notes[11] = list(notes[12])                          # an identical pair
    return notes


def _postings(notes: list) -> dict:
    post: dict = {}
    for i, toks in enumerate(notes):
        for t in toks:
            post.setdefault(t, []).append(i)
    return post


def _check_minima(notes, k, **kw):
    from atlas_plus.curation.duplicates import _lsh_minima
    cols, cut = _lsh_minima(notes, k, **kw)
    assert not cut and len(cols) == k
    want = _oracle_minima(notes, k)
    for i, w in enumerate(want):
        if w is not None:
            assert [col[i] for col in cols] == w, f"note {i}"


def test_minima_match_the_direct_computation_on_a_shared_vocabulary():
    _check_minima(_notes(1, 120, 400, 40, 120), 24)


def test_minima_match_when_the_vocabulary_is_sparse_and_notes_are_small():
    # few notes per token: the column walk settles little and most notes take the direct tail
    _check_minima(_notes(2, 90, 5000, 3, 12), 18)


def test_minima_match_when_the_walk_is_capped(monkeypatch):
    from atlas_plus.curation import duplicates as d
    monkeypatch.setattr(d, "_SIG_POPS_MAX", 8)           # stop the walk early: everyone left is signed directly
    _check_minima(_notes(3, 300, 900, 5, 40), 12)


def test_minima_match_with_the_callers_postings():
    # find_duplicates passes its own index; only tokens held by two or more notes are in play
    notes = _notes(4, 150, 300, 20, 60)
    post = _postings(notes)
    shared = [[t for t in toks if len(post[t]) >= 2] for toks in notes]
    _check_minima(shared, 21, postings=post)


def test_candidate_pairs_are_the_ones_the_direct_signatures_give():
    from atlas_plus.curation.duplicates import _lsh_pairs, _lsh_shape
    notes = _notes(5, 200, 350, 30, 90)
    r, b = _lsh_shape(0.4)
    sig = _oracle_minima(notes, r * b)
    want = set()
    for i in range(len(notes)):
        for j in range(i + 1, len(notes)):
            if sig[i] and sig[j] and any(sig[i][x * r:(x + 1) * r] == sig[j][x * r:(x + 1) * r] for x in range(b)):
                want.add((i, j))
    got = list(_lsh_pairs(notes, 0.4))
    assert None not in got and len(got) == len(set(got))
    assert set(got) == want and (11, 12) in want


def test_a_passed_deadline_returns_whole_bands_and_says_partial():
    from atlas_plus.curation.duplicates import _lsh_minima
    notes = _notes(6, 60, 200, 20, 40)
    cols, cut = _lsh_minima(notes, 30, deadline=time.monotonic() - 1)
    assert cut and cols == []


def test_the_column_walk_does_less_work_than_one_minimum_per_note():
    # In-process against the oracle (the sanctioned form of a timing check): the walk settles 250 notes
    # of 300 words from the first few dozen tokens of each column; the oracle takes 300 minima per column
    # per note. Measured about 30x apart; the bound leaves a wide margin for a slow runner.
    from atlas_plus.curation.duplicates import _lsh_minima
    notes = [n for n in _notes(7, 250, 2500, 280, 320)]
    k = 90
    t0 = time.perf_counter()
    _oracle_minima(notes, k)
    t_oracle = time.perf_counter() - t0
    t0 = time.perf_counter()
    _lsh_minima(notes, k)
    t_new = time.perf_counter() - t0
    assert t_new < 0.75 * t_oracle, (t_new, t_oracle)


# ---------------------------------------------------------------- repeated facts: stop at the cap
def _fact_corpus(root, notes: int, shared: int, own: int):
    rng = random.Random(9)
    words = [f"alpha{i:04d}" for i in range(4000)]
    shared_facts = [" ".join(rng.sample(words, 8)) for _ in range(shared)]
    (root / "reference").mkdir(parents=True, exist_ok=True)
    for n in range(notes):
        lines = [f"# note {n}", ""]
        lines += [f"- {s}" for s in shared_facts]
        lines += ["- " + " ".join(rng.sample(words, 9)) for _ in range(own)]
        (root / "reference" / f"n{n:03d}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_a_full_list_of_exact_repeats_tokenises_nothing(tmp_path, monkeypatch):
    from atlas_plus.curation import duplicates as d
    _fact_corpus(tmp_path, notes=12, shared=6, own=3)    # 6 facts in 12 notes: 66 exact pairs
    full = d.find_fact_duplicates(memory_root=tmp_path, limit=10_000)
    assert sum(1 for x in full if x["kind"] == "exact") == 66
    calls = []
    real = d._tokens
    monkeypatch.setattr(d, "_tokens", lambda norm: calls.append(1) or real(norm))
    capped = d.find_fact_duplicates(memory_root=tmp_path, limit=20)
    assert capped == full[:20] and all(x["kind"] == "exact" for x in capped)
    assert calls == []                                   # before: every fact of every note was tokenised
    assert d._LAST_PARTIAL == [0, 0]


def test_below_the_cap_near_repeats_are_still_found(tmp_path):
    from atlas_plus.curation import duplicates as d
    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "a.md").write_text("- the billing service retries failed payment requests three times with backoff\n", encoding="utf-8")
    (ref / "b.md").write_text("- the billing service retries failed payment requests four times with backoff\n", encoding="utf-8")
    out = d.find_fact_duplicates(memory_root=tmp_path, limit=501)
    assert [x["kind"] for x in out] == ["near"]


def test_the_index_build_has_a_budget_and_the_scan_says_partial(tmp_path, monkeypatch):
    from atlas_plus.curation import duplicates as d
    _fact_corpus(tmp_path, notes=30, shared=0, own=100)  # 3,000 distinct facts, no exact repeat
    clock = [0.0]

    def tick():
        clock[0] += 1.0                                  # every look at the clock is a second later
        return clock[0]
    monkeypatch.setattr(time, "monotonic", tick)
    monkeypatch.setenv("ATLAS_DUPLICATES_BUDGET_SECONDS", "0.5")
    calls = []
    real = d._tokens
    monkeypatch.setattr(d, "_tokens", lambda norm: calls.append(1) or real(norm))
    d.find_fact_duplicates(memory_root=tmp_path, limit=501)
    assert 0 < len(calls) < 2000                         # before: all 3,000 facts were tokenised whatever the budget
    scanned, total = d._LAST_PARTIAL
    assert 0 < total and scanned < total
    assert len(calls) < total <= 3100                    # the total estimates every fact, not only the part indexed


# ---------------------------------------------------------------- one report, one decay pass
def test_one_report_runs_the_decay_pass_once(tmp_path, monkeypatch):
    from atlas_plus.curation import weekly_report as w
    from atlas_plus.mcp.memory_server import routing
    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "old.md").write_text("---\ntitle: old\nconfidence: high\nlast_confirmed: 2020-01-01\n---\n\n"
                                "- the deploy pipeline needs a manual approval step before release\n", encoding="utf-8")
    calls = []
    real = routing.downgrade_stale_confirmations

    def counted(*a, **k):
        calls.append(k)
        return real(*a, **k)
    monkeypatch.setattr(routing, "downgrade_stale_confirmations", counted)
    text, _ = w.generate_report(memory_root=tmp_path, dry_run=True)
    assert len(calls) == 1 and calls[0].get("dry_run") is True      # before: two identical passes
    assert "## Never confirmed and older than 90 days" in text
    assert "## Unconfirmed Stale Facts (1 file(s)" in text and "reference/old.md" in text


# ---------------------------------------------------------------- doctor counts every note
def test_doctor_health_overview_counts_past_ten_thousand_notes(tmp_path, monkeypatch):
    # 12,000 real notes would take minutes to write; the per-file scoring is replaced by a constant, the
    # walk, the cap and the totals are the real code.
    from atlas_plus.curation import health
    from atlas_plus.doctor import cli as doctor
    ref = tmp_path / "reference"
    ref.mkdir()
    for n in range(40):
        (ref / f"n{n:02d}.md").write_text("- a fact\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "MEMORY_ROOT", tmp_path)
    assert doctor.check_memory_health_overview()["total_files"] == 40

    def scan(root, scope="", min_health=0.0, limit=200):  # a corpus of 12,000 notes, honouring the caller's cap
        return [{"score": 1.0 if n % 2 else 0.5} for n in range(min(limit, 12_000))]
    monkeypatch.setattr(health, "scan_memory_health", scan)
    out = doctor.check_memory_health_overview()
    assert out["total_files"] == 12_000                  # before: 9,999, reported as the whole corpus
    assert out["score_100"] == 6_000 and out["score_50"] == 6_000
