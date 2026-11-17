"""AG95 (sweep 25, SEX-027): redaction speed back after AG91's Nordic key names.

AG91 added ~65 key-name alternatives and the key=value scan grew 65% (full-width 30,000-repeat case 2.5 s -> 3.0 s,
1 MB of Danish prose +31%). Under (?i) the regex engine enters every key-name alternative at every word start and every
camel-head backtrack point. The alternation is now a first-character trie with exact case classes (the engine skips a
branch on its first character in C), built on first use; the keyword-line pass finds its password words with one
search over a literal trie instead of one `in` per word per line.

The speed tests compare against the SAME pattern skeleton with two key names on the same text (a ratio, not seconds),
so a slow or busy machine does not fail them. The equivalence tests prove the rewrite changes no match: every case
class against the engine's own (?i) over the whole code space, every key name in every engine-equivalent spelling,
and a frozen copy of the plain alternation as an oracle. Every secret-looking value is assembled at runtime.
"""
from __future__ import annotations

import random
import re
import time

import pytest

from atlas_plus import redaction
from atlas_plus.redaction import redact

V = "Fj0" + "rd#K7" + "q2x"

_DANISH = [
    "Husk at skifte din adgangskode hver tredje måned, og brug aldrig den samme to steder.",
    "Jeg har glemt nøglen til sommerhuset igen, så vi må ringe til naboen i morgen tidlig.",
    "API-nøglen udløber om 30 dage, så forny den i tide inden udgivelsen.",
    "Vi mødtes i Århus ved åen og spiste æblekage med fløde på en café ved havnen.",
    "Serveren i Ålborg kører nu Ubuntu 24.04 og PostgreSQL 16 med daglige backups.",
    "Hun læste avisen i toget på vej til København og drak en kop kaffe.",
]
_LOGS = [
    "2026-11-15T08:00:01Z INFO  web.request GET /api/notes?limit=50 200 12ms req=9f3a2b1c",
    "2026-11-15T08:00:02Z DEBUG worker.job started id=4be1 queue=default attempt=1",
    "2026-11-15T08:00:03Z WARN  auth.session token refresh scheduled in 300s for user 42",
    "2026-11-15T08:00:04Z ERROR sync.push failed: connection reset by peer (retry 2/5)",
]


def _text(lines, size, seed):
    rnd = random.Random(seed)
    out, n = [], 0
    while n < size:
        s = rnd.choice(lines)
        out.append(s)
        n += len(s) + 1
    return "\n".join(out)


def _best(fn, arg, runs=3):
    best = float("inf")
    for _ in range(runs):
        t0 = time.perf_counter()
        fn(arg)
        best = min(best, time.perf_counter() - t0)
    return best


def _skeleton(names: str) -> re.Pattern:
    """The key=value pattern's own prefix machinery (lookbehind, sixteen segments, camel head) with other names."""
    return re.compile(r"(?i)(?<![A-Za-z0-9_\-֐-׿؀-ۿ])(-{0,2}(?:[A-Za-z0-9]{1,32}[_-]){0,16}(?:[a-z0-9]{1,32})?"
                      + names + r")(?![A-Za-z0-9À-ɏ])(['\"]?\]?\s*[=:：＝]\s*)(\S{6,})")


@pytest.mark.parametrize("kind", ["danish", "logs", "fullwidth"])
def test_the_key_value_scan_costs_about_what_a_two_name_scan_costs(kind):
    # measured ratio to the two-name skeleton: pre-AG91 4.7-6.9, d790dc1 7.2-10.7 (every one of ~150 alternatives entered per
    # backtrack point), AG95 1.9-2.5
    text = {"danish": lambda: _text(_DANISH, 300_000, 1), "logs": lambda: _text(_LOGS, 300_000, 2),
            "fullwidth": lambda: "ＤＢ＿ＰＡＳＳＷＯＲＤ＝ｘｙｚｗｖｕ１２３ " * 10_000}[kind]()
    kv = redaction._KV_RE
    kv.sub("", "warm up password=xyzzy12")
    ref = _skeleton("(?:password|token)")
    t_kv = _best(lambda t: kv.sub(lambda m: m.group(0), t), text)
    t_ref = _best(lambda t: ref.sub(lambda m: m.group(0), t), text)
    # generous bound: CI interpreters differ (Python 3.12 on Linux measured higher ratios than 3.11 on Windows)
    assert t_kv < 6.0 * t_ref, f"{kind}: key=value scan {t_kv:.3f}s vs two-name skeleton {t_ref:.3f}s"


def test_the_trie_scans_danish_prose_faster_than_the_plain_alternation():
    # The trie against the frozen d790dc1 alternation, same process, same text: interpreter speed cancels out.
    # (The first version compared whole redact() to a skeleton with a fixed 19x bound; CI on Linux/Python 3.12
    # measured 21.8x on 2026-11-17 with the fix in place, so that bound measured the interpreter, not the fix.)
    # Measured locally: trie 1.9-2.5x the skeleton, plain alternation 7.2-10.7x, so the trie is ~4x faster.
    text = _text(_DANISH, 300_000, 3)
    kv = redaction._KV_RE
    oracle = _oracle_kv()
    kv.sub("", "warm up kodeord: " + V)
    oracle.sub("", "warm up kodeord: " + V)
    t_kv = _best(lambda t: kv.sub(lambda m: m.group(0), t), text)
    t_plain = _best(lambda t: oracle.sub(lambda m: m.group(0), t), text)
    assert t_kv < 0.75 * t_plain, f"trie {t_kv:.3f}s vs plain alternation {t_plain:.3f}s"


def test_the_full_width_case_of_sweep16_is_fast_again():
    big = "ＤＢ＿ＰＡＳＳＷＯＲＤ＝ｘｙｚｗｖｕ１２３ " * 30_000
    redact("warm up", env={})
    t0 = time.perf_counter()
    out = redact(big, env={})
    assert time.perf_counter() - t0 < 6.0 and "ｘｙｚｗｖｕ" not in out and out.count("[REDACTED]") == 30_000


# --- equivalence: the rewrite changes no match ----------------------------------------------------------------------

def _oracle_kv() -> re.Pattern:
    """The key=value pattern exactly as d790dc1 compiled it: the plain (?i) alternation in place of the trie."""
    names = redaction._KV_NAMES
    src = redaction._KV_RE.pattern
    fast = src[src.index("(?:[a-z0-9]{1,32})?") + len("(?:[a-z0-9]{1,32})?"):src.index(redaction._KV_SUFFIX)]
    return re.compile(src.replace(fast, names, 1))


def _all_chars() -> str:
    return "".join(chr(c) for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF)


def test_every_key_name_letter_in_every_spelling_the_engine_folds_matches_as_before():
    # the case classes must hold every character (?i) matches: the Kelvin sign, long s, dotless/dotted i, Greek symbol
    # forms (ϰ ϱ ϐ ϑ ϖ ͅ), the Cyrillic U+1C80 letters; a list built from lower()/upper() missed ten of them
    space = _all_chars()
    oracle = _oracle_kv()
    kv = redaction._KV_RE
    letters = sorted({ch for ch in redaction._KV_NAMES if ch.isalpha()})
    checked = 0
    for ch in letters:
        for var in set(re.findall("(?i)" + re.escape(ch), space)) - {ch}:
            for name in {n for n in ("пароль", "токен", "секрет", "kodeord", "nøglen", "token", "secret", "passwd", "client_secret",
                                     "κωδικός πρόσβασης", "συνθηματικό", "şifre", "hasło", "lykilorð", "pääsytunnus", "åtkomsttoken",
                                     "API-Schlüssel", "jelszó", "contraseña", "kata sandi", "mật khẩu", "salasana", "hemmelighed")
                         if ch in n}:
                s = "db_" + name.replace(ch, var) + ": " + V
                got = [(m.span(), m.groups()) for m in kv.finditer(s)]
                want = [(m.span(), m.groups()) for m in oracle.finditer(s)]
                assert got == want, (ascii(s), got, want)
                checked += 1
    assert checked > 100


def test_generated_key_value_lines_match_the_plain_alternation_exactly():
    oracle = _oracle_kv()
    kv = redaction._KV_RE
    import re._parser as sp  # noqa: PLC0415  (a sampler for the name fragments)

    rnd = random.Random(95)

    def gen(tree):
        out = []
        for op, av in tree:
            n = str(op)
            if n == "LITERAL":
                out.append(chr(av))
            elif n == "IN":
                opts = [chr(a) if str(o) == "LITERAL" else chr(rnd.randint(*a)) for o, a in av if str(o) in ("LITERAL", "RANGE")]
                out.append(rnd.choice(opts) if opts else "x")
            elif n == "BRANCH":
                out.append(gen(rnd.choice(av[1])))
            elif n == "SUBPATTERN":
                out.append(gen(av[-1]))
            elif n in ("MAX_REPEAT", "MIN_REPEAT"):
                out.append("".join(gen(av[2]) for _ in range(rnd.randint(av[0], min(av[1], av[0] + 2)))))
        return "".join(out)

    alts = redaction._top_alts(redaction._KV_NAMES[3:-1])
    vals = [V, "sommer" + "hus" + "2024", '"quoted value 42"', "required", "saknas", "${DB_PASS}", "abc", "tokens[0]"]
    for i in range(6000):
        name = gen(sp.parse(rnd.choice(alts)))
        name = name.upper() if i % 4 == 0 else name.title() if i % 4 == 1 else name
        if i % 7 == 0:
            name = name[:-1]
        s = rnd.choice(["", "MY_APP_", "db", "--", "x-"]) + name + rnd.choice(["", "_KEY", "et", "_v2"]) \
            + rnd.choice(["=", ": ", "：", " :\n  ", "]="]) + rnd.choice(vals)
        if i % 5 == 0:
            s = s.translate({c: c + 0xFEE0 for c in range(0x21, 0x7F)})
        assert [(m.span(), m.groups()) for m in kv.finditer(s)] == [(m.span(), m.groups()) for m in oracle.finditer(s)], ascii(s)


def test_the_kubernetes_sibling_rule_masks_the_nordic_names_too():
    out = redact("- name: API_NØGLE\n  value: " + V, env={})
    assert V not in out and "[REDACTED]" in out
    out = redact("- name: db_kodeord\n  value: required", env={})
    assert out.endswith("value: required")


class _AnyProbe:
    """The probe as d790dc1 ran it: one `in` per password word."""

    def search(self, s):
        return True if any(w in s for w in redaction._PW_WORDS_STRIPPED) else None


def test_the_keyword_line_pass_gives_what_the_per_word_probe_gave(monkeypatch):
    rnd = random.Random(5)
    words = redaction._PW_WORDS_STRIPPED
    lines = []
    for _ in range(3000):
        w = rnd.choice(words)
        lines.append("".join(rnd.choice("abc øæåö -") for _ in range(rnd.randint(0, 5))) + w[: rnd.randint(1, len(w))]
                     + rnd.choice(["", "et", "en ", " til routeren"]) + rnd.choice([" er ", ": ", " on ", " is "]) + rnd.choice([V, "Windows11", "sommer" + "hus" + "2024"]))
    text = "\n".join(lines)
    got = redaction._keyword_line_redact(text)
    monkeypatch.setattr(redaction, "_PW_WORDS_RE", _AnyProbe(), raising=False)
    want = redaction._keyword_line_redact(text)
    assert got == want
    assert got.count("[REDACTED]") > 300
    # the lines AG91 added still mask, NFC and NFD
    import unicodedata

    for tmpl in ("Kodeordet er {V}", "Nøglen er {V}", "Lösenordet är {V}", "Salasana on {V}", "Lykilorðið er {V}"):
        for form in ("NFC", "NFD"):
            line = unicodedata.normalize(form, tmpl.replace("{V}", V))
            assert V not in redact(line, env={}), line


def test_import_stays_cheap_and_the_first_call_builds_the_patterns_once():
    import importlib.util
    import sys

    sys.modules.pop("atlas_plus.redaction_ag95_probe", None)
    spec = importlib.util.spec_from_file_location("atlas_plus.redaction_ag95_probe", redaction.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.redact("kodeord: " + V, env={}) == "kodeord: [REDACTED]"
    first = mod._KV_RE
    assert mod._KV_RE is first  # built once
