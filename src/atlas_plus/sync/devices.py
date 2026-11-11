"""Device and peer registry: pairing, authentication, revocation.

Pairing is a LOCAL operator action on the hub (CLI), never an HTTP route.
Tokens are 256-bit urlsafe secrets shown exactly once at pair time and
stored only as sha256 hashes; authentication hashes the presented token and
compares with hmac.compare_digest. Revocation keeps the record (audit
trail) and refuses the token from then on.

Registry file: <state_dir>/sync/devices.json (atomic replace on write).
Design: docs/SEXTANT-SYNC-DESIGN-2026-08.md sections 4 (F3) and 6.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from atlas_plus import paths as atlas_paths
from atlas_plus.sync import policy

VALID_KINDS = ("device", "peer")
VALID_SCOPES = ("ro", "rw")


@dataclass
class Device:
    device_id: str
    name: str
    kind: str = "device"
    scope: str = "rw"
    prefixes: tuple[str, ...] = field(default_factory=tuple)
    token_sha256: str = ""
    created_utc: str = ""
    revoked: bool = False

    def principal(self) -> policy.Principal:
        return policy.Principal(
            name=self.name,
            kind=self.kind,
            scope=self.scope,
            prefixes=tuple(self.prefixes),
        )


def registry_path() -> Path:
    return atlas_paths.state_dir() / "sync" / "devices.json"


def _load_raw(path: Path | None = None) -> list[dict]:
    p = path or registry_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("devices", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, OSError):
        # A corrupt registry fails CLOSED: no device authenticates until the
        # operator repairs or re-pairs. Never guess.
        return []


def _save_raw(devices: list[dict], path: Path | None = None) -> None:
    p = path or registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"devices": devices}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def load_devices(path: Path | None = None) -> list[Device]:
    out = []
    for raw in _load_raw(path):
        try:
            raw = dict(raw)
            raw["prefixes"] = tuple(raw.get("prefixes", ()))
            out.append(Device(**raw))
        except TypeError:
            continue  # unknown-shape record: skip, do not crash auth
    return out


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def pair(
    name: str,
    kind: str = "device",
    scope: str = "rw",
    prefixes: tuple[str, ...] = (),
    path: Path | None = None,
) -> tuple[Device, str]:
    """Register a new principal. Returns (device, plaintext_token).

    The plaintext token is returned ONCE and never stored. Peers must come
    with at least one allowlist prefix (default-deny would otherwise make
    the pairing useless and invite over-broad grants later).
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}")
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope must be one of {VALID_SCOPES}")
    if kind == "peer" and not prefixes:
        raise ValueError("a peer requires at least one allowlist prefix")
    for prefix in prefixes:
        if not policy.is_safe_relpath(prefix.rstrip("/")):
            raise ValueError(f"unsafe prefix: {prefix!r}")
    devices = _load_raw(path)
    if any(d.get("name") == name and not d.get("revoked") for d in devices):
        raise ValueError(f"an active principal named {name!r} already exists")

    token = secrets.token_urlsafe(32)
    device = Device(
        device_id=uuid.uuid4().hex[:12],
        name=name,
        kind=kind,
        scope=scope,
        prefixes=tuple(prefixes),
        token_sha256=_token_hash(token),
        created_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        revoked=False,
    )
    record = asdict(device)
    record["prefixes"] = list(device.prefixes)
    devices.append(record)
    _save_raw(devices, path)
    return device, token


def authenticate(token: str, path: Path | None = None) -> Device | None:
    """Return the active Device matching this token, else None. Fail closed."""
    if not token or len(token) > 512:
        return None
    presented = _token_hash(token)
    for device in load_devices(path):
        if device.revoked or not device.token_sha256:
            continue
        if hmac.compare_digest(presented, device.token_sha256):
            return device
    return None


def revoke(name_or_id: str, path: Path | None = None) -> bool:
    """Revoke by name or device_id. Returns True when something was revoked."""
    devices = _load_raw(path)
    hit = False
    for raw in devices:
        if raw.get("name") == name_or_id or raw.get("device_id") == name_or_id:
            if not raw.get("revoked"):
                raw["revoked"] = True
                hit = True
    if hit:
        _save_raw(devices, path)
    return hit
