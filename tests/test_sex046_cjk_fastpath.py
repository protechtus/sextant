"""SEX-046: the CJK splitter's fast path returns exactly what the two substitutions returned.

The oracle below is the splitter as it was before the fast path (both substitutions, always). Functional: it runs
the real function on text with and without CJK characters; the timing check compares both in one process."""
from __future__ import annotations

import random
import time

from atlas_plus.mcp.memory_server import fts


def _oracle(text: str) -> str:
    text = fts._CJK_RUN_RE.sub(lambda m: fts._CJK_SEP.join(m.group(0)), text)
    return fts._CJK_ASCII_BOUNDARY_RE.sub(fts._CJK_SEP, text)


SAMPLES = [
    "",
    "plain ascii text with numbers 2025 and punctuation; nothing else.",
    "caf\u00e9 na\u00efve \u00e6ble \u0434\u0435\u043d\u044c \u05e9\u05dc\u05d5\u05dd \u0645\u0631\u062d\u0628\u0627",
    "\u5927\u962a\u57ce\u306e\u6b74\u53f2",
    "2025\u5e74 Python\u7f16\u7a0b iPhone15\u5728",
    "one \u4e2d two",
    "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35 hello \ud55c\uad6d\uc5b4",
    "emoji \U0001f600 and a surrogate-range ideograph \U00020000\U00020001 end",
    "tabs\tand\nnewlines\r\nonly",
    "A" * 5000,
]


def test_fast_path_equals_the_two_substitutions():
    for s in SAMPLES:
        assert fts._cjk_split(s) == _oracle(s), repr(s[:40])


def test_random_mixed_text_equals_the_oracle():
    rng = random.Random(46)
    alphabet = ("abc XYZ 019 .,;\n" + "\u00e9\u00f8\u0434\u05d0\u0627"
                + "\u4e2d\u6587\u3042\u30a2\ud55c\u0e01\u1780" + "\U0001f600\U00020000")
    chars = list(alphabet)
    for _ in range(400):
        s = "".join(rng.choice(chars) for _ in range(rng.randint(0, 60)))
        assert fts._cjk_split(s) == _oracle(s), repr(s)


def test_text_without_cjk_comes_back_as_the_same_object():
    s = "nothing to split here " * 20
    assert fts._cjk_split(s) is s


def test_fast_path_is_not_slower_than_the_oracle_on_plain_text():
    text = ("The deploy queue retried twice before the cache warmed up, 2025-11-25. " * 60)
    for fn in (fts._cjk_split, _oracle):   # warm both
        fn(text)

    def best(fn):
        times = []
        for _ in range(5):
            t = time.perf_counter()
            for _ in range(200):
                fn(text)
            times.append(time.perf_counter() - t)
        return min(times)

    assert best(fts._cjk_split) < 0.75 * best(_oracle)
