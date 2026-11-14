"""Sync policy: what may leave this system, to whom. Fail closed.

The single gate every sync surface calls before a file crosses a boundary
(manifest build, file serve, push apply, client pull). Rules, in order:

1. The path itself must be a safe corpus-relative posix path.
2. Excluded directories never sync (derived, historical, or sync-internal).
3. Tier 3 secrets (ADR-016) never sync. The glob fence is imported from
   backup.py so the backup fence and the sync fence cannot drift.
4. Principals: your own paired devices see everything that survives 1-3
   (optionally narrowed to path prefixes at pair time); federation peers
   see NOTHING except their explicit allowlist prefixes; read-only
   principals cannot write.

Design: docs/SEXTANT-SYNC-DESIGN-2026-08.md sections 5 and 8.
"""

from __future__ import annotations

import fnmatch
import os.path
from pathlib import Path
import re
from dataclasses import dataclass, field

from atlas_plus.backup import _SECRET_PATTERNS

# Single-sourced from backup.py (ADR-016 Tier 3) plus sync-specific additions:
# sqlite artifacts are derived state and may hold unversioned personal data.
SECRET_PATTERNS: tuple[str, ...] = tuple(_SECRET_PATTERNS) + (
    "**/*.sqlite",
    "**/*.sqlite-wal",
    "**/*.sqlite-shm",
    "**/*.db",
)

# Directory names (any path segment) that never sync.
EXCLUDED_DIR_NAMES: frozenset[str] = frozenset(
    {"archive", "versions", "_sync", ".git", "__pycache__", ".obsidian"}
)

# Corpus-relative prefixes that never sync (sync's own working dirs; syncing
# them back out would loop conflicts and inboxes between systems).
EXCLUDED_PREFIXES: tuple[str, ...] = (
    "_atlas/sync-conflicts/",
    "_atlas/sync-inbox/",
)

# Comparison forms (audit 2026-11-09 row 4): Windows and macOS file systems
# are case-insensitive and Win32 silently drops trailing dots and spaces, so
# ".GIT/config", ".git./config" and "Versions/x.md" all name the excluded
# directories on disk. Every deny check compares normalized components.
_EXCLUDED_DIR_NORM: frozenset[str] = frozenset(n.casefold() for n in EXCLUDED_DIR_NAMES)
_EXCLUDED_PREFIX_NORM: tuple[str, ...] = tuple(p.casefold() for p in EXCLUDED_PREFIXES)

# NTFS-reserved device names: "CON", "nul.md", "COM1.txt" open a device, not
# a file, on Windows no matter the extension.
RESERVED_NAMES: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{c}" for c in "123456789\u00b9\u00b2\u00b3"}
    | {f"lpt{c}" for c in "123456789\u00b9\u00b2\u00b3"}
)


def normalize_component(part: str) -> str:
    """The form a path component takes on a case-insensitive Win32 file system."""
    return part.rstrip(". ").casefold()


# A Win32 8.3 short name ("GIT~1" for ".git", "CLAUDE~1" for ".claude") aliases a
# directory the lexical checks below would deny (round-B re-audit 2026-11-09, row 4).
# No corpus path legitimately needs a tilde followed by a digit, so refuse the shape.
_SHORT_NAME_RE = re.compile(r"~\d")


def is_portable_component(part: str) -> bool:
    """False for a component Windows would rewrite, alias or treat as a device.

    Trailing dots or spaces are dropped by Win32 (so "x." aliases "x"),
    reserved device names ignore their extension ("nul.md" is NUL), and a
    tilde-digit component may be an 8.3 alias of a denied directory.
    """
    if not part or part != part.rstrip(". "):
        return False
    if _SHORT_NAME_RE.search(part):
        return False
    # Characters NTFS cannot store: a POSIX hub may list "q?.md", which no Windows spoke
    # can write (the pull crashed with EINVAL mid-run; round C 2026-11-10, lane A F4).
    if any(ch in part for ch in '<>:"|?*') or any(ord(ch) < 32 for ch in part):
        return False
    return part.split(".", 1)[0].rstrip(" ").casefold() not in RESERVED_NAMES


def _is_reparse_point(path: Path) -> bool:
    """Symlink or Windows junction (either can lead a corpus-relative path out of the corpus)."""
    try:
        if path.is_symlink():
            return True
        st = path.lstat()
    except OSError:
        return False
    # IsReparseTagNameSurrogate (bit 29): junction 0xA0000003 and symlink 0xA000000C redirect
    # the NAME to another place; cloud placeholders (OneDrive/Dropbox Files On-Demand), dedup,
    # WOF and app-exec links also carry FILE_ATTRIBUTE_REPARSE_POINT but stay where they are
    # and must not be refused (round C 2026-11-10, lane C F1).
    return bool(getattr(st, "st_reparse_tag", 0) & 0x20000000)


def resolved_target(root: Path, rel: str, principal: "Principal | None" = None,
                    direction: str = "read") -> tuple[Path | None, str | None]:
    """Policy check on the path AS THE FILE SYSTEM WILL SEE IT.

    Returns (target, None) or (None, reason). Beyond deny_reason on the spelled
    path: no existing component between root and target may be a symlink or
    junction, the real path must stay under the real root, and the real
    corpus-relative path (8.3 names expanded, case as stored) must pass the
    same policy (round-B re-audit 2026-11-09, rows 4 and N5).
    """
    reason = deny_reason(rel, principal, direction)
    if reason is not None:
        return None, reason
    target = root / rel
    probe = root
    for part in rel.split("/"):
        probe = probe / part
        if probe.exists() and _is_reparse_point(probe):
            return None, "path crosses a symlink or junction"
    try:
        real_root = Path(os.path.realpath(root))
        real_target = Path(os.path.realpath(target))
        real_rel = real_target.relative_to(real_root).as_posix()
    except (OSError, ValueError):
        return None, "path resolves outside the corpus"
    if real_rel != rel:
        reason = deny_reason(real_rel, principal, direction)
        if reason is not None:
            return None, f"{reason} (resolved: {real_rel})"
    return target, None


@dataclass(frozen=True)
class Principal:
    """Who is asking. kind 'device' = your own machine; 'peer' = a foreign system."""

    name: str
    kind: str = "device"  # "device" | "peer"
    scope: str = "rw"  # "ro" | "rw"
    prefixes: tuple[str, ...] = field(default_factory=tuple)


def is_safe_relpath(rel: str) -> bool:
    """True when rel is a plain corpus-relative posix path with no escape tricks."""
    if not rel or len(rel) > 1024:
        return False
    if "\\" in rel or "\x00" in rel:
        return False
    if rel.startswith("/") or ":" in rel:
        return False
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    return True


# Secret matching is on the manifest hot path (once per corpus file); the
# original per-call fnmatch.fnmatch loop was 32 patterns x N files and
# dominated warm manifest builds (measured, quality loop run 21). The
# patterns are module-level constants, so compile them ONCE into two
# alternation regexes -- one against the full rel path, one against the
# basename -- with the same normcase semantics fnmatch.fnmatch applies.
def _compile_alternation(patterns: tuple[str, ...]) -> re.Pattern:
    # IGNORECASE (hardening, quality loop run 32): os.path.normcase only
    # lowercases on Windows, so the secret fence was case-SENSITIVE on
    # POSIX -- `.ENV` or `server.KEY` walked straight past it on the very
    # systems public self-hosters deploy to. A security fence matches
    # case-insensitively on every OS; a false positive costs one excluded
    # file, a false negative ships a secret.
    return re.compile("|".join(
        f"(?:{fnmatch.translate(os.path.normcase(p))})" for p in patterns
    ), re.IGNORECASE)


_SECRET_FULL_RE = _compile_alternation(SECRET_PATTERNS)
_SECRET_BARE_RE = _compile_alternation(tuple(
    p[3:] if p.startswith("**/") else p for p in SECRET_PATTERNS
))


def _matches_secret(rel: str) -> bool:
    # Patterns are rooted like "**/*.key"; also match top-level files by
    # testing the bare name form ("*.key" would miss under "**/" on some
    # fnmatch implementations, so test both the full path and basename).
    name = rel.rsplit("/", 1)[-1]
    return bool(_SECRET_FULL_RE.match(os.path.normcase(rel))
                or _SECRET_BARE_RE.match(os.path.normcase(name)))


def _prefix_match(rel: str, prefixes: tuple[str, ...]) -> bool:
    for prefix in prefixes:
        norm = prefix if prefix.endswith("/") else prefix + "/"
        if rel == prefix or rel.startswith(norm):
            return True
    return False


def deny_reason(
    rel: str,
    principal: Principal | None = None,
    direction: str = "read",
    hub: bool = False,
) -> str | None:
    """Return a human-readable denial reason, or None when the path may cross.

    direction is "read" (leaving this system) or "write" (arriving here).
    A None principal means a local/trusted caller (manifest build for self).
    """
    if not is_safe_relpath(rel):
        return "invalid path"
    parts = rel.split("/")
    if not all(is_portable_component(p) for p in parts):
        return "invalid path"
    norm_parts = [normalize_component(p) for p in parts]
    if any(p in _EXCLUDED_DIR_NORM for p in norm_parts):
        return "excluded directory"
    norm_rel = "/".join(norm_parts)
    if any(norm_rel == p.rstrip("/") or norm_rel.startswith(p) for p in _EXCLUDED_PREFIX_NORM):
        return "sync-internal path"
    # Everything in a SUBDIRECTORY of _atlas/ is derived or parked (sync conflicts and
    # inboxes, archives of them, intake ledgers, cleanup reports): never syncable. Plain
    # files directly under _atlas/ (routing.yaml) are configuration and may sync
    # (self-hoster pass 2026-11-10: an archived parked copy was pushed to the hub).
    if norm_parts[0] == "_atlas" and len(norm_parts) > 2:
        return "sync-internal path"
    if norm_parts[0] == "_atlas" and direction == "write" and hub:
        # a device pushing _atlas/routing.yaml rewrote every route on the hub at once (MN35): the hub's
        # configuration flows hub -> devices only; edit it on the hub. ``hub`` is set by the hub's push
        # handler alone: a device applying a PULL is a write too, and the rule locked devices out of the
        # file for good (AG47)
        return "hub configuration (edit it on the hub)"
    if _matches_secret(rel):
        return "secret (tier 3) never syncs"
    if principal is not None:
        if direction == "write" and principal.scope != "rw":
            return "read-only principal"
        if principal.kind == "peer":
            if not principal.prefixes or not _prefix_match(rel, principal.prefixes):
                return "not in peer allowlist"
        elif principal.prefixes and not _prefix_match(rel, principal.prefixes):
            return "outside device scope"
    return None


def filter_manifest(files: dict, principal: Principal | None) -> dict:
    """Drop every entry the principal may not see. Never raises."""
    return {
        rel: entry
        for rel, entry in files.items()
        if deny_reason(rel, principal, "read") is None
    }
