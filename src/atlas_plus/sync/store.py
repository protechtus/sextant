"""Sync-side state: base manifests, check-ins, and the audit trail.

Everything lives under <state_dir>/sync/ and is derived or operational --
never part of the corpus, never synced itself. Base manifests are the
three-way anchor per remote principal; check-ins are the "device X was last
seen at T with corpus state H" table doctor reads; the audit log is an
append-only jsonl of every boundary event.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from atlas_plus import paths as atlas_paths


def sync_dir() -> Path:
    return atlas_paths.state_dir() / "sync"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def hub_generation(root: Path | None = None) -> int:
    """How many pushes this hub has applied. A hub restored from an older backup reports a lower number than its devices last
    saw, which is how a device tells a rollback from a genuine edit (MN71: a restore with pairings rolled every device back)."""
    p = (root or sync_dir()) / "generation"
    try:
        return int(p.read_text(encoding="ascii").strip() or 0)
    except (OSError, ValueError):
        return 0


def bump_hub_generation(root: Path | None = None) -> int:
    p = (root or sync_dir()) / "generation"
    g = hub_generation(root) + 1
    p.parent.mkdir(parents=True, exist_ok=True)
    _record_generation_mark(p.parent, g)  # the mark first: a crash between the two leaves an unused mark, never an unmarked count
    tmp = p.with_name(f"generation.{os.getpid()}.part")
    tmp.write_text(str(g), encoding="ascii")
    os.replace(tmp, p)
    return g


# A count alone cannot tell a restored hub that has since applied as many pushes as it lost from the hub a device last saw
# (AG79: a second device pushed once after the restore and the first device pulled yesterday's note over today's). Every
# count gets a random mark; a device keeps the mark of the count it recorded and asks for that count's mark again.
_MARKS_FILE = "generation-marks"
_MARKS_KEEP = 4096


def _record_generation_mark(d: Path, g: int) -> str:
    import secrets
    mark = secrets.token_hex(8)
    p = d / _MARKS_FILE
    with open(p, "a", encoding="ascii", newline="\n") as fh:
        fh.write(f"{g} {mark}\n")
    if g % 1024 == 0:  # bounded: keep the newest marks
        try:
            lines = p.read_text(encoding="ascii").splitlines()[-_MARKS_KEEP:]
            tmp = p.with_name(f"{_MARKS_FILE}.{os.getpid()}.part")
            tmp.write_text("\n".join(lines) + "\n", encoding="ascii")
            os.replace(tmp, p)
        except OSError:
            pass
    return mark


def generation_mark(g: int, root: Path | None = None) -> str | None:
    """The mark this hub gave count ``g`` (the newest one, should a restore have reused the count), or None when unknown."""
    if not isinstance(g, int) or g <= 0:
        return None
    try:
        text = ((root or sync_dir()) / _MARKS_FILE).read_text(encoding="ascii")
    except (OSError, ValueError):
        return None
    found = None
    prefix = f"{g} "
    for line in text.splitlines():
        if line.startswith(prefix):
            found = line[len(prefix):].strip() or None
    return found


# -- Base manifests (three-way anchor per remote) ---------------------------

def base_manifest_path(principal_id: str, root: Path | None = None) -> Path:
    safe = "".join(c for c in principal_id if c.isalnum() or c in "-_") or "unknown"
    return (root or sync_dir()) / f"base-{safe}.json"


def save_base_manifest(principal_id: str, manifest: dict, root: Path | None = None) -> Path:
    path = base_manifest_path(principal_id, root)
    _atomic_write_json(path, manifest)
    return path


def load_base_manifest(principal_id: str, root: Path | None = None) -> dict | None:
    path = base_manifest_path(principal_id, root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None  # unreadable base -> caller treats every diff as new/conflict


# -- Check-ins ---------------------------------------------------------------

def checkins_path(root: Path | None = None) -> Path:
    return (root or sync_dir()) / "checkins.json"


def record_checkin(device_id: str, payload: dict, root: Path | None = None) -> dict:
    path = checkins_path(root)
    table: dict = {}
    if path.exists():
        try:
            table = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            table = {}
    entry = {
        "received_utc": _now(),
        "host": str(payload.get("host", ""))[:200],
        "sextant_version": str(payload.get("sextant_version", ""))[:50],
        "manifest_hash": str(payload.get("manifest_hash", ""))[:64],
    }
    table[device_id] = entry
    _atomic_write_json(path, table)
    return entry


def load_checkins(root: Path | None = None) -> dict:
    path = checkins_path(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


# -- Audit trail ---------------------------------------------------------------

def audit_path(root: Path | None = None) -> Path:
    return (root or sync_dir()) / "audit.jsonl"


# Every push/pull/check-in appends a line; without a cap the trail grows for
# the life of the install (sweep finding #5). When the file crosses the cap
# it is trimmed in place to the newest tail -- an audit trail, not an
# archive; long-term history lives in the corpus git journal.
AUDIT_MAX_BYTES = 5 * 1024 * 1024
AUDIT_KEEP_LINES = 5000


def _trim_audit(path: Path) -> None:
    try:
        if path.stat().st_size <= AUDIT_MAX_BYTES:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(True)
        tmp = path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(lines[-AUDIT_KEEP_LINES:]), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def audit(event: str, root: Path | None = None, **fields) -> None:
    """Append one audit line. Never raises; auditing must not break syncing."""
    try:
        path = audit_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": _now(), "event": event, **fields}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
        _trim_audit(path)
    except OSError:
        pass
    try:
        # Mirror onto the live activity feed (Helm map); audit.jsonl stays
        # the authoritative trail.
        from atlas_plus import activity
        activity.record("sync", event, device=str(fields.get("device", "")))
    except Exception:
        pass
