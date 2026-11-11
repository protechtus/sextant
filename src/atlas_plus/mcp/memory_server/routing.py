"""P3-T2: Deterministic file routing for memory_remember writes.

Loads routing rules from .memory/_atlas/routing.yaml and resolves a
(scope, fact) pair to a target Path inside .memory/.

Epistemic provenance fields (Angle 5):
  first_observed: YYYY-MM-DD  -- set once at file creation; never updated
  last_confirmed: YYYY-MM-DD  -- updated on every write or explicit confirm call
  source: <tag>               -- who wrote the fact (agent, suggest-facts, human, telegram...)
  confidence: high|medium|low -- semantic confidence level
  contradictions: [path1, path2] -- conflicting memory files detected by curation
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import os
from pathlib import Path
from typing import Callable, Optional

import yaml

from .config import MEMORY_ROOT
from atlas_plus.autonomous import require_autonomous_writes

ROUTING_CONFIG = MEMORY_ROOT / "_atlas" / "routing.yaml"

_DEFAULT_FRONTMATTER = {
    "type": "reference",
    "status": "active",
    "priority": "normal",
}

# Sources that carry implicit high human confidence
_HIGH_CONFIDENCE_SOURCES = {"human-direct", "human", "telegram"}


def _map_confidence(confidence_float: Optional[float], source: str) -> str:
    """Map numeric confidence score or source tag to semantic level (high/medium/low)."""
    if source in _HIGH_CONFIDENCE_SOURCES:
        return "high"
    if confidence_float is None:
        return "medium"
    if confidence_float >= 0.85:
        return "high"
    if confidence_float >= 0.70:
        return "medium"
    return "low"


def _update_frontmatter_field(raw: str, key: str, value: str) -> str:
    """Add or update a single scalar frontmatter field. Idempotent. Returns updated raw."""
    if not raw.startswith("---"):
        return raw
    end = raw.find("\n---", 3)
    if end == -1:
        return raw
    fm_block = raw[3:end]
    lines = fm_block.splitlines()
    found = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}:"):
            new_lines.append(f"{key}: {value}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}: {value}")
    return "---" + "\n".join(new_lines) + "\n---" + raw[end + 4:]


def _append_to_frontmatter_list(raw: str, key: str, item: str) -> str:
    """Add item to a frontmatter list field (YAML flow style). Creates field if missing."""
    if not raw.startswith("---"):
        return raw
    end = raw.find("\n---", 3)
    if end == -1:
        return raw
    fm_block = raw[3:end]
    lines = fm_block.splitlines()
    found = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}:"):
            found = True
            existing = line[len(f"{key}:"):].strip()
            if existing.startswith("[") and existing.endswith("]"):
                inner = existing[1:-1].strip()
                items = [x.strip().strip("\"'") for x in inner.split(",") if x.strip()]
            else:
                items = []
            if item not in items:
                items.append(item)
            new_lines.append(f"{key}: [{', '.join(items)}]")
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}: [{item}]")
    return "---" + "\n".join(new_lines) + "\n---" + raw[end + 4:]


# Cross-process write lock (expert-pass 2026-08-26): append_fact is
# read-modify-write, and the MCP server + Brain worker are separate
# PROCESSES sharing the corpus -- 8 concurrent writers empirically kept 3
# of 8 facts before this lock. O_CREAT|O_EXCL lockfiles are portable
# (Windows + POSIX); stale locks (crashed writer) are broken after 30s.
import hashlib as _hashlib
import time as _time
from contextlib import contextmanager


def _atomic_write_text(target: Path, content: str) -> None:
    """tmp + os.replace, the same durability pattern sync/apply.py uses --
    a crash mid-write leaves the old file intact, never a half-written one.

    Windows hardening (full-suite flake, 2026-09-23): unique tmp name per
    write (a fixed name collides if two writers ever overlap), and replace
    is retried briefly -- Defender/Search can hold a just-created file for
    a moment, turning bare os.replace into a spurious PermissionError.
    """
    import threading

    tmp = target.with_name(
        f"{target.name}.{os.getpid()}-{threading.get_ident()}.w-tmp"
    )
    # fsync before the rename so a power loss cannot leave a zero-length note
    # behind a successful replace (round-B 2026-11-09, row 31).
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())
    last_err: OSError | None = None
    for delay in (0.0, 0.05, 0.1, 0.2, 0.4):
        if delay:
            _time.sleep(delay)
        try:
            os.replace(tmp, target)
            # Every corpus write through this primitive voids the FTS
            # freshness window (expert pass 2026-10-05, M1: hooking only
            # append_fact left decisions/anti-memories/confirms invisible
            # to search for up to the debounce window).
            _mark_search_dirty()
            return
        except PermissionError as exc:
            last_err = exc
    # All retries exhausted: don't leave the orphaned tmp behind
    # (expert review 2026-09-27, minor finding).
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    raise last_err  # type: ignore[misc]


def locked_atomic_rewrite(target: Path, fn: "Callable[[str], str]") -> None:
    """Read-modify-write ``target`` under the cross-process write lock.

    Reads the current text (empty string if the file does not exist yet)
    while holding ``_write_lock``, calls ``fn(text)``, and lands the result
    with ``_atomic_write_text``. If ``fn`` returns the text unchanged, nothing
    is written. Every corpus writer that does read-modify-write should go
    through this helper so it cannot drop a concurrent ``append_fact``.
    """
    with _write_lock(target):
        try:
            current = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = ""
        updated = fn(current)
        if updated == current and target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(target, updated)


# Tunable at module level so tests can shrink the windows. Legitimate holds
# last milliseconds; STALE only needs to be far above that.
_LOCK_TIMEOUT = 10.0
_LOCK_STALE = 30.0


def _lock_key(target: Path) -> str:
    """Stable cross-process identity for ``target`` (audit 2026-11-09, row 17a).

    Keyed on the corpus-relative path computed from the RESOLVED memory root
    and the resolved target, so two processes that spell the same corpus
    differently (8.3 short name vs long name, junction, subst drive, case)
    still take the same lock. A target outside the corpus falls back to its
    resolved, case-normalised absolute path.
    """
    try:
        resolved = Path(os.path.realpath(target))
    except (OSError, ValueError):
        resolved = Path(os.path.abspath(target))
    try:
        root = Path(os.path.realpath(MEMORY_ROOT))
        rel = resolved.relative_to(root)
        return "rel:" + rel.as_posix().lower()
    except (OSError, ValueError):
        return "abs:" + os.path.normcase(str(resolved)).lower()


@contextmanager
def _write_lock(target: Path, timeout: float | None = None, stale: float | None = None):
    timeout = _LOCK_TIMEOUT if timeout is None else timeout
    stale = _LOCK_STALE if stale is None else stale
    lock_dir = MEMORY_ROOT / "_atlas" / ".locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / (_hashlib.sha256(_lock_key(target).encode("utf-8")).hexdigest()[:16] + ".lock")
    # The deadline must be able to OUTLAST one full stale-observation window,
    # or an orphaned lock (crashed writer) could never be recovered from.
    deadline = _time.monotonic() + timeout + stale
    # Staleness is judged by OUR OWN monotonic observation of an unchanging
    # lock, never by comparing filesystem mtime to the wall clock -- clock
    # skew there made a LIVE lock look stale, and breaking a live lock
    # reintroduces exactly the lost-update race this exists to prevent.
    # Owner token (expert deep review 2026-09-27, CRITICAL finding): a slow
    # (not crashed) holder could outlive the stale window, have its lock
    # broken by a waiter, and then its unconditional release would unlink
    # the WAITER's fresh lock -- letting a third writer in concurrently.
    # Each acquirer writes its token into the file; release only unlinks
    # if the file still carries OUR token.
    import uuid as _uuid
    owner_token = f"{os.getpid()}:{_uuid.uuid4().hex}"
    first_seen: tuple[float, float] | None = None  # (observed_at_monotonic, mtime)
    fd = None
    # Windows delete-pending EACCES clears within milliseconds; a lock dir
    # that is GENUINELY unwritable (container uid mismatch, ACL deny) does
    # not. After this many consecutive EACCES-with-no-lock-file attempts
    # (~0.5s), re-raise the real PermissionError instead of stalling to the
    # 40s deadline and masking it as RuntimeError (expert pass 2026-10-05,
    # M2 -- callers catch OSError; a sync tool call must fail fast).
    eacces_no_lock = 0
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, owner_token.encode("ascii"))
            os.fsync(fd)
            break
        except (FileExistsError, PermissionError) as open_exc:
            # PermissionError too (CI windows lane, quality loop run 22):
            # on Windows, O_CREAT|O_EXCL against a lock the holder has just
            # unlinked -- still in delete-pending state -- raises EACCES,
            # not EEXIST. It means the same thing here: busy, retry.
            if _time.monotonic() > deadline:
                raise RuntimeError(
                    f"could not acquire write lock for {target.name} within {timeout}s "
                    "(another writer is holding it; retry shortly)"
                ) from None
            try:
                mtime = lock_path.stat().st_mtime
            except OSError:
                if isinstance(open_exc, PermissionError):
                    eacces_no_lock += 1
                    if eacces_no_lock >= 20:
                        raise open_exc  # unwritable lock dir, not a race
                first_seen = None  # holder released (or delete-pending)
                _time.sleep(0.01)  # bounded by the deadline check above
                continue
            eacces_no_lock = 0
            now = _time.monotonic()
            if first_seen is None or first_seen[1] != mtime:
                first_seen = (now, mtime)  # new holder (or first sighting)
            elif now - first_seen[0] > stale:
                # Held unchanged for `stale`s: crashed writer. Windows may
                # refuse the unlink while the (dead-but-open) handle lingers;
                # treat that as "observe again", never a crash.
                try:
                    lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
                first_seen = None
                continue
            if now > deadline:
                raise RuntimeError(
                    f"could not acquire write lock for {target.name} within {timeout}s "
                    "(another writer is holding it; retry shortly)"
                )
            _time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.close(fd)
        finally:
            # Release ONLY our own lock: if a waiter judged us stale and
            # replaced the file, its token is in there, not ours -- leaving
            # it alone is the difference between one stolen lock and a
            # three-writer pileup.
            try:
                if lock_path.read_text(encoding="ascii", errors="replace") == owner_token:
                    lock_path.unlink(missing_ok=True)
            except OSError:
                pass


# Built-in routes used when no _atlas/routing.yaml exists (2026-08-26): a
# fresh `sextant-init` corpus previously had ZERO valid scopes, so the very
# first remember failed with "Valid scopes: []". These mirror the documented
# canonical scopes; a routing.yaml, once created, replaces them entirely.
_BUILTIN_ROUTES: dict = {
    "identity":    {"target": "identity/user.md",        "create_if_missing": True,
                    "frontmatter_template": {"type": "identity", "status": "active", "priority": "critical"}},
    "preference":  {"target": "feedback/{topic}.md",     "create_if_missing": True,
                    "frontmatter_template": {"type": "feedback", "status": "active", "priority": "normal"}},
    "project":     {"target": "projects/{name}.md",      "create_if_missing": True,
                    "frontmatter_template": {"type": "project", "status": "active", "priority": "normal"}},
    "reference":   {"target": "reference/{topic}.md",    "create_if_missing": True,
                    "frontmatter_template": {"type": "reference", "status": "active", "priority": "normal"}},
    "decision":    {"target": "decisions/{topic}.md",    "create_if_missing": True,
                    "frontmatter_template": {"type": "decision", "status": "active", "priority": "normal"}},
    "constraint":  {"target": "constraints/{topic}.md",  "create_if_missing": True,
                    "frontmatter_template": {"type": "constraint", "status": "active", "priority": "high"}},
    "observation": {"target": "observations/{topic}.md", "create_if_missing": True,
                    "frontmatter_template": {"type": "observation", "status": "active", "priority": "normal"}},
}


def _load_routes() -> dict:
    if not ROUTING_CONFIG.exists():
        return {"routes": _BUILTIN_ROUTES}
    try:
        loaded = yaml.safe_load(ROUTING_CONFIG.read_text(encoding="utf-8")) or {}
        return loaded if loaded.get("routes") else {"routes": _BUILTIN_ROUTES}
    except Exception:
        return {"routes": _BUILTIN_ROUTES}


def _is_reparse_point(path: Path) -> bool:
    """Symlink on any OS, or a name-surrogate reparse point (junction) on Windows; False when absent."""
    try:
        st = path.lstat()
    except OSError:
        return False
    if path.is_symlink():
        return True
    # IsReparseTagNameSurrogate (bit 29): junction 0xA0000003 and symlink 0xA000000C redirect
    # the NAME to another place; cloud placeholders (OneDrive/Dropbox Files On-Demand), dedup,
    # WOF and app-exec links also carry FILE_ATTRIBUTE_REPARSE_POINT but stay where they are
    # and must not be refused (round C 2026-11-10, lane C F1).
    return bool(getattr(st, "st_reparse_tag", 0) & 0x20000000)


def resolve_target(scope: str, for_read: bool = False) -> tuple[Path, dict]:
    """Resolve scope string to (target_path, frontmatter_template).

    Scope examples:
      'identity'
      'preference:editor'
      'project:atlas-memory-plus'
      'reference:acme'

    Returns (absolute_path, frontmatter_template_dict).
    Raises ValueError if scope is unknown or target must exist but doesn't.
    """
    routes = _load_routes().get("routes", {})

    # Parse scope into key + optional topic/name
    if ":" in scope:
        key, _, param = scope.partition(":")
        key = key.strip().lower()  # `Reference:x` is `reference:x` (power-user pass 2026-11-10)
        param = param.strip().lower().replace(" ", "-")
        # Reject path traversal in the parameter component
        # ':' too (audit 2026-11-09, row 25): "c:secret" is a drive-relative
        # path that drops the route's directory, and "notes:hidden" is an
        # NTFS alternate data stream.
        if any(c in param for c in ("/", "\\", "..", ":")) or os.path.isabs(param):
            raise ValueError(
                f"Invalid scope parameter {param!r}: must not contain path separators, "
                "':' or '..'."
            )
        # Windows reserved device names: opening e.g. CON.md addresses the
        # CONSOLE DEVICE and blocks forever (empirically hung the MCP server
        # thread, expert-pass 2026-08-26). Rejected on every OS for corpus
        # portability -- a corpus written on Linux must stay clonable to NTFS.
        if param.split(".")[0] in (
            "con", "prn", "aux", "nul",
            *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
        ):
            raise ValueError(
                f"Invalid scope parameter {param!r}: reserved device name on Windows."
            )
    else:
        key = scope.strip().lower()
        param = ""

    rule = routes.get(key)
    if not rule:
        raise ValueError(
            f"Unknown scope: {scope!r}. "
            f"Valid scopes: {list(routes.keys())}; scopes that take a topic are written "
            f"'reference:<topic>' or 'project:<name>'. "
            f"An optional {ROUTING_CONFIG} overrides these built-in routes."
        )

    template: str = rule.get("target", "")
    if "{topic}" in template:
        if not param:
            raise ValueError(f"Scope '{key}' requires a topic, e.g. '{key}:editor'")
        template = template.replace("{topic}", param)
    elif "{name}" in template:
        if not param:
            raise ValueError(f"Scope '{key}' requires a name, e.g. '{key}:atlas-memory-plus'")
        template = template.replace("{name}", param)

    # YAML templates use "/" (portable); convert to the OS path separator.
    target = MEMORY_ROOT / Path(*template.split("/"))

    # Defense in depth: confirm the path stays inside MEMORY_ROOT even if a
    # routing.yaml template contained relative components. Pure string
    # normalization (normpath/normcase) on purpose: Path.resolve() touches
    # the filesystem and is NON-DETERMINISTIC on Windows while a sibling
    # thread is creating the parent directory (empirically failed 2/6
    # concurrent writers on CI, 2026-08-26). normpath collapses ".."
    # without any filesystem access.
    target_n = os.path.normcase(os.path.normpath(str(target)))
    root_n = os.path.normcase(os.path.normpath(str(MEMORY_ROOT)))
    if not (target_n == root_n or target_n.startswith(root_n + os.sep)):
        raise ValueError(
            f"Routing template {template!r} resolves outside MEMORY_ROOT. "
            "Check routing.yaml for path traversal in target fields."
        )
    # No component between the root and the target may be a symlink or a Windows
    # junction: a `projects` junction took interactive writes OUT of the corpus while the
    # read tool (which resolves) refused the very file it had just written (wave-2
    # 2026-11-09, R2-02 / DATA-04). lstat on existing components only -- deterministic,
    # unlike resolve() of a path a sibling thread is still creating.
    probe = MEMORY_ROOT
    for part in template.split("/"):
        probe = probe / part
        if _is_reparse_point(probe):
            raise ValueError(
                f"Scope {scope!r} resolves through a symlink or junction "
                f"({probe}); the corpus must be a plain directory tree."
            )

    create_ok = rule.get("create_if_missing", False)

    # for_read: a SEARCH scoped to a not-yet-written file is a valid question with an empty
    # answer, not an error; only writers need the create_if_missing check (2026-11-09).
    if not for_read and not target.exists() and not create_ok:
        raise ValueError(
            f"Target file does not exist and create_if_missing=false: {target}. "
            "Create the file first or use a different scope."
        )

    fm_template = dict(rule.get("frontmatter_template", _DEFAULT_FRONTMATTER))
    return target, fm_template


def append_fact(
    target: Path,
    fact: str,
    fm_template: dict,
    source: str = "agent",
    confidence: Optional[float] = None,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    interactive: bool = False,
) -> None:
    """Append fact to target under a dated header. Creates file if needed.

    source: write provenance tag ('agent', 'human-direct', 'suggest-facts', 'telegram').
    confidence: numeric 0-1 score mapped to high/medium/low frontmatter field.
    session_id: the Brain/Claude session that generated this fact (stored in frontmatter).
    task_id: the Brain task that triggered this write (stored in frontmatter).
    """
    # Kill switch at the write PRIMITIVE (audit HIGH-3 / ATL-013): the gate was previously only
    # at the MCP wrapper, so non-MCP callers (suggest-facts, curation, CLI) bypassed it. Gating
    # here covers every caller. Fail-closed when ATLAS_AUTONOMOUS is off/unset -- EXCEPT
    # interactive=True (a user-invoked MCP tool call: explicit consent, allowed by default;
    # ATLAS_INTERACTIVE_WRITES=off restores full gating). Background callers never set it.
    from atlas_plus.autonomous import require_write
    require_write("memory_write", interactive=interactive)
    # Delimiter guard ONLY: keeps a fact from closing the frontmatter block.
    # It is deliberately not a general YAML/Markdown sanitizer -- the body is
    # never fed back through a YAML parser.
    if "---" in fact:
        raise ValueError("Fact cannot contain '---' (YAML frontmatter delimiter).")
    date_str = date.today().isoformat()
    today_header = f"## {date_str}"
    conf_level = _map_confidence(confidence, source)

    # Inline provenance comment: invisible in rendered markdown, parseable by curation tools.
    # Include session/task IDs when provided for full traceability.
    provenance_parts = [f"source:{source}"]
    if session_id:
        provenance_parts.append(f"session:{session_id[:40]}")
    if task_id:
        provenance_parts.append(f"task:{task_id[:24]}")
    tagged_fact = f"{fact}  <!-- {' '.join(provenance_parts)} -->"

    # The whole exists-check/read/modify/write sequence runs under the
    # cross-process lock -- concurrent writers previously lost each other's
    # facts silently.
    with _write_lock(target):
        if not target.exists():
            # New file: build frontmatter with full provenance fields
            slug = target.stem
            fm = {
                "id": slug,
                "title": slug.replace("-", " ").title(),
                **fm_template,
                "updated": date_str,
                "first_observed": date_str,
                "source": source,
                "confidence": conf_level,
            }
            if session_id:
                fm["source_session"] = session_id[:60]
            if task_id:
                fm["source_task"] = task_id[:36]
            fm_lines = ["---"]
            for k, v in fm.items():
                fm_lines.append(f"{k}: {v}")
            fm_lines.append("---\n")
            content = "\n".join(fm_lines) + f"\n{today_header}\n\n{tagged_fact}\n"
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(target, content)
            return

        # Existing file: update provenance fields + append under dated header
        raw = target.read_text(encoding="utf-8")
        raw = _update_frontmatter_field(raw, "updated", date_str)
        raw = _update_frontmatter_field(raw, "last_confirmed", date_str)

        # Append under today's header (or add the header)
        if today_header in raw:
            idx = raw.index(today_header) + len(today_header)
            raw = raw[:idx] + f"\n\n{tagged_fact}" + raw[idx:]
        else:
            raw = raw.rstrip() + f"\n\n{today_header}\n\n{tagged_fact}\n"

        _atomic_write_text(target, raw)


def _mark_search_dirty() -> None:
    """Tell the FTS layer its freshness debounce is void: this process just
    wrote memory, so the next search must sweep (quality loop run 27 --
    keeps remember-then-search exact while bursts of reads skip the
    130ms-per-query scandir sweep). Best-effort: never break a WRITE over
    the search index."""
    try:
        from .fts import mark_dirty
        mark_dirty()
    except Exception:  # pragma: no cover -- import cycle or partial install
        pass


def confirm_fact(target: Path, source: str = "agent") -> bool:
    """Update last_confirmed and updated for a memory file without appending content.

    Called when an agent reads a fact and verifies it is still accurate.
    Returns True if the file exists and was updated.
    """
    if not target.exists():
        return False
    date_str = date.today().isoformat()
    with _write_lock(target):
        raw = target.read_text(encoding="utf-8", errors="replace")
        raw = _update_frontmatter_field(raw, "updated", date_str)
        raw = _update_frontmatter_field(raw, "last_confirmed", date_str)
        _atomic_write_text(target, raw)
    return True


def downgrade_stale_confirmations(
    threshold_days: int = 90,
    memory_root: Optional[Path] = None,
    dry_run: bool = False,
) -> list[dict]:
    """Lower confidence to 'low' for files whose last_confirmed is older than threshold_days.

    Only downgrades files that already have a `confidence` frontmatter field and
    whose current confidence is higher than 'low'. Files without `last_confirmed`
    are skipped (they have never been actively verified, so we don't know their
    validation state and shouldn't penalise them silently).

    Args:
        threshold_days: Files not confirmed in this many days are candidates.
        memory_root:    Override for testing; defaults to MEMORY_ROOT.
        dry_run:        If True, report candidates without writing files.

    Returns:
        List of {path, old_confidence, last_confirmed, age_days} for each downgraded file.
    """
    root = memory_root or MEMORY_ROOT
    if not root.exists():
        return []

    today = date.today()
    cutoff = date.fromisoformat(
        (today.replace(year=today.year - (threshold_days // 365))
         if threshold_days >= 365 else today).__class__.fromordinal(
             today.toordinal() - threshold_days
         ).isoformat()
    )

    import yaml as _yaml
    downgraded: list[dict] = []
    skip_dirs = {"archive", "_sync", "versions", "_atlas", "anti"}

    for md_file in root.rglob("*.md"):
        rel_parts = md_file.relative_to(root).parts
        if any(p in skip_dirs for p in rel_parts):
            continue
        if md_file.name.startswith("_"):
            continue
        try:
            raw = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not raw.startswith("---"):
            continue
        end = raw.find("\n---", 3)
        if end == -1:
            continue
        try:
            fm = _yaml.safe_load(raw[3:end]) or {}
        except Exception:
            continue

        # Only act on files that have been explicitly confirmed at some point
        last_confirmed_raw = fm.get("last_confirmed")
        if not last_confirmed_raw:
            continue

        # Parse last_confirmed date
        try:
            lc_str = str(last_confirmed_raw)[:10]
            lc_date = date.fromisoformat(lc_str)
        except (ValueError, AttributeError):
            continue

        if lc_date >= cutoff:
            continue  # Still fresh enough

        age_days = (today - lc_date).days
        current_conf = str(fm.get("confidence", "")).lower()
        if current_conf == "low" or not current_conf:
            continue  # Already downgraded or no confidence field to update

        rel_path = str(md_file.relative_to(root)).replace("\\", "/")
        downgraded.append({
            "path": rel_path,
            "old_confidence": current_conf,
            "last_confirmed": lc_str,
            "age_days": age_days,
        })

        if not dry_run:
            # ATL-018 / audit HIGH-3 completion: gate the .memory/ frontmatter downgrade write.
            require_autonomous_writes("memory_confidence_downgrade")
            # Re-read under the cross-process lock: the scan-time `raw` may be
            # stale by the time we mutate, and the write must be atomic
            # (Codex audit item 9: this was an unlocked plain write_text).
            try:
                with _write_lock(md_file):
                    fresh = md_file.read_text(encoding="utf-8", errors="replace")
                    updated = _update_frontmatter_field(fresh, "confidence", "low")
                    _atomic_write_text(md_file, updated)
            except OSError:
                pass

    return downgraded


def mark_contradiction(path_a: str, path_b: str) -> tuple[bool, bool]:
    """Record a detected contradiction between two memory files.

    Adds path_b to path_a's contradictions: list and vice versa.
    Both paths are relative to MEMORY_ROOT.
    Returns (updated_a, updated_b).
    """
    updated_a = updated_b = False
    for this_rel, other_rel in [(path_a, path_b), (path_b, path_a)]:
        target = MEMORY_ROOT / this_rel.replace("\\", "/")
        if not target.exists():
            continue
        try:
            with _write_lock(target):
                raw = target.read_text(encoding="utf-8", errors="replace")
                raw = _append_to_frontmatter_list(raw, "contradictions", other_rel)
                _atomic_write_text(target, raw)
            if this_rel == path_a:
                updated_a = True
            else:
                updated_b = True
        except OSError:
            pass
    return updated_a, updated_b
