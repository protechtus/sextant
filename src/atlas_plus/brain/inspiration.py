"""Inspiration Engine: harness hallucination as verified-gated inspiration.

The same machinery that makes a model hallucinate makes it creative. This module
turns that into an asset: generate divergent candidates, score how self-consistent
they are (a usable uncertainty signal), and capture each as an UNVERIFIED idea that
can only become a fact by passing a verifier later.

Design (see docs/INSPIRATION-ENGINE.md and research/hallucinations.md):
  - score_consistency(answers): cluster N sampled answers by meaning, compute the
    dominant-cluster fraction and a normalized semantic entropy, and return a verdict
    (grounded / uncertain / speculative) plus a confidence.
  - capture_ideas(...): one Idea per cluster, tagged status="unverified" with lane,
    confidence, support, and provenance.
  - inspire(generate, prompt, n): end-to-end. `generate` is injected -- the real
    system passes Brain's Ollama call; tests pass a fake. This module never calls an
    LLM itself, which keeps it deterministic and unit-testable.

Clustering note: the MVP equivalence is lexical (normalized text plus token-set
Jaccard) and is pluggable via an `equiv` callable. Embedding- or NLI-based semantic
equivalence is the planned upgrade.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# A pluggable equivalence predicate: do two answers mean the same thing?
EquivFn = Callable[[str, str], bool]

GROUNDED_THRESHOLD = 0.8     # dominant-cluster fraction at/above this => grounded
SPECULATIVE_THRESHOLD = 0.5  # dominant-cluster fraction at/below this => speculative
JACCARD_THRESHOLD = 0.6      # token-set overlap to treat two answers as equivalent


def normalize_answer(s: str) -> str:
    """Lowercase, strip surrounding punctuation, and collapse whitespace for matching."""
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip(" \t\n\r.,;:!?\"'()[]{}")


def _tokens(s: str) -> set[str]:
    return set(t for t in re.split(r"[^a-z0-9]+", normalize_answer(s)) if t)


def jaccard(a: set[str], b: set[str]) -> float:
    """Token-set overlap in [0, 1]. Two empty sets are treated as identical (1.0)."""
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def default_equiv(a: str, b: str, threshold: float = JACCARD_THRESHOLD) -> bool:
    """MVP semantic-equivalence proxy: normalized-equal, or token-set Jaccard >= threshold."""
    if normalize_answer(a) == normalize_answer(b):
        return True
    return jaccard(_tokens(a), _tokens(b)) >= threshold


def cluster_answers(answers: list[str], equiv: Optional[EquivFn] = None) -> list[list[int]]:
    """Greedy single-linkage clustering of answers by equivalence to a cluster representative.

    Returns a list of clusters, each a list of indices into `answers`. The first member
    of a cluster is its representative.
    """
    eq = equiv or default_equiv
    clusters: list[list[int]] = []
    for i, ans in enumerate(answers):
        placed = False
        for cluster in clusters:
            if eq(answers[cluster[0]], ans):
                cluster.append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])
    return clusters


def semantic_entropy(cluster_sizes: list[int], n: int) -> float:
    """Normalized Shannon entropy over cluster mass, in [0, 1].

    0.0 when all samples agree (one cluster); 1.0 when all samples are distinct.
    """
    if n <= 1 or not cluster_sizes:
        return 0.0
    h = 0.0
    for size in cluster_sizes:
        p = size / n
        if p > 0:
            h -= p * math.log(p)
    max_h = math.log(n)
    if max_h <= 0:
        return 0.0
    return min(1.0, h / max_h)


@dataclass
class ConsistencyResult:
    """Outcome of scoring a set of sampled answers for self-consistency."""

    n: int
    clusters: list[list[int]]
    cluster_sizes: list[int]
    top_fraction: float          # dominant-cluster size / n  (self-consistency)
    entropy: float               # normalized semantic entropy in [0, 1]
    verdict: str                 # "grounded" | "uncertain" | "speculative"
    confidence: float            # 0..1 (currently == top_fraction)
    grounded_threshold: float = GROUNDED_THRESHOLD
    speculative_threshold: float = SPECULATIVE_THRESHOLD


def score_consistency(
    answers: list[str],
    *,
    equiv: Optional[EquivFn] = None,
    grounded_threshold: float = GROUNDED_THRESHOLD,
    speculative_threshold: float = SPECULATIVE_THRESHOLD,
) -> ConsistencyResult:
    """Cluster answers and derive a self-consistency verdict + confidence.

    grounded when the dominant cluster covers >= grounded_threshold of samples;
    speculative when it covers <= speculative_threshold; uncertain in between.
    """
    n = len(answers)
    if n == 0:
        return ConsistencyResult(0, [], [], 0.0, 0.0, "speculative", 0.0,
                                 grounded_threshold, speculative_threshold)
    clusters = cluster_answers(answers, equiv=equiv)
    sizes = sorted((len(c) for c in clusters), reverse=True)
    top_fraction = sizes[0] / n
    entropy = semantic_entropy(sizes, n)
    if top_fraction >= grounded_threshold:
        verdict = "grounded"
    elif top_fraction <= speculative_threshold:
        verdict = "speculative"
    else:
        verdict = "uncertain"
    return ConsistencyResult(
        n=n,
        clusters=clusters,
        cluster_sizes=sizes,
        top_fraction=round(top_fraction, 4),
        entropy=round(entropy, 4),
        verdict=verdict,
        confidence=round(top_fraction, 4),
        grounded_threshold=grounded_threshold,
        speculative_threshold=speculative_threshold,
    )


@dataclass
class Idea:
    """A captured candidate. Always starts unverified; only a verifier can promote it."""

    text: str
    status: str = "unverified"   # unverified | confirmed | rejected
    lane: str = "speculative"    # speculative | grounded
    confidence: float = 0.0      # support fraction in [0, 1]
    support: int = 1             # number of samples backing this idea
    source: str = ""             # provenance (model / prompt / run id)


def capture_ideas(
    result: ConsistencyResult,
    answers: list[str],
    *,
    source: str = "",
) -> list[Idea]:
    """Turn each answer cluster into an unverified Idea, most-supported first.

    The representative of each cluster (its first member) is the idea text. An idea
    whose support fraction reaches the grounded threshold is tagged lane="grounded";
    everything else is lane="speculative". Status is always "unverified" at capture --
    promotion to a fact happens only through a verifier.
    """
    ideas: list[Idea] = []
    n = result.n or 1
    for cluster in result.clusters:
        support = len(cluster)
        fraction = support / n
        rep = answers[cluster[0]].strip()
        lane = "grounded" if fraction >= result.grounded_threshold else "speculative"
        ideas.append(Idea(
            text=rep,
            status="unverified",
            lane=lane,
            confidence=round(fraction, 4),
            support=support,
            source=source,
        ))
    ideas.sort(key=lambda i: i.support, reverse=True)
    return ideas


def _slug(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", normalize_answer(text)).strip("-")
    return (s[:max_len].rstrip("-")) or "idea"


def idea_to_markdown(idea: Idea, created: Optional[str] = None) -> str:
    """Render an Idea as a markdown file body with YAML frontmatter. Deterministic.

    `created` is passed in (not generated here) so output is testable; real callers
    supply an ISO 8601 timestamp.
    """
    lines = [
        "---",
        "type: idea",
        f"status: {idea.status}",
        f"lane: {idea.lane}",
        f"confidence: {idea.confidence}",
        f"support: {idea.support}",
        f"source: {idea.source}",
    ]
    if created:
        lines.append(f"created: {created}")
    lines += ["---", "", idea.text, ""]
    return "\n".join(lines)


def persist_ideas(ideas: list[Idea], root: str | Path, created: Optional[str] = None) -> list[Path]:
    """Write each idea to `root` as `<NN>-<slug>.md`. Returns the paths written."""
    root_path = Path(root)
    root_path.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, idea in enumerate(ideas, start=1):
        dest = root_path / f"{i:02d}-{_slug(idea.text)}.md"
        n = 2
        while dest.exists():  # a same-day rerun must not overwrite earlier ideas
            dest = root_path / f"{i:02d}-{_slug(idea.text)}-{n}.md"
            n += 1
        dest.write_text(idea_to_markdown(idea, created=created), encoding="utf-8")
        paths.append(dest)
    return paths


@dataclass
class InspirationRun:
    """A full inspire() pass: the prompt, the raw samples, the score, and captured ideas."""

    prompt: str
    samples: list[str]
    result: ConsistencyResult
    ideas: list[Idea] = field(default_factory=list)


def inspire(
    generate: Callable[[str], str],
    prompt: str,
    *,
    n: int = 5,
    source: str = "brain/inspire",
    equiv: Optional[EquivFn] = None,
    grounded_threshold: float = GROUNDED_THRESHOLD,
    speculative_threshold: float = SPECULATIVE_THRESHOLD,
) -> InspirationRun:
    """Generate N candidates, score their self-consistency, and capture unverified ideas.

    `generate(prompt) -> str` is injected so this is testable without an LLM and so the
    real Brain can pass its own (hot, sampled) model call. Nothing here is ever treated
    as a fact: every returned Idea is status="unverified" pending a verifier.
    """
    if n < 1:
        n = 1
    samples = [generate(prompt) for _ in range(n)]
    result = score_consistency(
        samples,
        equiv=equiv,
        grounded_threshold=grounded_threshold,
        speculative_threshold=speculative_threshold,
    )
    ideas = capture_ideas(result, samples, source=source)
    return InspirationRun(prompt=prompt, samples=samples, result=result, ideas=ideas)


# ---------------------------------------------------------------------------
# Adapters (kept separate from the pure core so the core stays dependency-free
# and unit-testable; these touch the network / render reports).
# ---------------------------------------------------------------------------

def ollama_generate(
    prompt: str,
    *,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    temperature: float = 1.0,
    timeout: float = 120.0,
    system: Optional[str] = None,
) -> str:
    """One divergent completion from Ollama /api/chat (no tools), honoring temperature.

    This is the real `generate` callable passed to `inspire()` by the Brain executor.
    It is kept out of the pure core and is monkeypatched in tests, so nothing here runs
    a model during unit tests. Env defaults match the Brain agent
    (ATLAS_BRAIN_MODEL, ATLAS_BRAIN_OLLAMA_URL).
    """
    import os
    import httpx

    model = model or os.environ.get("ATLAS_BRAIN_MODEL", "qwen3:8b")
    base = (base_url or __import__('atlas_plus.llm.ollama_url', fromlist=['default_ollama_url']).default_ollama_url()).rstrip("/")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    resp = httpx.post(f"{base}/api/chat", json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return ((data.get("message") or {}).get("content") or "").strip()


def run_to_markdown(run: InspirationRun, *, run_id: str = "", created: Optional[str] = None) -> str:
    """Render an inspiration run as a markdown report: verdict, captured ideas, raw samples."""
    r = run.result
    lines = [
        "---",
        f"id: {run_id or 'inspire'}-inspire",
        "type: inspiration-run",
        "status: unverified",
        f"verdict: {r.verdict}",
        f"confidence: {r.confidence}",
        f"entropy: {r.entropy}",
    ]
    if created:
        lines.append(f"created: {created}")
    lines += [
        "---",
        "",
        "# Inspiration run",
        "",
        f"Prompt: {run.prompt}",
        "",
        f"- samples: {r.n}",
        f"- verdict: {r.verdict} (top fraction {r.top_fraction}, entropy {r.entropy})",
        f"- ideas captured: {len(run.ideas)} (all status=unverified)",
        "",
        "## Ideas (most-supported first)",
        "",
    ]
    for i, idea in enumerate(run.ideas, start=1):
        lines.append(f"{i}. [{idea.lane}, support {idea.support}, conf {idea.confidence}] {idea.text}")
    lines += ["", "## Raw samples", ""]
    for i, s in enumerate(run.samples, start=1):
        lines.append(f"{i}. {s}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Increment 6: embedding-based semantic equivalence (true semantic entropy).
# Pluggable into score_consistency(answers, equiv=...). The embed function is
# injected so unit tests never call a model.
# ---------------------------------------------------------------------------

def ollama_embed(text: str, *, model: Optional[str] = None, base_url: Optional[str] = None,
                 timeout: float = 30.0) -> list[float]:
    """Embed text via Ollama /api/embed. Default model ATLAS_EMBED_MODEL or nomic-embed-text.

    /api/embed, not legacy /api/embeddings: newer Ollama hard-errors the
    legacy endpoint on over-context input instead of truncating (third
    call site caught by the run-26 static gate, 2026-10-04).
    """
    import os
    import httpx

    model = model or os.environ.get("ATLAS_EMBED_MODEL", "nomic-embed-text")
    base = (base_url or __import__('atlas_plus.llm.ollama_url', fromlist=['default_ollama_url']).default_ollama_url()).rstrip("/")
    resp = httpx.post(f"{base}/api/embed", json={"model": model, "input": text}, timeout=timeout)
    resp.raise_for_status()
    rows = resp.json().get("embeddings") or []
    return list(rows[0]) if rows else []


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity in [-1, 1]; 0.0 for empty or mismatched-length vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def make_embedding_equiv(
    embed_fn: Optional[Callable[[str], list[float]]] = None,
    *,
    threshold: float = 0.83,
) -> EquivFn:
    """Build an equiv(a, b) that treats answers as equivalent when their embeddings'
    cosine similarity >= threshold (or they are normalized-equal). `embed_fn` is injected
    (default ollama_embed); embeddings are memoized per text within the returned closure.
    Pass the result as score_consistency(answers, equiv=make_embedding_equiv(...)).
    """
    if embed_fn is None:
        from atlas_plus.llm import get_provider
        embed_fn = get_provider("embed").embed  # LLM-agnostic; honors ATLAS_MODEL_EMBED_PROVIDER
    embed = embed_fn
    _cache: dict[str, list[float]] = {}

    def _emb(t: str) -> list[float]:
        if t not in _cache:
            _cache[t] = embed(t)
        return _cache[t]

    def equiv(a: str, b: str) -> bool:
        if normalize_answer(a) == normalize_answer(b):
            return True
        return cosine(_emb(a), _emb(b)) >= threshold

    return equiv
