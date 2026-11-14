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


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokens, stripping punctuation and YAML frontmatter."""
    # Strip frontmatter
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3:]
    words = re.findall(r"[a-z0-9]+", text.lower())
    # CJK body text has no spaces: without bigrams every Japanese note looked identical (MN23)
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
    return {w for w in words if (len(w) > 2 or _CJK_RE.search(w)) and w not in stops}  # CJK bigrams are two characters (MN23)


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
_NON_WORD_RE = re.compile(r"[^0-9a-z\u00c0-\uffff]+")


_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")


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
    return " ".join(_NON_WORD_RE.sub(" ", s.lower()).split())


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
    index: dict[str, list[int]] = {}
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
                idx = len(entries)
                entries.append((rel, fact, toks))
                for t in toks:
                    index.setdefault(t, []).append(idx)
    out: list[dict] = []
    seen_pairs: set[tuple[int, int]] = set()
    for norm, hits in exact.items():
        if len(hits) > 1:
            for (r1, f1), (r2, _f2) in zip(hits, hits[1:]):
                out.append({"kind": "exact", "file1": r1, "file2": r2, "similarity": 1.0, "fact": f1[:160]})
    # Near repeats: candidates share at least 3 index tokens; exact pairs are already reported.
    counts: dict[int, int]
    for i, (rel, fact, toks) in enumerate(entries):
        counts = {}
        for t in toks:
            for j in index.get(t, ()):
                if j > i:
                    counts[j] = counts.get(j, 0) + 1
        for j, shared in counts.items():
            if shared < 3 or (i, j) in seen_pairs:
                continue
            other = entries[j]
            if _norm_fact(fact) == _norm_fact(other[1]):
                continue
            sim = shared / len(toks | other[2])
            if sim >= near_threshold:
                seen_pairs.add((i, j))
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
    found: list[tuple[float, int, int]] = []
    # Postings are appended in ascending i, so they are sorted: bisect to the entries with j > i
    # instead of walking the whole list (AG48: 80 million set.add on 5,000 notes, 135 s). Once more
    # than the cap has been found, the working threshold rises to the cap's weakest pair: the
    # output is the best _MAX_FILE_PAIRS pairs either way, so the result stays exact.
    from bisect import bisect_right
    thr = threshold
    for i in range(len(file_list)):
        si = sizes[i]
        if si == 0:
            continue
        toks1 = tokens[file_list[i]]
        need = int(-(-(thr * si) // 1))                  # ceil(t * |A|)
        prefix = sorted(toks1, key=lambda t: (df[t], t))[:max(1, si - need + 1)]
        cand: set[int] = set()
        for tok in prefix:
            post = index[tok]
            cand.update(post[bisect_right(post, i):])
        for j in cand:
            sj = sizes[j]
            if min(si, sj) < thr * max(si, sj):
                continue                              # size band: Jaccard <= min/max
            common = len(toks1 & tokens[file_list[j]])
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
