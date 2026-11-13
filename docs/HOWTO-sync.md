# HOWTO: Sextant Sync (devices and system-to-system sharing)

Status: S1 (framework shipped, real-world loop in validation). Hosting
recipes: HOWTO-self-hosting.md; architecture background: ARCHITECTURE-PUBLIC.md.

Sextant Sync keeps the memory corpus (the markdown files) identical across
your machines, and lets two INDEPENDENT Sextant systems share narrow slices
of context. Derived state (search index, task queue, temporal graph) never
syncs; each machine rebuilds it locally.

## Concepts

- HUB: the install that serves `/sync/v1/*` (your main machine or a VPS).
- DEVICE: your own machine, paired with the hub; sees the whole corpus by
  default (minus secrets and derived dirs), can pull and push.
- PEER: someone else's Sextant system; sees NOTHING except the path
  prefixes you explicitly allowlist; their pushes land in a review inbox,
  never directly in your files.
- Secrets never sync. `.env*`, key material, sqlite files and friends are
  fenced in code (ADR-016 Tier 3); this is not configurable.

## Hub setup

```bash
# 1. Pair a device (LOCAL action on the hub; the token prints ONCE):
sextant-sync pair laptop

# 2. Run the hub (loopback by default; put your ingress in front):
sextant-sync serve                  # 127.0.0.1:8768

# See who is paired and when they last checked in:
sextant-sync devices
sextant-sync revoke laptop          # kill a lost device's access
```

Remote exposure is your ingress choice (in order of recommendation):
Tailscale/WireGuard mesh, Cloudflare tunnel + Access, or a reverse proxy
with HTTPS on a VPS. The hub itself never terminates public traffic.

## Device setup (the spoke)

```bash
# On the device, set (and persist in your shell profile) the hub address
# and the pairing token you were shown:
export ATLAS_SYNC_HUB=https://sync.example.com     # or http://100.x.y.z:8768 on a tailnet
export ATLAS_SYNC_DEVICE_TOKEN=paste-token-here

sextant-sync plan        # what would happen (no changes)
sextant-sync sync        # pull + push; conflicts quarantine for review
sextant-sync checkin     # cheap "am I current?" heartbeat
sextant-sync status      # local corpus, conflicts, inbox
```

If you ran `sextant-init` on the device, its template `identity/user.md`
differs from the hub's real one, so the very first sync reports a
conflict and parks the hub's copy under `_atlas/sync-conflicts/`. That is
the design working: copy the parked file over the template (or delete the
template before the first sync) and the next sync is clean. A device
initialised by a build older than 2026-11-13 on Windows may report a
conflict on every scaffold file (they were written with CRLF line endings;
the hub's are LF): take the hub's copies the same way, once. Sync compares
bytes, and every build now writes LF on every OS.

The hub caches its manifest for ten seconds, so `sextant-sync plan` right
after a write on the hub can say "in sync" until the next check.

## Conflict handling

If both sides changed the same file, NOTHING is overwritten: the other
side's copy is parked under `_atlas/sync-conflicts/<date>/...` with a
`.meta.json` explaining why, your copy stays live, and `sextant-sync status`
plus `sextant-doctor` show the pending count. The hub parks a copy too when a
push arrives against a file it changed meanwhile (its `status` counts it the
same way, and your side parks the hub's copy in the same sync, so one run
shows both). Resolving means making your canonical file agree with the other
side (edit it, or copy the parked copy over it); the parked copy is only a
copy. Archive or delete it afterwards so `status` stops counting it; anything
under a subdirectory of `_atlas/` never syncs, so archiving it there is safe.
The `sync` output names the parked path.

## Sharing with another system (federation)

```bash
# Allowlist exactly what they may see (repeatable --prefix):
sextant-sync pair friend --kind peer --prefix reference/sip --scope rw
```

A prefix is a DIRECTORY (`reference/sip/`). A topic written with
`--scope reference:sip` lives in the file `reference/sip.md`, which that
prefix does not cover: share `reference/` or keep shared topics in a folder.

The peer syncs against your hub like a device, but their manifest view is
just the allowlist, and anything they push lands in
`_atlas/sync-inbox/friend/` as a proposal with provenance. Review it and
move what you want into the corpus; nothing under `_atlas/` is indexed,
searched or primed, so a proposal reaches your assistant only after you do.

The allowlist above is the HUB's policy: what the hub shows and accepts.
It cannot decide what the other system is willing to send. The spoke side
of a federation declares its own outbound share, and nothing outside it is
read, serialized or sent (or pulled in):

```bash
# on the peer's machine (repeatable; or ATLAS_SYNC_SHARE=reference/sip/)
sextant-sync sync --hub https://hub.example --token <token> --share reference/sip
```

A peer's pushes are proposals, so a peer is "in sync" when its plan is
clean apart from unchanged proposals; `checkin` and `plan` say how many are
awaiting the hub's review, and an unchanged proposal is never sent twice.

When a hub reports that it pairs you as a peer, or does not say how it
pairs you (a hub older than 2026-11-10), and you have not declared a share,
`sync`, `plan` and `checkin` refuse before anything is read ("declare what
may leave this system"). Syncing with your OWN hub as a device needs no
share: the whole corpus is the point. The hub's own allowlist (its
`--prefix` at pair time) is intersected with your share, so a file the hub
would refuse is never read or sent either.

A conflict stays a conflict until you resolve it: every sync reports it
again and keeps both canonical copies (an identical parked copy is not
parked twice). Resolving means making your canonical file agree with the
hub's (edit it, or copy the parked version over it); the parked copy itself
is only a copy (deleting it clears the count but resolves nothing). The
three-way base for that file advances only once the two sides agree again. If the hub deletes a file
you edited, the sync reports it as deleted remotely and leaves your copy.

## Deletes

Never propagated automatically (archive-not-delete). A file deleted on one
side is REPORTED by `sextant-sync plan` and left alone; deliberate removal is
an operator action on each side.
