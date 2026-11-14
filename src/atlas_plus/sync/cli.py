"""atlas-sync / sextant-sync: the operator surface for Sextant Sync.

Hub-side:   serve, pair, devices, revoke, status
Spoke-side: plan, sync, checkin  (need --hub URL and --token, or the
            ATLAS_SYNC_HUB / ATLAS_SYNC_DEVICE_TOKEN env vars)

Pairing happens HERE, on the hub, as a local operator action; there is no
HTTP pairing route by design (design doc F3).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from atlas_plus import paths as atlas_paths
from atlas_plus.sync import devices, manifest, store


def _hub_args(args: argparse.Namespace) -> tuple[str, str]:
    hub = args.hub or os.environ.get("ATLAS_SYNC_HUB", "")
    token = args.token or os.environ.get("ATLAS_SYNC_DEVICE_TOKEN", "")
    if not hub:
        raise SystemExit("no hub URL: pass --hub or set ATLAS_SYNC_HUB")
    if not token:
        raise SystemExit("no device token: pass --token or set ATLAS_SYNC_DEVICE_TOKEN")
    return hub, token


def _prog_name() -> str:
    from atlas_plus.branding import prog as _prog
    return _prog("atlas-sync")


def _share_args(args: argparse.Namespace) -> list[str]:
    """Local outbound allowlist: repeatable --share, else ATLAS_SYNC_SHARE (comma-separated)."""
    share = [s for s in (getattr(args, "share", None) or []) if s and s.strip()]
    if not share:
        env = os.environ.get("ATLAS_SYNC_SHARE", "")
        share = [s for s in env.split(",") if s.strip()]
    return share


def cmd_serve(args: argparse.Namespace) -> int:
    from atlas_plus.sync import api

    sys.argv = ["atlas-sync serve"]
    if args.host:
        sys.argv += ["--host", args.host]
    if args.port:
        sys.argv += ["--port", str(args.port)]
    api.main()
    return 0


def cmd_pair(args: argparse.Namespace) -> int:
    prefixes = tuple(p.strip().strip("/") + "/" for p in (args.prefix or []) if p.strip())
    try:
        device, token = devices.pair(
            args.name, kind=args.kind, scope=args.scope, prefixes=prefixes
        )
    except ValueError as exc:
        print(f"pair failed: {exc}", file=sys.stderr)
        return 1
    store.audit("paired", device=device.name, kind=device.kind, scope=device.scope)
    print(f"paired {device.kind} '{device.name}' (id {device.device_id})")
    print("token (SHOWN ONCE, store it in the device's env as ATLAS_SYNC_DEVICE_TOKEN):")
    print(f"  {token}")
    if device.kind == "peer":
        print(f"peer allowlist: {', '.join(device.prefixes)}")
    return 0


def cmd_devices(args: argparse.Namespace) -> int:
    rows = devices.load_devices()
    checkins = store.load_checkins()
    if args.json:
        out = []
        for d in rows:
            record = {
                "device_id": d.device_id, "name": d.name, "kind": d.kind,
                "scope": d.scope, "prefixes": list(d.prefixes),
                "revoked": d.revoked, "created_utc": d.created_utc,
                "last_checkin": checkins.get(d.device_id, {}).get("received_utc"),
            }
            out.append(record)
        print(json.dumps(out, indent=2))
        return 0
    if not rows:
        from atlas_plus.branding import prog as _prog
        print(f"no paired devices or peers yet ({_prog('atlas-sync')} pair <name>)")
        return 0
    for d in rows:
        seen = checkins.get(d.device_id, {}).get("received_utc", "never")
        flags = " REVOKED" if d.revoked else ""
        scope = d.scope + (f" [{','.join(d.prefixes)}]" if d.prefixes else "")
        print(f"  {d.name:<20} {d.kind:<7} {scope:<24} last check-in: {seen}{flags}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    if devices.revoke(args.name):
        store.audit("revoked", device=args.name)
        print(f"revoked: {args.name}")
        return 0
    print(f"no active principal named {args.name!r}", file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    root = atlas_paths.memory_root()
    local = manifest.build_manifest(root)
    print(f"corpus: {root}")
    print(f"  syncable files: {len(local['files'])}")
    print(f"  manifest hash:  {local['manifest_hash'][:16]}")
    rows = [d for d in devices.load_devices() if not d.revoked]
    hub = os.environ.get("ATLAS_SYNC_HUB", "")
    if rows:
        print(f"  paired: {len(rows)} active principal(s) (this is a hub)")
    if hub:
        base = store.load_base_manifest("hub") or {}
        last = base.get("generated_utc", "never")
        print(f"  hub: {hub} (last sync base {last})")
    if not rows and not hub:
        print("  role: not paired as a hub, no hub configured (ATLAS_SYNC_HUB)")
    conflicts = root / "_atlas" / "sync-conflicts"
    pending = sum(1 for p in conflicts.rglob("*") if p.is_file() and not p.name.endswith(".meta.json")) if conflicts.exists() else 0
    if pending:
        print(f"  UNRESOLVED CONFLICTS: {pending} file(s) under _atlas/sync-conflicts/")
    inbox_dir = root / "_atlas" / "sync-inbox"
    proposals = sum(1 for p in inbox_dir.rglob("*") if p.is_file() and not p.name.endswith(".meta.json")) if inbox_dir.exists() else 0
    if proposals:
        print(f"  inbox proposals awaiting review: {proposals} file(s) under _atlas/sync-inbox/")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    from atlas_plus.sync.client import SyncClient, SyncClientError

    hub, token = _hub_args(args)
    root = atlas_paths.memory_root()
    try:
        with SyncClient(hub, token) as client:
            plan, _local, _remote = client.plan(root, share=_share_args(args))
    except SyncClientError as exc:
        print(f"plan refused: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({
            "to_pull": plan.to_pull, "to_push": plan.to_push,
            "conflicts": plan.conflicts, "local_deleted": plan.local_deleted,
            "remote_deleted": plan.remote_deleted, "in_sync": plan.in_sync,
        }, indent=2))
        return 0
    print(plan.summary())
    share = _share_args(args)
    if share and plan.in_sync == 0 and not (plan.to_pull or plan.to_push or plan.conflicts or plan.proposed):
        print("  note: nothing on either side matches this share (check it against the hub's --prefix; "
              "`sync` prints the hub's allowlist)")
    for label, items in (
        ("pull", plan.to_pull), ("push", plan.to_push),
        ("CONFLICT", plan.conflicts),
        ("proposed, awaiting the hub's review", plan.proposed),
        ("deleted remotely (no action)", plan.remote_deleted),
        ("deleted locally (no action)", plan.local_deleted),
    ):
        for rel in items:
            print(f"  {label}: {rel}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from atlas_plus.sync.client import SyncClient, SyncClientError

    hub, token = _hub_args(args)
    root = atlas_paths.memory_root()
    try:
        with SyncClient(hub, token) as client:
            report = client.sync_once(root, dry_run=args.dry_run, share=_share_args(args))
    except SyncClientError as exc:
        print(f"sync refused: {exc}", file=sys.stderr)
        return 2
    print(report.summary())
    share = _share_args(args)
    if share:
        from atlas_plus.sync import policy as _policy
        from atlas_plus.sync.client import normalize_share as _norm
        declared = _norm(share)
        hub = tuple(h if h.endswith("/") else h + "/" for h in report.hub_prefixes)
        if hub and not any(d == h or d.startswith(h) or h.startswith(d) for d in declared for h in hub):
            print(f"  note: the hub allows only {', '.join(report.hub_prefixes)} for this pairing; "
                  f"your --share ({', '.join(declared)}) is outside it, so nothing can sync")
        elif report.local_files == 0 and not report.pulled:
            print("  note: the declared share matches no local file yet (check the prefix spelling)")
    for rel in report.conflicts:
        where = report.parked.get(rel) or str(root / "_atlas" / "sync-conflicts")
        both = " (the hub also parked your pushed copy)" if rel in report.push_conflicts else ""
        print(f"  CONFLICT: {rel} -- your copy is kept; the hub's copy is parked at {where}{both} "
              "(resolve, then archive or delete the parked copy; docs/HOWTO-sync.md, Conflict handling)")
    if report.proposed:
        print(f"  {len(report.proposed)} proposal(s) awaiting the hub's review (unchanged since you sent them)")
    leftover = root / "_atlas" / "sync-conflicts"
    if not report.conflicts and leftover.exists():
        n = sum(1 for p in leftover.rglob("*") if p.is_file() and not p.name.endswith(".meta.json"))
        if n:
            print(f"  note: {n} parked cop{'y' if n == 1 else 'ies'} remain under {leftover} -- archive or delete "
                  "them once resolved so `status` and `doctor` stop counting them")
    import re as _re
    for item in report.errors:
        print(f"  error: {_re.sub(chr(91) + chr(92) + 'x00-' + chr(92) + 'x1f' + chr(92) + 'x7f' + chr(93), ' ', str(item))[:300]}")  # the hub's text is untrusted: no control characters, 300 chars (AG49)
    if report.remote_deleted or report.local_deleted:
        print(
            f"  deletions reported, not propagated: "
            f"{len(report.remote_deleted)} remote, {len(report.local_deleted)} local"
        )
    return 0 if not report.errors else 1


def cmd_checkin(args: argparse.Namespace) -> int:
    from atlas_plus.sync.client import SyncClient

    hub, token = _hub_args(args)
    root = atlas_paths.memory_root()
    from atlas_plus.sync.client import _share_principal, normalize_share
    # The hub compares against ITS view of this caller; a shared subset must hash the
    # same subset (round C 2026-11-10, lane A F9).
    share_list = _share_args(args)
    local_policy = _share_principal(normalize_share(share_list))
    local = manifest.build_manifest(root, local_policy)
    try:
        with SyncClient(hub, token) as client:
            # Same refusal as plan/sync: a peer (or a hub that does not say) gets nothing,
            # not even a hash of the whole corpus, until a share is declared (claims pass
            # 2026-11-10). The machine name is not sent either.
            from atlas_plus.sync.client import _hub_policy
            hub_policy = _hub_policy(client.remote_manifest())
            if local_policy is None and (hub_policy is None or hub_policy.kind == "peer"):
                print("checkin refused: the hub pairs this system as a federation PEER (or did not "
                      "say how); declare --share <prefix> first -- nothing was sent", file=sys.stderr)
                return 2
            if hub_policy is not None and hub_policy.kind == "peer":
                # A peer's hash never equals the hub's (its pushes are proposals, not canonical):
                # "current" for a peer means the plan is clean apart from proposals.
                plan, _l, _r = client.plan(root, share=share_list)
                client.checkin({"manifest_hash": local["manifest_hash"]})
                if plan.to_pull or plan.to_push or plan.conflicts:
                    print(f"not current: {plan.summary()} (run: {_prog_name()} sync)")
                else:
                    n = len(plan.proposed)
                    print("in sync with hub" + (f" ({n} proposal(s) awaiting the hub's review)" if n else ""))
                return 0
            result = client.checkin({"manifest_hash": local["manifest_hash"]})
    except Exception as exc:  # noqa: BLE001 - a bad token or a dead hub is a message, not a traceback
        from atlas_plus.branding import prog as _prog
        detail = str(exc)
        if "401" in detail:
            detail = "unauthorized: token rejected by hub"
        print(f"checkin refused: {detail}", file=sys.stderr)
        return 2
    same = result.get("server_manifest_hash") == local["manifest_hash"]
    print("in sync with hub" if same else f"hub has different corpus state (run: {_prog_name()} plan)")
    return 0


def main() -> None:
    from atlas_plus._console import utf8_stdout_when_piped
    utf8_stdout_when_piped()
    from atlas_plus.branding import prog as _prog
    parser = argparse.ArgumentParser(
        prog=_prog("atlas-sync"), description="Sextant Sync: devices, hub, and corpus sync"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the hub API (loopback by default)")
    p.add_argument("--host", default="")
    p.add_argument("--port", type=int, default=0)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("pair", help="pair a new device or peer (hub-local action)")
    p.add_argument("name")
    p.add_argument("--kind", choices=devices.VALID_KINDS, default="device")
    p.add_argument("--scope", choices=devices.VALID_SCOPES, default="rw")
    p.add_argument("--prefix", action="append",
                   help="restrict to a corpus path prefix (required for peers; repeatable)")
    p.set_defaults(func=cmd_pair)

    p = sub.add_parser("devices", help="list paired devices and peers")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("revoke", help="revoke a device or peer by name or id")
    p.add_argument("name")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("status", help="local sync status (corpus, principals, conflicts)")
    p.set_defaults(func=cmd_status)

    for name, func, extra in (
        ("plan", cmd_plan, True),
        ("sync", cmd_sync, False),
        ("checkin", cmd_checkin, False),
    ):
        p = sub.add_parser(name, help=f"{name} against a hub (spoke-side)")
        p.add_argument("--hub", default="")
        p.add_argument("--token", default="")
        if extra:
            p.add_argument("--json", action="store_true")
        if name == "sync":
            p.add_argument("--dry-run", action="store_true")
        if name in ("plan", "sync", "checkin"):
            p.add_argument("--share", action="append", metavar="PREFIX",
                           help="corpus prefix this system agrees to exchange (repeatable; "
                                "or ATLAS_SYNC_SHARE=a/,b/). Required when the hub pairs you "
                                "as a peer; nothing outside it is read, sent or pulled")
        p.set_defaults(func=func)

    if len(sys.argv) == 1:  # after the subcommands exist, so the usage line lists them
        parser.print_help()
        print(f"\n{_prog_name()} needs a subcommand -- try: {_prog_name()} status", file=sys.stderr)
        raise SystemExit(2)
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
