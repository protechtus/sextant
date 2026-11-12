"""First-run scaffolding: create the memory corpus skeleton and state dir.

The stranger's first ten minutes (optimization arc X1, 2026-07-30): a fresh
install lands on a machine with no memory corpus, no identity file, and ~30
commands. This command gives that machine a working shape in one step:

    atlas-init            # create ~/.memory skeleton + ~/.atlas, print next steps
    atlas-init --dry-run  # show what would be created, touch nothing

(The public distribution also exposes it as `sextant-init`.)

Idempotent and non-destructive by design: existing files and directories are
NEVER touched or overwritten; re-running reports "exists" and creates only
what is missing. Respects ATLAS_MEMORY_ROOT / ATLAS_STATE_DIR / the workspace
resolution in atlas_plus.paths.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from atlas_plus import paths as atlas_paths

_SCOPE_DIRS = ["identity", "feedback", "projects", "reference", "sessions"]

_GITIGNORE = """\
# Written by sextant-init. Per-machine state that must not travel with a shared corpus:
# history copies (one per machine, names collide), write locks, the search index.
versions/
_atlas/.locks/
_atlas/*.sqlite
_atlas/*.sqlite-*
*.w-tmp
*.imp-tmp
"""

_IDENTITY_TEMPLATE = """\
---
id: user
title: Your Name
type: identity
status: active
priority: critical
---

# Your Name

Introduce yourself to your own memory. Sessions are primed from this file,
so write it the way you would brief a sharp new collaborator:

- What you go by, and anything a session should always know about you.
- How you like to work (short answers? plans first? tests always?).
- Current focus: the projects or goals that matter right now.

Replace this file's contents freely; it is yours. The `title:` above becomes
your display name in session context (override with ATLAS_USER_NAME).
"""

_SCOPE_READMES = {
    "feedback": "Durable corrections and preferences. One rule per file.\n",
    "projects": "One file per ongoing project: state, goals, constraints.\n",
    "reference": "External facts worth keeping: systems, people, glossaries.\n",
    "sessions": "Session logs land here (hooks write these for you).\n",
}


def run_init(dry_run: bool = False) -> dict:
    """Create the skeleton. Returns {created: [...], existed: [...]}.

    Never overwrites anything. dry_run lists actions without touching disk.
    """
    memory_root = atlas_paths.memory_root()
    state_dir = atlas_paths.state_dir()
    created: list[str] = []
    existed: list[str] = []

    def _ensure_dir(p: Path) -> None:
        if p.is_dir():
            existed.append(str(p))
        else:
            created.append(str(p))
            if not dry_run:
                p.mkdir(parents=True, exist_ok=True)

    def _ensure_file(p: Path, content: str) -> None:
        if p.exists():
            existed.append(str(p))
        else:
            created.append(str(p))
            if not dry_run:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")

    _ensure_dir(memory_root)
    for scope in _SCOPE_DIRS:
        _ensure_dir(memory_root / scope)
    _ensure_file(memory_root / "identity" / "user.md", _IDENTITY_TEMPLATE)
    for scope, blurb in _SCOPE_READMES.items():
        _ensure_file(memory_root / scope / "_README.md", blurb)
    # A corpus pushed to git carried per-machine history copies and lock files; two people then
    # hit add/add conflicts on `versions/` and a committed lock stalled every write (AG19).
    _ensure_file(memory_root / ".gitignore", _GITIGNORE)
    # The marker claims the directory for the destructive commands, so it may only go into a
    # directory that is empty, new, or already Sextant's (round-B re-audit 2026-11-09, row 2:
    # ATLAS_STATE_DIR=~/Documents must stop here, not after the first `reset --state`).
    from atlas_plus import uninstall_cmd as _un
    if state_dir.exists() and any(state_dir.iterdir()) and not _un.looks_like_sextant_state(state_dir):
        sample = ", ".join(sorted(p.name for p in list(state_dir.iterdir())[:5]))
        raise SystemExit(
            f"REFUSED: ATLAS_STATE_DIR={state_dir} already holds other things ({sample}, ...)\n"
            "and shows no sign of being Sextant state. Sextant will not claim it: point\n"
            f"ATLAS_STATE_DIR at a new or empty directory (for example {state_dir / 'sextant-state'})."
        )
    _ensure_dir(state_dir)
    # Positive ownership marker: the uninstaller refuses to delete a "state dir" without it
    # (data-safety audit 2026-11-09: a mistyped ATLAS_STATE_DIR could aim rmtree anywhere).
    marker = state_dir / ".sextant-state"
    if not dry_run and not marker.exists():
        marker.write_text("created by sextant-init; marks this directory as Sextant state. Do not delete.\n", encoding="utf-8")

    return {
        "memory_root": str(memory_root),
        "state_dir": str(state_dir),
        "created": created,
        "existed": existed,
        "dry_run": dry_run,
    }


def main() -> None:
    """Entry point for atlas-init / sextant-init."""
    from atlas_plus._console import utf8_stdout_when_piped
    utf8_stdout_when_piped()
    parser = argparse.ArgumentParser(
        description="Create the memory corpus skeleton and state directory (first run).",
    )
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Show what would be created without touching disk.")
    args = parser.parse_args()

    result = run_init(dry_run=args.dry_run)
    verb = "Would create" if args.dry_run else "Created"

    print(f"Memory corpus: {result['memory_root']}")
    print(f"State dir:     {result['state_dir']}")
    if result["created"]:
        print(f"\n{verb}:")
        for p in result["created"]:
            print(f"  + {p}")
    if result["existed"] and not result["created"]:
        print("\nEverything already exists -- nothing to do.")

    if not args.dry_run:
        print(
            "\nNext steps:\n"
            "  1. Edit identity/user.md -- introduce yourself to your memory.\n"
            "  2. Wire your MCP client: run `sextant setup` -- it prints the one-line\n"
            "     `claude mcp add ...` for THIS install (paths baked in) and the JSON for\n"
            "     Claude Desktop or Cursor. Then `sextant verify` proves the round-trip.\n"
            "  2b. See what's here: sextant components\n"
            "  3. Check health: sextant-doctor (alias of atlas-doctor)\n"
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
