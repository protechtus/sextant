"""atlas-intake: operate the intake drop folder.

    atlas-intake run       [--source DIR] [--dry-run] [--leave] [--json]
    atlas-intake watch     [--source DIR] [--interval 60] [--leave]
    atlas-intake status    [--source DIR]
    atlas-intake manifest  [--source DIR]   (regenerate + print)
    atlas-intake rules                       (print the effective rule list)

``run`` is what the scheduler calls every few minutes; ``watch`` is the
same thing in a loop for a terminal session.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import engine, rules as rules_mod, scan, store


def _sources(args) -> list[Path]:
    """Every drop folder this invocation covers: each ``--source``, else ``ATLAS_INTAKE_DIR``."""
    given = getattr(args, "source", None) or []
    if isinstance(given, (str, Path)):
        given = [given]
    try:
        return [engine.source_dir(s) for s in given] or [engine.source_dir(None)]
    except engine.IntakeConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _source(args) -> Path:
    return _sources(args)[0]


def _print_report(report: store.RunReport, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True, ensure_ascii=False))
        return
    print(report.summary_line())
    for r in report.records:
        line = f"  [{r.status:>15}] {r.source_rel}"
        if r.dest:
            line += f" -> {r.dest}"
        if r.reason:
            line += f"  ({r.reason})"
        if r.versioned:
            line += f"  [versioned: {r.versioned}]"
        if r.actions:
            line += "  " + ", ".join(f"{k}: {v}" for k, v in r.actions.items())
        print(line)
    for rel, why in report.skipped:
        print(f"  [{'skipped':>15}] {rel}  ({why})")
    for note in report.notes:
        if not note.startswith("Traceback"):
            print(f"  note: {note}")


def cmd_run(args) -> int:
    rc = 0
    for src in _sources(args):
        report = engine.run_once(
            src,
            dry_run=args.dry_run,
            leave_originals=args.leave,
            settle_seconds=args.settle,
            min_age_seconds=args.min_age,
            notify=not args.no_notify,
            skip_executables=args.skip_executables,
            date_from_mtime=args.date_from_mtime,
            recursive=not getattr(args, "top_level", False),
        )
        _print_report(report, args.json)
        if any(r.status == "error" for r in report.records):
            rc = 1
    return rc


def cmd_watch(args) -> int:
    src = _source(args)
    print(f"watching {src} every {args.interval}s (Ctrl+C to stop)")
    try:
        while True:
            report = engine.run_once(
                src, leave_originals=args.leave, settle_seconds=args.settle,
                min_age_seconds=args.min_age, notify=not args.no_notify,
            )
            if report.records:
                _print_report(report, False)
            time.sleep(max(5, args.interval))
    except KeyboardInterrupt:
        print("stopped")
    return 0


def cmd_status(args) -> int:
    try:
        src = engine.source_dir(getattr(args, "source", None))
    except engine.IntakeConfigError as exc:
        src = None
        print(f"source: NOT CONFIGURED ({exc})")
    if src is not None:
        print(f"source:  {src}  ({'present' if src.is_dir() else 'MISSING'})")
        if src.is_dir():
            cands, skipped = scan.discover(src)
            print(f"pending: {len(cands)} candidate(s), {len(skipped)} ignored")
            for c in cands[:10]:
                print(f"  - {c.rel} ({c.size} bytes)")
    print(f"landing: {rules_mod.landing_root()}")
    print(f"ledger:  {store.ledger_path()}")
    records = store.ledger_load()
    if records:
        from collections import Counter

        counts = Counter(d.get("status", "?") for d in records)
        print("history: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        last = records[-1]
        print(f"last:    {last.get('ts', '')} {last.get('status', '')} {last.get('source_rel', '')}")
    else:
        print("history: empty")
    run = store.last_run()
    if run:
        print(f"last run: {run.get('run_id')} {run.get('counts')}")
    return 0


def cmd_manifest(args) -> int:
    src = _source(args)
    path = engine.write_manifest(src)
    print(path.read_text(encoding="utf-8"))
    return 0


def cmd_brand_import(args) -> int:
    """Extract and validate a kit zip that is already in the archive (re-run after a refusal)."""
    rc = 0
    for z in args.zip:
        p = Path(z)
        if not p.is_file():
            print(f"  [error] not a file: {p}")
            rc = 1
            continue
        out = engine.brand_import_zip(p, project=args.project)
        print(f"  [{'refused' if out.startswith('refused') else 'ok':>8}] {p.name}: {out}")
        if out.startswith("refused"):
            rc = 1
    return rc


def cmd_rules(args) -> int:
    print(f"rules file: {rules_mod.rules_path()} ({'present' if rules_mod.rules_path().is_file() else 'absent, defaults only'})")
    for i, r in enumerate(rules_mod.load_rules(), 1):
        match = json.dumps(r.match, sort_keys=True) if r.match else "*"
        print(f"{i:>2}. {r.name:<24} {match}")
        print(f"    -> {r.dest}" + (f"   actions: {', '.join(r.on_import)}" if r.on_import else ""))
    return 0


def _common(p: argparse.ArgumentParser, *, run_flags: bool = True) -> None:
    p.add_argument("--source", action="append", help="drop folder; repeatable (default: ATLAS_INTAKE_DIR)")
    if run_flags:
        p.add_argument("--leave", action="store_true", help="leave originals in place instead of archiving under _imported/")
        p.add_argument("--settle", type=float, default=2.0, help="seconds between the two size samples (default 2)")
        p.add_argument("--min-age", type=float, default=5.0, help="skip files modified fewer than N seconds ago (default 5)")
        p.add_argument("--no-notify", action="store_true", help="do not push a summary notification")
        p.add_argument("--skip-executables", action="store_true",
                       help="leave installers and scripts in place (for a Downloads sweep)")
        p.add_argument("--date-from-mtime", action="store_true",
                       help="{date} in destinations is the file's modification date, not today")
        p.add_argument("--top-level", action="store_true",
                       help="only loose files at the top of the folder; never descend (Downloads sweep)")


def main(argv: list[str] | None = None) -> None:
    from atlas_plus._console import utf8_stdout_when_piped
    utf8_stdout_when_piped()
    from atlas_plus.branding import prog as _prog

    from atlas_plus import _next_step  # usage errors name --help (LES-054)
    parser = _next_step.ArgumentParser(prog=_prog("atlas-intake"),
                                     description="Import files dropped into the intake folder, sorted, with a manifest.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="process the folder once")
    _common(p)
    p.add_argument("--dry-run", action="store_true", help="show what would happen; write nothing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("watch", help="process the folder in a loop")
    _common(p)
    p.add_argument("--interval", type=int, default=60)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("status", help="pending files and ledger summary")
    _common(p, run_flags=False)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("manifest", help="regenerate and print the manifest")
    _common(p, run_flags=False)
    p.set_defaults(func=cmd_manifest)

    p = sub.add_parser("rules", help="print the effective routing rules")
    p.set_defaults(func=cmd_rules)

    p = sub.add_parser("brand-import", help="extract + validate an archived kit zip into brand/_incoming (re-run)")
    p.add_argument("zip", nargs="+", help="archived zip path(s)")
    p.add_argument("--project", help="brand slug (default: from the zip name)")
    p.set_defaults(func=cmd_brand_import)

    args = parser.parse_args(argv)
    raise SystemExit(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    main()
