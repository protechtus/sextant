"""CLI: token-budget snapshot + task preflight.

    python -m atlas_plus.budget                     # current 5h-window snapshot
    python -m atlas_plus.budget --estimate 900000   # can I finish a ~900k-gross task?
    python -m atlas_plus.budget --size large        # preflight by named size
    python -m atlas_plus.budget --json
"""

from __future__ import annotations

import argparse
import json

from .meter import human
from .preflight import DEFAULT_MARGIN, SIZE_PRESETS, preflight, preflight_size, snapshot


def main() -> int:
    ap = argparse.ArgumentParser(description="Atlas token-budget preflight")
    ap.add_argument("--estimate", type=int, default=None, help="task gross-token estimate")
    ap.add_argument("--size", choices=sorted(SIZE_PRESETS), default=None,
                    help="preflight by named size instead of an explicit estimate")
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN, help="safety margin (default 0.25)")
    ap.add_argument("--ceiling-days", type=int, default=14)
    ap.add_argument("--window-budget", type=int, default=0,
                    help="known real 5h cap; 0 = use empirical ceiling")
    ap.add_argument("--quick", action="store_true",
                    help="fast: 5h-used + 1h-burn only (short lookback, no ceiling)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.quick:
        from .meter import WINDOW_HOURS, load_events, window_sum
        ev = load_events(6)  # only need the last 5h (+1h burn); short lookback = fast
        used, burn = window_sum(ev, WINDOW_HOURS), window_sum(ev, 1)
        if args.json:
            print(json.dumps({"used_5h": used, "burn_1h": burn}))
        else:
            print(f"5h used {human(used)}  burn {human(burn)}/h")
        return 0

    if args.estimate is not None:
        v = preflight(args.estimate, margin=args.margin, ceiling_days=args.ceiling_days,
                      window_budget=args.window_budget)
    elif args.size is not None:
        v = preflight_size(args.size, margin=args.margin, ceiling_days=args.ceiling_days,
                           window_budget=args.window_budget)
    else:
        snap = snapshot(ceiling_days=args.ceiling_days, window_budget=args.window_budget)
        if args.json:
            print(json.dumps(snap.as_dict()))
            return 0
        print("Token budget -- current 5h rate-limit window")
        print(f"  used (5h):  {human(snap.used_5h)}")
        print(f"  ceiling:    {human(snap.ceiling)}  (empirical max 5h-rolling spend)")
        print(f"  headroom:   {human(snap.headroom)}")
        print(f"  burn (1h):  {human(snap.burn_1h)}/h")
        print(f"  output 5h:  {human(snap.output_5h)}")
        print("  (pass --estimate N or --size to preflight a task)")
        return 0

    if args.json:
        print(json.dumps(v.as_dict()))
        return 0

    icon = {"proceed": "OK", "tight": "TIGHT", "insufficient": "STOP"}[v.status]
    print(f"[{icon}] task ~{human(v.estimated_tokens)} gross  (need {human(v.required)} with {v.margin:.0%} margin)")
    print(f"  headroom {human(v.headroom)} / ceiling {human(v.snapshot.ceiling)} "
          f"(used {human(v.snapshot.used_5h)} of the 5h window)")
    if v.deficit:
        print(f"  short by {human(v.deficit)}"
              + (f"; window frees it in ~{v.minutes_until_enough:.0f} min"
                 if v.minutes_until_enough else "; cannot be freed by waiting"))
    print(f"  -> {v.recommendation}")
    return 0 if v.status != "insufficient" else 2


if __name__ == "__main__":
    raise SystemExit(main())
