# HOWTO: Self-host Sextant on a server

**Skill check, honestly:** this guide assumes you are comfortable in a
Linux shell and have met systemd and a reverse proxy before. If that is
not you yet, start with [HOWTO-docker.md](HOWTO-docker.md) -- the
container path makes far fewer assumptions -- and come back here when a
always-on server setup feels worth learning the pieces for.

Run the whole stack on a small VPS (or homelab box) and reach it from every
device. Sextant needs no GPU for its core: the memory server, web dashboard,
and sync hub are lightweight Python services; the local-LLM features (Brain
workers, nightly synthesis) need an Ollama endpoint, which can live on the
same box (CPU models), a machine at home, or be skipped entirely.

Two recipes, in order of recommendation:

- RECIPE A -- Tailscale mesh (private, simplest, no public exposure)
- RECIPE B -- public VPS behind Caddy (HTTPS, reachable from anything)

Both start from the same base install. Commands assume Ubuntu 24.04 LTS and
a user named `sextant`; adjust freely. Windows and macOS installs work the
same way minus systemd (use Task Scheduler / launchd, see ARCHITECTURE.md).

## 0. Base install

Running in a container instead? Stop here and follow [HOWTO-docker.md](HOWTO-docker.md); the
rest of this page is the bare-metal path. (any server)

```bash
sudo adduser --system --group --home /home/sextant sextant
sudo -u sextant -s
cd ~ && git clone https://github.com/protechtus/sextant.git && cd sextant
python3 -m venv .venv    # fails on "ensurepip"? apt install python3-venv, or see the
                         # no-sudo fallback in GETTING-STARTED (venv --without-pip + get-pip)
.venv/bin/pip install -e ".[telegram,summarizer]"

# First-run scaffold + smoke:
.venv/bin/sextant-init
.venv/bin/sextant --version
.venv/bin/sextant-doctor
```

Set the secrets once, in the service environment (never in the repo). At
minimum for a server deployment:

```bash
# /home/sextant/sextant.env   (chmod 600, owned by the sextant user; systemd reads
# KEY=VALUE lines here, no `export`, no quotes needed; comments ONLY on their own
# lines, because systemd keeps a trailing "# ..." as part of the value)
# Where the corpus lives: without these two lines the service serves the sextant
# user's home defaults, which is fine only if that is where you ran sextant-init.
ATLAS_MEMORY_ROOT=/home/sextant/.memory
ATLAS_STATE_DIR=/home/sextant/.atlas
# Memory server bearer (python3 -c "import secrets; print(secrets.token_urlsafe(32))")
ATLAS_MCP_TOKEN=<long random>
# Dashboard key
ATLAS_WEB_API_KEY=<long random>
# Brain API (if running Brain)
ATLAS_BRAIN_API_KEY=<long random>
# Optional: OLLAMA_BASE_URL, ATLAS_TELEGRAM_BOT_TOKEN (+ ATLAS_TELEGRAM_ALLOWED_USERS), ATLAS_NTFY_URL
```

Generate tokens with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.

## 1. systemd units

One unit per service; enable only what you use. The three most useful on a
server: the MCP memory server (your corpus, reachable by MCP clients), the
web dashboard, and the sync hub (device check-ins).

`/etc/systemd/system/sextant-mcp.service`:

```ini
[Unit]
Description=Sextant MCP memory server
After=network.target

[Service]
User=sextant
WorkingDirectory=/home/sextant/sextant
EnvironmentFile=/home/sextant/sextant.env
ExecStart=/home/sextant/sextant/.venv/bin/sextant-mcp
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Repeat the pattern for the others, changing only Description/ExecStart:

| unit | ExecStart |
|---|---|
| sextant-web.service | `.../.venv/bin/sextant-web` |
| sextant-sync.service | `.../.venv/bin/sextant-sync serve` |
| sextant-brain.service | `.../.venv/bin/sextant-brain` (needs Ollama) |

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sextant-mcp sextant-web sextant-sync
systemctl status sextant-mcp
```

Every service binds 127.0.0.1 only (8765 MCP, 8766 web, 8768 sync); nothing
is public yet. That is the point: exposure is a deliberate step, below.

## RECIPE A -- Tailscale (recommended for personal use)

Zero public surface: your devices and the server join a private mesh, and
the loopback services are reached over it.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4          # e.g. 100.101.102.103
```

Have the services listen on the tailnet IP as well (env, per service):
you do not need `ATLAS_WEB_HOST=0.0.0.0`; instead prefer `tailscale serve`, which
proxies the tailnet to loopback with TLS and keeps the bind local:

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8766   # dashboard
sudo tailscale serve --bg --https=8768 http://127.0.0.1:8768  # sync hub
```

From any device on your tailnet:
`https://<server-name>.<tailnet>.ts.net` is the dashboard (key still
required -- transport is not authentication), and the sync hub URL for
`ATLAS_SYNC_HUB` is `https://<server-name>.<tailnet>.ts.net:8768`.

Pair a device (on the SERVER, pairing is deliberately CLI-local):

```bash
.venv/bin/sextant-sync pair laptop
# copy the printed token to the laptop's env as ATLAS_SYNC_DEVICE_TOKEN
```

On the laptop: `sextant-sync plan`, then `sextant-sync sync`. See
HOWTO-sync.md for conflict handling and federation.

## RECIPE B -- public VPS behind Caddy

For reaching the stack from anywhere without a mesh. Caddy terminates HTTPS
with automatic certificates and reverse-proxies to loopback. The app-layer
keys remain the real gate; Caddy adds TLS + a place for extra controls
(basic auth, IP allowlists) if you want belt-and-suspenders.

```bash
sudo apt install -y caddy ufw
sudo ufw default deny incoming
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp
sudo ufw enable
```

`/etc/caddy/Caddyfile` (replace the domain; DNS A record points at the VPS):

```caddy
sextant.example.com {
    # Sync hub under its own path prefix
    handle /sync/* {
        reverse_proxy 127.0.0.1:8768
    }

    # MCP server for remote MCP clients (optional -- omit if unused)
    handle /mcp* {
        reverse_proxy 127.0.0.1:8765
    }

    # Everything else: the web dashboard (fallback handle)
    handle {
        reverse_proxy 127.0.0.1:8766
    }
}
```

(`sextant-mcp` serves HTTP by default and refuses to start without
`ATLAS_MCP_TOKEN`; the `--stdio` flag is only for editor-spawned local use.)

```bash
sudo systemctl reload caddy
curl -s https://sextant.example.com/health          # web liveness
curl -s https://sextant.example.com/sync/v1/health  # sync hub liveness
```

Device config for sync: `ATLAS_SYNC_HUB=https://sextant.example.com`
(the client calls `/sync/v1/...`, which the Caddyfile routes to the hub).

Hardening notes for a public deployment:

- Everything stays useless without the bearer keys; still, prefer exposing
  ONLY what you use (delete the `/mcp*` block if no remote MCP client).
- The web dashboard and MCP server rate-limit and lock out repeated auth
  failures per source (MCP: ten failures, then HTTP 429 for five minutes, the
  correct token included); watch them in the dashboard's security panel. Every
  client behind one NAT shares a counter; `ATLAS_MCP_TRUSTED_PEERS` exempts a
  peer you own (never a public range).
- Prove the remote path from a client machine, not only `claude mcp list`:
  `sextant verify --url https://sextant.example.com/mcp --token "$ATLAS_MCP_TOKEN"`
  (or `python scripts/mcp_roundtrip.py --url ...` from a clone) writes one
  fact and searches it back over HTTP.
- Keep the corpus backed up OFF the VPS: `sextant-backup` (rclone, any
  remote) or a nightly `tar` of `~/.memory` pulled by another machine --
  the corpus is plain markdown, so any file backup works.
- Update cadence: `git pull && .venv/bin/pip install -e ".[...]"` and
  restart units; the suite (`python -m pytest tests -q`) is the smoke test.

## Which pieces need what

| Service | Needs | Skip when |
|---|---|---|
| MCP memory server | nothing extra | you only use the dashboard locally |
| Web dashboard | nothing extra | headless/API-only use |
| Sync hub | nothing extra | single-machine use |
| Brain + workers | an Ollama endpoint (CPU is fine for small models) | you do not want background LLM jobs |
| Telegram bot | a bot token | no mobile control wanted |
| Nightly hygiene jobs | Brain running | -- |

A server that was off during the nightly window catches up on the missed
hygiene jobs automatically at the next Brain start.
