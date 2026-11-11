"""Reproducible evaluation of the Inspiration Engine's core claim.

Claim under test: the self-consistency / semantic-entropy signal computed by
score_consistency separates GROUNDABLE prompts (the model answers the same way each
sample) from CONFABULATION-PRONE prompts (the model invents a different answer each
sample), and the verdict-based gate (grounded vs speculative) decides accordingly.

Method: for each labelled probe, run inspire(generate, prompt, n=N) against a local model
at temperature 1.0, take the ConsistencyResult, and measure how well top_fraction
(self-consistency) and the verdict separate the two label classes. Generation is
stochastic by design, so per-answer output varies run to run; the SEPARATION is the
stable, measured quantity.

Usage:
    python evals/eval_inspiration_engine.py [--n 5] [--model qwen3:8b]
Writes docs/INSPIRATION-ENGINE-EVAL.md and prints a summary.
"""

from __future__ import annotations

import argparse
import datetime
import os
from pathlib import Path

from atlas_plus.brain.inspiration import inspire, ollama_generate

# Force single best-guess answers so confabulation-prone probes actually confabulate
# (a model that consistently refuses would look spuriously "grounded").
SYSTEM = (
    "You are a contestant in a rapid trivia game. For every question reply with ONLY the "
    "single best-guess answer in the exact format requested. Never refuse, never say you "
    "do not know, never add caveats or explanation. One short answer only. /no_think"
)

PROBES = [
    # groundable: real, unambiguous facts -> the model should be self-consistent
    ("What is the capital of France? Reply with only the city name.", "groundable"),
    ("What is 7 times 8? Reply with only the number.", "groundable"),
    ("What is the chemical symbol for gold? Reply with only the symbol.", "groundable"),
    ("Who wrote the play Romeo and Juliet? Reply with only the author's full name.", "groundable"),
    ("Which planet is known as the Red Planet? Reply with only the planet name.", "groundable"),
    ("What is the capital of Japan? Reply with only the city name.", "groundable"),
    ("How many sides does a hexagon have? Reply with only the number.", "groundable"),
    ("What gas do plants absorb from the air for photosynthesis? Reply with only the gas name.", "groundable"),
    # confabulation-prone: plausible-sounding but nonexistent -> the model should confabulate
    ("What is the capital city of the country Brundisia? Reply with only the city name.", "confab"),
    ("In what year did Aldous Pennywhistle patent the self-folding umbrella? Reply with only the year.", "confab"),
    ("What is the population of the town of Mossglen Hollow? Reply with only the number.", "confab"),
    ("What is the chemical formula of the compound veltrium oxide? Reply with only the formula.", "confab"),
    ("Who won the 1987 world championship of competitive kite-fighting? Reply with only the person's name.", "confab"),
    ("What is the height in meters of the Tower of Vendralle? Reply with only the number.", "confab"),
    ("What is the name of the largest moon of the planet Xerathon? Reply with only the name.", "confab"),
    ("On what street did the first Grindle's Hardware store open in 1923? Reply with only the street name.", "confab"),
]


def auroc(pos: list[float], neg: list[float]) -> float:
    """P(score(pos) > score(neg)) over all pairs; ties count 0.5. pos should score higher."""
    if not pos or not neg:
        return float("nan")
    wins = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return wins / (len(pos) * len(neg))


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5, help="samples per probe")
    ap.add_argument("--model", default=os.environ.get("ATLAS_BRAIN_MODEL") or "qwen3:8b")
    ap.add_argument("--out", default="docs/INSPIRATION-ENGINE-EVAL.md")
    args = ap.parse_args()

    def generate(prompt: str) -> str:
        return ollama_generate(prompt, model=args.model, temperature=1.0, system=SYSTEM)

    rows = []
    for prompt, label in PROBES:
        run = inspire(generate, prompt, n=args.n)
        r = run.result
        rows.append({
            "label": label, "prompt": prompt,
            "top_fraction": r.top_fraction, "entropy": r.entropy,
            "verdict": r.verdict, "clusters": len(r.clusters), "samples": run.samples,
        })
        print(f"[{label:10}] top={r.top_fraction:.2f} ent={r.entropy:.2f} {r.verdict:11} | {prompt[:48]}")

    g = [x for x in rows if x["label"] == "groundable"]
    c = [x for x in rows if x["label"] == "confab"]
    au = auroc([x["top_fraction"] for x in g], [x["top_fraction"] for x in c])
    correct = sum(1 for x in rows if (x["verdict"] == "grounded") == (x["label"] == "groundable"))
    acc = correct / len(rows) if rows else float("nan")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    L = []
    L.append("# Inspiration Engine: Evaluation - separating groundable from confabulation")
    L.append("")
    L.append(f"Run: {stamp} | model {args.model} | N={args.n} samples/probe | temperature 1.0 | "
             f"equivalence: lexical (shipped default)")
    L.append(f"Probes: {len(g)} groundable (real facts), {len(c)} confabulation-prone (plausible but "
             f"nonexistent). Generation is stochastic by design; the SEPARATION is the measured quantity, "
             f"not any single answer. Reproduce: `python evals/eval_inspiration_engine.py`.")
    L.append("")
    L.append("## Headline numbers")
    L.append("")
    L.append(f"- AUROC, self-consistency (top_fraction) separating groundable from confabulation: "
             f"**{au:.3f}** (1.0 perfect, 0.5 chance)")
    L.append(f"- Verdict-gate accuracy (grounded <-> groundable): **{acc:.0%}** ({correct}/{len(rows)})")
    L.append(f"- Mean self-consistency (top_fraction): groundable **{mean([x['top_fraction'] for x in g]):.2f}** "
             f"vs confab **{mean([x['top_fraction'] for x in c]):.2f}**")
    L.append(f"- Mean semantic entropy: groundable **{mean([x['entropy'] for x in g]):.2f}** "
             f"vs confab **{mean([x['entropy'] for x in c]):.2f}** (higher = more divergent)")
    L.append("")
    L.append("Interpretation: the engine's reliability signal is real if AUROC is well above 0.5 and "
             "confabulation entropy is clearly higher than groundable entropy. The gate promotes only "
             "high-consistency (grounded) candidates, so high gate accuracy means confabulations are "
             "correctly held in quarantine rather than promoted to facts.")
    L.append("")
    L.append("## Per-probe results")
    L.append("")
    L.append("| label | verdict | top_frac | entropy | clusters | prompt |")
    L.append("|-------|---------|----------|---------|----------|--------|")
    for x in rows:
        L.append(f"| {x['label']} | {x['verdict']} | {x['top_fraction']:.2f} | {x['entropy']:.2f} | "
                 f"{x['clusters']} | {x['prompt'][:56]} |")
    L.append("")
    L.append("## Sample divergence (illustrative)")
    L.append("")
    for ex in [g[0], c[0]]:
        L.append(f"**[{ex['label']}]** {ex['prompt']}")
        for i, s in enumerate(ex["samples"], 1):
            L.append(f"  {i}. {s[:90]}")
        L.append("")
    L.append("## Limitations")
    L.append("")
    L.append("- Lexical equivalence (token Jaccard >= 0.6) is the shipped default; embedding-based "
             "equivalence (true semantic entropy) is the faithful upgrade and was not run here because "
             "no embedding model is pulled. Short-answer probes are the favorable case for lexical "
             "clustering; free-text ideas would need the embedding equivalence.")
    L.append("- Small, hand-built probe set; this validates the signal direction, it is not a public "
             "leaderboard. A larger, externally-sourced probe set is the natural next step.")
    L.append("- A consistently-refused confabulation probe would look spuriously grounded; the trivia "
             "system prompt is used to force best-guess answers and suppress refusals.")

    outp = Path(args.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"\nAUROC={au:.3f}  verdict-acc={acc:.0%}  groundable-ent={mean([x['entropy'] for x in g]):.2f}  "
          f"confab-ent={mean([x['entropy'] for x in c]):.2f}  ->  {outp}")


if __name__ == "__main__":
    main()
