"""sextant versions: report the size of the corpus history (``versions/``) and prune it.

Every write copies the whole note into ``versions/<same path>/<stem>_<date>_vN<ext>`` first
(see mcp/memory_server/versioning.py). That is the undo history, and it grows by one full
copy of the note per write: 500 small facts made 2 MB of history for an 89 KB corpus, and a
200 KB note edited fifty times added 10 MB (AG18, sweep 11). Nothing told the user, and no
command trimmed it. This one does, report first and only ever the ``versions/`` tree:

    sextant versions                       # count, size, oldest, biggest notes
    sextant versions --prune --older-than 90 --keep-last 3          # report what would go
    sextant versions --prune --older-than 90 --keep-last 3 --yes    # delete it

A copy is kept when it is among the newest ``--keep-last`` copies of its note OR newer than
``--older-than`` days. The live notes are never touched.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from atlas_plus import paths as atlas_paths

_SLOT_RE = re.compile(r"^(?P<stem>.+)_(?P<date>\d{4}-\d{2}-\d{2})_v(?P<n>\d+)(?P<ext>\.[^.]*)?$")


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:,.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:,.1f} MB"
    return f"{n / 1024 ** 3:,.2f} GB"


def scan(root: Path) -> dict:
    """Every version copy under ``root/versions`` grouped by the note it belongs to."""
    vdir = root / "versions"
    notes: dict[str, list[tuple[Path, date, int, int]]] = defaultdict(list)
    total = 0
    count = 0
    unparsed = 0
    if not vdir.is_dir():
        return {"versions_dir": str(vdir), "exists": False, "files": 0, "bytes": 0, "notes": {}}
    for dirpath, _dirs, files in os.walk(vdir):
        d = Path(dirpath)
        for name in files:
            p = d / name
            try:
                st = p.stat()
            except OSError:
                continue
            count += 1
            total += st.st_size
            m = _SLOT_RE.match(name)
            if not m:
                unparsed += 1
                continue
            try:
                when = date.fromisoformat(m.group("date"))
            except ValueError:
                unparsed += 1
                continue
            rel_note = (d.relative_to(vdir) / (m.group("stem") + (m.group("ext") or ""))).as_posix()
            notes[rel_note].append((p, when, int(m.group("n")), st.st_size))
    for copies in notes.values():
        copies.sort(key=lambda c: (c[1], c[2]))
    return {"versions_dir": str(vdir), "exists": True, "files": count, "bytes": total,
            "unparsed": unparsed, "notes": notes}


def plan_prune(info: dict, older_than_days: int, keep_last: int) -> list[tuple[Path, int]]:
    cutoff = date.today() - timedelta(days=older_than_days)
    doomed: list[tuple[Path, int]] = []
    for copies in info["notes"].values():
        keep = set(id(c) for c in copies[-keep_last:]) if keep_last > 0 else set()
        for c in copies:
            p, when, _n, size = c
            if id(c) in keep or when >= cutoff:
                continue
            doomed.append((p, size))
    return doomed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sextant versions",
        description="Report the corpus history (versions/) and prune old copies. Report first; "
                    "--yes deletes. Live notes are never touched.")
    ap.add_argument("--prune", action="store_true", help="plan a prune (add --yes to delete)")
    ap.add_argument("--older-than", type=int, default=90, metavar="DAYS",
                    help="prune copies older than DAYS (default 90)")
    ap.add_argument("--keep-last", type=int, default=3, metavar="N",
                    help="always keep the newest N copies of every note (default 3)")
    ap.add_argument("--yes", action="store_true", help="actually delete the planned copies")
    ap.add_argument("--json", action="store_true", help="machine-readable report")
    args = ap.parse_args(argv)
    if args.older_than < 0 or args.keep_last < 0:
        ap.error("--older-than and --keep-last take non-negative numbers")

    root = atlas_paths.memory_root()
    info = scan(root)
    if not info["exists"]:
        msg = {"versions_dir": info["versions_dir"], "files": 0, "bytes": 0}
        print(json.dumps(msg) if args.json else
              f"no history yet: {info['versions_dir']} does not exist (it appears the first time an "
              "existing note is rewritten)")
        return 0
    notes = info["notes"]
    biggest = sorted(((sum(c[3] for c in cs), n, len(cs)) for n, cs in notes.items()), reverse=True)[:5]
    oldest = min((c[1] for cs in notes.values() for c in cs), default=None)
    report = {
        "versions_dir": info["versions_dir"], "files": info["files"], "bytes": info["bytes"],
        "notes_with_history": len(notes), "oldest": oldest.isoformat() if oldest else None,
        "biggest": [{"note": n, "copies": k, "bytes": b} for b, n, k in biggest],
    }
    doomed: list[tuple[Path, int]] = []
    if args.prune:
        doomed = plan_prune(info, args.older_than, args.keep_last)
        report["prune"] = {"older_than_days": args.older_than, "keep_last": args.keep_last,
                           "files": len(doomed), "bytes": sum(s for _p, s in doomed)}
    if args.json and not args.yes:
        print(json.dumps(report, indent=2))
        return 0
    print(f"history: {report['files']:,} copies, {_size(report['bytes'])}, "
          f"{report['notes_with_history']:,} note(s), oldest {report['oldest']} ({info['versions_dir']})")
    for b, n, k in biggest:
        print(f"  {_size(b):>10}  {k:>4} copies  {n}")
    if not args.prune:
        print("prune with: sextant versions --prune --older-than 90 --keep-last 3  (report; add --yes to delete)")
        return 0
    print(f"prune plan: copies older than {args.older_than} day(s), keeping the newest {args.keep_last} of "
          f"every note: {len(doomed):,} file(s), {_size(sum(s for _p, s in doomed))}")
    if not doomed:
        return 0
    if not args.yes:
        print("nothing deleted (report only). Re-run with --yes to delete these copies; the live notes are untouched.")
        return 0
    removed = 0
    freed = 0
    for p, size in doomed:
        try:
            p.unlink()
            removed += 1
            freed += size
        except OSError as exc:
            print(f"  could not delete {p}: {exc}", file=sys.stderr)
    # Empty directories left behind are removed bottom-up; the versions/ root stays.
    vdir = Path(info["versions_dir"])
    for dirpath, dirs, files in sorted(os.walk(vdir), key=lambda t: -len(t[0])):
        if Path(dirpath) != vdir and not dirs and not files:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
    print(f"deleted {removed:,} copies, freed {_size(freed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
