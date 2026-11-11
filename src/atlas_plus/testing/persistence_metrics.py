"""Persistence metrics -- measures fact survival, accuracy, and dedup quality.

Used by Layer 3 (cross-LLM) tests to produce structured comparison reports.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.integration.fixtures.planted_conversations import PlantedFact


def _tokenize(text: str) -> set[str]:
    _STOPS = {
        "the", "a", "an", "and", "or", "of", "to", "in", "is", "it",
        "that", "this", "for", "be", "with", "on", "at", "by", "from",
        "not", "are", "was", "were", "as", "if", "but", "all", "any",
    }
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def drift_score(original: str, extracted: str) -> float:
    """Semantic drift score: 0.0 = identical, 1.0 = completely different.

    Based on Jaccard distance of token sets. Lower is better.
    """
    a = _tokenize(original)
    b = _tokenize(extracted)
    return 1.0 - _jaccard(a, b)


def measure_persistence(
    facts: list,
    memory_root: Path,
) -> list[dict]:
    """Measure survival and accuracy for each planted fact.

    Returns list of result dicts, one per fact.
    """
    results = []
    for fact in facts:
        files = []
        for f in memory_root.rglob("*.md"):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
                if fact.key_phrase.lower() in text.lower():
                    files.append(str(f.relative_to(memory_root)))
            except OSError:
                pass

        survived = len(files) > 0
        # For accuracy: find the extracted text in the file and compute drift
        extracted_text = ""
        if survived:
            try:
                target = memory_root / files[0]
                raw = target.read_text(encoding="utf-8", errors="replace")
                # Find the section after a ## date header
                lines = raw.split("\n")
                in_body = False
                body_lines = []
                for line in lines:
                    if line.startswith("## 20"):
                        in_body = True
                        continue
                    if in_body and line.strip():
                        body_lines.append(line.strip())
                extracted_text = " ".join(body_lines[:3])
            except Exception:
                pass

        drift = drift_score(fact.full_content, extracted_text) if extracted_text else 1.0

        results.append({
            "id": fact.id,
            "key_phrase": fact.key_phrase,
            "scope": fact.scope,
            "survived": survived,
            "found_in": files,
            "drift_score": round(drift, 3),
            "contradicts": fact.contradicts,
        })
    return results


def write_report(
    provider: str,
    model: str,
    results: list[dict],
    state_dir: Path,
) -> Path:
    """Write a JSON persistence report to .atlas/."""
    facts_planted = len(results)
    facts_survived = sum(1 for r in results if r["survived"])
    avg_drift = sum(r["drift_score"] for r in results) / max(facts_planted, 1)

    # Dedup check: facts that contradict should not both survive as duplicates
    contradictions = [r for r in results if r.get("contradicts")]
    dedup_correct = True
    for r in contradictions:
        prior_id = r["contradicts"]
        prior = next((x for x in results if x["id"] == prior_id), None)
        if prior and prior["survived"] and r["survived"]:
            # Both survived -- check if they're in the same file (acceptable) or different (dedup failed)
            if set(r["found_in"]) == set(prior["found_in"]):
                dedup_correct = True  # same file, one section each -- acceptable
            else:
                dedup_correct = False

    report = {
        "provider": provider,
        "model": model,
        "date": date.today().isoformat(),
        "facts_planted": facts_planted,
        "facts_survived": facts_survived,
        "survival_rate": round(facts_survived / max(facts_planted, 1), 3),
        "avg_drift_score": round(avg_drift, 3),
        "dedup_correct": dedup_correct,
        "results": results,
    }

    state_dir.mkdir(parents=True, exist_ok=True)
    out = state_dir / f"persistence-report-{provider}-{report['date']}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return out
