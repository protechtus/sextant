"""Sextant Sync hub API. FastAPI, loopback bind, per-device bearer auth.

Protocol v1 (design doc section 7):
  GET  /sync/v1/health    liveness only, unauthenticated, no corpus info
  GET  /sync/v1/manifest  policy-filtered manifest for the caller
  GET  /sync/v1/file      one file's bytes + X-Sextant-Sha256
  POST /sync/v1/push      batch changeset with base-hash preconditions
  POST /sync/v1/checkin   device heartbeat

Security posture mirrors the Brain server: bind 127.0.0.1 by default and
put remote exposure behind the operator's ingress (tunnel, Tailscale,
reverse proxy). There is no shared server token: every caller is a paired
device or peer with its own revocable credential, so an empty registry
means every request is 401 and serving is safe by construction.
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response

from atlas_plus import paths as atlas_paths
from atlas_plus.auth import AuthFailureLog, bearer_token
from atlas_plus.sync import PROTOCOL_VERSION, apply, devices, manifest, policy, store

DEFAULT_PORT = 8768  # registered in deploy/ports.md
MAX_PUSH_CHANGES = 500
MAX_FILE_BYTES = 20 * 1024 * 1024  # corpus files are markdown; 20 MB is generous


def _corpus_root() -> Path:
    return atlas_paths.memory_root()


# Manifest cache: hashing every corpus file per request is the API's only
# expensive operation, and check-ins are periodic heartbeats. A few seconds
# of staleness is harmless -- push preconditions are re-verified per file
# against the disk at apply time -- so /manifest and /checkin share one
# short-TTL unfiltered build, filtered per principal on the way out. Pushes
# invalidate immediately.
DEFAULT_MANIFEST_CACHE_TTL_SECONDS = 10.0
_MANIFEST_CACHE: dict = {"path": None, "expires": 0.0, "manifest": None}


def _manifest_ttl() -> float:
    # Read at call time so operators (and tests) can tune or disable via env.
    raw = os.environ.get("ATLAS_SYNC_MANIFEST_TTL", "").strip()
    try:
        return float(raw) if raw else DEFAULT_MANIFEST_CACHE_TTL_SECONDS
    except ValueError:
        return DEFAULT_MANIFEST_CACHE_TTL_SECONDS


def _cached_full_manifest(root: Path) -> dict:
    import time

    now = time.monotonic()
    if (
        _MANIFEST_CACHE["manifest"] is not None
        and _MANIFEST_CACHE["path"] == str(root)
        and now < _MANIFEST_CACHE["expires"]
    ):
        return _MANIFEST_CACHE["manifest"]
    built = manifest.build_manifest(root)
    _MANIFEST_CACHE.update(
        path=str(root), expires=now + _manifest_ttl(), manifest=built
    )
    return built


def _invalidate_manifest_cache() -> None:
    _MANIFEST_CACHE.update(path=None, expires=0.0, manifest=None)


def _manifest_for(root: Path, principal) -> dict:
    full = _cached_full_manifest(root)
    files = policy.filter_manifest(full["files"], principal)
    return {
        "generated_utc": full["generated_utc"],
        "manifest_hash": manifest.files_hash(files),
        "files": files,
    }


# Failed-auth accounting (wave-2 2026-11-09, SEC-04): the hub audited every bad token but
# never slowed the source down. Same primitive and policy as the dashboard: counted per
# real source, loopback without a forwarded source is never locked out.
AUTH_FAILURES = AuthFailureLog()
_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _sync_trusted():
    import os as _os
    from atlas_plus.auth import trusted_peer_matcher
    return trusted_peer_matcher(_os.environ.get("ATLAS_SYNC_TRUSTED_PEERS") or _os.environ.get("ATLAS_MCP_TRUSTED_PEERS", ""))


_LOCK_WARNED: dict[str, float] = {}


def _source_ips(request: Request) -> tuple[str, str]:
    from atlas_plus.auth import forwarded_ip
    peer = request.client.host if request.client else ""
    return peer, forwarded_ip(request.headers, peer, trusted=_sync_trusted())  # a listed proxy names the real device (AG47: one bad client behind compose locked every device)


async def _require_principal(request: Request) -> devices.Device:
    peer_ip, fwd_ip = _source_ips(request)
    _peer_flood = bool(fwd_ip) and peer_ip not in _LOOPBACK and AUTH_FAILURES.failures_via_peer(peer_ip) >= 10 * AUTH_FAILURES.max_failures_per_window  # a listed proxy whose clients rotate names (MN37: 220 guesses, never a 429)
    if ((fwd_ip or peer_ip not in _LOOPBACK) and AUTH_FAILURES.is_rate_limited(fwd_ip or peer_ip)) or _peer_flood:
        import logging as _logging
        import time as _time
        _src = fwd_ip or peer_ip
        # one line per locked CLIENT per window, at most five per peer, then one summary line per peer (AG65: behind one proxy only
        # the first locked client was named, the dashboard's rule since AG59)
        _now = _time.time()
        _win = AUTH_FAILURES.window_seconds
        if _now - _LOCK_WARNED.get(_src, 0.0) > _win:
            _LOCK_WARNED[_src] = _now
            _pk = f"peer:{peer_ip}"
            _named = _LOCK_WARNED.get(_pk, 0)
            if _now - _LOCK_WARNED.get(_pk + ":t", 0.0) > _win:
                _named = 0
                _LOCK_WARNED[_pk + ":t"] = _now
            if _named < 5:
                _LOCK_WARNED[_pk] = _named + 1
                _logging.getLogger("atlas_plus.sync.api").warning("sync hub: %s is locked out after repeated auth failures%s; refusing with 429", _src, f" (via {peer_ip})" if fwd_ip else "")
            elif _named == 5:
                _LOCK_WARNED[_pk] = _named + 1
                _logging.getLogger("atlas_plus.sync.api").warning("sync hub: more than 5 clients via %s are locked out this window; the rest are not named", peer_ip)
            if len(_LOCK_WARNED) > 512:
                _LOCK_WARNED.pop(next(iter(_LOCK_WARNED)))  # the hub left no trace (AG44)
        raise HTTPException(
            status_code=429, detail="too many failed authentication attempts",
            headers={"Retry-After": str(int(AUTH_FAILURES.window_seconds))},
        )
    token = bearer_token(request.headers.get("authorization", ""))
    device = devices.authenticate(token)
    if device is None:
        store.audit(
            "auth_failure",
            client=((lambda p, f: f or p)(*_source_ips(request)) if request.client else "unknown"),  # the forwarded device, not the proxy (MN41)
            path=request.url.path,
        )
        AUTH_FAILURES.record(path=request.url.path, client_ip=peer_ip, cf_ip=fwd_ip,
                             reason="bad_key" if token else "missing_key")
        raise HTTPException(status_code=401, detail="unauthorized")
    return device


def create_app() -> FastAPI:
    _invalidate_manifest_cache()  # fresh app, fresh view (also isolates tests)
    app = FastAPI(title="Sextant Sync", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/sync/v1/health")
    async def health() -> dict:
        return {"status": "ok", "role": "hub", "protocol": PROTOCOL_VERSION}

    @app.get("/health")
    async def health_alias() -> dict:
        """The same liveness answer at the path every other service uses (doctor, monitors)."""
        return {"status": "ok", "role": "hub", "protocol": PROTOCOL_VERSION}

    @app.get("/sync/v1/manifest")
    async def get_manifest(device: devices.Device = Depends(_require_principal)) -> dict:
        principal = device.principal()
        result = dict(_manifest_for(_corpus_root(), principal))
        # The caller learns HOW this hub pairs it. A spoke uses this only to be MORE
        # restrictive (a peer with no local share list refuses to push); local consent
        # is never inferred from it (wave-2 2026-11-09, R2-01).
        result["principal"] = {
            "name": principal.name, "kind": principal.kind,
            "scope": principal.scope, "prefixes": list(principal.prefixes),
        }
        store.audit("manifest", device=device.name, files=len(result["files"]))
        return result

    @app.get("/sync/v1/file")
    async def get_file(
        path: str, device: devices.Device = Depends(_require_principal)
    ) -> Response:
        principal = device.principal()
        target, reason = policy.resolved_target(_corpus_root(), path, principal, "read")
        if reason is not None or target is None:
            store.audit("file_denied", device=device.name, path=path, reason=reason)
            raise HTTPException(status_code=403, detail=reason or "invalid path")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="not found")
        try:
            content = target.read_bytes()
        except OSError:
            # A file can be listed in the manifest yet unreadable by pull
            # time (permissions changed, exclusive lock; the digest cache
            # may also have skipped re-reading it). Clean per-file 409
            # beats an unhandled 500 mid-sync (expert pass 2026-10-05,
            # finding 6 -- the client skips and retries next cycle).
            store.audit("file_unreadable", device=device.name, path=path)
            raise HTTPException(status_code=409, detail="file unreadable on hub")
        sha = apply.current_sha256(_corpus_root(), path) or ""
        store.audit("file_served", device=device.name, path=path)
        return Response(
            content=content,
            media_type="application/octet-stream",
            headers={"X-Sextant-Sha256": sha},
        )

    @app.post("/sync/v1/push")
    async def push(
        body: dict, device: devices.Device = Depends(_require_principal)
    ) -> dict:
        principal = device.principal()
        if principal.scope != "rw":
            raise HTTPException(status_code=403, detail="read-only principal")
        changes = body.get("changes")
        if not isinstance(changes, list) or not changes:
            raise HTTPException(status_code=400, detail="changes must be a non-empty list")
        if len(changes) > MAX_PUSH_CHANGES:
            raise HTTPException(
                status_code=400, detail=f"too many changes (max {MAX_PUSH_CHANGES})"
            )
        root = _corpus_root()
        results = []
        for change in changes:
            rel = str(change.get("path", ""))
            outcome: dict = {"path": rel}
            try:
                raw = base64.b64decode(change.get("content_b64", ""), validate=True)
            except (binascii.Error, ValueError):
                outcome["result"] = "error"
                outcome["detail"] = "content_b64 is not valid base64"
                results.append(outcome)
                continue
            if len(raw) > MAX_FILE_BYTES:
                outcome["result"] = "error"
                outcome["detail"] = "file too large"
                results.append(outcome)
                continue
            announced = change.get("sha256")
            base_sha = change.get("base_sha256")  # None = sender believes file is new
            try:
                if device.kind == "peer":
                    reason = policy.deny_reason(rel, principal, "write", hub=True)
                    if reason is not None:
                        raise apply.SyncPolicyError(reason)
                    landed = apply.inbox(root, device.name, rel, raw, announced)
                    outcome["result"] = "inbox"
                    outcome["landed"] = landed.relative_to(root).as_posix()
                else:
                    # LOAD-BEARING ordering: no `await` between this hash check
                    # and apply_change below -- single asyncio worker means no
                    # interleaving. Adding an await here reintroduces the
                    # conflict race the quarantine guarantee depends on.
                    # Policy BEFORE the hash check: a denied path must come back "denied", never
                    # "conflict", and must never park attacker bytes in the quarantine (W1, 2026-11-09).
                    reason = policy.deny_reason(rel, principal, "write", hub=True)
                    if reason is not None:
                        raise apply.SyncPolicyError(reason)
                    current = apply.current_sha256(root, rel)
                    if current is not None and current != base_sha:
                        parked = apply.quarantine(
                            root, rel, raw,
                            reason=f"push conflict from {device.name}: base "
                            f"{str(base_sha)[:12]} != current {current[:12]}",
                        )
                        outcome["result"] = "conflict"
                        outcome["quarantined"] = parked.relative_to(root).as_posix()
                    else:
                        apply.apply_change(root, rel, raw, announced, principal)
                        outcome["result"] = "applied"
            except apply.SyncPolicyError as exc:
                outcome["result"] = "denied"
                outcome["detail"] = str(exc)
            except apply.SyncIntegrityError as exc:
                outcome["result"] = "error"
                outcome["detail"] = str(exc)
            except Exception as exc:  # noqa: BLE001 - one bad change never 500s the batch (round C, F6)
                outcome["result"] = "error"
                outcome["detail"] = f"internal error: {type(exc).__name__}"
                store.audit("push_exception", device=device.name, path=rel, error=repr(exc)[:200])
            store.audit(
                "push_" + outcome["result"], device=device.name, path=rel
            )
            results.append(outcome)
        _invalidate_manifest_cache()  # the corpus just changed
        return {"results": results}

    @app.post("/sync/v1/checkin")
    async def checkin(
        body: dict, device: devices.Device = Depends(_require_principal)
    ) -> dict:
        entry = store.record_checkin(device.device_id, body if isinstance(body, dict) else {})
        server_manifest = _manifest_for(_corpus_root(), device.principal())
        store.audit("checkin", device=device.name)
        return {
            "acknowledged": True,
            "received_utc": entry["received_utc"],
            "server_manifest_hash": server_manifest["manifest_hash"],
        }

    return app


def main() -> None:
    """Entry point for `atlas-sync serve` (also callable directly)."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Sextant Sync hub server")
    parser.add_argument("--host", default=os.environ.get("ATLAS_SYNC_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("ATLAS_SYNC_PORT", DEFAULT_PORT))
    )
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        active = [d for d in devices.load_devices() if not d.revoked]
        if not active:
            raise SystemExit(
                "refusing to bind a non-loopback host with an empty device registry; "
                "pair a device first (atlas-sync pair <name>)"
            )
    # Single-worker only: the manifest cache + auth audit state are in-process.
    import os as _os
    uvicorn.run(create_app(), host=args.host, port=args.port,
                log_level=_os.environ.get("ATLAS_SYNC_LOG_LEVEL", "warning").lower(), access_log=False, proxy_headers=False)  # every request incl. file paths was logged at INFO (MN35)


if __name__ == "__main__":
    main()
