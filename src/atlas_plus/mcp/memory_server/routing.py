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
import json as _json
import logging as _logging
import os
import re as _re
from pathlib import Path
from typing import Callable, Optional

import yaml

from .config import MEMORY_ROOT

log = _logging.getLogger(__name__)
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
import unicodedata as _ud
from contextlib import contextmanager


def _fsync_dir(directory: Path) -> None:
    """Make the rename itself durable on POSIX (a power cut, not a crash, could otherwise
    forget it); Windows has no directory fsync and needs none here."""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        # A write that fails half-way (a lone surrogate, disk full) must not leave its temp
        # file in the corpus (AG10, sweep 10).
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    last_err: OSError | None = None
    for delay in (0.0, 0.05, 0.1, 0.2, 0.4):
        if delay:
            _time.sleep(delay)
        try:
            os.replace(tmp, target)
            _fsync_dir(target.parent)
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
        except UnicodeDecodeError as exc:
            raise ValueError(f"{target} is not UTF-8 (byte {exc.start}): re-save the note as UTF-8 and "
                             "retry; nothing was written") from None
        updated = fn(current)
        if updated == current and target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(target, updated)


# Tunable at module level so tests can shrink the windows. Legitimate holds
# last milliseconds; STALE only needs to be far above that.
_LOCK_TIMEOUT = 10.0
_LOCK_STALE = 30.0
_LOCK_ANCIENT = 1800.0   # a lock file last touched over 30 min ago was never a live writer (AG19)


_HOST_ID: str | None = None


def _lock_host() -> str:
    """Identity of THIS process table: hostname plus OS plus the machine id. WSL reports the
    Windows computer name, so a Windows waiter looked up a Linux pid in its own table and broke
    a LIVE WSL lock (AG22, sweep 12)."""
    global _HOST_ID
    if _HOST_ID is not None:
        return _HOST_ID
    import socket as _socket
    try:
        name = _re.sub(r"[^A-Za-z0-9.-]", "", _socket.gethostname())[:40] or "host"
    except OSError:
        name = "host"
    machine = ""
    try:
        if os.name != "nt":
            for cand in ("/etc/machine-id", "/var/lib/dbus/machine-id", "/proc/sys/kernel/random/boot_id"):
                try:
                    machine = Path(cand).read_text(encoding="ascii", errors="ignore").strip()[:8]
                    if machine:
                        break
                except OSError:
                    continue
    except Exception:  # noqa: BLE001
        machine = ""
    _HOST_ID = f"{name}.{os.name}{'.' + machine if machine else ''}"[:60]
    return _HOST_ID


def _pid_alive(pid: int) -> bool:
    """Is a process with this id running on this machine? Unknown counts as alive."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return ctypes.get_last_error() == 5      # access denied: it exists
            try:
                code = ctypes.c_ulong()
                if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259             # STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _proc_start(pid: int) -> str:
    """An identifier of WHEN this process started (empty when unknown). A restarted container runs
    the server as PID 1 again, so a lock left by the crashed PID 1 looked alive and the first write
    waited 30 s (MN18, sweep 11); the start time tells the two apart."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = k32.OpenProcess(0x1000, False, pid)
            if not handle:
                return ""
            try:
                ft = (wintypes.FILETIME * 4)()
                if k32.GetProcessTimes(handle, ctypes.byref(ft[0]), ctypes.byref(ft[1]),
                                       ctypes.byref(ft[2]), ctypes.byref(ft[3])):
                    return str((ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime)
                return ""
            finally:
                k32.CloseHandle(handle)
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        # field 22 (1-based) is starttime in clock ticks since boot; the comm field may hold spaces
        return stat.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        return ""


def _break_lock(lock_path: Path) -> bool:
    """Remove a lock judged dead by claiming it first: the file is renamed to a unique name and
    the renamed file deleted, so a second waiter that judged the same lock dead finds nothing to
    rename and cannot delete the fresh lock a first waiter has since taken (AG22, sweep 12)."""
    import uuid as _uuid
    claimed = lock_path.with_name(lock_path.name + f".dead-{_uuid.uuid4().hex[:8]}")
    try:
        os.replace(lock_path, claimed)
    except OSError:
        return False
    try:
        claimed.unlink(missing_ok=True)
    except OSError:
        pass
    return True


def _lock_holder_dead(lock_path: Path) -> bool:
    """True when the lock's token names a process on THIS machine that no longer runs. A lock
    left by a killed writer stalled the next write for 30 s with no word (AG15, sweep 11)."""
    try:
        token = lock_path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return False
    parts = token.strip().split(":")
    if len(parts) < 3 or parts[2] != _lock_host():
        return False   # another machine's lock (shared corpus): only the stale rule applies
    try:
        pid = int(parts[0])
    except ValueError:
        return False
    if not _pid_alive(pid):
        return True
    if len(parts) >= 4 and parts[3]:
        started = _proc_start(pid)
        if started and started != parts[3]:
            return True   # the pid was reused by a later process (a restarted container's PID 1)
    return False


_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_SUPERSCRIPT_DIGITS = "\u00b9\u00b2\u00b3"
MAX_FACT_CHARS = 65_536


def _is_reserved_device_name(name: str) -> bool:
    """Windows device names, which open the DEVICE instead of a file: CON hung the server
    (expert pass 2026-08-26), CONIN$ hung it again (AG10, sweep 10), and COM followed by a
    superscript digit is reserved too. The comparison ignores an extension and trailing spaces."""
    base = name.split(".")[0].rstrip(" ").lower()
    if base in ("con", "prn", "aux", "nul", "conin$", "conout$"):
        return True
    if len(base) == 4 and base[:3] in ("com", "lpt") and (base[3].isdigit() or base[3] in _SUPERSCRIPT_DIGITS):
        return base[3] != "0"
    return False


def _looks_like_short_name(name: str) -> bool:
    for i, c in enumerate(name):
        if c == "~" and i + 1 < len(name) and name[i + 1].isdigit():
            return True
    return False


# A plain YAML scalar that every parser reads back as the same string: starts with a letter
# or underscore, then letters, digits, space, dot, underscore, dash. Anything else (a leading
# `[`, `{`, `&`, `@`, `` ` ``, `!`, `%`, `'`, `-`, `~`, a ` #` comment, a number or date) is
# written double-quoted. `reference:null` made a note whose id read back as None and
# `reference:[a` one whose frontmatter did not parse at all (MN8, sweep 10).
_PLAIN_SCALAR_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_ .\-]*$")
_YAML_WORDS = frozenset({"null", "~", "true", "false", "yes", "no", "on", "off", "y", "n"})
# Letters by category that render as nothing: the Hangul fillers and the Braille blank.
_BLANK_LOOKING = frozenset({"\u115f", "\u1160", "\u3164", "\uffa0", "\u2800"})
MAX_NAME_BYTES = 200   # UTF-8 bytes or UTF-16 units: 255 on NTFS/ext4 less `.md` and the temp suffix


def _yaml_scalar(value) -> str:
    s = str(value)
    if s.lower() in _YAML_WORDS or " #" in s or not _PLAIN_SCALAR_RE.match(s) or s != s.strip():
        return _json.dumps(s, ensure_ascii=False)
    return s


def _scrub_controls(text: str) -> str:
    """Drop C0/C1 control characters except tab, newline and carriage return, and replace lone
    surrogates: ESC/BEL/OSC sequences stored in a note reached the operator's terminal through
    `sextant search`, and a lone surrogate failed deep inside the temp-file write (AG10)."""
    out = []
    for c in text:
        cat = _ud.category(c)
        if cat == "Cc" and c not in "\t\n\r":
            continue
        if cat == "Cs":
            out.append("\ufffd")
            continue
        out.append(c)
    return "".join(out)


def _lock_key(target: Path) -> str:
    r"""Stable cross-process identity for ``target`` (audit 2026-11-09, row 17a).

    Keyed on the corpus-relative path computed from the RESOLVED memory root
    and the resolved target, so two processes that spell the same corpus
    differently (8.3 short name vs long name, junction, subst drive, case)
    still take the same lock. A target outside the corpus falls back to its
    resolved, case-normalised absolute path.

    Windows (2026-11-12, the one-in-twenty fact loss on windows-latest CI): for a path
    that does not exist yet, ``ntpath.realpath`` strips its ``\\?\`` prefix only when a
    second probe fails with the SAME error code as the first. A concurrent writer creating
    the scope's directory between the two probes turns PATH_NOT_FOUND into FILE_NOT_FOUND,
    so one thread gets ``\\?\C:\...``, ``relative_to`` fails, and that writer takes a
    DIFFERENT lock file from everybody else: two holders, one lost update. The prefix and
    the case are therefore normalised here before the comparison, and the key no longer
    depends on which error a race produced.
    """
    resolved = _canonical(target)
    try:
        rel = resolved.relative_to(_canonical(MEMORY_ROOT))
        return "rel:" + rel.as_posix().lower()
    except (OSError, ValueError):
        return "abs:" + str(resolved).lower()


def _canonical(path: Path) -> Path:
    """realpath with the Windows extended-length prefix removed and the case folded."""
    try:
        s = os.path.realpath(path)
    except (OSError, ValueError):
        s = os.path.abspath(path)
    if os.name == "nt":
        if s.startswith("\\\\?\\UNC\\"):
            s = "\\" + s[7:]           # \\?\UNC\server\share -> \\server\share
        elif s.startswith("\\\\?\\"):
            s = s[4:]                  # \\?\C:\dir -> C:\dir
        s = os.path.normcase(s)
    return Path(s)


@contextmanager
def _write_lock(target: Path, timeout: float | None = None, stale: float | None = None):
    timeout = _LOCK_TIMEOUT if timeout is None else timeout
    stale = _LOCK_STALE if stale is None else stale
    lock_dir = MEMORY_ROOT / "_atlas" / ".locks"
    if not _atlas_dir_ok():
        raise ValueError(f"{MEMORY_ROOT / '_atlas'} is a link that leaves the corpus; the corpus must be a "
                         "plain directory tree (nothing was written)")
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / (_hashlib.sha256(_lock_key(target).encode("utf-8")).hexdigest()[:16] + ".lock")
    if lock_path.is_dir():
        # A planted directory stalled every write to the note for 40 s and then failed (AG22).
        raise ValueError(f"{lock_path} is a directory where the write lock file goes; remove it")
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
    owner_token = f"{os.getpid()}:{_uuid.uuid4().hex}:{_lock_host()}:{_proc_start(os.getpid())}"
    first_seen: tuple[float, float] | None = None  # (observed_at_monotonic, mtime)
    last_holder_check = 0.0
    waited_notice = 0.0
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
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
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
            # Probe the holder only once the lock has sat unchanged for 2 s: reading the file
            # while a live holder releases it makes the unlink fail on Windows (no delete
            # sharing on a read handle), which left the lock behind for the full stale window.
            if (first_seen is not None and first_seen[1] == mtime and now - first_seen[0] >= 2.0
                    and now - last_holder_check >= 1.0):
                last_holder_check = now
                if _lock_holder_dead(lock_path):
                    log.info("write lock on %s was left by a process that no longer runs; clearing it",
                             target.name)
                    _break_lock(lock_path)
                    first_seen = None
                    continue
            if first_seen is not None and now - first_seen[0] > 5 and now - waited_notice > 5:
                waited_notice = now
                log.warning("still waiting for the write lock on %s (held for %.0f s by another writer; "
                            "a lock a crashed writer left is broken after %.0f s)",
                            target.name, now - first_seen[0], stale)
            if _time.time() - mtime > _LOCK_ANCIENT:
                # A committed or copied lock from another machine (a corpus shared through git)
                # cost every write 30 s; no writer holds a lock for half an hour, so a lock that
                # old is broken at once even across a clock skew of minutes (AG19, sweep 12).
                log.info("write lock on %s is %.0f min old; breaking it", target.name, (_time.time() - mtime) / 60)
                _break_lock(lock_path)
                first_seen = None
                continue
            if first_seen is None or first_seen[1] != mtime:
                first_seen = (now, mtime)  # new holder (or first sighting)
            elif now - first_seen[0] > stale:
                # Held unchanged for `stale`s: crashed writer. Windows may
                # refuse the unlink while the (dead-but-open) handle lingers;
                # treat that as "observe again", never a crash.
                _break_lock(lock_path)
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
            for _attempt in range(300):   # up to 3 s: a reader holding the file for 0.8 s leaked the lock (MN16)
                try:
                    if lock_path.read_text(encoding="ascii", errors="replace") == owner_token:
                        lock_path.unlink(missing_ok=True)
                    break
                except PermissionError:
                    # A waiter's read handle (Windows: no delete sharing) refuses the unlink for
                    # a moment; a lock left behind here cost every writer the stale window.
                    _time.sleep(0.01)
                    if _attempt == 299:
                        log.warning("could not release the write lock on %s (another process held the lock "
                                    "file open); the next write clears it after the stale window", target.name)
                except OSError:
                    break


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


_CORPUS_INTERNAL_DIRS = frozenset({"_atlas", "versions", "_sync", "archive", ".locks"})


def _truthy(value) -> bool:
    """YAML `create_if_missing: "false"` (a string) counted as true (AG16, sweep 11)."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(value)


def _atlas_dir_ok() -> bool:
    """The corpus's `_atlas` directory must be a plain directory inside the corpus: through a
    junction it loaded routes from outside and created locks outside (AG16, sweep 11)."""
    from atlas_plus._fsutil import inside, is_link
    atlas_dir = MEMORY_ROOT / "_atlas"
    if not atlas_dir.exists():
        return True
    return not is_link(atlas_dir) and inside(atlas_dir, MEMORY_ROOT)


def _load_routes() -> dict:
    if not ROUTING_CONFIG.exists():
        return {"routes": _BUILTIN_ROUTES}
    if not _atlas_dir_ok():
        log.warning("%s is ignored: _atlas resolves outside the corpus (a link); built-in routes apply",
                    ROUTING_CONFIG)
        return {"routes": _BUILTIN_ROUTES}
    try:
        loaded = yaml.safe_load(ROUTING_CONFIG.read_text(encoding="utf-8")) or {}
    except Exception:
        return {"routes": _BUILTIN_ROUTES}
    routes = loaded.get("routes") if isinstance(loaded, dict) else None
    if not isinstance(routes, dict) or not routes:
        return {"routes": _BUILTIN_ROUTES}
    # A rule that is not a mapping, or whose target is not a string, used to raise
    # AttributeError/TypeError out of every write (AG16, sweep 11): such rules are dropped.
    clean: dict = {}
    for name, rule in routes.items():
        if not isinstance(name, str) or not isinstance(rule, dict) or not isinstance(rule.get("target"), str):
            log.warning("routing.yaml: route %r is not a mapping with a string target; ignored", name)
            continue
        fm = rule.get("frontmatter_template", None)
        if fm is not None and not isinstance(fm, dict):
            log.warning("routing.yaml: route %r has a frontmatter_template that is not a mapping; ignored", name)
            rule = {k: v for k, v in rule.items() if k != "frontmatter_template"}
        clean[name] = rule
    if not clean:
        return {"routes": _BUILTIN_ROUTES}
    out = dict(loaded)
    out["routes"] = clean
    return out


def _check_template_parts(template: str, scope: str) -> None:
    """Every component of the resolved route target is checked the way a scope name is: a
    route that named `reference/CON.md` opened the console device and froze the MCP server for
    good; `{topic}/x.md` with `evil:_atlas` wrote into the index directory (AG16, sweep 11)."""
    parts = template.split("/")
    if not template.lower().endswith(".md") or len(parts) < 1 or not parts[-1][:-3]:
        raise ValueError(f"Route for {scope!r} must name a .md file inside the corpus (target {template!r}).")
    if template.startswith("/") or template.startswith("\\"):
        raise ValueError(f"Route for {scope!r} must be a relative path inside the corpus (target {template!r}).")
    for i, part in enumerate(parts):
        if part in ("", ".", ".."):
            raise ValueError(f"Route for {scope!r} has an empty, '.' or '..' component (target {template!r}).")
        if ":" in part or "\\" in part:
            raise ValueError(f"Route for {scope!r}: ':' and '\\' cannot appear in a target component "
                             f"(target {template!r}).")
        if part.endswith(".") or part.endswith(" "):
            raise ValueError(f"Route for {scope!r}: a component must not end with a dot or a space "
                             f"(Windows drops it; target {template!r}).")
        if _is_reserved_device_name(part):
            raise ValueError(f"Route for {scope!r}: {part!r} is a reserved device name on Windows "
                             f"(target {template!r}).")
        if any(_ud.category(c) in _FORBIDDEN_CATEGORIES for c in part) or any(c in part for c in '*?"<>|'):
            raise ValueError(f"Route for {scope!r}: a target component carries characters a file name "
                             f"cannot (target {template!r}).")
    if parts[0].lower() in _CORPUS_INTERNAL_DIRS or not template.endswith(".md"):
        # Case variants (`_ATLAS/`, `Versions/`) and `.MD` wrote notes nothing indexes (MN16).
        if not template.endswith(".md"):
            raise ValueError(f"Route for {scope!r} must end in lowercase `.md` (target {template!r}).")
        raise ValueError(f"Route for {scope!r} targets {parts[0]}/, which is Sextant's own directory "
                         f"(index, history, locks): notes there are never searched (target {template!r}).")


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
        # NFC first: a decomposed 'café' is the same note as the composed one, not a second
        # file that displays identically (AG10, sweep 10).
        param = _ud.normalize("NFC", param.strip()).lower().replace(" ", "-")
        # Reject path traversal in the parameter component
        # ':' too (audit 2026-11-09, row 25): "c:secret" is a drive-relative
        # path that drops the route's directory, and "notes:hidden" is an
        # NTFS alternate data stream.
        if any(c in param for c in ("\\", "..", ":")) or os.path.isabs(param) or param.startswith("/"):
            raise ValueError(
                f"Invalid scope parameter {param!r}: must not contain '\\', ':' or '..' or start with '/'."
            )
        if "/" in param:
            # `reference:obsidian/README` addresses a note in a subfolder (an imported vault, MN15);
            # every segment is checked below as a name and again as a path component.
            segments = param.split("/")
            if any(seg in ("", ".", "..") or seg.startswith(".") for seg in segments):
                raise ValueError(f"Invalid scope parameter {param!r}: an empty, '.', '..' or hidden segment.")
            if any(seg.lower() in _CORPUS_INTERNAL_DIRS for seg in segments):
                # `reference:_atlas/x` was written and searchable but could never be read (AG22).
                raise ValueError(f"Invalid scope parameter {param!r}: a segment names one of Sextant's own "
                                 "directories (_atlas, versions, archive, _sync).")
        # Windows reserved device names: opening e.g. CON.md addresses the
        # CONSOLE DEVICE and blocks forever (empirically hung the MCP server
        # thread, expert-pass 2026-08-26). Rejected on every OS for corpus
        # portability -- a corpus written on Linux must stay clonable to NTFS.
        if _is_reserved_device_name(param):
            raise ValueError(
                f"Invalid scope parameter {param!r}: reserved device name on Windows."
            )
        # Control characters break the YAML frontmatter (a newline in the id line) and cannot
        # exist on NTFS; a name that is only dots or starts with one is a hidden or parent entry;
        # a very long name fails deep inside the temp-file write with an internal file name
        # (MN3, pipeline sweep 2026-11-11).
        # Every Unicode control, format, separator and unassigned code point: NEL (U+0085) and
        # the line/paragraph separators are line breaks to YAML and broke the frontmatter; a
        # zero-width joiner or a right-to-left override makes a name that displays as another
        # (AG10, sweep 10). ord < 32 and 127 are inside category Cc.
        if any(_ud.category(c) in _FORBIDDEN_CATEGORIES or c in _BLANK_LOOKING for c in param):
            raise ValueError(f"Invalid scope parameter {param!r}: control or invisible characters are not allowed.")
        if param and not any(c.isalnum() for c in param):
            # `reference:-` or a Braille blank: a note nobody can name back (MN8, sweep 10).
            raise ValueError(f"Invalid scope parameter {param!r}: a name needs at least one letter or digit.")
        if any(c in param for c in '*?"<>|'):
            raise ValueError(f"Invalid scope parameter {param!r}: the characters * ? \" < > | cannot be used in a file name.")
        if _looks_like_short_name(param):
            # `PROJEC~1` is NTFS's 8.3 alias of another file: writing through it renamed the
            # long-named note (MN12, sweep 10). No real name needs a tilde followed by a digit.
            raise ValueError(f"Invalid scope parameter {param!r}: a tilde followed by a digit is a Windows "
                             "short-name alias of another file.")
        if param and (param.strip(".") == "" or param.startswith(".")):
            raise ValueError(f"Invalid scope parameter {param!r}: must not start with a dot.")
        if param and (param.endswith(".") or any(seg.endswith(".") for seg in param.split("/"))):
            raise ValueError(f"Invalid scope parameter {param!r}: must not end with a dot (Windows strips it).")
        if any(_is_reserved_device_name(seg) for seg in param.split("/")):
            raise ValueError(f"Invalid scope parameter {param!r}: reserved device name on Windows.")
        if len(param) > 120:
            raise ValueError(f"Invalid scope parameter: {len(param)} characters; the limit is 120.")
        encoded = max(len(param.encode("utf-8")), len(param.encode("utf-16-le")) // 2)
        if encoded > MAX_NAME_BYTES:
            # 120 emoji are 480 UTF-8 bytes and 240 UTF-16 units: the write failed deep inside the
            # temp file with an OSError naming an internal path (MN8, sweep 10).
            raise ValueError(f"Invalid scope parameter: {len(param)} characters encode to {encoded} bytes; "
                             f"a file name holds {MAX_NAME_BYTES}. Use a shorter name.")
    else:
        key = scope.strip().lower()
        param = ""

    rule = routes.get(key)
    if rule and param and not any(ph in str(rule.get("target", "")) for ph in ("{topic}", "{name}")):
        # `identity:foo` used to drop the topic silently and write identity/user.md (AG10 F11).
        raise ValueError(f"Scope {key!r} takes no topic ({scope!r} would be ignored); write {key!r} alone.")
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

    _check_template_parts(template, scope)
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

    create_ok = _truthy(rule.get("create_if_missing", False))

    # for_read: a SEARCH scoped to a not-yet-written file is a valid question with an empty
    # answer, not an error; only writers need the create_if_missing check (2026-11-09).
    if not for_read and not target.exists() and not create_ok:
        raise ValueError(
            f"Target file does not exist and create_if_missing=false: {target}. "
            "Create the file first or use a different scope."
        )

    fm_raw = rule.get("frontmatter_template", _DEFAULT_FRONTMATTER)
    fm_template = dict(fm_raw) if isinstance(fm_raw, dict) else dict(_DEFAULT_FRONTMATTER)
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
    pre_write=None,
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
    if len(fact) > MAX_FACT_CHARS:
        raise ValueError(f"Fact too long: {len(fact):,} characters; the limit is {MAX_FACT_CHARS:,}. "
                         "Store the document as a file and remember its path instead.")
    # The scrub runs FIRST: `-\x07--` passed the guard and the scrub then assembled the
    # delimiter it was meant to block (MN8, sweep 10).
    fact = _scrub_controls(fact)
    if _re.search(r"(?m)^\s*---", fact):
        # Only a LINE that starts with --- can close the frontmatter; a scene break mid-sentence
        # is a writer's fact (MN14, sweep 11).
        raise ValueError("Fact cannot contain a line starting with '---' (the YAML frontmatter delimiter); "
                         "mid-sentence dashes are fine.")
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
        if pre_write is not None and target.exists():
            # The version copy is taken HERE, under the lock: taken outside it, concurrent
            # writers copied the same pre-image and the history lost every state in between
            # (MN3, pipeline sweep 2026-11-11).
            pre_write(target)
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
                if k in ("id", "title", "source_session", "source_task"):
                    v = _yaml_scalar(v)
                fm_lines.append(f"{k}: {v}")
            fm_lines.append("---\n")
            content = "\n".join(fm_lines) + f"\n{today_header}\n\n{tagged_fact}\n"
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(target, content)
            return

        # Existing file: update provenance fields + append under dated header
        try:
            raw = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            # The old message was the codec's own line with no file name (AG17, sweep 11).
            raise ValueError(f"{target} is not UTF-8 (byte {exc.start}): re-save the note as UTF-8 and "
                             "retry; nothing was written") from None
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
        if "decisions" in target.parts:
            # Confirming a decision is reviewing it (the decisions command keyed on last_reviewed, AG20).
            raw = _update_frontmatter_field(raw, "last_reviewed", date_str)
        _atomic_write_text(target, raw)
    return True


def downgrade_stale_confirmations(
    threshold_days: int = 90,
    memory_root: Optional[Path] = None,
    dry_run: bool = False,
    interactive: bool = False,
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

        # A note never confirmed counts from its last edit (or first observation): skipping it
        # hid every planted never-confirmed fact from the curator (AG20, sweep 12).
        last_confirmed_raw = fm.get("last_confirmed")
        never = not last_confirmed_raw
        ref_raw = last_confirmed_raw or fm.get("updated") or fm.get("first_observed")
        if not ref_raw:
            continue
        try:
            lc_str = str(ref_raw)[:10]
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
            "last_confirmed": "never" if never else lc_str,
            "age_days": age_days,
        })

        if not dry_run:
            # A human at the terminal passes interactive=True; the nightly job is gated by
            # ATLAS_AUTONOMOUS (the recommended fix used to raise under the default config, AG20).
            from atlas_plus.autonomous import require_write
            require_write("memory_confidence_downgrade", interactive=interactive)
            try:
                from .versioning import version_before_write as _vbw
                _vbw(md_file, root=root)
            except Exception:  # noqa: BLE001
                pass
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
