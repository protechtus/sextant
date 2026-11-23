"""Compare answer jobs on the questions they share with a reference job.

    python subset.py <reference-job> <job> [<job> ...]

Every job is scored only on the reference job's questions, so a job that ran on all 500 can be
set beside one that ran on a half. Prints accuracy with a 95% Wilson interval and an exact
McNemar test of each job against the reference. Costs nothing: it reads verdicts on disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import report  # noqa: E402


def main(argv: list[str]) -> int:
    ref = report.score_job(argv[0])
    if ref is None:
        raise SystemExit(f"{argv[0]}: no judged results yet (run finish.py, or check the job name)")
    keep = set(ref["rows"])
    types = {r["question_id"]: r["question_type"] for r in common.load_index("s")}
    print(f"{len(keep)} questions (those in {argv[0]})")
    print(f"{'job':46s} {'acc':>6s}  {'95% CI':15s} {'tok/q':>8s}  vs reference (job-only / ref-only / p)")
    for name in argv:
        scored = report.score_job(name)
        if scored is None:
            print(f"{name}: no judged results yet, skipped")
            continue
        missing = keep - set(scored["rows"])
        if missing:
            print(f"{name}: missing {len(missing)} of the reference questions, skipped")
            continue
        rows = {q: scored["rows"][q] for q in keep}
        hits = sum(r["correct"] for r in rows.values())
        low, high = report.wilson(hits, len(rows))
        tokens = sum(r.get("input_tokens", 0) for r in rows.values()) / len(rows)
        a_only = sum(rows[q]["correct"] and not ref["rows"][q]["correct"] for q in keep)
        b_only = sum(ref["rows"][q]["correct"] and not rows[q]["correct"] for q in keep)
        p = report.mcnemar_exact(a_only, b_only)
        print(f"{name:46s} {hits / len(rows):6.1%}  {low:.1%} to {high:.1%}  {tokens:8,.0f}  {a_only:3d} / {b_only:3d} / p={p:.3g}")
        by_type: dict[str, list[bool]] = {}
        for q, r in rows.items():
            kind = "abstention" if common.is_abstention(q) else types[q]
            by_type.setdefault(kind, []).append(r["correct"])
        print("    " + "  ".join(f"{k} {sum(v)}/{len(v)}" for k, v in sorted(by_type.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
