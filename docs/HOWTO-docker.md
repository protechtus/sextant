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
docker compose exec mcp sextant search "installed by an agent"   # the fact is on the volume
```

On Windows hosts (Docker Desktop) write the `.env` from PowerShell with
`python -c "..." | Set-Content -Encoding ascii .env`: the `>` redirection
there can produce UTF-16, which compose cannot read.

Two things about keys that only show up later:

- A variable already set in your shell **overrides** the `.env` value (compose
  precedence). If `ATLAS_MCP_TOKEN` is exported in your profile, the container
  gets that one, not the file's; `docker compose config | grep TOKEN` shows
  which won.
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

The dashboard is at `http://127.0.0.1:8766/?key=<ATLAS_WEB_API_KEY>` (the key
lands in browser history and any proxy log; a private window helps). Both
keys are required by the compose file; a missing one stops `docker compose
up` with the variable's name instead of a restart loop. Ports taken on the
host already? Put the host side of each mapping in a
`docker-compose.override.yml` (`ports: ["127.0.0.1:18765:8765"]`) rather
than editing the tracked file.

## Your data

One volume, two directories -- the same layout as a bare install:

```bash
docker compose exec mcp ls /data/memory      # the corpus (plain markdown)
docker volume ls | grep sextant-data         # the volume's real name: <project>_sextant-data,
                                             # where <project> is the clone's folder name
docker run --rm -v <project>_sextant-data:/data -v "$PWD:/backup" \
  busybox tar czf /backup/sextant-backup.tgz /data   # cold backup of corpus AND state
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
