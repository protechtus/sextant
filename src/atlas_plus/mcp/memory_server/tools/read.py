"""memory.read — return the full content of a .memory/ file."""

from pathlib import Path

import yaml

from ..config import MAX_READ_BYTES, MEMORY_ROOT, safe_resolve_within


def _safe_resolve(rel: str) -> Path | None:
    """Resolve rel against MEMORY_ROOT, rejecting traversal, excluded dirs, and dot-dirs.

    Delegates to the shared confiner (audit HIGH-2): this now also blocks dot-prefixed
    paths like ``.git/config`` that the old version leaked.
    """
    return safe_resolve_within(MEMORY_ROOT, rel)


def memory_read(path: str) -> dict:
    """Read a .memory/ file by relative path.

    Returns {path, frontmatter, body, truncated, error}.
    """
    target = _safe_resolve(path)
    if target is None:
        return {"path": path, "error": "invalid path", "frontmatter": {}, "body": "", "truncated": False}

    if not target.exists():
        return {"path": path, "error": "not found", "frontmatter": {}, "body": "", "truncated": False}

    try:
        raw_bytes = target.read_bytes()
    except OSError as e:
        return {"path": path, "error": str(e), "frontmatter": {}, "body": "", "truncated": False}

    truncated = len(raw_bytes) > MAX_READ_BYTES
    # utf-8-sig: a file saved by Windows PowerShell 5.1 carries a BOM that would otherwise
    # show up as a stray character and hide the frontmatter (persona pass 2026-11-10).
    raw = raw_bytes[:MAX_READ_BYTES].decode("utf-8-sig", errors="replace")

    frontmatter: dict = {}
    body = raw
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            try:
                frontmatter = yaml.safe_load(raw[3:end].strip()) or {}
            except Exception:
                pass
            body = raw[end + 4:].strip()

    return {
        "path": path,
        "frontmatter": frontmatter,
        "body": body,
        "truncated": truncated,
        "error": None,
    }
