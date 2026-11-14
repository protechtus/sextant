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

    def summary(self) -> str:
        return (
            f"pulled {len(self.pulled)}, pushed {len(self.pushed)}, "
            f"conflicts {len(self.conflicts)}, denied {len(self.denied)}, "
            f"errors {len(self.errors)}"
        )


class SyncClient:
    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if http_client is not None:
            # Injected client (e.g. starlette TestClient) already knows its
            # base URL/transport; we only attach the credential.
            self._client = http_client
            if token:
                self._client.headers["Authorization"] = f"Bearer {token}"
        else:
            self._client = httpx.Client(
                base_url=base_url,
                headers={"Authorization": f"Bearer {token}"},
                transport=transport,
                timeout=timeout,
            )

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
        response = self._get("/sync/v1/manifest")
        if response.status_code == 401:
            raise SyncClientError("unauthorized: token rejected by hub")
        if response.status_code != 200:
            raise SyncClientError(f"manifest: HTTP {response.status_code}: {_detail(response)}")
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
        if response.status_code != 200:
            raise SyncClientError(f"{rel}: HTTP {response.status_code}: {_detail(response)}")
        return response.content, response.headers.get("X-Sextant-Sha256", "")

    def push(self, changes: list[dict]) -> dict:
        response = self._post("/sync/v1/push", {"changes": changes})
        if response.status_code == 401:
            raise SyncClientError("unauthorized: token rejected by hub")
        if response.status_code != 200:
            raise SyncClientError(f"push: HTTP {response.status_code}: {_detail(response)}")
        try:
            data = response.json()
        except ValueError:
            raise SyncClientError("push: hub answered with a non-JSON body")
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise SyncClientError("push: malformed results from hub")
        return data

    def checkin(self, payload: dict) -> dict:
        response = self._post("/sync/v1/checkin", payload)
        response.raise_for_status()
        return response.json()

    # -- composite ------------------------------------------------------------

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
        remote = self.remote_manifest()
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
        base = store.load_base_manifest(base_id)
        base_files = {k: v for k, v in ((base or {}).get("files", {}) or {}).items()
                      if _visible(k, local_policy, hub_policy)}
        plan = manifest.diff3(local["files"], remote_files, base_files)
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
        return plan, local, remote

    def sync_once(self, root: Path, base_id: str = "hub", dry_run: bool = False,
                  share: Sequence[str] | None = None) -> SyncReport:
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
        hub_policy = _hub_policy(_remote)
        remote_files_all = _remote.get("files", {})
        report = SyncReport(
            remote_deleted=list(plan.remote_deleted),
            local_deleted=list(plan.local_deleted),
            local_files=len(local.get("files", {})),
            hub_prefixes=list(hub_policy.prefixes) if hub_policy else [],
        )
        if dry_run:
            report.conflicts = list(plan.conflicts)
            return report

        old_base = store.load_base_manifest(base_id) or {}
        old_base_files: dict = old_base.get("files", {}) or {}
        proposed: dict = dict(old_base.get("proposed", {}) or {})
        report.proposed = list(plan.proposed)
        # A local delete is reported, never propagated, and never undone: the base keeps the
        # entry so the next run reports it again instead of pulling the hub copy back
        # (round C 2026-11-10, lane A F2).
        unresolved: set[str] = set(plan.local_deleted)
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
                if apply.current_sha256(root, rel) != planned:
                    report.parked[rel] = str(apply.quarantine(
                        root, rel, content,
                        reason="pull conflict: local file changed after planning",
                    ))
                    report.conflicts.append(rel)
                    unresolved.add(rel)
                    continue
                apply.apply_change(root, rel, content, announced or None, local_policy)
                report.pulled.append(rel)
            except apply.SyncPolicyError as exc:
                report.denied.append(rel)
                report.errors.append(f"{rel}: denied ({exc})")  # `denied 1` named nothing (AG47)
                unresolved.add(rel)
            except (apply.SyncIntegrityError, SyncClientError, OSError) as exc:
                report.errors.append(f"{rel}: {exc}")
                unresolved.add(rel)

        if plan.to_push:
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
                        "base_sha256": base_entry["sha256"] if base_entry else None,
                        "content_b64": base64.b64encode(raw).decode("ascii"),
                    }
                )
            if changes:
                try:
                    results = self.push(changes).get("results", [])
                except SyncClientError as exc:
                    # The whole batch failed to land: every file stays unresolved and the
                    # run continues to the conflicts pass and the base save (F4).
                    for change in changes:
                        report.errors.append(f"{change['path']}: {exc}")
                        unresolved.add(change["path"])
                    results = []
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
                            report.parked[rel] = str(apply.quarantine(
                                root, rel, content, reason="push conflict: the hub changed it too"))
                        except Exception as exc:  # noqa: BLE001
                            report.errors.append(f"{rel}: could not fetch the hub's copy ({_detail(exc)})")
                    elif result == "denied":
                        report.denied.append(rel)
                        unresolved.add(rel)
                    else:
                        report.errors.append(f"{rel}: {item.get('detail', result)}")
                        unresolved.add(rel)

        for rel in plan.conflicts:
            unresolved.add(rel)
            if rel not in remote_files_all:
                # Local edit vs hub delete: that is a deletion to report, not a file to
                # fetch (round C 2026-11-10, lane A F1: it 404'd as an error forever).
                report.remote_deleted.append(rel)
                continue
            try:
                content, _sha = self.fetch_file(rel)
                report.parked[rel] = str(apply.quarantine(
                    root, rel, content, reason="pull conflict: both sides changed"))
                report.conflicts.append(rel)
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
        refreshed["files"] = files
        refreshed["manifest_hash"] = manifest.files_hash(files)
        store.save_base_manifest(base_id, refreshed)
        try:
            self.checkin({"manifest_hash": refreshed["manifest_hash"]})
        except (SyncClientError, httpx.HTTPError):
            pass  # check-in is best-effort; the sync itself succeeded
        return report
