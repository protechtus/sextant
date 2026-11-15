"""P7-T1: Duplicate detection across .memory/ using word-overlap Jaccard similarity."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from atlas_plus.mcp.memory_server.config import EXCLUDED_DIRS, MEMORY_ROOT
from atlas_plus.curation.scan import is_excluded_mirror

DEFAULT_THRESHOLD = 0.40

# Duplicate detection targets the curatable FACT scopes (feedback, identity,
# projects, reference, decisions). Tickets, sessions, personas, visitors and
# archives are structured or historical records that legitimately share
# boilerplate -- including them swamps the report with false positives (e.g.
# ~1800 near-identical archived tickets = ~1.5M bogus pairs). This mirrors the
# non-curatable set already used by dream_mode.
_NON_CURATABLE_DIRS = EXCLUDED_DIRS | {
    "_atlas", "_archive", "sessions", "tickets", "personas", "visitors", "gaps",
}


_MAX_FILE_PAIRS = 500  # the report shows a handful; snippets for more than this are wasted reads (AG32)
_LAST_CAPPED = False   # True when the last file-level scan found more pairs than the cap (AG42: the count read as a total)
_LAST_PARTIAL = [0, 0]  # [notes scanned, notes total] when the last scan stopped at its time budget (AG75)
# AG84: on a shared vocabulary every rare-first prefix token still sits in thousands of notes, so the exact
# prefix walk is quadratic (2,500 / 5,000 / 10,000 / 20,000 notes: 2 / 7 / 26 / ~100 s). The walk's size is
# known before it starts (the posting entries its prefixes touch); above this many the candidates come from
# MinHash banding instead. Every similarity shown is still the exact Jaccard; only recall is probabilistic.
_EXACT_MAX_WORK = 8_000_000  # about 3 s of exact walk (0.37 us per posting entry, measured)
_LSH_RECALL = 0.99      # chance that a pair AT the threshold becomes a candidate (higher pairs: more)
_LAST_APPROX = [False]  # True when the last file-level scan used the MinHash candidates


def _exact_max_work() -> int:
    """-1 = always exact (ATLAS_DUPLICATES_EXACT=on); else the posting-entry bound, which
    ATLAS_DUPLICATES_EXACT_WORK overrides (0 = always the banded candidates)."""
    import os as _os
    if _os.environ.get("ATLAS_DUPLICATES_EXACT", "").strip().lower() in ("1", "on", "true", "yes"):
        return -1
    try:
        return max(0, int(_os.environ.get("ATLAS_DUPLICATES_EXACT_WORK") or _EXACT_MAX_WORK))
    except ValueError:
        return _EXACT_MAX_WORK


def _lsh_shape(threshold: float) -> tuple[int, int]:
    """(rows per band, bands) so that a pair with Jaccard == threshold collides in some band with
    probability >= _LSH_RECALL: P = 1 - (1 - J**r) ** b."""
    import math
    t = min(max(threshold, 0.05), 0.99)
    r = 2 if t < 0.3 else 3 if t < 0.55 else 4 if t < 0.75 else 6
    b = math.ceil(math.log(1 - _LSH_RECALL) / math.log(1 - t ** r))
    return r, max(1, min(b, 400))


def _lsh_pairs(shared_toks: list, threshold: float, deadline: Optional[float] = None):
    """Candidate pairs (i, j), i < j, from MinHash bands over each note's SHARED tokens (a token in one
    note only can never be in an intersection; leaving it out raises every estimate, so it costs no
    recall). One shake-128 digest per distinct token gives all r*b hash values; a note's signature is
    the column-wise minimum, computed in C (map/min/zip). Past ``deadline`` (time.monotonic) it yields
    None once and stops, so a scan whose candidates are mostly repeats still honours its budget."""
    import hashlib
    import time as _time
    from operator import eq
    from array import array
    r, b = _lsh_shape(threshold)
    k = r * b
    table: dict[str, array] = {}
    # One bucket dict per band and each note's band keys: candidates are yielded lazily, band by band, and a
    # pair already yielded by an earlier band is skipped by comparing the two notes' earlier keys. The old
    # code put every pair of every bucket into one set and sorted it before yielding the first: 3,000
    # template notes (one bucket per band) took 52 s past a 5 s budget and returned NO pairs (AG85).
    buckets: list[dict[int, list[int]]] = [{} for _ in range(b)]
    keys: list = [None] * len(shared_toks)
    bands = range(b)
    cut = False
    for i, toks in enumerate(shared_toks):
        if deadline is not None and not i & 255 and i and _time.monotonic() > deadline:
            cut = True  # the notes signed so far still give candidates; the scan reports itself partial (MN79)
            break
        if not toks:
            continue
        rows = []
        for t in toks:
            h = table.get(t)
            if h is None:
                h = table[t] = array("I", hashlib.shake_128(t.encode("utf-8", "surrogatepass")).digest(4 * k))
            rows.append(h)
        it = iter(map(min, zip(*rows)))
        # one key per band, built in C: (band, r minima); a hash collision only adds a candidate, never loses one
        ki = keys[i] = array("q", map(hash, zip(bands, *([it] * r))))
        for band, key in enumerate(ki):
            buckets[band].setdefault(key, []).append(i)
    from itertools import combinations
    done_groups: set[tuple] = set()  # a bucket holding exactly the notes of an earlier band's bucket adds nothing
    tick = 0
    for band in bands:
        for members in buckets[band].values():
            if len(members) < 2:
                continue
            group = tuple(members)  # the tuple itself: a hash collision here would lose pairs
            if group in done_groups:
                continue
            done_groups.add(group)
            for a, c in combinations(members, 2):  # members ascend, so a < c
                tick += 1
                if deadline is not None and not tick & 4095 and _time.monotonic() > deadline:
                    yield None
                    return
                if band and any(map(eq, keys[a][:band], keys[c][:band])):
                    continue  # an earlier band already yielded this pair
                yield a, c
        buckets[band] = {}
    if cut:
        yield None


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokens, stripping punctuation and YAML frontmatter."""
    # Strip frontmatter
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3:]
    text = re.sub(r"<!--.*?-->", " ", text)  # provenance comments are not content: every non-Latin note tokenised to `source human direct` and scored 1.00 against every other (AG52)
    emoji: set[str] = set()
    if not text.isascii():
        import unicodedata as _ud
        text = _dup_fold(_fold_marks(_recompose(_ud.normalize("NFC", text))))  # AG89: prefixes and emoji as search folds them; one form for a letter with its marks (MN46); Arabic and Hebrew points folded like search does (MN49); joiners and sara am like search (MN55); ASCII skips it (AG60)
        emoji = set(_EMOJI_RE.findall(text))   # an emoji is a word to search (AG83) and was invisible here (AG89)
    words = _WORD_RE.findall(text.lower())  # letters of every script with their combining marks attached (AG60: deleting the marks merged Hindi words)
    # CJK, Thai, Lao, Khmer and Burmese body text has no spaces: without bigrams every Japanese note looked identical (MN23)
    for run in re.findall(_CJK_RE.pattern + "+", text):
        words.extend(run[i:i + 2] for i in range(len(run) - 1))
    # Drop very common stop words and single chars
    stops = {
        "the", "a", "an", "and", "or", "of", "to", "in", "is", "it",
        "that", "this", "for", "be", "with", "on", "at", "by", "from",
        "not", "are", "was", "were", "as", "if", "but", "all", "any",
        "has", "have", "had", "do", "does", "did", "will", "would",
        "can", "could", "should", "may", "might", "must", "shall",
    }
    return {w for w in words if (len(w) > 2 or _CJK_RE.search(w)) and w not in stops} | emoji  # CJK bigrams are two characters (MN23)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _snippet(path: Path, max_chars: int = 120) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        # strip frontmatter
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                text = text[end + 3:]
        return text.strip()[:max_chars].replace("\n", " ")
    except OSError:
        return ""


@dataclass
class DuplicatePair:
    """A pair of memory files with overlapping content above the similarity threshold."""

    file1: str
    file2: str
    similarity: float
    snippet1: str
    snippet2: str

    def as_dict(self) -> dict:
        """Return a JSON-serialisable dict including rounded similarity and content snippets."""
        return {
            "file1": self.file1,
            "file2": self.file2,
            "similarity": round(self.similarity, 3),
            "snippet1": self.snippet1,
            "snippet2": self.snippet2,
        }


_FACT_SKIP_PREFIXES = ("#", "---", "<!--", "```", "|", ">")
_PROVENANCE_RE = re.compile(r"\s*<!--[^>]*-->")
_NON_WORD_RE = re.compile(r"[^0-9a-z\u00c0-\uffff\U0001F000-\U0001FAFF]+")  # astral emoji are words too (AG89)


# the same run classes search uses: CJK, Hangul and the space-less South-East Asian scripts (MN46)
from atlas_plus.mcp.memory_server.fts import _CJK_RE, _EMOJI_RE, _WORD_RE, _dup_fold, _fold_marks, _recompose  # noqa: E402

_BITSET_MAX_TOKENS = 131072  # shared tokens; with bit indexes and a bytearray build the bitsets stay cheap to here (MN49: 10 s vs 16 s at 100,000); above it the set intersection bounds memory


def _weight(s: str) -> int:
    """Length for the minimum-size gates, counting an ideograph or kana as three Latin letters
    (``毎朝コーヒーを黒で飲む`` is a whole fact in 11 characters: FN5, final readiness pass)."""
    return len(s) + 2 * len(_CJK_RE.findall(s))


def _tokens(norm: str) -> frozenset[str]:
    """Near-duplicate tokens: Latin words of four letters or more, plus every CJK bigram (an
    unspaced sentence has one whitespace token and could never be a near repeat)."""
    out: set[str] = set()
    for word in norm.split():
        if _CJK_RE.search(word):
            cjk = "".join(_CJK_RE.findall(word))
            out.update(cjk[i:i + 2] for i in range(len(cjk) - 1))
            latin = _CJK_RE.sub(" ", word).split()
            out.update(t for t in latin if len(t) >= 4)
        elif len(word) >= 4:
            out.add(word)
    return frozenset(out)


def _note_facts(text: str) -> list[str]:
    """The fact lines of a note: body lines that are not headings, rules, comments or tables."""
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            body = text[end + 4:]
    out = []
    for line in body.splitlines():
        s = _PROVENANCE_RE.sub("", line).strip()
        if _weight(s) < 25 or s.startswith(_FACT_SKIP_PREFIXES):
            continue
        out.append(s)
    return out


def _norm_fact(s: str) -> str:
    import unicodedata as _ud
    s = _dup_fold(_fold_marks(_recompose(_ud.normalize("NFKC", s)))).casefold()  # prefixes and emoji too (AG89); like search: NFC vs NFD, harakat, half-width, tonos, İ (MN62: the file scanner was fixed, this sibling was not)
    return " ".join(_NON_WORD_RE.sub(" ", s).split())


def find_fact_duplicates(
    memory_root: Optional[Path] = None,
    scope: str = "",
    near_threshold: float = 0.7,
    limit: int = 500,
) -> list[dict]:
    """Repeated FACTS across (and inside) notes: exact repeats after normalisation, and near
    repeats by token-set Jaccard. The file-level scan above dilutes a repeated fact inside a
    seven-fact note; a curator had no command that listed duplicate facts (AG20, sweep 12)."""
    root = memory_root or MEMORY_ROOT
    search_root = root / scope if scope else root
    files = [
        p for p in search_root.rglob("*.md")
        if not any(part in _NON_CURATABLE_DIRS for part in p.relative_to(root).parts)
        and not is_excluded_mirror(p.relative_to(root).parts)
        and not p.name.startswith("_")
    ]
    exact: dict[str, list[tuple[str, str]]] = {}
    entries: list[tuple[str, str, frozenset]] = []
    norms: list[str] = []
    df: dict[str, int] = {}
    for f in sorted(files):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(f.relative_to(root)).replace("\\", "/")
        for fact in _note_facts(text):
            norm = _norm_fact(fact)
            if _weight(norm) < 20:
                continue
            exact.setdefault(norm, []).append((rel, fact))
            toks = _tokens(norm)
            if len(toks) >= 4:
                entries.append((rel, fact, toks))
                norms.append(norm)
                for t in toks:
                    df[t] = df.get(t, 0) + 1
    out: list[dict] = []
    seen_pairs: set[tuple[int, int]] = set()
    for norm, hits in exact.items():
        if len(hits) > 1:
            for (r1, f1), (r2, _f2) in zip(hits, hits[1:]):
                out.append({"kind": "exact", "file1": r1, "file2": r2, "similarity": 1.0, "fact": f1[:160]})
    # Near repeats: a pair qualifies when it shares at least 3 tokens AND its Jaccard reaches the
    # threshold. Exact prefix filter (AG90; the old walk counted every posting of every common
    # word, quadratic: 50 s on 2,500 facts): order each fact's tokens rarest-first; a pair
    # sharing >= o tokens has its rarest shared token inside both facts' first |x| - o + 1 tokens,
    # with o = max(3, ceil(t * |x|)) a lower bound on the overlap any qualifying pair needs. Only
    # those prefix tokens are indexed, every candidate is verified on the full sets, so the result
    # is the same pair set as the full walk.
    import bisect as _bisect
    import math as _math
    import time as _time
    _t = float(near_threshold)
    prefixes: list[tuple[str, ...]] = []
    index: dict[str, list[int]] = {}
    for idx, (_rel, _fact, toks) in enumerate(entries):
        order = sorted(toks, key=lambda t: (df[t], t))
        need = max(3, _math.ceil((_t - 1e-9) * len(order)))
        pre = tuple(order[:max(0, len(order) - need + 1)])
        prefixes.append(pre)
        for t in pre:
            index.setdefault(t, []).append(idx)  # ascending: built in entry order
    try:
        _budget = float(__import__("os").environ.get("ATLAS_DUPLICATES_BUDGET_SECONDS") or 60)
    except ValueError:
        _budget = 60.0
    _deadline = _time.monotonic() + _budget if _budget > 0 else None
    _LAST_PARTIAL[0] = _LAST_PARTIAL[1] = 0
    for i, (rel, fact, toks) in enumerate(entries):
        if _deadline is not None and i % 64 == 0 and _time.monotonic() > _deadline:
            _LAST_PARTIAL[0], _LAST_PARTIAL[1] = i, len(entries)  # the facts scan had no budget: the weekly report never finished (MN67)
            break
        cands: set[int] = set()
        for t in prefixes[i]:
            post = index[t]
            cands.update(post[_bisect.bisect_right(post, i):])
        n_i = len(toks)
        for j in sorted(cands):
            other = entries[j]
            n_j = len(other[2])
            if min(n_i, n_j) / max(n_i, n_j) < _t:
                continue  # Jaccard <= min/max: cannot reach the threshold
            shared = len(toks & other[2])
            if shared < 3 or norms[i] == norms[j]:
                continue
            sim = shared / len(toks | other[2])
            if sim >= near_threshold:
                out.append({"kind": "near", "file1": rel, "file2": other[0], "similarity": round(sim, 2),
                            "fact": fact[:160], "other": other[1][:160]})
        if len(out) >= limit:
            break
    out.sort(key=lambda d: (d["kind"] != "exact", -d["similarity"]))
    return out[:limit]


_OVERFLOWED = [False]  # set when the working threshold rose past the caller's (more than the cap was found)


def find_duplicates(
    memory_root: Optional[Path] = None,
    threshold: float = DEFAULT_THRESHOLD,
    scope: str = "",
) -> list[DuplicatePair]:
    """Return pairs of .memory/ files whose content is suspiciously similar.

    Args:
        memory_root: Root of the .memory/ corpus. Defaults to MEMORY_ROOT.
        threshold: Jaccard similarity floor (0-1). Default 0.40.
        scope: Optional subdirectory to restrict search (e.g. 'feedback').

    Returns list of DuplicatePair sorted by similarity descending.
    """
    root = memory_root or MEMORY_ROOT
    search_root = root / scope if scope else root

    files = [
        p for p in search_root.rglob("*.md")
        if not any(part in _NON_CURATABLE_DIRS for part in p.relative_to(root).parts)
        and not is_excluded_mirror(p.relative_to(root).parts)
        and not p.name.startswith("_")
    ]

    tokens: dict[Path, set[str]] = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            tokens[f] = _tokenize(text)
        except OSError:
            tokens[f] = set()

    pairs: list[DuplicatePair] = []
    # Size-band prune: Jaccard(A,B) <= min(|A|,|B|) / max(|A|,|B|), so two token
    # sets whose sizes differ by more than `threshold` can never reach it. Sort
    # ascending by size and break the inner scan as soon as the ratio drops below
    # the threshold -- every later file is only larger. This keeps results EXACT
    # (no pair that could qualify is skipped) while avoiding the full O(n^2) walk.
    file_list = sorted(tokens.keys(), key=lambda f: len(tokens[f]))
    sizes = [len(tokens[f]) for f in file_list]
    # Candidate pairs come from an inverted index (token -> notes): a pair can only reach the
    # threshold when it shares at least threshold * max(|A|,|B|) tokens, so pairs that share
    # fewer are never compared. The all-pairs walk made `review report` unusable past about
    # 800 notes (AG32, sweep 14: killed at 621 s and 1.4 GB on 5,000 notes). Snippets are read
    # for the pairs kept, not for every candidate.
    index: dict[str, list[int]] = {}
    for i, f in enumerate(file_list):
        for tok in tokens[f]:
            index.setdefault(tok, []).append(i)
    # Prefix filtering (exact): a pair with Jaccard >= t shares at least ceil(t*|A|) of A's tokens,
    # so at least one shared token lies among A's |A| - ceil(t*|A|) + 1 RAREST tokens (one global
    # order: document frequency, then the token). Only those short posting lists are walked; the
    # walk over every posting list ran 1.3 billion times on 5,000 notes (AG42, sweep 15). Each
    # candidate then gets the exact set intersection.
    df = {tok: len(ix) for tok, ix in index.items()}
    # Every note's token set as one Python int (one bit per token that appears in at least two notes; a
    # token in one note can never be shared). `(a & b).bit_count()` is the intersection size in C: the
    # per-pair set intersection cost 10 us and 125 of 132 seconds on 5,000 notes (AG54, round six).
    shared_bit: dict[str, int] = {}
    for tok, n_docs in df.items():
        if n_docs >= 2:
            shared_bit[tok] = len(shared_bit)  # the bit index: storing `1 << k` per token was quadratic in memory (799 MB on 98,000 tokens: MN43)
    # Bitsets pay off while the shared vocabulary is small: each AND and popcount costs V/64 words, and on a
    # 100,000-word vocabulary the scan took 77 s and 2.3 GB against 60 s and 411 MB for set intersection
    # (MN48, round seven). Past the bound the exact set intersection is used instead. Each note's int is
    # assembled in a bytearray and converted once: `b |= 1 << k` per token copied a V-bit integer every time.
    use_bits = len(shared_bit) <= _BITSET_MAX_TOKENS
    bits: list[int] = []
    if use_bits:
        nbytes = (len(shared_bit) + 7) // 8 or 1
        for f in file_list:
            ba = bytearray(nbytes)
            for tok in tokens[f]:
                bit = shared_bit.get(tok)
                if bit is not None:
                    ba[bit >> 3] |= 1 << (bit & 7)
            bits.append(int.from_bytes(ba, "little"))
    found: list[tuple[float, int, int]] = []
    # Postings are appended in ascending i, so they are sorted: bisect to the entries with j > i
    # instead of walking the whole list (AG48: 80 million set.add on 5,000 notes, 135 s). Once more
    # than the cap has been found, the working threshold rises to the cap's weakest pair: the
    # output is the best _MAX_FILE_PAIRS pairs either way, so the result stays exact.
    from bisect import bisect_right
    import time as _time
    try:
        _budget = float(__import__("os").environ.get("ATLAS_DUPLICATES_BUDGET_SECONDS") or 60)
    except ValueError:
        _budget = 60.0
    _deadline = _time.monotonic() + _budget if _budget > 0 else None
    _LAST_PARTIAL[0] = _LAST_PARTIAL[1] = 0
    _LAST_APPROX[0] = False
    thr = threshold
    # The exact walk's size, before it runs: the posting entries its prefixes would touch (AG84).
    ordered = [sorted(tokens[f], key=lambda t: (df[t], t)) for f in file_list]
    _limit = _exact_max_work()
    _r, _b = _lsh_shape(threshold)
    if 1 - (1 - min(max(threshold, 0.05), 0.99) ** _r) ** _b < _LSH_RECALL:
        _limit = -1  # 400 bands cannot promise the 99% recall below a threshold of about 0.11 (-t 0.05: 63%): stay exact (AG85)
    if _limit >= 0:
        work = 0
        for i, si in enumerate(sizes):
            if si:
                need = int(-(-(threshold * si) // 1))
                for tok in ordered[i][:max(1, si - need + 1)]:
                    work += df[tok]
            if work > 2 * _limit:
                break
        if work // 2 > _limit:  # each posting entry is walked from one side of the pair only
            _LAST_APPROX[0] = True
    if _LAST_APPROX[0]:
        shared = [[t for t in o if df[t] >= 2] for o in ordered]
        reached = bytearray(len(file_list))  # notes that met at least one candidate, for the partial count
        for pair in _lsh_pairs(shared, threshold, _deadline):
            if pair is None:  # the budget ran out inside the candidate walk
                _LAST_PARTIAL[0], _LAST_PARTIAL[1] = sum(reached), len(file_list)
                break
            i, j = pair
            reached[i] = reached[j] = 1
            si, sj = sizes[i], sizes[j]
            if min(si, sj) < thr * max(si, sj):
                continue
            common = (bits[i] & bits[j]).bit_count() if use_bits else len(tokens[file_list[i]] & tokens[file_list[j]])
            sim = common / (si + sj - common)
            if sim >= thr:
                found.append((sim, i, j))
                if len(found) >= 2 * _MAX_FILE_PAIRS:
                    found.sort(key=lambda t: t[0], reverse=True)
                    del found[_MAX_FILE_PAIRS:]
                    thr = max(thr, found[-1][0])
                    _OVERFLOWED[0] = True
    for i in range(len(file_list) if not _LAST_APPROX[0] else 0):
        if _deadline is not None and i % 64 == 0 and _time.monotonic() > _deadline:
            _LAST_PARTIAL[0], _LAST_PARTIAL[1] = i, len(file_list)  # the strongest pairs so far; 20,000 notes took 250 s and 3 GB (AG75)
            break
        si = sizes[i]
        if si == 0:
            continue
        toks1 = tokens[file_list[i]]
        bi = bits[i] if use_bits else 0
        need = int(-(-(thr * si) // 1))                  # ceil(t * |A|)
        prefix = ordered[i][:max(1, si - need + 1)]
        cand: set[int] = set()
        for tok in prefix:
            post = index[tok]
            cand.update(post[bisect_right(post, i):])
        for j in cand:
            sj = sizes[j]
            if min(si, sj) < thr * max(si, sj):
                continue                              # size band: Jaccard <= min/max
            common = (bi & bits[j]).bit_count() if use_bits else len(toks1 & tokens[file_list[j]])
            sim = common / (si + sj - common)
            if sim >= thr:
                found.append((sim, i, j))
        if len(found) >= 2 * _MAX_FILE_PAIRS:
            found.sort(key=lambda t: t[0], reverse=True)
            del found[_MAX_FILE_PAIRS:]
            thr = max(thr, found[-1][0])
            _OVERFLOWED[0] = True
    found.sort(key=lambda t: t[0], reverse=True)
    global _LAST_CAPPED
    _LAST_CAPPED = len(found) > _MAX_FILE_PAIRS or _OVERFLOWED[0]
    _OVERFLOWED[0] = False
    for sim, i, j in found[:_MAX_FILE_PAIRS]:
        f1, f2 = file_list[i], file_list[j]
        pairs.append(DuplicatePair(
            file1=str(f1.relative_to(root)).replace("\\", "/"),
            file2=str(f2.relative_to(root)).replace("\\", "/"),
            similarity=sim,
            snippet1=_snippet(f1),
            snippet2=_snippet(f2),
        ))
    return pairs
