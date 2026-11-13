# HOWTO: Sextant in Docker

The fastest server path: one image, compose picks the roles. Everything
persistent lives on a single named volume (`sextant-data`); the containers
are disposable. Ports bind to the HOST's loopback by default -- exposing
anything to a network stays a deliberate step (Tailscale or a reverse
proxy, exactly as in HOWTO-self-hosting.md).

## Quickstart

```bash
git clone https://github.com/protechtus/sextant.git && cd sextant

# Generate the two keys the stack refuses to run without, into a .env file
# next to docker-compose.yml (compose auto-loads it, so the keys survive new
# terminals and reboots -- a plain `export` would be lost with the shell):
python3 -c "import secrets; print(f'ATLAS_MCP_TOKEN={secrets.token_urlsafe(32)}'); print(f'ATLAS_WEB_API_KEY={secrets.token_urlsafe(32)}')" > .env
cat .env    # your keys; .gitignore already keeps this file out of version control

docker compose up -d mcp web        # memory server :8765 + dashboard :8766
docker compose run --rm mcp sextant-init     # scaffold the corpus (once)
docker compose exec mcp sextant-doctor       # corpus + state check

curl -s http://127.0.0.1:8765/health         # {"status":"ok","service":"atlas-mcp"}
docker compose exec mcp sextant verify       # stdio round-trip inside the image: ROUND-TRIP: OK
docker compose exec mcp sextant search "install verified"   # the fact verify just wrote is on the volume
```

The server answers a few seconds after the container starts (the log line is
`Uvicorn running on`); a curl before that looks like a dead port. To prove a
write over HTTP from the host, the way a remote client will use it:

```bash
set -a; . ./.env; set +a
python scripts/mcp_roundtrip.py --url http://127.0.0.1:8765/mcp --token "$ATLAS_MCP_TOKEN"
```

(`sextant verify --url ...` is the same thing from any machine that has the
package.)

### Without compose: one container by hand

```bash
docker build -t sextant .
docker run -d --name sextant-mcp --restart unless-stopped \
  -p 127.0.0.1:8765:8765 -v sextant-data:/data \
  -e ATLAS_MCP_HOST=0.0.0.0 -e ATLAS_MCP_TOKEN="$ATLAS_MCP_TOKEN" \
  -e ATLAS_MCP_TRUSTED_PEERS=172.17.0.1 \
  sextant sextant-mcp
docker exec sextant-mcp sextant-init
```

`ATLAS_MCP_HOST=0.0.0.0` is required: the default loopback bind inside the
container is unreachable through the port publish, and the port is published
on the host's loopback only, so nothing outside the host can reach it.
`--restart unless-stopped` is what compose's `restart:` gives you; without it
the server stays down after a crash or after Docker (or the host) restarts.
Load the keys first (`set -a; . ./.env; set +a`) so `$ATLAS_MCP_TOKEN` is
set: with it empty the container starts, refuses with "Set ATLAS_MCP_TOKEN",
and the restart policy hides that in a restart loop. Pick your own container
and volume names if another install already uses `sextant-mcp` and
`sextant-data`. `ATLAS_MCP_TRUSTED_PEERS` lists
the Docker bridge gateway because every host client arrives from that address
and ten failed requests from it (one client with a stale token) would otherwise
lock out every client, your correct one included, for five minutes (HTTP 429);
list it only when the port is published on `127.0.0.1`. Installing straight
from a read-only bind mount fails (pip cannot write `egg-info` beside the
source); build the image or copy the source first.

On Windows hosts (Docker Desktop) write the `.env` from PowerShell with
`python -c "..." | Set-Content -Encoding ascii .env` (`python`, not
`python3`, which is a Store stub on many machines): the `>` redirection
there can produce UTF-16 or a byte-order mark, which compose may not read.
Two more Windows traps: in Git Bash, MSYS rewrites a leading `/data` or
`/backup` in a command line into `C:/Program Files/Git/...`, so prefix the
`docker` commands below with `MSYS_NO_PATHCONV=1` (or run them from
PowerShell); and in PowerShell write `"${PWD}:/backup"`, since `"$PWD:/backup"`
is a parse error.

Two things about keys that only show up later:

- A variable already set in your shell **overrides** the `.env` value (compose
  precedence). If `ATLAS_MCP_TOKEN` is exported in your profile, the container
  gets that one, not the file's: every client that reads the file then gets
  `401` and `claude mcp list` says Failed, with no warning from compose.
  `docker compose config | grep TOKEN` shows which won; `unset ATLAS_MCP_TOKEN`
  (or a fresh shell) before `up` fixes it.
- The doctor's Services rows mean little under compose: each container probes
  its own loopback, so the MCP row is up and every other service reads down
  even when it is running. Use `docker compose ps` and the curls for liveness,
  and do not script `sextant-doctor --strict` against a container.

Wire your MCP client at `http://127.0.0.1:8765/mcp` with the bearer token.
For Claude Code that is one command; the token must come from the file, since
a fresh shell has no `$ATLAS_MCP_TOKEN` (an empty header gets a 401):

```bash
set -a; . ./.env; set +a      # load the keys into this shell (bash/zsh)
claude mcp add --transport http sextant http://127.0.0.1:8765/mcp \
  --header "Authorization: Bearer $ATLAS_MCP_TOKEN"
```

The MCP token is a secret too: `claude mcp get sextant` prints the header in
clear and the command line lands in shell history, which is why the example
takes it from the variable. The dashboard is at
`http://127.0.0.1:8766/?key=<ATLAS_WEB_API_KEY>` (the key
lands in browser history and any proxy log; a private window helps). Both
keys are required by the compose file; a missing one stops `docker compose
up` with the variable's name instead of a restart loop. Ports taken on the
host already? Put the host side of each mapping in a
`docker-compose.override.yml` rather than editing the tracked file, and mark
the list `!override`, or compose APPENDS your mapping to the original and
still tries to bind the taken port:

```yaml
services:
  mcp:
    ports: !override ["127.0.0.1:18765:8765"]
  web:
    ports: !override ["127.0.0.1:18766:8766"]
  sync:
    ports: !override ["127.0.0.1:18768:8768"]   # only if you run the sync profile
```

(`!override` needs Compose 2.24 or newer; `docker compose config` shows the
published ports that will actually be used, and the curls above then need
the new ports too.)

## Updating a Compose install

`git checkout main` if the clone is pinned to a commit (a detached-HEAD
`git pull` prints a help page, exits 0, and the next build silently rebuilds
the old source), then `git pull && docker compose build && docker compose up -d`.
The volume is untouched; the first search after the update rebuilds the
keyword index once when the tokenizer changed (the container log says so),
and last week's image still reads the new index if you roll back. Ten wrong
tokens from the host lock the correct client out for five minutes in Compose
too; set `ATLAS_MCP_TRUSTED_PEERS` on the `mcp` service to the Compose
network's gateway (`docker network inspect <project>_default`) if your own
tooling can trip it.

## Alpine and other base images

The image is `python:3.11-slim` (Debian). `python:3.12-alpine` (musl) works
too: `apk add --no-cache git` is the only system package, every native
dependency has a musllinux wheel, and nothing compiles. Two musl notes: the
shipped test suite needs the `telegram` and `summarizer` extras installed or
their tests are skipped, and musl gives non-main threads small stacks, so
the dashboard rejects deeply nested JSON bodies before parsing them rather
than relying on a RecursionError. With `sleep infinity` as PID 1 (a
debugging container) child processes are never reaped; the shipped image
runs the server as PID 1 and needs no `init: true`.

## Your data

One volume, two directories -- the same layout as a bare install:

```bash
docker compose exec mcp ls /data/memory      # the corpus (plain markdown)
docker volume ls | grep sextant-data         # the volume's real name: <project>_sextant-data,
                                             # where <project> is the clone's folder name
                                             # (or what you passed to -p)
docker run --rm -v <project>_sextant-data:/data -v "$PWD:/backup" \
  busybox tar czf /backup/sextant-backup.tgz /data   # backup of corpus AND state (stop the
                                             # containers first for a cold copy; a live copy
                                             # is consistent for notes, not for a mid-write DB)
                                             # (state holds the sync device registry and the task DB)
# restore into a fresh volume:
docker run --rm -v <project>_sextant-data:/data -v "$PWD:/backup" \
  busybox tar xzf /backup/sextant-backup.tgz -C /
```

Editing the corpus from the host: bind-mount instead of the named volume. Put
it in a `docker-compose.override.yml` next to the compose file (compose merges
it automatically, and `git pull` never conflicts):

```yaml
services:
  mcp:
    volumes: ["./my-sextant-data:/data"]
  web:
    volumes: ["./my-sextant-data:/data"]
```

The containers run as uid 1000 (`sextant`); the image
owns `/data` for the named volume, but a bind-mounted host directory keeps its own
owner, so `chown -R 1000:1000 ./my-sextant-data` (or make it world-writable) before
the first start, or the first write fails with permission denied.

## Tearing it down

`docker compose down` stops and removes the containers and keeps the volume,
which is why `up -d` later still finds your notes. `docker compose down -v`
also deletes the volume, corpus included: take the cold backup above first.
Images are never removed by either; they are named after the Compose
project (`sextant-mcp`, `sextant-web` for a clone in a folder called
`sextant`; `docker images | grep sextant` lists them), so two clones never
overwrite each other's build. `docker image rm sextant-mcp sextant-web` if you want
that too.

## Device sync

```bash
docker compose run --rm sync sextant-sync pair laptop   # BEFORE first start:
                                                        # prints the device token once
docker compose --profile sync up -d sync                # hub on :8768
```

(The pair-first order is deliberate: the sync hub refuses a non-loopback
bind with an empty device registry.) Devices point ATLAS_SYNC_HUB at
whatever ingress fronts :8768 -- see HOWTO-sync.md and HOWTO-self-hosting.md.

## Optional: local-LLM layer (Brain + Ollama)

`ATLAS_BRAIN_API_KEY` is required for this profile; the brain refuses to start
without it (the other services ignore it).

```bash
# into .env like the other two keys: an `export` is gone after a reboot, and the brain
# container then restart-loops on an empty key
python3 -c "import secrets; print(f'ATLAS_BRAIN_API_KEY={secrets.token_urlsafe(32)}')" >> .env
docker compose --profile brain up -d
docker compose exec ollama ollama pull qwen3:8b
docker compose exec ollama ollama pull nomic-embed-text   # semantic search
```

Without this profile you still get the memory server, dashboard, and sync;
the Brain/nightly-jobs layer is the part that needs a model runtime.

## Honesty notes

- The engine's daily-driver deployment is bare-metal Windows; the container
  path is CI-proven Linux but has fewer real-world miles. Reports welcome.
- No Windows containers; on Windows hosts use Docker Desktop (WSL2) or the
  bare pip install from the README.
- The image is built from `python:3.11-slim` and the current dependency resolve,
  not from `constraints.txt`; rebuilds on different days can differ. Pin the
  base tag and `ollama/ollama` to a digest if you need reproducible images.
- Scheduled-task conveniences (backup fleet) do not apply in containers;
  use the volume backup pattern above or your orchestrator's scheduling.
