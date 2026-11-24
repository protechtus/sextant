"""SEX-046, second part: one report lists, reads and parses each note once.

The curation report's sections each walked the corpus, opened every note and loaded its frontmatter for
themselves. They now share one pass (``curation/_corpus_pass``). Checked by running the report and
counting what reached the filesystem and the YAML loader, and by comparing the report text with the one
the sections give without sharing; nothing here reads source text, sleeps or measures time.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from atlas_plus.curation import _corpus_pass as cp


def _corpus(root: Path, n: int = 12) -> list[Path]:
    """A small corpus that gives every section something to say."""
    made = []
    for scope in ("feedback", "reference", "decisions"):
        (root / scope).mkdir(parents=True, exist_ok=True)
    for i in range(n):
        scope = ("feedback", "reference")[i % 2]
        extra = "contradictions:\n  - reference/n001.md\n" if i == 4 else ""
        confirmed = "last_confirmed: 2020-02-01\n" if i % 3 == 0 else ""
        body = (
            f"---\ntitle: Note {i}\nupdated: 2020-01-0{1 + i % 9}\nconfidence: high\n{confirmed}"
            f"tags: [alpha, topic{i % 4}]\n{extra}---\n\n"
            "The deploy window for the billing service is Tuesday morning before nine.\n"
            f"Number {i} keeps its own sentence about subject {i * 7} so the notes differ somewhat.\n"
            "Always run the database migration with the maintenance flag switched on first.\n"
        )
        p = root / scope / f"n{i:03d}.md"
        p.write_text(body, encoding="utf-8")
        made.append(p)
    d = root / "decisions" / "use-sqlite.md"
    d.write_text("---\ntitle: Use sqlite\nmade: 2020-01-01\nproject: demo\n---\n\nWe keep the index in sqlite.\n",
                 encoding="utf-8")
    made.append(d)
    (root / "feedback" / "plain.md").write_text("No frontmatter here, only a plain line of text to read.\n",
                                                encoding="utf-8")
    made.append(root / "feedback" / "plain.md")
    return made


@pytest.fixture
def counted_reads(monkeypatch):
    """Every Path.read_text call under the corpus, by path."""
    seen: dict[str, int] = {}
    real = Path.read_text

    def read_text(self, *a, **k):
        seen[str(self)] = seen.get(str(self), 0) + 1
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    return seen


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path))


def test_one_report_opens_each_note_once(tmp_path, monkeypatch, counted_reads):
    from atlas_plus.curation import weekly_report as w
    _isolate(monkeypatch, tmp_path)
    root = tmp_path / ".memory"
    notes = _corpus(root)
    text, _ = w.generate_report(memory_root=root, dry_run=True)
    assert "## Stale Files (" in text and "## Memory Topic Coverage (" in text
    per_note = {str(p): counted_reads.get(str(p), 0) for p in notes}
    assert all(c >= 1 for c in per_note.values()), per_note        # every note was read
    # The old report opened a note six times and more (duplicates, facts, stale, decay, convergence, topics).
    # What is left above one is a section this pass does not serve, and must stay rare.
    assert max(per_note.values()) <= 2, per_note
    assert sum(per_note.values()) <= len(notes) + 4, per_note


def test_the_report_text_is_the_same_with_and_without_the_shared_pass(tmp_path, monkeypatch):
    from atlas_plus.curation import weekly_report as w
    _isolate(monkeypatch, tmp_path)
    root = tmp_path / ".memory"
    _corpus(root, n=30)
    shared, _ = w.generate_report(memory_root=root, dry_run=True)
    alone, _ = w._generate_report(root, 6, 0.40, "", True)          # the sections, each on its own
    assert shared == alone
    assert "Repeated facts (" in shared and "| `alpha` | 30 |" in shared


def test_one_report_loads_each_frontmatter_block_once(tmp_path, monkeypatch):
    from atlas_plus.curation import weekly_report as w
    _isolate(monkeypatch, tmp_path)
    root = tmp_path / ".memory"
    notes = _corpus(root)
    loads: list[str] = []
    real = cp._fast_yaml_load
    monkeypatch.setattr(cp, "_fast_yaml_load", lambda block: loads.append(block) or real(block))
    w.generate_report(memory_root=root, dry_run=True)
    assert loads, "the sections no longer load frontmatter through the shared pass"
    assert len(loads) == len(set(loads))                             # no block was loaded twice
    with_fm = len(notes) - 1                                         # plain.md has none
    # the sections cut the block two ways (with and without the line break after `---`): still one load per note
    assert len(loads) <= with_fm, (len(loads), with_fm)


def test_leading_line_breaks_do_not_change_what_a_block_loads_as():
    from atlas_plus._yaml import fast_safe_load
    blocks = [
        "title: x\nupdated: 2020-01-01\ntags: [a, b]",
        "title: 'quoted: colon'\nlist:\n  - one\n  - two\nnested:\n  k: v",
        "text: |\n  kept line\n  second line\nafter: 1",
        "folded: >\n  a\n  b\n",
        "empty:\nnumber: 3\nflag: true",
        "",
    ]
    with cp.shared_pass() as cur:
        for b in blocks:
            for lead in ("", "\n", "\r\n", "\n\n"):
                assert cp.load_frontmatter(lead + b) == fast_safe_load(lead + b), (lead, b)
        assert cur.parses == len(blocks)


def test_the_ascii_path_of_the_note_tokenizer_matches_the_full_path():
    import random
    import re

    from atlas_plus.curation import duplicates as d

    def full_path(text: str) -> set:                                 # the tokenizer as it was, without the ASCII exit
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                text = text[end + 3:]
        text = re.sub(r"<!--.*?-->", " ", text)
        words = d._WORD_RE.findall(text.lower())
        for run in re.findall(d._CJK_RE.pattern + "+", text):
            words.extend(run[i:i + 2] for i in range(len(run) - 1))
        return {w for w in words if (len(w) > 2 or d._CJK_RE.search(w)) and w not in d._STOPS}

    rng = random.Random(4646)
    chars = [chr(c) for c in range(128)]
    words = ["the", "Deploy", "is", "on", "a", "ok", "x1", "re-run", "it's", "A_B", "<!-- source: human -->", "---",
             "pytest", "-q", "\n", "\t", "10.0.0.1", "e.g.", "C:\\Projects\\x", "<!--", "-->", "naive", "to"]
    samples = ["".join(chars), "---\ntitle: T\n---\nBody of the note <!-- src --> stays. Is it OK?\n",
               "--- unterminated front\nwords here", "<!-- only a comment -->", ""]
    samples += [" ".join(rng.choice(words) for _ in range(rng.randint(1, 40))) for _ in range(400)]
    samples += ["".join(rng.choice(chars) for _ in range(rng.randint(1, 120))) for _ in range(300)]
    for s in samples:
        assert s.isascii()
        assert d._tokenize(s) == full_path(s), repr(s)
    mixed = "---\ntitle: x\n---\nCaf\u00e9 \u6bce\u671d\u30b3\u30fc\u30d2\u30fc is ok \U0001F600 done"
    assert "\u6bce\u671d" in d._tokenize(mixed) and "done" in d._tokenize(mixed)   # non-ASCII keeps the full path


def test_ranked_order_is_the_order_the_tuple_key_gave():
    import random
    rng = random.Random(7)
    vocab = [f"w{i}" for i in range(300)] + ["a", "B", "\u00e9t\u00e9", "zz"]
    df = {t: rng.randint(1, 9) for t in vocab}
    rank = {t: r for r, t in enumerate(sorted(df, key=lambda t: (df[t], t)))}
    for _ in range(50):
        note = set(rng.sample(vocab, rng.randint(1, 60)))
        assert sorted(note, key=rank.__getitem__) == sorted(note, key=lambda t: (df[t], t))


def test_the_ascii_path_of_fact_normalising_matches_the_full_chain():
    import random
    import unicodedata as ud

    from atlas_plus.curation import duplicates as d

    def full_chain(s: str) -> str:                                   # what every fact went through before
        s = d._dup_fold(d._fold_marks(d._recompose(ud.normalize("NFKC", s)))).casefold()
        return " ".join(d._NON_WORD_RE.sub(" ", s).split())

    rng = random.Random(46)
    ascii_chars = [chr(c) for c in range(128)]
    samples = ["".join(ascii_chars), "Always RUN `pytest -q` before a push; e.g. A-B_C's 100% (ok)!", "  \t spaced\r\nout  "]
    samples += ["".join(rng.choice(ascii_chars) for _ in range(rng.randint(1, 80))) for _ in range(400)]
    samples += [a + b for a in ascii_chars for b in ("x", " ", "1")]
    for s in samples:
        assert s.isascii()
        assert d._norm_fact(s) == full_chain(s), repr(s)
    assert d._norm_fact("Café ＡＢＣ") == full_chain("Café ＡＢＣ")  # non-ASCII still takes the chain


def test_outside_a_pass_nothing_is_remembered(tmp_path):
    p = tmp_path / "a.md"
    p.write_text("one", encoding="utf-8")
    assert cp.read_text(p) == "one"
    p.write_text("two", encoding="utf-8")
    assert cp.read_text(p) == "two"                                  # a section run alone sees the file as it is
    assert [x.name for x in cp.md_files(tmp_path)] == ["a.md"]
    (tmp_path / "b.md").write_text("x", encoding="utf-8")
    assert sorted(x.name for x in cp.md_files(tmp_path)) == ["a.md", "b.md"]


def test_inside_a_pass_a_note_is_read_once_and_dropped_at_the_end(tmp_path, counted_reads):
    p = tmp_path / "a.md"
    p.write_text("---\ntitle: x\n---\nbody", encoding="utf-8")
    with cp.shared_pass() as outer:
        assert cp.read_text(p) == cp.read_text(p)
        with cp.shared_pass() as inner:                              # a nested block joins the outer one
            assert inner is outer
            cp.read_text(p)
        cp.read_text(p)                                              # still inside the outer block
        assert counted_reads[str(p)] == 1 and outer.reads == 1
        assert cp.md_files(tmp_path) is cp.md_files(tmp_path) and outer.walks == 1
        a = cp.load_frontmatter("title: x\n")
        assert a == {"title": "x"} and cp.load_frontmatter("title: x\n") is a and outer.parses == 1
    assert not outer.texts and not outer.lists and not outer.fms    # nothing outlives the report
    p.write_text("changed", encoding="utf-8")
    assert cp.read_text(p) == "changed"


def test_an_unreadable_note_raises_for_every_section_and_is_tried_once(tmp_path, monkeypatch):
    p = tmp_path / "gone.md"
    tries = []
    real = Path.read_text

    def read_text(self, *a, **k):
        if self == p:
            tries.append(1)
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    with cp.shared_pass():
        for _ in range(3):
            with pytest.raises(OSError):
                cp.read_text(p)
    assert len(tries) == 1


def test_a_corpus_past_the_memory_limit_is_still_read_right(tmp_path, counted_reads):
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text("x" * 40, encoding="utf-8")
    b.write_text("y" * 40, encoding="utf-8")
    with cp.shared_pass(max_chars=50) as cur:
        assert cp.read_text(a) == "x" * 40 and cp.read_text(b) == "y" * 40
        assert cp.read_text(a) == "x" * 40 and cp.read_text(b) == "y" * 40
        assert cur.chars == 40                                       # only the first fits; the second is read again
    assert counted_reads[str(a)] == 1 and counted_reads[str(b)] == 2


def test_a_block_that_does_not_parse_raises_each_time(tmp_path):
    with cp.shared_pass() as cur:
        for _ in range(2):
            with pytest.raises(Exception):
                cp.load_frontmatter("a: [unclosed\n")
        assert "a: [unclosed\n" not in cur.fms
