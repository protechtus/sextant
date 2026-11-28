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
from atlas_plus._term import safe as _safe  # printed note paths show control/bidi characters as escapes (SEX-048)


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
    except devices.DuplicateNameError as exc:
        print(f"pair failed: {exc}. To give it a new token: {_prog_name()} revoke {args.name}, then "
              f"{_prog_name()} pair {args.name} (the old token stops working at the revoke)", file=sys.stderr)
        return 1
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
    every = devices.load_devices()
    # Revoked rows are history: hidden unless --all (AG82); JSON follows the same rule.
    rows = every if getattr(args, "all", False) else [d for d in every if not d.revoked]
    hidden = len(every) - len(rows)
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
        if hidden:
            print(f"no active devices or peers ({hidden} revoked, hidden: --all shows them; "
                  f"{_prog('atlas-sync')} pair <name>)")
        else:
            print(f"no paired devices or peers yet ({_prog('atlas-sync')} pair <name>)")
        return 0
    from collections import Counter
    names = Counter(d.name.casefold() for d in rows)
    table = []
    for d in rows:
        seen = checkins.get(d.device_id, {}).get("received_utc", "never")
        flags = " REVOKED" if d.revoked else ""
        scope = d.scope + (f" [{','.join(d.prefixes)}]" if d.prefixes else "")
        # `--all` after a revoke and re-pair showed two identical `laptop` rows (AG88): the id tells them apart
        label = f"{d.name} ({d.device_id})" if names[d.name.casefold()] > 1 else d.name
        table.append((label, d.kind, scope, seen, flags))
    # The columns were fixed at 20/24: once an id showed, the rows no longer lined up (SEX-023)
    w_label = max([20] + [len(r[0]) for r in table])
    w_scope = max([24] + [len(r[2]) for r in table])
    for label, kind, scope, seen, flags in table:
        print(f"  {label:<{w_label}} {kind:<7} {scope:<{w_scope}} last check-in: {seen}{flags}")
    if hidden:
        print(f"  ({hidden} revoked, hidden: --all shows them)")
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
        if _plain_http_remote(hub):  # status said nothing about an http:// hub address (SEX-023)
            print("  WARNING: the hub address is plain http:// to another machine: the token and your notes "
                  "travel unencrypted (use https://, a tunnel or a VPN; docs/HOWTO-sync.md)")
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


def _plain_http_remote(url: str) -> bool:
    """True for an http:// hub address whose host is not this machine (loopback is not on the wire)."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "http" or not host:
        return False
    return not (host == "localhost" or host == "::1" or host.startswith("127."))


def cmd_plan(args: argparse.Namespace) -> int:
    from atlas_plus.sync.client import SyncClient, SyncClientError

    hub, token = _hub_args(args)
    root = atlas_paths.memory_root()
    try:
        with SyncClient(hub, token) as client:
            plan, _local, _remote = client.plan(root, share=_share_args(args), on_older=_on_older(args))
    except SyncClientError as exc:
        print(f"plan refused: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({
            "to_pull": plan.to_pull, "to_push": plan.to_push,
            "conflicts": plan.conflicts, "local_deleted": plan.local_deleted,
            "remote_deleted": plan.remote_deleted, "in_sync": plan.in_sync,
            "kept_local_edits": plan.kept_local_edits,
            "too_large": plan.too_large, "hub_restored": plan.hub_restored,
            "hub_older": plan.hub_older,
        }, indent=2))
        return 0
    print(plan.summary())
    if plan.hub_restored:
        from atlas_plus.sync.client import RESTORED_HUB_AHEAD
        print(f"  WARNING: {RESTORED_HUB_AHEAD} (docs/HOWTO-sync.md, After the hub was restored from a backup)")
    if plan.hub_older:
        from atlas_plus.sync.client import HUB_OLDER
        print(f"  WARNING: {HUB_OLDER.format(n=len(plan.hub_older))} (docs/HOWTO-sync.md, After the hub was restored from a backup)")
    share = _share_args(args)
    if share and plan.in_sync == 0 and not (plan.to_pull or plan.to_push or plan.conflicts or plan.proposed):
        print("  note: nothing on either side matches this share (check it against the hub's --prefix; "
              "`sync` prints the hub's allowlist)")
    for label, items in (
        ("pull", plan.to_pull), ("push", plan.to_push),
        ("CONFLICT", plan.conflicts),
        ("proposed, awaiting the hub's review", plan.proposed),
        ("edited here, deleted on the hub (your copy is kept, not pushed)", plan.kept_local_edits),
        ("too large for the hub, not sent", plan.too_large),
        ("the hub's copy is OLDER than yours or gone, not pulled (yours kept)", plan.hub_older),
        ("deleted remotely (no action)", [r for r in plan.remote_deleted if r not in plan.kept_local_edits]),
        ("deleted locally (no action)", plan.local_deleted),
    ):
        for rel in items:
            print(f"  {label}: {_safe(rel)}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from atlas_plus.sync.client import SyncClient, SyncClientError

    hub, token = _hub_args(args)
    root = atlas_paths.memory_root()
    try:
        with SyncClient(hub, token) as client:
            report = client.sync_once(root, dry_run=args.dry_run, share=_share_args(args),
                                      progress=None if args.dry_run else _progress_printer(),
                                      on_older=_on_older(args))
    except SyncClientError as exc:
        print(f"sync refused: {exc}", file=sys.stderr)
        return 2
    print(report.summary())
    if args.dry_run:  # it printed only counts: name the files, as `plan` does (SEX-023)
        for rel in report.would_pull:
            print(f"  would pull: {_safe(rel)}")
        for rel in report.would_push:
            print(f"  would push: {_safe(rel)}")
    if args.dry_run and report.hub_restored:
        from atlas_plus.sync.client import RESTORED_HUB_AHEAD
        print(f"  WARNING: {RESTORED_HUB_AHEAD}")
    if args.dry_run and report.hub_older:
        from atlas_plus.sync.client import HUB_OLDER
        print(f"  WARNING: {HUB_OLDER.format(n=len(report.hub_older))}")
    for rel in report.hub_older:
        print(f"  the hub's copy is OLDER than yours or gone, {'a sync would not pull it' if args.dry_run else 'not pulled'}: {_safe(rel)}")
    if args.dry_run:
        for rel in report.too_large:
            print(f"  too large for the hub, a sync would not send it: {_safe(rel)}")
    share = _share_args(args)
    if share:
        from atlas_plus.sync import policy as _policy
        from atlas_plus.sync.client import normalize_share as _norm
        declared = _norm(share)
        hub = tuple(h if h.endswith("/") else h + "/" for h in report.hub_prefixes)
        if hub and not any(d == h or d.startswith(h) or h.startswith(d) for d in declared for h in hub):
            print(f"  note: the hub allows only {', '.join(report.hub_prefixes)} for this pairing; "
                  f"your --share ({', '.join(declared)}) is outside it, so nothing can sync")
        elif report.local_files == 0 and not (report.pulled or report.would_pull):
            print("  note: the declared share matches no local file yet (check the prefix spelling)")
    for rel in report.conflicts:
        if args.dry_run:  # nothing is parked in a dry run: it said "is parked at" (AG82)
            print(f"  CONFLICT: {_safe(rel)} -- a sync would keep your copy and park the hub's under "
                  f"{root / '_atlas' / 'sync-conflicts'}")
            continue
        where = report.parked.get(rel) or str(root / "_atlas" / "sync-conflicts")
        both = " (the hub also parked your pushed copy)" if rel in report.push_conflicts else ""
        print(f"  CONFLICT: {_safe(rel)} -- your copy is kept; the hub's copy is parked at {where}{both} "
              "(resolve, then archive or delete the parked copy; docs/HOWTO-sync.md, Conflict handling)")
    if report.proposed:
        print(f"  {len(report.proposed)} proposal(s) awaiting the hub's review (unchanged since you sent them)")
    leftover = root / "_atlas" / "sync-conflicts"
    if not report.conflicts and leftover.exists():
        n = sum(1 for p in leftover.rglob("*") if p.is_file() and not p.name.endswith(".meta.json"))
        if n:
            print(f"  note: {n} parked cop{'y remains' if n == 1 else 'ies remain'} under {leftover} -- archive or delete "
                  "them once resolved so `status` and `doctor` stop counting them")
    import re as _re
    for item in report.errors:
        print(f"  error: {_re.sub(chr(91) + chr(92) + 'x00-' + chr(92) + 'x1f' + chr(92) + 'x7f' + chr(93), ' ', str(item))[:300]}")  # the hub's text is untrusted: no control characters, 300 chars (AG49)
    for rel in report.kept_local_edits:
        print(f"  edited here, deleted on the hub: {_safe(rel)} -- your copy is kept, not pushed")
    if report.remote_deleted or report.local_deleted:
        print(
            f"  deletions reported, not propagated: "
            f"{len(report.remote_deleted)} remote, {len(report.local_deleted)} local"
        )
    return 0 if not report.errors else 1


PROGRESS_MIN = 200    # a sync moving fewer files than this prints no progress
PROGRESS_EVERY = 500  # then one line per this many files


def _progress_printer():
    """A long first sync printed nothing until the end (AG82): a count line, then every N files, on stderr."""
    state = {"last": 0}

    def show(stage: str, done: int, total: int, pulls: int, pushes: int) -> None:
        if total < PROGRESS_MIN:
            return
        if stage == "start":
            print(f"  syncing {total} files ({pulls} to pull, {pushes} to push) ...", file=sys.stderr, flush=True)
            return
        if stage == "checkin":
            print("  checking in with the hub (it re-reads its corpus; this can take a while on a big corpus) ...",
                  file=sys.stderr, flush=True)
            return
        if stage == "end":  # "3002/3002 files" counted a file the hub had refused (AG88)
            if done == 0 and total:
                # "0/300 files transferred (300 not: see below)" read as 300 failures when the sync had
                # only stopped before its first file (the hub went away: MN92, SEX-053 item 5). Not "before
                # any file was transferred": a hub killed mid-request had stored 113 of the 300 (MN95).
                print(f"  stopped: the hub confirmed no file (0 of {total}: see below)", file=sys.stderr, flush=True)
            elif done != total:
                print(f"  {done}/{total} files transferred ({total - done} not: see below)", file=sys.stderr, flush=True)
            elif state["last"] != done:
                print(f"  {done}/{total} files", file=sys.stderr, flush=True)
            state["last"] = done
            return
        if done - state["last"] >= PROGRESS_EVERY or done == total:
            state["last"] = done
            print(f"  {done}/{total} files", file=sys.stderr, flush=True)

    return show


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
            _remote, restored = client.manifest_and_restore()
            hub_policy = _hub_policy(_remote)
            if local_policy is None and (hub_policy is None or hub_policy.kind == "peer"):
                print("checkin refused: the hub pairs this system as a federation PEER (or did not "
                      "say how); declare --share <prefix> first -- nothing was sent", file=sys.stderr)
                return 2
            if hub_policy is not None and hub_policy.kind == "peer":
                # A peer's hash never equals the hub's (its pushes are proposals, not canonical):
                # "current" for a peer means the plan is clean apart from proposals.
                plan, _l, _r = client.plan(root, share=share_list)
                client.checkin({"manifest_hash": local["manifest_hash"]})
                if plan.hub_restored:
                    from atlas_plus.sync.client import RESTORED_HUB_AHEAD
                    print(f"not current: {RESTORED_HUB_AHEAD} (run: {_prog_name()} plan)")
                    return 1
                elif plan.hub_older:
                    from atlas_plus.sync.client import HUB_OLDER
                    print(f"not current: {HUB_OLDER.format(n=len(plan.hub_older))}")
                    return 1
                elif plan.to_pull or plan.to_push or plan.conflicts or plan.too_large:
                    print(f"not current: {plan.summary()} (run: {_prog_name()} sync)")
                    return 1
                else:
                    n = len(plan.proposed)
                    print("in sync with hub" + (f" ({n} proposal(s) awaiting the hub's review)" if n else ""))
                return 0
            result = client.checkin({"manifest_hash": local["manifest_hash"]})
            inert = None
            if result.get("server_manifest_hash") != local["manifest_hash"] and not restored:
                # MN92 F5: a reported delete or a file too large for the hub keeps the hashes apart forever;
                # say that sync will not act on it instead of a bare "different corpus state"
                plan, _l, _r = client.plan(root, share=share_list)
                if not (plan.to_pull or plan.to_push or plan.conflicts or plan.hub_older or plan.hub_restored):
                    _rd = [r for r in plan.remote_deleted if r not in plan.kept_local_edits]
                    inert = (len(_rd), len(plan.local_deleted), len(plan.too_large), len(plan.kept_local_edits))
    except Exception as exc:  # noqa: BLE001 - a bad token or a dead hub is a message, not a traceback
        from atlas_plus.sync.client import token_rejected
        detail = str(exc)
        if "401" in detail:
            detail = token_rejected()
        print(f"checkin refused: {detail}", file=sys.stderr)
        return 2
    if restored:  # it said "different corpus state" about a hub restored from a backup (AG88)
        from atlas_plus.sync.client import RESTORED_HUB_AHEAD
        print(f"not current: {RESTORED_HUB_AHEAD} (run: {_prog_name()} plan)")
        return 1  # "not current" exited 0: a heartbeat script could not tell (SEX-023)
    from atlas_plus.sync.client import HUB_OLDER, hub_older_files
    older = hub_older_files(_remote, local["files"])
    if older:  # it said "different corpus state" and plan then pulled the older copies (SEX-017)
        print(f"not current: {HUB_OLDER.format(n=len(older))}")
        return 1
    same = result.get("server_manifest_hash") == local["manifest_hash"]
    if not same and inert is not None and any(inert):
        n_rd, n_ld, n_big, n_kept = inert
        bits = [f"{n} {what}" for n, what in ((n_rd, "deleted remotely"), (n_ld, "deleted locally"),
                                              (n_kept, "edited here, deleted on the hub"),
                                              (n_big, "too large for the hub")) if n]
        print(f"not current: only differences sync will not act on: {', '.join(bits)}; see `{_prog_name()} plan` "
              "(remove or archive the file on every side to clear it; docs/HOWTO-sync.md, Deletes)")
        return 1
    print("in sync with hub" if same else f"hub has different corpus state (run: {_prog_name()} plan)")
    return 0 if same else 1


def _on_older(args: argparse.Namespace) -> str:
    """The operator's explicit choice for files the hub holds an older copy of (SEX-017)."""
    if getattr(args, "pull_older", False) and getattr(args, "push_newer", False):
        raise SystemExit("--pull-older and --push-newer are opposites: pick one")
    return "pull" if getattr(args, "pull_older", False) else "push" if getattr(args, "push_newer", False) else ""


def cmd_accept_restore(args: argparse.Namespace) -> int:
    """On the hub: the corpus as it is now is the newest (a deliberate rollback), so devices pull it again."""
    root = atlas_paths.memory_root()
    files = manifest.build_manifest(root)["files"]
    accepted = store.accept_current(files)
    if not accepted:
        print("nothing to accept: the hub holds no copy older than one it held before")
        return 0
    # A file the restore removed is not pulled away from the devices (deletes never sync): it was
    # listed under "devices pull them" and then stayed on every device (MN95).
    present = [rel for rel in accepted if rel in files]
    gone = [rel for rel in accepted if rel not in files]
    if present:
        print(f"accepted {len(present)} file(s) as the hub's newest copies; devices pull them at their next sync "
              "(their own copies stay under versions/):")
        for rel in present:
            print(f"  {_safe(rel)}")
    if gone:
        print(f"accepted {len(gone)} file(s) the hub no longer holds; devices KEEP their copies and report them "
              "as deleted remotely (deletes never sync; remove or archive them on each device: docs/HOWTO-sync.md, "
              "Deletes):")
        for rel in gone:
            print(f"  {_safe(rel)}")
    return 0


_VALUE_FLAGS = ("--token", "--hub")
_KNOWN_FLAGS = ("--hub", "--token", "--json", "--dry-run", "--share", "--help", "-h", "--all",
                "--pull-older", "--push-newer")


def _join_dash_values(argv: list[str]) -> list[str]:
    """`--token -AbC...` is "expected one argument" to argparse; 1 token in 64 starts with `-` (AG82)."""
    out: list[str] = []
    i = 0
    while i < len(argv):
        cur = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if cur in _VALUE_FLAGS and nxt is not None and nxt.startswith("-") and nxt != "--" \
                and nxt.split("=", 1)[0] not in _KNOWN_FLAGS:
            out.append(f"{cur}={nxt}")
            i += 2
            continue
        out.append(cur)
        i += 1
    return out


def main() -> None:
    from atlas_plus._console import utf8_stdout_when_piped
    utf8_stdout_when_piped()
    from atlas_plus.branding import prog as _prog
    from atlas_plus import _next_step  # usage errors name --help (LES-054)
    parser = _next_step.ArgumentParser(
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
    p.add_argument("--all", action="store_true", help="include revoked devices and peers")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("revoke", help="revoke a device or peer by name or id")
    p.add_argument("name")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("accept-restore", help="hub-side: take the corpus as it is now as the newest (after a "
                                              "deliberate rollback), so devices stop refusing its older copies")
    p.set_defaults(func=cmd_accept_restore)

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
        if name in ("plan", "sync"):
            p.add_argument("--pull-older", action="store_true",
                           help="take the hub's copies even where they are OLDER than yours (after a deliberate "
                                "rollback on the hub; your copies stay under versions/)")
            p.add_argument("--push-newer", action="store_true",
                           help="put your copies back on the hub where the hub's are OLDER than yours "
                                "(after the hub's corpus was restored from a backup)")
        if name in ("plan", "sync", "checkin"):
            p.add_argument("--share", action="append", metavar="PREFIX",
                           help="corpus prefix this system agrees to exchange (repeatable; "
                                "or ATLAS_SYNC_SHARE=a/,b/). Required when the hub pairs you "
                                "as a peer; nothing outside it is read, sent or pulled")
        p.set_defaults(func=func)

    if len(sys.argv) == 1:  # after the subcommands exist, so the usage line lists them
        parser.print_help()
        print(f"\n{_prog_name()} needs a subcommand -- try: `{_prog_name()} status` (or `{_prog_name()} --help`)", file=sys.stderr)
        raise SystemExit(2)
    args = parser.parse_args(_join_dash_values(sys.argv[1:]))
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
