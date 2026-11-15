r"""AG85 (sweep 23 agentic fix wave, expert review of the sweep 22 fold).

- MinHash-banded duplicate scan: every pair of every bucket went into one set that was sorted before the
  first candidate was yielded, so a template-heavy corpus (one bucket per band) ran far past its time budget
  and then, because the budget check came first, returned NO pairs (3,000 notes, 5 s budget: 52 s, 0 pairs).
- below a threshold of about 0.11 the 400-band cap cannot give the promised 99% recall (-t 0.05: 63%).
- redaction: `API key: revoked`, `token: pending`, `Client secret: rotated` were masked (the Turkish status
  words were in the list, their English siblings were not).
- setup hook parser: POSIX workspace paths were case-folded (two Linux workspaces merged), `//srv/share`
  and `\\srv\share` were two workspaces, and `/` parsed as '' (an unscoped install).
"""
from __future__ import annotations

import os
import shlex
import time

import pytest

from atlas_plus.curation import duplicates as D


def _template_corpus(root, n):
    d = root / "reference"
    d.mkdir(parents=True)
    words = " ".join(f"word{c}{c}x" for c in "abcdefghijklmnopqrst")
    for i in range(n):
        (d / f"note{i:05d}.md").write_text(f"Template note {words} uniq{i}q\n", encoding="utf-8")


def test_banded_scan_on_template_corpus_returns_pairs_within_budget(tmp_path, monkeypatch):
    _template_corpus(tmp_path, 1500)
    monkeypatch.setenv("ATLAS_DUPLICATES_EXACT_WORK", "0")  # force the MinHash candidates
    monkeypatch.setenv("ATLAS_DUPLICATES_BUDGET_SECONDS", "1")
    t = time.monotonic()
    pairs = D.find_duplicates(tmp_path)
    elapsed = time.monotonic() - t
    assert D._LAST_APPROX[0] is True
    assert len(pairs) == D._MAX_FILE_PAIRS  # every pair is a near duplicate; the cap's worth comes back
    assert all(p.similarity > 0.8 for p in pairs)
    assert elapsed < 30


def test_lsh_generator_stops_at_its_deadline(tmp_path):
    shared = [["t%d" % k for k in range(20)] for _ in range(3000)]  # one bucket per band, 4.5M pairs
    t = time.monotonic()
    out = []
    for pair in D._lsh_pairs(shared, 0.4, time.monotonic() + 0.5):
        out.append(pair)
        if pair is None:
            break
    assert out[-1] is None
    assert time.monotonic() - t < 15


def test_banded_pairs_match_exact_pairs(tmp_path, monkeypatch):
    import random
    rnd = random.Random(5)
    vocab = [f"v{i:03d}zz" for i in range(300)]
    d = tmp_path / "reference"
    d.mkdir()
    for i in range(300):
        ws = rnd.sample(vocab, 30)
        (d / f"n{i:04d}.md").write_text(" ".join(ws), encoding="utf-8")
        if i % 10 == 0:
            (d / f"n{i:04d}b.md").write_text(" ".join(ws[:26] + rnd.sample(vocab, 4)), encoding="utf-8")
    monkeypatch.setenv("ATLAS_DUPLICATES_EXACT", "on")
    exact = {(p.file1, p.file2) for p in D.find_duplicates(tmp_path)}
    monkeypatch.delenv("ATLAS_DUPLICATES_EXACT")
    monkeypatch.setenv("ATLAS_DUPLICATES_EXACT_WORK", "0")
    banded = {(p.file1, p.file2) for p in D.find_duplicates(tmp_path)}
    assert D._LAST_APPROX[0] is True
    assert exact and banded == exact


def test_low_threshold_stays_exact(tmp_path, monkeypatch):
    d = tmp_path / "reference"
    d.mkdir()
    for i in range(40):
        (d / f"n{i}.md").write_text(f"alpha{i % 3}x bravo{i % 5}x charlie{i % 7}x delta{i}x echo{i}x foxtrot{i}x", encoding="utf-8")
    monkeypatch.setenv("ATLAS_DUPLICATES_EXACT", "on")
    exact = {(p.file1, p.file2) for p in D.find_duplicates(tmp_path, threshold=0.05)}
    monkeypatch.delenv("ATLAS_DUPLICATES_EXACT")
    monkeypatch.setenv("ATLAS_DUPLICATES_EXACT_WORK", "0")
    got = {(p.file1, p.file2) for p in D.find_duplicates(tmp_path, threshold=0.05)}
    assert D._LAST_APPROX[0] is False  # 400 bands give 63% recall at 0.05: the exact scan runs
    assert got == exact


@pytest.mark.parametrize("text", [
    "API key: revoked", "token: pending", "Client secret: rotated", "Secret key: unchanged",
    "Access key: pending", "La clave API: pendiente", "password: deprecated",
])
def test_status_word_after_key_name_is_not_masked(text):
    from atlas_plus.redaction import redact
    assert redact(text) == text


def test_real_value_after_two_word_key_name_still_masked():
    from atlas_plus.redaction import redact
    value = "Xk29" + "fLq8" + "Zp7w"
    for name in ("Secret key", "API key", "Client secret", "access key"):
        out = redact(f"{name}: {value}")
        assert value not in out and "[REDACTED]" in out


def _item(ws):
    return {"hooks": [{"type": "command",
                       "command": f"python -m atlas_plus.hooks.run --env ATLAS_WORKSPACE_ROOT={shlex.quote(ws)} stop"}]}


def test_posix_workspaces_differing_in_case_stay_two(monkeypatch):
    from atlas_plus import setup_cmd as S
    monkeypatch.setattr(S.os, "name", "posix")
    a, b = _item("/home/u/WS"), _item("/home/u/ws")
    assert S._hook_workspace(a) != S._hook_workspace(b)
    merged = S._merge([a], [b])  # the second install's merge keeps the first install's hook
    assert a in merged and b in merged


def test_unc_spellings_are_one_workspace_and_root_is_scoped():
    from atlas_plus import setup_cmd as S
    import os
    back = "\\" * 2 + "srv" + "\\" + "share" + "\\" + "ws"
    if os.name == "nt":  # a share is a Windows notion; on POSIX `//home/u/ws` is an ordinary path (MN79)
        assert S._hook_workspace(_item(back)) == S._hook_workspace(_item("//srv/share/ws"))
        merged = S._merge([_item(back)], [_item("//srv/share/ws")])
        assert len(merged) == 1  # the forward-slash spelling replaced the backslash one: one Stop hook
    else:
        assert S._hook_workspace(_item("//home/u/WS")) != S._hook_workspace(_item("//home/u/ws"))
    assert S._hook_workspace(_item("/")) != ""
