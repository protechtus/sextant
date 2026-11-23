"""Carry dispatched jobs to the end: collect answers as their batches finish, send each
collected answer job to the judge, collect the verdicts, write the report.

    python finish.py [--judge opus] [--poll 60]

Safe to stop and start again: every step looks at what is already on disk. All spend still
goes through gate.py.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arms  # noqa: E402
import gate  # noqa: E402
import report  # noqa: E402


def _dispatched(prefix: str) -> list[str]:
    return sorted(p.name for p in gate.jobs_dir().iterdir()
                  if p.name.startswith(prefix) and (p / "state.json").exists())


def _collected(name: str) -> bool:
    return (gate.job_dir(name) / "results.jsonl").exists()


def step(judge: str) -> bool:
    """One pass. Returns True when nothing is left in flight."""
    waiting = False
    for name in _dispatched("ans.") + _dispatched(f"judge-{judge}."):
        if _collected(name):
            continue
        outcome = gate.collect(name)
        if outcome["state"] != "collected":
            waiting = True
            continue
        settled = outcome["settled"]
        print(f"collected {name}: {settled['requests'] - settled['failed']}/{settled['requests']} ok, "
              f"{settled['actual_usd']:.2f} USD (estimate {settled['est_usd']:.2f})", flush=True)
    for name in _dispatched("ans."):
        judge_name = f"judge-{judge}.{name}"
        if not _collected(name) or (gate.job_dir(judge_name) / "state.json").exists():
            continue
        if arms.build_judge_job(name, judge) is None:
            continue
        try:
            est = gate.dispatch(judge_name, "batch")
        except gate.GateError as exc:
            print(f"REFUSED {judge_name}: {exc}", flush=True)
            continue
        print(f"judging {name}: estimate {est['est_usd']:.2f} USD", flush=True)
        waiting = True
    return not waiting


def main(argv: list[str]) -> int:
    judge = argv[argv.index("--judge") + 1] if "--judge" in argv else "opus"
    poll = int(argv[argv.index("--poll") + 1]) if "--poll" in argv else 60
    while not step(judge):
        time.sleep(poll)
    report.main([judge])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
