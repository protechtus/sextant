"""Sync client: a spoke talking to a hub over the v1 protocol.

Thin by design: HTTP verbs plus one composite sync_once() that does the
plan/pull/push loop with the local corpus. Transport is injectable so tests
run the real client against the real app in-process (httpx.ASGITransport);
production passes a base_url and lets httpx do HTTPS through whatever
ingress fronts the hub.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import httpx

from atlas_plus.sync import apply, manifest, policy, store


class SyncClientError(Exception):
    """Transport- or protocol-level failure talking to the hub."""


class SyncAuthError(SyncClientError):
    """The hub rejected the token mid-run: stop instead of one error per file (MN65)."""



def normalize_share(share: Sequence[str] | None) -> tuple[str, ...]:
    """Corpus-relative prefixes this system agrees to exchange, as ``dir/`` strings."""
    out: list[str] = []
    for raw in share or ():
        for item in str(raw).split(","):
            item = item.strip().strip("/")
            if not item:
                continue
            if not policy.is_safe_relpath(item):
                hint = " (a share is a directory prefix such as reference/shared, not a scope)" if ":" in item else ""
                raise SyncClientError(f"invalid share prefix {raw!r}{hint}")
            out.append(item + "/")
    return tuple(dict.fromkeys(out))


def _hub_policy(remote: dict) -> policy.Principal | None:
    """The allowlist the HUB applies to this caller, as it advertises it in the manifest.

    Intersected with the local share so a file the hub would deny is never read or sent
    (round C 2026-11-10, lane A F3: inside the local share but outside the hub's
    allowlist crossed the wire on every sync, only to come back `denied`). A hub that
    does not identify the pairing yields None; plan() then refuses unless a share is
    declared (F7).
    """
    principal = remote.get("principal") if isinstance(remote, dict) else None
    if not isinstance(principal, dict):
        return None
    prefixes = principal.get("prefixes") or ()
    if not isinstance(prefixes, (list, tuple)):
        prefixes = ()
    return policy.Principal(
        name=str(principal.get("name") or "hub"),
        kind="peer" if principal.get("kind") == "peer" else "device",
        scope="ro" if principal.get("scope") == "ro" else "rw",
        prefixes=tuple(str(p) for p in prefixes),
    )


def _visible(rel: str, local_policy, hub_policy) -> bool:
    return (policy.deny_reason(rel, local_policy, "read") is None
            and (hub_policy is None or policy.deny_reason(rel, hub_policy, "read") is None))


def _detail(response: httpx.Response) -> str:
    """A hub's error detail, or the start of a non-JSON body (an ingress page), never a crash."""
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("detail"):
            return str(data["detail"])[:200]
    except ValueError:
        pass
    text = (response.text or "").strip().replace("\n", " ")
    return text[:80] or f"HTTP {response.status_code}"


def _url_hint(base_url: str, response: httpx.Response) -> str:
    """verify's URL hints for the sync client (AG82): a redirect, a path suffix, plain HTTP to TLS."""
    from urllib.parse import urlsplit
    code = response.status_code
    loc = (response.headers.get("Location") or "")[:120]
    parts = urlsplit(base_url)
    root = f"{parts.scheme}://{parts.netloc}" if parts.netloc else base_url
    if code in (301, 302, 303, 307, 308):
        if parts.scheme == "http" and loc.startswith("https://"):
            return (f" (the hub redirects plain HTTP to HTTPS ({loc}); the token was not sent there: "
                    "use the https:// address of the hub, with its TLS port)")
        return f" (the hub redirected to {loc or '?'}; the token was not sent there: check the --hub address)"
    if code == 404 and parts.path.rstrip("/"):
        return (f" (--hub is the hub's root address, without a path: use {root}; the client adds "
                "/sync/v1/... itself, and /mcp is the memory server, not the hub)")
    if code == 400 and parts.scheme == "http" and "https" in (response.text or "")[:300].lower():
        return f" (this port speaks HTTPS: use https://{parts.netloc})"
    return ""


def _share_principal(share: tuple[str, ...]) -> policy.Principal | None:
    """The LOCAL policy for an outbound share: only these prefixes may leave or arrive.

    Modelled as a peer principal so the same deny_reason() that fences a foreign
    caller on the hub fences this system's own egress (wave-2 2026-11-09, R2-01).
    """
    if not share:
        return None
    return policy.Principal(name="local-share", kind="peer", scope="rw", prefixes=share)


@dataclass
class SyncReport:
    """What one sync_once() actually did."""

    pulled: list[str] = field(default_factory=list)
    pushed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    remote_deleted: list[str] = field(default_factory=list)
    local_deleted: list[str] = field(default_factory=list)
    # Files the plan could see on this side after the share and the hub's allowlist
    # were applied (0 with a --share that matches nothing: worth telling the operator).
    local_files: int = 0
    # The hub's allowlist for this caller, as it advertised it (empty = everything).
    hub_prefixes: list[str] = field(default_factory=list)
    # Conflicts the HUB detected on a push (it parked our copy; its own copy arrives next sync).
    push_conflicts: list[str] = field(default_factory=list)
    # Where each pull conflict's hub copy was parked locally (rel -> absolute path).
    parked: dict = field(default_factory=dict)
    # Files this peer has proposed (hub inbox) whose local copy is unchanged since: not
    # re-sent, not pulled over, reported as "awaiting the hub's review".
    proposed: list[str] = field(default_factory=list)
    # Edited here, deleted on the hub (also in remote_deleted): kept here, not pushed (AG82).
    kept_local_edits: list[str] = field(default_factory=list)
    # Not sent: over the hub's per-file limit, or this exact copy was already refused (AG88).
    too_large: list[str] = field(default_factory=list)
    # The hub reports an older state than this device last saw (restored from a backup).
    hub_restored: bool = False

    def summary(self) -> str:
        return (
            f"pulled {len(self.pulled)}, pushed {len(self.pushed)}, "
            f"conflicts {len(self.conflicts)}, denied {len(self.denied)}, "
            f"errors {len(self.errors)}"
        )


RESTORED_HUB = ("the hub reports an older state than this device last synced (it was restored from a backup?): "
                "nothing was pulled over your copies; every difference is parked as a conflict to review")
RESTORED_HUB_AHEAD = ("the hub reports an older state than this device last synced (it was restored from a backup?): "
                      "a sync pulls nothing over your copies and parks every difference as a conflict to review")


def _check_hub_url(base_url: str) -> None:
    """`--hub 127.0.0.1:8768` reached httpx as a URL with no scheme (or `localhost:` as the scheme) and the
    operator got httpx's own sentence about protocols (AG88). One rule for plan, sync and checkin."""
    if not base_url:
        return
    import re as _re
    from urllib.parse import urlsplit
    raw = base_url
    if raw != raw.strip() or raw[:1] in "\"'" or raw[-1:] in "\"'":
        # MN82: a pasted space or quotes became `http://%20http://...` advice
        raise SyncClientError(
            f"the hub address {raw!r} has spaces or quotes around it: use {raw.strip().strip(chr(34) + chr(39)).strip()} "
            "(set it with --hub or ATLAS_SYNC_HUB, without quotes inside the value)")
    if _re.match(r"(?i)https?(?::/?(?!/)|//)", raw) and "://" not in raw:
        # `http//host`, `https:/host`: a misspelt scheme, not a missing one (MN82)
        rest = _re.sub(r"(?i)^https?:?/{0,2}", "", raw)
        scheme_word = "https" if raw.lower().startswith("https") else "http"
        raise SyncClientError(
            f"the hub address {raw!r} has a misspelt scheme: use {scheme_word}://{rest} "
            "(set it with --hub or ATLAS_SYNC_HUB)")
    scheme = urlsplit(base_url).scheme.lower() if "://" in base_url else ""
    if scheme not in ("http", "https"):
        shown = base_url.split("://", 1)[-1] if scheme else base_url
        raise SyncClientError(
            f"the hub address {base_url!r} needs http:// or https:// in front: use http://{shown} "
            "(or https://... when a TLS proxy fronts the hub); set it with --hub or ATLAS_SYNC_HUB")


def _pending_parked(root: Path) -> set[str]:
    """Corpus paths that still have a parked copy (its sidecar AND its bytes) under _atlas/sync-conflicts/."""
    import json
    out: set[str] = set()
    qdir = root / "_atlas" / "sync-conflicts"
    if not qdir.is_dir():
        return out
    for meta in qdir.rglob("*.meta.json"):
        try:
            rel = json.loads(meta.read_text(encoding="utf-8")).get("original_path")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(rel, str) and meta.with_name(meta.name[: -len(".meta.json")]).is_file():
            out.add(rel)
    return out


def _keeps_every_line(theirs: bytes, mine: bytes) -> bool:
    """True when every line of `theirs` is also a line of `mine` (line endings ignored)."""
    have = {line.rstrip(b"\r") for line in mine.split(b"\n")}
    return all(line.rstrip(b"\r") in have for line in theirs.split(b"\n"))


def _too_large_limit(remote: dict) -> int | None:
    limits = remote.get("limits") if isinstance(remote, dict) else None
    value = limits.get("max_file_bytes") if isinstance(limits, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _mb(n: int) -> str:
    return f"{n // (1024 * 1024)} MB" if n >= 1024 * 1024 else f"{n:,} bytes"


class SyncClient:
    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        transport: httpx.BaseTransport | None = None,
        timeout: float | None = None,  # ATLAS_SYNC_TIMEOUT, default 120 s: a cold 10,000-file hub took 32.7 s (AG71)
        http_client: httpx.Client | None = None,
    ) -> None:
        import hashlib as _hashlib
        # Which pairing a base belongs to (AG78): a domain-separated digest of the token, never
        # the token and never the digest the hub stores.
        self._credential = _hashlib.sha256(b"sextant-sync-base:" + token.encode("utf-8")).hexdigest()[:16] if token else ""
        if http_client is not None:
            # Injected client (e.g. starlette TestClient) already knows its
            # base URL/transport; we only attach the credential.
            self._client = http_client
            if token:
                self._client.headers["Authorization"] = f"Bearer {token}"
        else:
            _check_hub_url(base_url)
            if timeout is None:
                import os as _os
                try:
                    timeout = float(_os.environ.get("ATLAS_SYNC_TIMEOUT") or 120)
                except ValueError:
                    timeout = 120.0
                if not timeout > 0:
                    timeout = 120.0  # `-5` raised ValueError out of httpx, `0` read as no hub (MN65)
            try:
                self._client = httpx.Client(
                    base_url=base_url,
                    headers={"Authorization": f"Bearer {token}"},
                    transport=transport,
                    timeout=timeout,
                )
            except (OSError, ValueError) as exc:  # ssl.SSLError is an OSError: httpx loads SSL_CERT_FILE here (MN47: a traceback for a missing or garbage file)
                import os as _os
                _cf = _os.environ.get("SSL_CERT_FILE") or ""  # httpx reads SSL_CERT_FILE only (AG60)
                raise SyncClientError(
                    f"the certificate bundle {_cf!r} (SSL_CERT_FILE) cannot be read as a PEM certificate file: {type(exc).__name__}: "
                    f"{str(exc)[:160]}. Point SSL_CERT_FILE at the hub's CA certificate, or unset it."
                ) from exc

    def _own_base(self, base_id: str) -> dict | None:
        """The base, unless another pairing recorded it (AG78: after a re-pair against a hub
        restored from an older backup, the old base made the hub's older notes look like
        edits and pulled them over newer local ones). No base means every difference is a
        conflict: nothing is overwritten. A base with no stamp (older build) is adopted."""
        keyed = store.load_base_manifest(self._base_key(base_id))
        if keyed is not None:
            return keyed
        base = store.load_base_manifest(base_id)
        stamp = (base or {}).get("credential")
        if base is not None and stamp and self._credential and stamp != self._credential:
            return None
        return base

    def _base_key(self, base_id: str) -> str:
        """One base per pairing: a device that also syncs with a second hub (federation, `--hub
        --token`) overwrote the single `base-hub.json`, and every one-sided edit on either hub
        became a conflict at the next sync with the other (MN67). The legacy file is still read."""
        if base_id == "hub" and self._credential:
            return f"hub-{self._credential}"
        return base_id

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SyncClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- protocol verbs -----------------------------------------------------

    def _hub_label(self) -> str:
        try:
            return str(self._client.base_url) or "the hub"
        except Exception:  # noqa: BLE001
            return "the hub"

    def _transport_error(self, verb: str, path: str, exc: Exception) -> SyncClientError:
        # A refused connection is "no hub answering", not a WinError dump (persona rerun 2026-11-10).
        _s = str(exc)
        if "WRONG_VERSION_NUMBER" in _s or "record layer failure" in _s:
            return SyncClientError(
                f"{self._hub_label()} answered plain HTTP to a TLS handshake: use http:// for this hub "
                "(or put it behind a TLS proxy)"  # it said "no hub answering" with the hub running (AG82)
            )
        _low = _s.lower()
        if isinstance(exc, (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError)) and (
                "10054" in _s or "forcibly closed" in _low or "reset" in _low or "disconnected" in _low) \
                and self._hub_label().startswith("http://"):
            _alt = "https://" + self._hub_label()[len("http://"):]
            return SyncClientError(
                f"{self._hub_label()} closed the connection: if this port is a TLS proxy (Caddy, nginx), use {_alt}"
            )  # a raw WinError 10054 (AG82)
        if "CERTIFICATE_VERIFY_FAILED" in _s or "certificate verify failed" in _s.lower() or "ssl" in type(exc).__name__.lower():
            return SyncClientError(
                f"the hub at {self._hub_label()} presented a certificate this client does not trust (a private CA, "
                "`tls internal`, or a wrong SSL_CERT_FILE): point SSL_CERT_FILE at the CA certificate, or use the "
                "hub's http:// address on a trusted network"  # it said "no hub answering" (MN41)
            )
        if isinstance(exc, (httpx.UnsupportedProtocol, httpx.InvalidURL)):
            try:
                _check_hub_url(self._hub_label())
            except SyncClientError as bad:
                return bad
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return SyncClientError(
                f"no hub answering at {self._hub_label()} -- is `sextant-sync serve` running there, "
                f"and is ATLAS_SYNC_HUB / --hub the right address?"
            )
        if isinstance(exc, httpx.TimeoutException):
            return SyncClientError(f"{verb} {path}: the hub did not answer in time")
        return SyncClientError(f"{verb} {path}: {exc}")

    def _get(self, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.get(path, **kwargs)
        except httpx.HTTPError as exc:
            raise self._transport_error("GET", path, exc) from exc
        return response

    def _post(self, path: str, payload: dict) -> httpx.Response:
        try:
            response = self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise self._transport_error("POST", path, exc) from exc
        return response

    def health(self) -> dict:
        response = self._get("/sync/v1/health")
        response.raise_for_status()
        return response.json()

    def remote_manifest(self) -> dict:
        since = getattr(self, "_since_generation", None)  # the count this device last recorded: the hub answers with its mark (AG79)
        response = self._get("/sync/v1/manifest", params={"since": since}) if isinstance(since, int) and since > 0 \
            else self._get("/sync/v1/manifest")
        if response.status_code == 401:
            raise SyncClientError("unauthorized: token rejected by hub")
        if response.status_code != 200:
            _hint = _url_hint(self._hub_label(), response)
            raise SyncClientError(f"manifest: HTTP {response.status_code}" + (_hint or f": {_detail(response)}"))
        try:
            data = response.json()
        except ValueError:
            raise SyncClientError("manifest: hub answered with a non-JSON body")
        files = data.get("files") if isinstance(data, dict) else None
        if not isinstance(files, dict) or not all(
            isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("sha256"), str)
            for k, v in files.items()
        ):
            raise SyncClientError("manifest: malformed file table from hub")
        return data

    def fetch_file(self, rel: str) -> tuple[bytes, str]:
        response = self._get("/sync/v1/file", params={"path": rel})
        # Every non-2xx is a per-file SyncClientError the sync loop records and moves past;
        # a 409 (unreadable on hub), a 5xx or an ingress HTML page must not abort the run
        # (round C 2026-11-10, lane A F4).
        if response.status_code == 401:
            raise SyncAuthError("the hub rejected this device's token (revoked, or a new pairing): sync stopped")  # 9,815 lines of 401 (MN65)
        if response.status_code != 200:
            raise SyncClientError(f"{rel}: HTTP {response.status_code}: {_detail(response)}")
        return response.content, response.headers.get("X-Sextant-Sha256", "")

    def push(self, changes: list[dict], on_batch=None) -> dict:
        """``on_batch(results)`` runs after every request the hub answered: a 3,000-file push was
        silent for 21 s (AG88)."""
        if len(changes) > 500:  # the hub takes at most 500 per request: 3,000 new notes failed every sync (MN71)
            out: list = []
            gen = mark = None
            for i in range(0, len(changes), 500):
                try:
                    part = self.push(changes[i:i + 500], on_batch)
                except SyncAuthError:
                    if not out:
                        raise
                    # a later batch was refused: what already landed is still reported as landed (AG79)
                    out.extend({"path": ch.get("path", "?"), "result": "unsent"} for ch in changes[i:])
                    return {"results": out, "generation": gen, "generation_mark": mark,
                            "stopped": "the hub rejected this device's token (revoked, or a new pairing): sync stopped"}
                except SyncClientError as exc:
                    if not out:
                        raise
                    # a later batch failed (a 502 from the proxy): batch 1 had landed but all 620 files read as errors (AG79)
                    out.extend({"path": ch.get("path", "?"), "result": "error", "detail": str(exc)} for ch in changes[i:i + 500])
                    continue
                out.extend(part.get("results", []))
                if isinstance(part.get("generation"), int):
                    gen, mark = part["generation"], part.get("generation_mark")
            return {"results": out, "generation": gen, "generation_mark": mark}
        response = self._post("/sync/v1/push", {"changes": changes})
        if response.status_code == 401:
            raise SyncAuthError("the hub rejected this device's token (revoked, or a new pairing): sync stopped")  # one sentence, not one per file (AG73)
        if response.status_code != 200:
            raise SyncClientError(f"push: HTTP {response.status_code}: {_detail(response)}")
        try:
            data = response.json()
        except ValueError:
            raise SyncClientError("push: hub answered with a non-JSON body")
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise SyncClientError("push: malformed results from hub")
        if on_batch is not None:
            on_batch(data["results"])
        return data

    def checkin(self, payload: dict) -> dict:
        response = self._post("/sync/v1/checkin", payload)
        response.raise_for_status()
        return response.json()

    # -- composite ------------------------------------------------------------

    def manifest_and_restore(self, base_id: str = "hub") -> tuple[dict, bool]:
        """The hub's manifest, and whether the hub is older than this device last saw. checkin
        compared hashes only and said "different corpus state" about a restored hub (AG88)."""
        seen = self._own_base(base_id) or {}
        self._since_generation = seen.get("hub_generation") if isinstance(seen.get("hub_generation"), int) else None
        remote = self.remote_manifest()
        return remote, self._restored(seen or None, remote)

    @staticmethod
    def _restored(base: dict | None, remote: dict) -> bool:
        _rg = remote.get("generation") if isinstance(remote, dict) else None
        _seen_mark = (base or {}).get("hub_generation_mark")
        _now_mark = remote.get("since_mark") if isinstance(remote, dict) else None
        # the hub is older than this device last saw (restored from a backup) (MN71); a count this
        # hub reached again after the restore carries a different mark (AG79)
        return base is not None and isinstance(_rg, int) and (
            _rg < int(base.get("hub_generation") or 0)
            or (isinstance(_seen_mark, str) and isinstance(_now_mark, str) and _now_mark != _seen_mark))

    def plan(self, root: Path, base_id: str = "hub",
             share: Sequence[str] | None = None) -> tuple[manifest.SyncPlan, dict, dict]:
        """Compute the three-way plan against the hub. Returns (plan, local, remote).

        ``share`` is the LOCAL outbound allowlist (corpus-relative prefixes). When given,
        every table the plan is computed from -- local, remote and base -- is filtered to
        it BEFORE anything is read or serialized, so a file outside the share is never
        planned, fetched, pushed or pulled. When the hub reports that it pairs this
        caller as a federation peer and no share is declared, the plan refuses: the
        remote's view of the allowlist is never taken as this operator's consent
        (wave-2 2026-11-09, R2-01).
        """
        prefixes = normalize_share(share)
        local_policy = _share_principal(prefixes)
        _seen = self._own_base(base_id) or {}
        remote, _ = self.manifest_and_restore(base_id)
        hub_policy = _hub_policy(remote)
        if local_policy is None and (hub_policy is None or hub_policy.kind == "peer"):
            raise SyncClientError(
                "the hub pairs this system as a federation PEER (or did not say how it pairs "
                "it); declare what may leave this system with --share <prefix> (repeatable) "
                "or ATLAS_SYNC_SHARE before syncing -- nothing was sent"
            )
        local = manifest.build_manifest(root, local_policy)
        local["files"] = {k: v for k, v in local["files"].items()
                          if _visible(k, local_policy, hub_policy)}
        remote_files = {k: v for k, v in remote["files"].items()
                        if _visible(k, local_policy, hub_policy)}
        base = self._own_base(base_id)
        self._rolled_back = False
        if self._restored(base, remote):
            # restored from a backup: every difference is a conflict (MN71, AG79)
            base = None
            self._rolled_back = True
        base_files = {k: v for k, v in ((base or {}).get("files", {}) or {}).items()
                      if _visible(k, local_policy, hub_policy)}
        plan = manifest.diff3(local["files"], remote_files, base_files)
        # Edited here, gone from the hub: sync reports a remote deletion and keeps the copy, while
        # plan said CONFLICT forever (AG82). One classification for both.
        plan.kept_local_edits = [rel for rel in plan.conflicts
                                 if rel not in remote_files and rel in local["files"]]
        if plan.kept_local_edits:
            plan.conflicts = [rel for rel in plan.conflicts if rel not in plan.kept_local_edits]
            plan.remote_deleted = sorted(set(plan.remote_deleted) | set(plan.kept_local_edits))
        # After a restore the conflict parked against the restored hub had no anchor and came back on every
        # sync, so HOWTO-sync's "archive the parked folder and run sync: your files are pushed" never pushed
        # (MN82). Once its parked copy is gone, and while the hub still holds the copy that was parked, the
        # device's file goes up against that copy; a hub copy that changed since stays a conflict.
        self._restored_anchor = {}
        _after_restore = _seen.get("restored") if isinstance(_seen.get("restored"), dict) else {}
        if _after_restore and not self._rolled_back and plan.conflicts:
            pending = _pending_parked(root)
            for rel in list(plan.conflicts):
                sha = (remote_files.get(rel) or {}).get("sha256")
                if not (sha and _after_restore.get(rel) == sha and rel in local["files"] and rel not in pending):
                    continue
                try:  # and only when the file kept every line the hub's copy has: a push never drops one
                    hub_bytes, _ = self.fetch_file(rel)
                    mine = (root / rel).read_bytes()
                except (SyncClientError, httpx.HTTPError, OSError):
                    continue
                if hashlib.sha256(hub_bytes).hexdigest() == sha and _keeps_every_line(hub_bytes, mine):
                    plan.conflicts.remove(rel)
                    plan.to_push.append(rel)
                    self._restored_anchor[rel] = sha
            plan.to_push.sort()
        # A peer's accepted proposals (hub inbox) are neither re-sent nor pulled over while the
        # local copy is unchanged; the plan lists them separately (persona rerun 2026-11-10:
        # a peer re-planned the same push forever and could never be "in sync").
        proposed = (base or {}).get("proposed", {}) or {}
        plan.proposed = sorted(
            rel for rel, sha in proposed.items()
            if (local["files"].get(rel) or {}).get("sha256") == sha
        )
        for rel in plan.proposed:
            for bucket in (plan.to_push, plan.to_pull, plan.conflicts):
                if rel in bucket:
                    bucket.remove(rel)
        plan.hub_restored = self._rolled_back
        # A file the hub cannot take is not sent: by the limit the hub advertises, or (an older hub)
        # because the hub already refused these exact bytes as too large. It went up every sync (AG88).
        limit = _too_large_limit(remote)
        refused = _seen.get("refused") if isinstance(_seen.get("refused"), dict) else {}
        plan.too_large = [rel for rel in plan.to_push
                          if (limit and int((local["files"].get(rel) or {}).get("size") or 0) > limit)
                          or (refused.get(rel) and refused.get(rel) == (local["files"].get(rel) or {}).get("sha256"))]
        plan.to_push = [rel for rel in plan.to_push if rel not in plan.too_large]
        return plan, local, remote

    def too_large_reason(self, rel: str, local: dict, remote: dict) -> str:
        size = int((local.get("files", {}).get(rel) or {}).get("size") or 0)
        limit = _too_large_limit(remote)
        if limit and size > limit:
            return (f"{rel}: not sent: {size:,} bytes is over the hub's limit of {_mb(limit)} per file "
                    "(split the file, or move it out of the synced corpus)")
        return (f"{rel}: not sent again: the hub refused this copy as too large ({size:,} bytes); "
                "split the file or move it out of the synced corpus (an edit is sent once more)")

    def sync_once(self, root: Path, base_id: str = "hub", dry_run: bool = False,
                  share: Sequence[str] | None = None, progress=None) -> SyncReport:
        """Plan, pull, push, record the new base. The whole spoke-side loop.

        Conflicts never overwrite local files: the hub's copy is parked in
        the local quarantine for the operator, the local copy stays live,
        and the hub keeps its own canonical copy (each side quarantines the
        OTHER side's version; nothing is lost, nothing is silently merged).

        A file whose exchange did not complete -- a conflict, a denied or
        errored transfer, a push the hub only parked in its inbox -- keeps
        its PREVIOUS base entry, so the next sync sees the same divergence
        again instead of adopting the local state as a new common ancestor
        and pulling the hub's copy over the local edit (wave-2 2026-11-09,
        R2-06). Only an explicit resolution (edit, archive) moves it on.
        """
        prefixes = normalize_share(share)
        local_policy = _share_principal(prefixes)
        plan, local, _remote = self.plan(root, base_id, prefixes)
        _hub_gen = _remote.get("generation") if isinstance(_remote, dict) else None
        _hub_mark = _remote.get("generation_mark") if isinstance(_remote, dict) else None
        hub_policy = _hub_policy(_remote)
        remote_files_all = _remote.get("files", {})
        report = SyncReport(
            remote_deleted=list(plan.remote_deleted),
            local_deleted=list(plan.local_deleted),
            local_files=len(local.get("files", {})),
            hub_prefixes=list(hub_policy.prefixes) if hub_policy else [],
            kept_local_edits=list(plan.kept_local_edits),
            too_large=list(plan.too_large),
            hub_restored=plan.hub_restored,
        )
        if dry_run:
            report.conflicts = list(plan.conflicts)
            return report

        old_base = {} if getattr(self, "_rolled_back", False) else (self._own_base(base_id) or {})
        if getattr(self, "_rolled_back", False):
            report.errors.append(RESTORED_HUB)
        old_base_files: dict = old_base.get("files", {}) or {}
        proposed: dict = dict(old_base.get("proposed", {}) or {})
        report.proposed = list(plan.proposed)
        # A local delete is reported, never propagated, and never undone: the base keeps the
        # entry so the next run reports it again instead of pulling the hub copy back
        # (round C 2026-11-10, lane A F2).
        unresolved: set[str] = set(plan.local_deleted)
        unresolved.update(plan.conflicts)  # every conflict keeps its old anchor even when the run stops early (AG73)
        unresolved.update(plan.kept_local_edits)  # old anchor kept: a restored hub copy is a conflict, never a pull over the edit
        _seen_base = self._own_base(base_id) or {}
        _seen_refused = _seen_base.get("refused")
        refused: dict = dict(_seen_refused) if isinstance(_seen_refused, dict) else {}
        for rel in plan.too_large:
            report.errors.append(self.too_large_reason(rel, local, _remote))
            unresolved.add(rel)
        # A long first sync printed nothing for minutes (AG82): the caller gets counts as files land.
        _total = len(plan.to_pull) + len(plan.to_push)
        if progress is not None and _total:
            progress("start", 0, _total, len(plan.to_pull), len(plan.to_push))
        _done = 0
        auth_stopped = False  # a 401 stops the WHOLE run: the push and conflicts passes went on with one 401 line per file (AG73)
        local_files = local.get("files", {})
        for rel in plan.to_pull:
            try:
                content, announced = self.fetch_file(rel)
                # TOCTOU guard (audit 2026-11-09 row 20): a pull is only valid
                # while the local file still matches the base it was planned
                # against (diff3 pulls only when local == base). A local edit
                # made after plan() would otherwise be overwritten by the hub
                # copy and adopted into the next base. Re-hash right before
                # the write; on mismatch park the hub copy as a conflict and
                # leave the local edit live.
                planned = (local_files.get(rel) or {}).get("sha256")
                _now = apply.current_sha256(root, rel)
                if _now is not None and _now == hashlib.sha256(content).hexdigest():
                    continue  # already these bytes here: not a conflict (MN65)
                if _now != planned:
                    report.parked[rel] = str(apply.quarantine(
                        root, rel, content,
                        reason="pull conflict: local file changed after planning",
                    ))
                    report.conflicts.append(rel)
                    unresolved.add(rel)
                    continue
                apply.apply_change(root, rel, content, announced or None, local_policy)
                report.pulled.append(rel)
                _done += 1
                if progress is not None:
                    progress("pull", _done, _total, len(plan.to_pull), len(plan.to_push))
            except apply.SyncPolicyError as exc:
                report.denied.append(rel)
                report.errors.append(f"{rel}: denied ({exc})")  # `denied 1` named nothing (AG47)
                unresolved.add(rel)
            except SyncAuthError as exc:
                report.errors.append(str(exc))
                unresolved.update(r for r in plan.to_pull if r not in report.pulled)
                auth_stopped = True
                break
            except (apply.SyncIntegrityError, SyncClientError, OSError) as exc:
                report.errors.append(f"{rel}: {exc}")
                unresolved.add(rel)

        if auth_stopped:
            unresolved.update(plan.to_push)
        if plan.to_push and not auth_stopped:
            base_files = old_base_files
            changes = []
            for rel in plan.to_push:
                # Belt and braces: the plan was built from the filtered manifest, and the
                # bytes are read only after the local share AND the hub's allowlist agree.
                if not _visible(rel, local_policy, hub_policy):
                    report.denied.append(rel)
                    unresolved.add(rel)
                    continue
                target = root / rel
                if not target.is_file():
                    continue
                try:
                    raw = target.read_bytes()
                except OSError as exc:
                    report.errors.append(f"{rel}: {exc}")
                    unresolved.add(rel)
                    continue
                base_entry = base_files.get(rel)
                changes.append(
                    {
                        "path": rel,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "base_sha256": base_entry["sha256"] if base_entry else getattr(self, "_restored_anchor", {}).get(rel),
                        "content_b64": base64.b64encode(raw).decode("ascii"),
                    }
                )
            if changes:
                _landed = [0]

                def _batch(part: list) -> None:  # counts only what the hub took, never its refusals (AG88)
                    _landed[0] += sum(1 for it in part if isinstance(it, dict) and it.get("result") in ("applied", "inbox"))
                    if progress is not None:
                        progress("push", _done + _landed[0], _total, len(plan.to_pull), len(plan.to_push))
                try:
                    # progress only when asked for: a wrapped push(changes) keeps working
                    _push_answer = self.push(changes, on_batch=_batch) if progress is not None else self.push(changes)
                    results = _push_answer.get("results", [])
                    if isinstance(_push_answer.get("generation"), int):
                        _hub_gen = _push_answer["generation"]  # the hub's own count after our push
                        _hub_mark = _push_answer.get("generation_mark")
                    if _push_answer.get("stopped"):
                        report.errors.append(str(_push_answer["stopped"]))
                        auth_stopped = True
                except SyncAuthError as exc:
                    report.errors.append(str(exc))
                    unresolved.update(ch["path"] for ch in changes)
                    auth_stopped = True
                    results = []
                except SyncClientError as exc:
                    # The whole batch failed to land: every file stays unresolved and the
                    # run continues to the conflicts pass and the base save (F4).
                    for change in changes:
                        report.errors.append(f"{change['path']}: {exc}")
                        unresolved.add(change["path"])
                    results = []
                _done += _landed[0]
                for item in results:
                    if not isinstance(item, dict):
                        continue
                    rel = str(item.get("path", "?"))
                    result = item.get("result")
                    if result == "applied":
                        report.pushed.append(rel)
                    elif result == "inbox":
                        # Parked as a proposal on the hub, not adopted: not common ground yet,
                        # but remembered so an unchanged copy is not proposed again.
                        report.pushed.append(rel)
                        unresolved.add(rel)
                        sent = next((ch for ch in changes if ch["path"] == rel), None)
                        if sent:
                            proposed[rel] = sent["sha256"]
                    elif result == "conflict":
                        report.conflicts.append(rel)
                        report.push_conflicts.append(rel)
                        unresolved.add(rel)
                        # Park the hub's copy here NOW, like a pull conflict, instead of making
                        # the user sync twice to see it (self-hoster pass 2026-11-10).
                        try:
                            content, _sha = self.fetch_file(rel)
                            if apply.current_sha256(root, rel) == hashlib.sha256(content).hexdigest():
                                report.conflicts.remove(rel); report.push_conflicts.remove(rel); unresolved.discard(rel)
                                continue  # the hub holds the same bytes: converged, not a conflict (MN65)
                            report.parked[rel] = str(apply.quarantine(
                                root, rel, content, reason="push conflict: the hub changed it too"))
                        except SyncAuthError as exc:
                            if not auth_stopped:
                                report.errors.append(str(exc))
                            auth_stopped = True
                        except Exception as exc:  # noqa: BLE001
                            report.errors.append(f"{rel}: could not fetch the hub's copy ({_detail(exc)})")
                    elif result == "unsent":
                        unresolved.add(rel)  # never reached the hub (the run stopped): one sentence above, not one per file
                    elif result == "denied":
                        report.denied.append(rel)
                        report.errors.append(f"{rel}: denied (hub said: {item.get('detail') or 'policy'})")  # `denied 1` named nothing (MN41)
                        unresolved.add(rel)
                    else:
                        report.errors.append(f"{rel}: {item.get('detail', result)}")
                        unresolved.add(rel)
                        if str(item.get("detail") or "").startswith("file too large"):
                            sent = next((ch for ch in changes if ch["path"] == rel), None)
                            if sent:
                                refused[rel] = sent["sha256"]  # remembered: the same bytes are not sent again

        if progress is not None and _total:
            progress("end", _done, _total, len(plan.to_pull), len(plan.to_push))
        for rel in plan.conflicts:
            if auth_stopped:
                break  # every conflict is already unresolved: nothing parked, nothing lost
            unresolved.add(rel)
            if rel not in remote_files_all:
                # Local edit vs hub delete: that is a deletion to report, not a file to
                # fetch (round C 2026-11-10, lane A F1: it 404'd as an error forever).
                report.remote_deleted.append(rel)
                continue
            try:
                content, _sha = self.fetch_file(rel)
                if apply.current_sha256(root, rel) == hashlib.sha256(content).hexdigest():
                    unresolved.discard(rel)
                    continue  # both sides made the same change: converged (MN65)
                report.parked[rel] = str(apply.quarantine(
                    root, rel, content, reason="pull conflict: both sides changed"))
                report.conflicts.append(rel)
            except SyncAuthError as exc:
                report.errors.append(str(exc))
                auth_stopped = True
                break
            except (SyncClientError, OSError) as exc:
                report.errors.append(f"{rel}: {exc}")

        # New base = local state after applying; next diff3 anchors here --
        # except for files whose exchange did not complete, which keep the
        # previous anchor so the same divergence is seen again next time.
        refreshed = manifest.build_manifest(root, local_policy)
        local_after = dict(refreshed["files"])  # what is really on disk now (proposals live here)
        files = {k: v for k, v in refreshed["files"].items()
                 if _visible(k, local_policy, hub_policy)}
        for rel in unresolved:
            if rel in old_base_files:
                files[rel] = old_base_files[rel]
            else:
                files.pop(rel, None)
        # Entries this run could not see (outside the share or the hub's allowlist) keep
        # their old anchor: a narrowed run must not forget what a full run knew (F2).
        for rel, entry in old_base_files.items():
            if rel not in files and not _visible(rel, local_policy, hub_policy):
                files[rel] = entry
        # Proposals whose local copy changed are dropped (they will be proposed again); a live
        # proposal stays OUT of the base: the hub never adopted it, so a later edit is a push,
        # not a conflict with nothing.
        refreshed["proposed"] = {
            rel: sha for rel, sha in proposed.items()
            if (local_after.get(rel) or {}).get("sha256") == sha
        }
        for rel in refreshed["proposed"]:
            files.pop(rel, None)
        refreshed["refused"] = {rel: sha for rel, sha in refused.items()
                                if (local_after.get(rel) or {}).get("sha256") == sha}  # an edit is tried again
        # The restored hub's copy each conflict was parked against (MN82): kept while the conflict is open.
        _prev = _seen_base.get("restored") if isinstance(_seen_base.get("restored"), dict) else {}
        _was_restored = getattr(self, "_rolled_back", False)
        restored = {} if _was_restored else dict(_prev)
        if _was_restored:
            for rel in report.parked:
                sha = (remote_files_all.get(rel) or {}).get("sha256")
                if sha:
                    restored[rel] = sha
        refreshed["restored"] = {rel: sha for rel, sha in restored.items() if rel in unresolved}
        refreshed["files"] = files
        refreshed["manifest_hash"] = manifest.files_hash(files)
        if isinstance(_hub_gen, int):
            refreshed["hub_generation"] = _hub_gen
            if isinstance(_hub_mark, str):
                refreshed["hub_generation_mark"] = _hub_mark
        if self._credential:
            refreshed["credential"] = self._credential
        store.save_base_manifest(self._base_key(base_id), refreshed)
        if self._base_key(base_id) != base_id:
            store.save_base_manifest(base_id, refreshed)  # the legacy file: `status` and an older build read it
        if auth_stopped:
            return report  # the check-in would be one more 401 (AG73)
        if progress is not None and _total:
            progress("checkin", _done, _total, len(plan.to_pull), len(plan.to_push))  # the hub re-hashes its corpus: 18 s silent on 6,000 files (AG88)
        try:
            self.checkin({"manifest_hash": refreshed["manifest_hash"]})
        except (SyncClientError, httpx.HTTPError):
            pass  # check-in is best-effort; the sync itself succeeded
        return report
