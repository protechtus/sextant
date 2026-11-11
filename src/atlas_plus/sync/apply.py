"""The only writer in the sync package. Rules-as-code on every apply.

Every change arriving from a remote lands through here:
- policy gate (write direction) before anything touches disk;
- sha256 verified against the announced hash (T6 in the design doc);
- version_before_write copies the pre-change file into versions/;
- writes are atomic (tmp + replace);
- conflicts and foreign (peer) content never overwrite canonical files:
  conflicts quarantine under _atlas/sync-conflicts/, peer pushes land in
  _atlas/sync-inbox/<peer>/ with a provenance sidecar.

Nothing here deletes. Ever. (archive-not-delete; deletes are reported by
the planner and resolved by the operator.)
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from atlas_plus.mcp.memory_server.versioning import version_before_write
from atlas_plus.sync import policy


class SyncPolicyError(Exception):
    """A change was denied by policy. The reason is the message."""


class SyncIntegrityError(Exception):
    """Announced hash did not match the content received."""


def _verify(content: bytes, expected_sha256: str | None) -> str:
    actual = hashlib.sha256(content).hexdigest()
    if expected_sha256 and actual != expected_sha256:
        raise SyncIntegrityError(
            f"content hash {actual[:12]} does not match announced {expected_sha256[:12]}"
        )
    return actual


def _atomic_write(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".sync-tmp")
    with tmp.open("wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)


def current_sha256(root: Path, rel: str) -> str | None:
    """Hash of the file as it exists locally, or None when absent."""
    target = root / rel
    if not target.is_file():
        return None
    return hashlib.sha256(target.read_bytes()).hexdigest()


def apply_change(
    root: Path,
    rel: str,
    content: bytes,
    expected_sha256: str | None = None,
    principal: policy.Principal | None = None,
) -> Path:
    """Apply one incoming change to the corpus. Returns the written path."""
    target, reason = policy.resolved_target(root, rel, principal, "write")
    if reason is not None or target is None:
        raise SyncPolicyError(reason or "invalid path")
    _verify(content, expected_sha256)
    version_before_write(target, root=root)
    _atomic_write(target, content)
    return target


def _collision_free(path: Path) -> Path:
    if not path.exists():
        return path
    n = 1
    while True:
        candidate = path.with_name(f"{path.stem}_v{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _already_landed(base: Path, rel: str, sha: str, dated: bool) -> Path | None:
    """An earlier copy of ``rel`` under ``base`` with these bytes, or None.

    An unresolved conflict is re-reported on every sync until the operator resolves it
    (wave-2 2026-11-09, R2-06); without this check every run would park another identical
    copy. A parked copy always lives at ``<base>/[<day>/]<rel.parent>/<stem>[_vN]<suffix>``,
    so only those directories are listed (O(days), not O(parked files)) and the stem is
    never turned into a glob pattern (``[draft]`` was a character class) (round C
    2026-11-10, lane A F5). The sidecar's recorded sha256 is the oracle, so no file is
    re-read; a sidecar that is not a JSON object is ignored (F6).
    """
    if not base.is_dir():
        return None
    rel_parent = Path(rel).parent
    try:
        parents = ([base / rel_parent] if not dated
                   else [day / rel_parent for day in base.iterdir() if day.is_dir()])
    except OSError:
        return None
    for parent in parents:
        if not parent.is_dir():
            continue
        try:
            names = [n for n in os.listdir(parent) if n.endswith(".meta.json")]
        except OSError:
            continue
        for name in sorted(names):
            sidecar = parent / name
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            if meta.get("original_path") == rel and meta.get("sha256") == sha:
                landed = sidecar.with_name(name[: -len(".meta.json")])
                if landed.is_file():
                    return landed
    return None


def quarantine(root: Path, rel: str, content: bytes, reason: str) -> Path:
    """Park a losing/conflicting copy under _atlas/sync-conflicts/<date>/.

    The canonical file is untouched; the parked copy carries a sidecar
    explaining why it is there. The relpath is validated for safety but NOT
    policy-gated: quarantine is where denied-but-received bytes go.
    """
    if not policy.is_safe_relpath(rel):
        rel = "invalid-path/" + hashlib.sha256(rel.encode()).hexdigest()[:16]
    existing = _already_landed(root / "_atlas" / "sync-conflicts", rel,
                               hashlib.sha256(content).hexdigest(), dated=True)
    if existing is not None:
        return existing
    day = datetime.now(timezone.utc).date().isoformat()
    parked = _collision_free(root / "_atlas" / "sync-conflicts" / day / rel)
    _atomic_write(parked, content)
    meta = {
        "reason": reason,
        "original_path": rel,
        "received_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    parked.with_name(parked.name + ".meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
    )
    return parked


def inbox(
    root: Path,
    peer_name: str,
    rel: str,
    content: bytes,
    expected_sha256: str | None = None,
) -> Path:
    """Land a federation peer's push as PROPOSED content, never canonical.

    Files go to _atlas/sync-inbox/<peer>/<relpath> with a provenance
    sidecar; an operator (or later a curation job) promotes them.
    """
    _verify(content, expected_sha256)
    if not policy.is_safe_relpath(rel):
        raise SyncPolicyError("invalid path")
    safe_peer = "".join(c for c in peer_name if c.isalnum() or c in "-_") or "peer"
    existing = _already_landed(root / "_atlas" / "sync-inbox" / safe_peer, rel,
                               hashlib.sha256(content).hexdigest(), dated=False)
    if existing is not None:
        return existing
    landed = _collision_free(root / "_atlas" / "sync-inbox" / safe_peer / rel)
    _atomic_write(landed, content)
    meta = {
        "source_system": peer_name,
        "original_path": rel,
        "received_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sha256": hashlib.sha256(content).hexdigest(),
        "status": "proposed",
    }
    landed.with_name(landed.name + ".meta.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
    )
    return landed
