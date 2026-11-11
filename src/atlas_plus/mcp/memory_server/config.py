"""Atlas MCP memory server configuration — all values from env vars with sensible defaults."""

import os
from pathlib import Path
from urllib.parse import unquote
from atlas_plus import paths as atlas_paths

MEMORY_ROOT = atlas_paths.memory_root()
MCP_PORT = atlas_paths.env_int("ATLAS_MCP_PORT", 8765)
STATE_DIR = atlas_paths.state_dir()
# NOTE: the canonical autonomous-write gate is atlas_plus.autonomous.require_autonomous_writes()
# (fail-closed). This module-level flag is currently unused; kept fail-closed (default "off") to
# avoid a stale fail-OPEN duplicate of the kill switch (audit 2026-06-04 / ATL-013).
# (stale module-level AUTONOMOUS flag removed 2026-09-22: frozen-at-import +
# denylist semantics; the canonical gate lives in atlas_plus.autonomous)

# Directories inside MEMORY_ROOT that are never exposed over MCP
# _atlas holds sync inbox proposals and parked conflict copies: review material, never a
# memory the model may be primed with before the operator promotes it (self-hoster 2026-11-10).
EXCLUDED_DIRS = {"archive", "_sync", "versions", "_atlas"}

# Search index (FTS5 + embedding vectors). Its OWN file: a heal after real corruption
# quarantines this file and rebuilds it from the corpus, and must never take Brain's task
# queue (state.sqlite) with it (round-B re-audit 2026-11-09, row 10 / N-H). Derived data:
# a missing file is rebuilt on the next search.
FTS_DB = STATE_DIR / "search.sqlite"

# Cache TTL in seconds for directory listings
LIST_CACHE_TTL = 30

# Maximum file size returned in full; larger files are truncated
MAX_READ_BYTES = 256 * 1024


def safe_resolve_within(root: Path, rel: str, *, exclude: set[str] | None = None) -> Path | None:
    """Resolve ``rel`` strictly under ``root``; return None on any traversal/exclusion.

    Single chokepoint for the MCP filesystem tools (audit 2026-06-04 HIGH-2). Rejects:
    null bytes, ``..``, absolute paths, drive/``:`` prefixes, URL-encoded traversal,
    any path that escapes ``root`` after resolution, any component in ``exclude``, and any
    dot-prefixed component (``.git``, dotfiles). An empty ``rel`` resolves to ``root`` itself.

    ``exclude`` defaults to EXCLUDED_DIRS; pass an empty set for roots without excluded dirs.
    """
    if exclude is None:
        exclude = EXCLUDED_DIRS
    rel = unquote(rel or "")
    if "\x00" in rel or ".." in rel or rel.startswith(("/", "\\")) or ":" in rel:
        return None
    # pathlib accepts "/" on every platform; the old "\\" conversion broke
    # POSIX with literal-backslash names (portability sweep, 2026-07-30).
    target = (root / rel).resolve()
    try:
        parts = target.relative_to(root.resolve()).parts
    except ValueError:
        return None
    if any(p in exclude or p.startswith(".") for p in parts):
        return None
    return target
