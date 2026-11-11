"""P7-T2: Contradiction detection — LLM-driven review of potentially related fact pairs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from atlas_plus.curation.duplicates import _snippet, _tokenize, _jaccard
from atlas_plus.mcp.memory_server.config import EXCLUDED_DIRS, MEMORY_ROOT

RELATED_THRESHOLD = 0.15  # lower than duplicate — we want candidate pairs
MAX_PAIRS_PER_CALL = 5    # batch size to keep prompts manageable


@dataclass
class ContradictionPair:
    """A pair of memory files where an LLM detected conflicting claims."""

    file1: str
    file2: str
    explanation: str
    confidence: str  # high | medium | low

    def as_dict(self) -> dict:
        """Return JSON-serialisable dict of the contradiction pair fields."""
        return {
            "file1": self.file1,
            "file2": self.file2,
            "explanation": self.explanation,
            "confidence": self.confidence,
        }


def _contradiction_prompt(pairs: list[tuple[str, str, str, str]]) -> str:
    """Build an LLM prompt to detect contradictions across fact pairs."""
    blocks = []
    for i, (f1, s1, f2, s2) in enumerate(pairs):
        blocks.append(
            f"Pair {i + 1}:\n"
            f"  File A ({f1}): {s1}\n"
            f"  File B ({f2}): {s2}"
        )

    joined = "\n\n".join(blocks)
    return f"""\
You are auditing a personal knowledge base for contradictions.
For each pair below, decide whether the two excerpts contradict each other.

A contradiction means they make incompatible factual claims (not just different emphasis).
Style differences, level of detail, or partial overlap are NOT contradictions.

Return ONLY valid JSON — a list with one object per pair:
[
  {{
    "pair": <1-based index>,
    "contradicts": true | false,
    "confidence": "high" | "medium" | "low",
    "explanation": "<one sentence if contradicts=true, else empty string>"
  }}
]

PAIRS:
{joined}

JSON:"""


def find_contradictions(
    memory_root: Optional[Path] = None,
    scope: str = "",
    dry_run: bool = False,
) -> list[ContradictionPair]:
    """Detect contradictory facts across .memory/ files using an LLM.

    Candidate pairs are first filtered by keyword overlap (Jaccard > RELATED_THRESHOLD)
    to keep LLM calls cheap. Only candidates are sent to the LLM.

    Args:
        memory_root: Root of the corpus. Defaults to MEMORY_ROOT.
        scope: Optional subdirectory to restrict search.
        dry_run: If True, return candidate pairs without calling the LLM.

    Returns list of ContradictionPair.
    """
    from atlas_plus.summarizer.providers import ProviderError, complete

    root = memory_root or MEMORY_ROOT
    search_root = root / scope if scope else root

    files = [
        p for p in search_root.rglob("*.md")
        if not any(part in EXCLUDED_DIRS for part in p.relative_to(root).parts)
        and not p.name.startswith("_")
    ]

    tokens: dict[Path, set[str]] = {}
    snippets: dict[Path, str] = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
            tokens[f] = _tokenize(text)
            snippets[f] = _snippet(f, max_chars=300)
        except OSError:
            tokens[f] = set()
            snippets[f] = ""

    # Find candidate pairs (related but not identical)
    file_list = list(tokens.keys())
    candidates: list[tuple[Path, Path]] = []
    for i in range(len(file_list)):
        for j in range(i + 1, len(file_list)):
            f1, f2 = file_list[i], file_list[j]
            sim = _jaccard(tokens[f1], tokens[f2])
            if RELATED_THRESHOLD <= sim < 0.85:  # exclude near-duplicates (handled by T1)
                candidates.append((f1, f2))

    if dry_run or not candidates:
        return [
            ContradictionPair(
                file1=str(f1.relative_to(root)).replace("\\", "/"),
                file2=str(f2.relative_to(root)).replace("\\", "/"),
                explanation="(dry-run — LLM not called)",
                confidence="low",
            )
            for f1, f2 in candidates
        ]

    results: list[ContradictionPair] = []

    # Process in batches
    for batch_start in range(0, len(candidates), MAX_PAIRS_PER_CALL):
        batch = candidates[batch_start: batch_start + MAX_PAIRS_PER_CALL]
        pair_data = [
            (
                str(f1.relative_to(root)).replace("\\", "/"),
                snippets[f1],
                str(f2.relative_to(root)).replace("\\", "/"),
                snippets[f2],
            )
            for f1, f2 in batch
        ]

        prompt = _contradiction_prompt(pair_data)
        try:
            text, _ = complete(prompt)
        except ProviderError:
            continue

        # Parse response
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(l for l in lines[1:] if not l.startswith("```")).strip()

        try:
            items = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("[")
            end = text.rfind("]") + 1
            if start >= 0 and end > start:
                try:
                    items = json.loads(text[start:end])
                except json.JSONDecodeError:
                    continue
            else:
                continue

        for item in items:
            idx = item.get("pair", 1) - 1
            if not item.get("contradicts", False):
                continue
            if idx < 0 or idx >= len(batch):
                continue
            f1, f2 = batch[idx]
            results.append(ContradictionPair(
                file1=str(f1.relative_to(root)).replace("\\", "/"),
                file2=str(f2.relative_to(root)).replace("\\", "/"),
                explanation=item.get("explanation", ""),
                confidence=item.get("confidence", "low"),
            ))

    return results
