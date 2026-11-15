"""AG84 (sweep 22, performance): the duplicate scan on a shared vocabulary, ticket numbering per create,
the Stop hook's "nothing new" decision, and the first search's silent index build."""

from __future__ import annotations

import itertools
import json
import os
import random
import time
from datetime import datetime
from pathlib import Path

import pytest


# ---------------------------------------------------------------- the file-level duplicate scan
def _shared_vocab_corpus(root: Path, n: int, seed: int = 7) -> None:
    rng = random.Random(seed)
    letters = "abcdefghijklmnopqrstuvwxyz"
    vocab = sorted({"".join(rng.choice(letters) for _ in range(rng.randint(4, 9))) for _ in range(1300)})[:1200]
    weights = [1.0 / (r + 1) ** 0.6 for r in range(len(vocab))]
    (root / "reference").mkdir(parents=True, exist_ok=True)
    prev = None
    for i in range(n):
        if prev is not None and i % 50 == 1:
            words = list(prev)
            for k in rng.sample(range(len(words)), max(1, len(words) // 10)):
                words[k] = rng.choices(vocab, weights)[0]
        else:
            words = rng.choices(vocab, weights, k=rng.randint(60, 110))
        prev = words
        (root / "reference" / f"n{i:05d}.md").write_text("---\nid: n\n---\n" + " ".join(words) + "\n", encoding="utf-8")


def _scan(root: Path):
    from atlas_plus.curation import duplicates as dup
    t = time.perf_counter()
    pairs = dup.find_duplicates(memory_root=root)
    return time.perf_counter() - t, pairs


def test_duplicate_scan_grows_about_linearly_on_a_shared_vocabulary(tmp_path, monkeypatch):
    """8x the notes cost ~40x on 6d3aa82 (every rare-first prefix token sits in a fixed share of all
    notes); with the MinHash candidates it is ~8-10x. The bound sits between, with room both ways."""
    monkeypatch.setenv("ATLAS_DUPLICATES_BUDGET_SECONDS", "0")
    monkeypatch.setenv("ATLAS_DUPLICATES_EXACT_WORK", "0")  # the banded candidates at both sizes
    small, big = tmp_path / "small", tmp_path / "big"
    _shared_vocab_corpus(small, 600)
    _shared_vocab_corpus(big, 4800)
    for p in [*small.rglob("*.md"), *big.rglob("*.md")]:
        p.read_bytes()                             # freshly written files: a Windows scanner reads them first
    _scan(small)                                   # warm imports
    t_small = min(_scan(small)[0] for _ in range(2))
    t_big, pairs = _scan(big)
    assert len(pairs) >= 90                        # the planted near-repeats (one per 50 notes) are all found
    assert t_big / max(t_small, 1e-3) < 20, (t_small, t_big)


def _pairs_near_threshold(root: Path) -> None:
    """60 pairs whose Jaccard sits just above 0.40, in 300 unrelated notes over one vocabulary."""
    rng = random.Random(11)
    vocab = ["".join(rng.choice("bcdfghjklmnpqrstvwxz") + rng.choice("aeiou") for _ in range(3)) for _ in range(3000)]
    vocab = sorted(set(vocab))
    (root / "reference").mkdir(parents=True, exist_ok=True)
    k = 0
    for p in range(60):
        common = rng.sample(vocab, 36)
        a = common + rng.sample(vocab, 25)
        b = common + rng.sample(vocab, 25)
        for words in (a, b):
            (root / "reference" / f"p{k:04d}.md").write_text("---\nid: p\n---\n" + " ".join(words) + "\n", encoding="utf-8")
            k += 1
    for i in range(300):
        (root / "reference" / f"r{i:04d}.md").write_text("---\nid: r\n---\n" + " ".join(rng.sample(vocab, 60)) + "\n", encoding="utf-8")


def test_banded_candidates_find_pairs_at_the_threshold_with_exact_similarities(tmp_path, monkeypatch):
    from atlas_plus.curation import duplicates as dup
    _pairs_near_threshold(tmp_path)
    monkeypatch.setenv("ATLAS_DUPLICATES_BUDGET_SECONDS", "0")
    monkeypatch.setenv("ATLAS_DUPLICATES_EXACT", "on")
    exact = {(p.file1, p.file2): p.similarity for p in dup.find_duplicates(memory_root=tmp_path)}
    assert not dup._LAST_APPROX[0]
    monkeypatch.delenv("ATLAS_DUPLICATES_EXACT")
    monkeypatch.setenv("ATLAS_DUPLICATES_EXACT_WORK", "0")
    banded = {(p.file1, p.file2): p.similarity for p in dup.find_duplicates(memory_root=tmp_path)}
    assert dup._LAST_APPROX[0]
    near = [s for s in exact.values() if s < 0.5]
    assert len(near) >= 50                          # the corpus really sits at the threshold
    assert set(banded) <= set(exact)                # nothing invented
    assert all(banded[k] == exact[k] for k in banded)  # the similarity is the exact Jaccard
    assert len(banded) >= 0.95 * len(exact)         # recall at J ~ 0.4 (the design target is 99%)


def test_small_corpus_stays_on_the_exact_scan(tmp_path, monkeypatch):
    from atlas_plus.curation import duplicates as dup
    _pairs_near_threshold(tmp_path)
    monkeypatch.delenv("ATLAS_DUPLICATES_EXACT_WORK", raising=False)
    pairs = dup.find_duplicates(memory_root=tmp_path)
    assert dup._LAST_APPROX[0] is False
    toks = {p.name: dup._tokenize(p.read_text(encoding="utf-8")) for p in (tmp_path / "reference").glob("*.md")}
    brute = {tuple(sorted((a, b))) for a, b in itertools.combinations(sorted(toks), 2)
             if len(toks[a] & toks[b]) / len(toks[a] | toks[b]) >= dup.DEFAULT_THRESHOLD}
    got = {tuple(sorted((p.file1.rsplit("/", 1)[-1], p.file2.rsplit("/", 1)[-1]))) for p in pairs}
    assert got == brute


# ---------------------------------------------------------------- ticket numbering
@pytest.fixture
def _tickets_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "ws" / ".atlas"))
    root = tmp_path / "tickets"
    root.mkdir()
    return root


def test_ticket_create_cost_does_not_grow_with_the_project(_tickets_env):
    from atlas_plus.tickets import store

    root = _tickets_env

    def batch(k):
        t = time.perf_counter()
        for i in range(k):
            store.create_ticket(f"t {i}", "proj", tickets_root=root, interactive=True)
        return (time.perf_counter() - t) / k

    batch(20)
    early = min(batch(40), batch(40))
    for i in range(2400):   # 2,500 tickets: 6d3aa82 listed the folder (and the archive) on every create
        (root / "proj" / f"PRO-{i + 1000:04d}.md").write_text("---\nid: x\n---\n", encoding="utf-8")
    batch(1)
    late = min(batch(40), batch(40))
    assert late < 3 * early, (early, late)


def test_ticket_numbers_never_recycle_after_outside_changes(_tickets_env):
    from atlas_plus.tickets import store

    root = _tickets_env
    ids = [store.create_ticket(f"t {i}", "proj", tickets_root=root, interactive=True).id for i in range(3)]
    assert ids == ["PRO-001", "PRO-002", "PRO-003"]
    arch = root / "_archive" / "2026-11-14" / "proj"
    arch.mkdir(parents=True)
    os.replace(root / "proj" / "PRO-003.md", arch / "PRO-003.md")       # archived by hand
    assert store.create_ticket("t", "proj", tickets_root=root, interactive=True).id == "PRO-004"
    (root / "proj" / "PRO-020.md").write_text("---\nid: PRO-020\n---\n", encoding="utf-8")  # another writer
    assert store.create_ticket("t", "proj", tickets_root=root, interactive=True).id == "PRO-021"
    (arch / "PRO-030.md").write_text("---\nid: PRO-030\n---\n", encoding="utf-8")  # straight into the archive
    assert store.create_ticket("t", "proj", tickets_root=root, interactive=True).id == "PRO-031"


# ---------------------------------------------------------------- the Stop hook's summarizer child
def _turn(i: int, sid: str) -> str:
    role = "user" if i % 2 == 0 else "assistant"
    return json.dumps({"type": role, "uuid": f"u{i}", "sessionId": sid, "timestamp": "2026-11-14T10:00:00Z",
                       "message": {"role": role, "content": [{"type": "text", "text": f"turn {i} " + "x" * 300}]}}) + "\n"


@pytest.fixture
def _hook_env(tmp_path, monkeypatch):
    import importlib
    ws = tmp_path / "ws"
    (ws / ".memory" / "sessions").mkdir(parents=True)
    cc = tmp_path / "cc" / "projects"
    (cc / "p").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(ws))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(ws / ".atlas"))
    monkeypatch.setenv("ATLAS_CLAUDE_CODE_ROOT", str(cc))
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    monkeypatch.setenv("ATLAS_OLLAMA_URL", "http://127.0.0.1:9355")
    from atlas_plus.summarizer import loader, renderer, cli
    monkeypatch.setattr(loader, "CLAUDE_CODE_ROOT", cc)
    monkeypatch.setattr(renderer, "MEMORY_SESSIONS_DIR", ws / ".memory" / "sessions")
    transcript = cc / "p" / "sess.jsonl"
    monkeypatch.setenv("ATLAS_TRANSCRIPT_FILE", str(transcript))
    return ws, transcript, cli, loader


def _mark_summarised(ws: Path, sid: str, turns: int) -> None:
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    (ws / ".memory" / "sessions" / f"{day}.md").write_text(
        f"## Session {sid}\n<!-- session:{sid} turns:{turns} -->\nsummary\n", encoding="utf-8")


def _run_child(cli, sid: str):
    from typer.testing import CliRunner
    import typer
    app = typer.Typer()
    app.command()(cli.main)
    return CliRunner().invoke(app, ["--source", "claude-code", "--since", "today", "--conversation-id", sid, "--auto-commit"])


def test_nothing_new_is_decided_without_parsing_the_transcript(_hook_env, monkeypatch):
    ws, transcript, cli, loader = _hook_env
    transcript.write_text("".join(_turn(i, "sess") for i in range(400)), encoding="utf-8")
    _mark_summarised(ws, "sess", 400)
    r1 = _run_child(cli, "sess")
    assert "nothing new" in r1.output, r1.output
    with transcript.open("a", encoding="utf-8") as f:   # two more replies
        f.write(_turn(400, "sess") + _turn(401, "sess"))

    def _no_full_parse(*a, **k):
        raise AssertionError("the whole transcript was parsed to find nothing new")
    monkeypatch.setattr(loader, "_load_claude_code", _no_full_parse)
    for lock in (ws / ".atlas" / "summarizer").glob("s-*.lock"):
        lock.unlink()                              # the first run's lock is released at process exit
    r2 = _run_child(cli, "sess")
    assert r2.exception is None or isinstance(r2.exception, SystemExit), r2.exception
    assert "402 now: nothing new" in r2.output, r2.output


def test_session_turn_count_matches_the_full_loader_through_appends_and_rewrites(_hook_env, tmp_path):
    ws, transcript, cli, loader = _hook_env
    cache = tmp_path / "offsets.json"
    lines = [_turn(i, "sess") for i in range(30)] + [_turn(i, "other") for i in range(5)] + ["not json\n", "\n"]
    transcript.write_text("".join(lines), encoding="utf-8")

    def full():
        return len(loader.load_turns(source="claude-code", conversation_id="sess"))

    assert loader.count_session_turns(transcript, "sess", cache) == full() == 30
    with transcript.open("a", encoding="utf-8") as f:
        f.write(_turn(30, "sess") + _turn(31, "sess")[:-1])             # the last line has no newline yet
    assert loader.count_session_turns(transcript, "sess", cache) == full() == 32
    with transcript.open("a", encoding="utf-8") as f:
        f.write("\n" + _turn(32, "sess")[:40])                           # a line still being written
    assert loader.count_session_turns(transcript, "sess", cache) == full() == 32
    transcript.write_text("".join(_turn(i, "sess") for i in range(7)), encoding="utf-8")  # rewritten, shorter
    assert loader.count_session_turns(transcript, "sess", cache) == full() == 7
    transcript.write_text("".join(_turn(i, "sess") for i in range(7)).replace("turn 6", "turn 9"), encoding="utf-8")
    assert loader.count_session_turns(transcript, "sess", cache) == full() == 7   # same size, other bytes


# ---------------------------------------------------------------- the first search's index build
def test_first_search_on_a_large_corpus_says_it_is_building_the_index(tmp_path, monkeypatch, capsys):
    import importlib
    ws = tmp_path / "ws"
    mem = ws / ".memory"
    (mem / "reference").mkdir(parents=True)
    for i in range(260):
        (mem / "reference" / f"n{i}.md").write_text(f"---\nid: n{i}\n---\nnote number {i} about kettles\n", encoding="utf-8")
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(ws))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(ws / ".atlas"))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    monkeypatch.setenv("ATLAS_SEMANTIC_SEARCH", "off")
    from atlas_plus.mcp.memory_server import config, fts, server
    monkeypatch.setattr(config, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "FTS_DB", ws / ".atlas" / "search.sqlite")
    (ws / ".atlas").mkdir()
    capsys.readouterr()
    first = server.memory_search_tool("kettles")
    err = capsys.readouterr().err
    assert first["results"]
    assert "building the search index for 26" in err, err          # said before the wait
    assert first.get("index_rebuilt", {}).get("kind") == "rebuild"  # and the agent is told too
    assert first["index_rebuilt"]["notes"] >= 260
    second = server.memory_search_tool("kettles")
    assert "index_rebuilt" not in second and "building" not in capsys.readouterr().err


def test_a_turn_holding_a_raw_line_separator_is_not_dropped(_hook_env, tmp_path):
    """JSON.stringify leaves U+2028/U+2029 raw; str.splitlines() broke such a turn in two and json
    refused both halves, so the turn was missing from the summary and from the turn count."""
    ws, transcript, cli, loader = _hook_env
    rows = [json.loads(_turn(0, "sess")), json.loads(_turn(1, "sess"))]
    rows[1]["message"]["content"] = "first line\u2028second line\u2029third"
    transcript.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    assert len(loader.load_turns(source="claude-code", conversation_id="sess")) == 2
    assert loader.count_session_turns(transcript, "sess", tmp_path / "o.json") == 2
