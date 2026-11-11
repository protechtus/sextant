"""One-shot script: bulk-archive all tickets in the _archive/ directory.

Reads every *.md in .memory/tickets/_archive/, updates status: archived in
frontmatter, writes back. Safe to re-run (idempotent on already-archived files).

Usage: python scripts/bulk_archive_tickets.py [--dry-run]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from atlas_plus import paths as atlas_paths

MEMORY_ROOT = atlas_paths.memory_root()
ARCHIVE_DIR = MEMORY_ROOT / "tickets" / "_archive"


def _update_status_archived(text: str) -> str | None:
    """Return text with frontmatter status set to archived, or None if not a ticket."""
    if not text.startswith("---"):
        return None
    end = text.find("---", 3)
    if end == -1:
        return None
    fm_block = text[3:end]
    body = text[end + 3:]
    if "status:" not in fm_block:
        return None
    # Already archived -- skip
    if re.search(r"^status:\s*archived\s*$", fm_block, re.MULTILINE):
        return None
    updated = re.sub(r"^status:.*$", "status: archived", fm_block, flags=re.MULTILINE)
    # Also bump updated date
    from datetime import date
    today = date.today().isoformat()
    updated = re.sub(r"^updated:.*$", f"updated: '{today}'", updated, flags=re.MULTILINE)
    return f"---{updated}---{body}"


def main(dry_run: bool = False) -> None:
    if not ARCHIVE_DIR.exists():
        print(f"Archive directory not found: {ARCHIVE_DIR}")
        sys.exit(1)

    files = list(ARCHIVE_DIR.rglob("*.md"))
    print(f"Found {len(files)} ticket files in _archive/")

    updated = 0
    skipped = 0
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        new_text = _update_status_archived(text)
        if new_text is None:
            skipped += 1
            continue
        if not dry_run:
            path.write_text(new_text, encoding="utf-8")
        updated += 1

    verb = "Would update" if dry_run else "Updated"
    print(f"{verb} {updated} tickets to status: archived")
    print(f"Skipped {skipped} (already archived or not parseable)")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    main(dry_run=dry_run)
