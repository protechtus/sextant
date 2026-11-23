"""Command line for the answer and judge stages (the stages that cost money).

    python proof.py build <arm> <model> [--rep N] [--per-type N] [--types a,b] [--suffix .pilot]
    python proof.py plan                 # every built job: estimate, route, budget (no network)
    python proof.py dispatch <job> [--mode auto|sync|batch]
    python proof.py status [<job>]
    python proof.py collect [<job>]      # all dispatched jobs when no name is given
    python proof.py judge <answer-job> [--judge opus]
    python proof.py ledger

`plan` and `build` never touch the network. `dispatch` applies the rules in gate.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arms  # noqa: E402
import gate  # noqa: E402


def _opt(argv: list[str], flag: str, default: str | None = None) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else default


def _job_names() -> list[str]:
    return sorted(p.name for p in gate.jobs_dir().iterdir() if (p / "meta.json").exists())


def plan() -> None:
    spent = gate.committed()
    print(f"sync limit {gate.sync_limit():.2f} USD | budget {gate.budget():.2f} USD | "
          f"settled {spent['settled_usd']:.2f} | in flight {spent['pending_usd']:.2f}")
    print(f"{'job':58s} {'reqs':>5s} {'in tok':>12s} {'sync $':>8s} {'batch $':>8s}  route")
    total = 0.0
    for name in _job_names():
        if (gate.job_dir(name) / "state.json").exists():
            continue
        est = gate.estimate(name)
        over = est["sync_usd"] > gate.sync_limit()
        cost = est["batch_usd"] if over else est["sync_usd"]
        total += cost
        print(f"{name:58s} {est['requests']:5d} {est['input_tokens']:12,d} {est['sync_usd']:8.2f} "
              f"{est['batch_usd']:8.2f}  {'batch (over the sync limit)' if over else 'sync allowed'}")
    print(f"not yet dispatched, at the chosen routes: {total:.2f} USD "
          f"(would bring committed spend to {spent['total_usd'] + total:.2f} of {gate.budget():.2f})")


def main(argv: list[str]) -> int:
    if not argv:
        raise SystemExit(__doc__)
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "build":
            types = _opt(rest, "--types")
            per_type = _opt(rest, "--per-type")
            every = _opt(rest, "--every")
            rows = arms.select(types.split(",") if types else None, int(per_type) if per_type else None,
                               every=int(every) if every else None)
            name = arms.build_answer_job(rest[0], rest[1], int(_opt(rest, "--rep", "1")), rows,
                                         suffix=_opt(rest, "--suffix", "") or "",
                                         effort=_opt(rest, "--effort", "medium") or "medium")
            print(json.dumps(gate.estimate(name)))
        elif cmd == "plan":
            plan()
        elif cmd == "dispatch":
            print(json.dumps(gate.dispatch(rest[0], _opt(rest, "--mode", "auto") or "auto"), indent=1))
        elif cmd == "status":
            for name in ([rest[0]] if rest else _job_names()):
                print(json.dumps(gate.status(name)))
        elif cmd == "collect":
            names = [rest[0]] if rest else [n for n in _job_names() if (gate.job_dir(n) / "state.json").exists()]
            for name in names:
                print(json.dumps(gate.collect(name)))
        elif cmd == "judge":
            name = arms.build_judge_job(rest[0], _opt(rest, "--judge", "opus") or "opus")
            print(json.dumps(gate.estimate(name)) if name else "nothing to judge")
        elif cmd == "ledger":
            for entry in gate.ledger():
                print(json.dumps(entry))
            print(json.dumps(gate.committed()))
        else:
            raise SystemExit(__doc__)
    except gate.GateError as exc:
        print(f"REFUSED: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
