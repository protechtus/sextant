"""SEX-046: the index text normalisation keeps its ASCII shortcut, and the shortcut changes nothing.

`fts._norm_index_text` first marks sentence breaks with a private-use sentinel. The sentinel is not
ASCII, so every later step (the folds, the emoji clusters, the affix split, the CJK split) lost its
own `isascii()` shortcut and scanned the whole text. An ASCII note now returns right after the break
step. The oracle below is the full chain, written out here on purpose: the two must stay equal for
every text, or the index would store different rows (guardrail G21 pins those too).
"""
from __future__ import annotations

import time
import unicodedata

import pytest

from atlas_plus.mcp.memory_server import fts


def _oracle(text: str) -> str:
    broken = fts._BREAK_RE.sub(lambda m: " " + fts._SENTINEL + " ", text)
    return fts._cjk_split(fts._affix_split(fts._emoji_split(fts._fold_marks(
        fts._recompose(unicodedata.normalize("NFKC", broken))))))


ASCII_TEXTS = [
    "",
    "one line",
    "A sentence. Another one! A third? Yes; no: maybe, and so on...\nNext line.\n\n\nGap.",
    "version 1.2.3 of file.name.md, e.g. the U.S. case; a.b",
    'He said "hello". She said \'no\'. A "quoted phrase" here.',
    "keys 1 2 3 # * 0# 9* #1 and a trailing dot.",
    "tabs\tand\r\nCRLF lines\r\nand a lone\rreturn.",
    "code `x = y;` and a path C:/dir/file.txt; url http://example.com/a?b=c&d=e.",
    "---\ntitle: T\n---\n# Heading\n- item one.\n- item two!\n",
    "!!!???...,,,;;;:::",
    "\n\n\n",
    "ends with punctuation.",
    ". starts with punctuation",
    "\x00control\x1f characters\x7f here.",
    "a" * 5000 + ". " + "b" * 5000,
]

OTHER_TEXTS = [
    "caf\u00e9 au lait. na\u00efve!",
    "\u6771\u4eac\u90fd\u306e\u4eba\u53e3\u3002\u5927\u962a\u57ce",
    "ascii then \u00e5rhus and K\u00f8benhavn.",
    "keycap 1\ufe0f\u20e3 and #\u20e3 here.",
    "\U0001F44D\U0001F3FD thumbs. \u2764\ufe0f heart",
    "\u05d4\u05e9\u05e8\u05ea \u05e6\u05d4\"\u05dc. \u0627\u0644\u062e\u0627\u062f\u0645",
    "full width \uff21\uff22\uff23\u3001next\u3002",
    "a sentinel \ue000 typed by hand.",
    "Thai \u0e17\u0e4d\u0e32 and Greek \u03ac\u03bb\u03c6\u03b1.",
    "Stra\u00dfe, \u0451\u043b\u043a\u0430, Diyarbak\u0131r.",
]


@pytest.mark.parametrize("text", ASCII_TEXTS)
def test_ascii_text_gives_the_same_index_text_as_the_full_chain(text):
    assert text.isascii()
    assert fts._norm_index_text(text) == _oracle(text)


@pytest.mark.parametrize("text", OTHER_TEXTS)
def test_other_text_gives_the_same_index_text_as_the_full_chain(text):
    assert not text.isascii()
    assert fts._norm_index_text(text) == _oracle(text)


def test_every_ascii_character_alone_and_in_pairs_with_a_break():
    """No single ASCII character, and no ASCII character beside a break or a quote, is changed by
    the steps the shortcut skips."""
    chars = [chr(c) for c in range(128)]
    for ch in chars:
        for shape in (ch, ch + ". x", "x." + ch, '"' + ch + '"', ch + "\n" + ch, "1" + ch, ch + "#"):
            assert fts._norm_index_text(shape) == _oracle(shape), repr(shape)


def test_the_indexed_body_of_an_ascii_note_is_unchanged(monkeypatch):
    """Through the caller the index uses: the body, the frontmatter values and no alias tail."""
    fm = '{"title": "A plain title.", "tags": ["one", "two"], "n": 3}'
    body = "First sentence. Second sentence!\n\nA list:\n- alpha, beta; gamma.\n"
    new = fts._index_body(fm, body)
    monkeypatch.setattr(fts, "_norm_index_text", _oracle)
    assert fts._index_body(fm, body) == new


def test_the_shortcut_is_faster_than_the_full_chain():
    """In process against the oracle, never a wall-clock bound (lesson of AG95)."""
    text = ("The quick brown fox jumps over the lazy dog. Then it rests; the dog does not, and so on!\n"
            * 4000)
    assert text.isascii()

    def best(fn):
        times = []
        for _ in range(3):
            t = time.perf_counter()
            fn(text)
            times.append(time.perf_counter() - t)
        return min(times)

    t_new, t_oracle = best(fts._norm_index_text), best(_oracle)
    assert t_new < 0.75 * t_oracle, (t_new, t_oracle)
