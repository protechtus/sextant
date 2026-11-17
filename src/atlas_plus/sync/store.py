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


def private_mkdir(d: Path) -> None:
    """Create ``d`` (and, when it lies under the default sync dir, the state root and the sync dir first) with mode
    0700 on POSIX for every directory this call creates. The device registry holds token hashes and the audit trail
    names every device; under umask 022 both were readable by any local account (MN86, sweep 25)."""
    from atlas_plus._fsutil import make_private_dir
    try:
        sd = sync_dir()
        chain = [atlas_paths.state_dir(), sd] if (d == sd or sd in d.parents) else []
    except SystemExit:
        chain = []
    for p in chain:
        make_private_dir(p)
    make_private_dir(d)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, data: dict) -> None:
    private_mkdir(path.parent)
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
    private_mkdir(p.parent)
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


_CORPUS_FILE = "generation-corpus"


def observe_corpus(corpus_hash: str, root: Path | None = None) -> int:
    """Count a corpus change that did not come through a push (MN77).

    Only pushes bumped the generation, so a note the hub itself wrote (its own assistant, `remember`, an editor) and a
    device then pulled was invisible to the restore check: a hub restored from a backup taken before that write showed
    the same count and mark, and every device pulled the older copy over the newer one without a word. The hub records
    the corpus hash it served at each count; a different hash at the same count is a local write and gets a new count.
    A count without a record (a push just bumped it, or a hub older than this build) is recorded, not bumped.
    """
    d = root or sync_dir()
    g = hub_generation(root)
    p = d / _CORPUS_FILE
    try:
        rec_g, rec_h = p.read_text(encoding="ascii").split()
    except (OSError, ValueError):
        rec_g, rec_h = None, None
    if rec_g == str(g) and rec_h == corpus_hash:
        return g
    if rec_g == str(g):
        g = bump_hub_generation(root)
    private_mkdir(d)
    tmp = p.with_name(f"{_CORPUS_FILE}.{os.getpid()}.part")
    tmp.write_text(f"{g} {corpus_hash}", encoding="ascii")
    os.replace(tmp, p)
    return g


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


# -- File history: "older content came back" (SEX-017) ------------------------
# The generation count and its marks live in the state dir, so a corpus restored on its own over a live state
# dir (or a backup tool that saves only the notes) left them untouched; the corpus-hash bump (MN77) then made
# the older corpus look like a NEWER hub edit and every device pulled the older copies with rc 0. The hub now
# remembers the last few contents it held per file: a file whose content is one it held BEFORE its newest is
# reported as older, with the newer hashes, so a device holding one of them refuses to pull it.
_HISTORY_FILE = "file-history.json"
_HISTORY_KEEP = 8


def _load_history(d: Path) -> dict:
    try:
        data = json.loads((d / _HISTORY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_history(d: Path, hist: dict) -> None:
    private_mkdir(d)
    tmp = d / f"{_HISTORY_FILE}.{os.getpid()}.part"
    tmp.write_text(json.dumps(hist, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    os.replace(tmp, d / _HISTORY_FILE)


def observe_files(files: dict, root: Path | None = None) -> dict:
    """Record the content of every file the hub serves; return {rel: [newer sha256, ...]} for each file whose
    content is an OLDER one this hub held (and, when that happened, for each file the hub held and no longer
    has: a restore drops notes born after the backup). Never raises on a bad history file."""
    d = root or sync_dir()
    hist = _load_history(d)
    changed = False
    older: dict[str, list] = {}
    for rel, entry in files.items():
        sha = entry.get("sha256") if isinstance(entry, dict) else None
        if not isinstance(sha, str):
            continue
        seq = hist.get(rel)
        if not isinstance(seq, list) or not seq:
            hist[rel] = [sha]
            changed = True
        elif seq[-1] != sha:
            if sha in seq:
                older[rel] = seq[seq.index(sha) + 1:]
            else:
                hist[rel] = (seq + [sha])[-_HISTORY_KEEP:]
                changed = True
    gone = [rel for rel in hist if rel not in files]
    if older:
        for rel in gone:
            if isinstance(hist[rel], list) and hist[rel]:
                older[rel] = list(hist[rel])
    elif gone:
        for rel in gone:  # a plain delete (archive-not-delete moves a note out): forget it
            hist.pop(rel, None)
        changed = True
    if changed:
        _save_history(d, hist)
    return older


def record_written(written: dict, root: Path | None = None) -> None:
    """Devices pushed these bytes ({rel: sha256}): they are the newest content now, even when one equals an older
    content (a revert). One read and one write per push request."""
    d = root or sync_dir()
    hist = _load_history(d)
    for rel, sha in written.items():
        seq = [s for s in (hist.get(rel) or []) if s != sha] if isinstance(hist.get(rel), list) else []
        hist[rel] = (seq + [sha])[-_HISTORY_KEEP:]
    try:
        _save_history(d, hist)
    except OSError:
        pass  # the push landed; a lost record only risks a false "older" report, which the operator can accept


def accept_current(files: dict, root: Path | None = None) -> list[str]:
    """Operator action on the hub (`sextant-sync accept-restore`): the corpus as it is now is the newest for every
    file reported older, and files the hub no longer has are forgotten. Returns the paths accepted."""
    d = root or sync_dir()
    older = observe_files(files, d)
    hist = _load_history(d)
    for rel in older:
        if rel in files:
            sha = files[rel]["sha256"]
            hist[rel] = ([s for s in hist[rel] if s != sha] + [sha])[-_HISTORY_KEEP:]
        else:
            hist.pop(rel, None)
    if older:
        _save_history(d, hist)
    return sorted(older)


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
        private_mkdir(path.parent)
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
