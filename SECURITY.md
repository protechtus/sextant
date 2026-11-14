# Security policy

## Reporting a vulnerability

Please do NOT open a public issue for security problems. Use GitHub's private
vulnerability reporting on this repository ("Report a vulnerability" under the
Security tab). You will get an acknowledgement within a few days.

Private vulnerability reporting is enabled on this repository. If the button is ever missing, email hello@protechtus.com with the subject `Sextant security` (no details in the subject), or open an issue from any template (blank issues are off) titled `security contact request` with no details in it; the maintainer replies within a few days with a private channel. Never put the vulnerability itself in a public issue.

## Scope

Sextant is designed to run on a machine you own, binding loopback by default.
Reports most valuable to this project:

- Auth bypass on any network surface (MCP server, Brain API, web UI, bots)
- Path traversal or memory-corpus escape via tool inputs
- Secrets leaking into logs, transcripts, or derived state
- Anything that lets a remote party reach a write path with
  `ATLAS_AUTONOMOUS` off

Out of scope: issues that require the attacker to already have local code
execution as the same user, and self-inflicted misconfiguration (for example
deliberately binding services to 0.0.0.0 without auth).

## Behind a reverse proxy

Every service counts failed authentication attempts per source and locks a
source out after ten failures (the dashboard, Brain and sync hub count over
a minute and lock for a minute; the MCP server counts over five minutes and
locks for five). The source is the TCP peer, except when the peer is
loopback (a tunnel or reverse proxy on the same host): then it is the
address in `Cf-Connecting-Ip` (or the header named by
`ATLAS_FORWARDED_IP_HEADER`), else the last non-loopback hop of
`X-Forwarded-For`, which is the hop the proxy on this host appended (an
appending proxy leaves the first hop client-chosen). A proxy that is not on
loopback is trusted the same way once it is listed in `ATLAS_MCP_TRUSTED_PEERS`
(the dashboard reads `ATLAS_WEB_TRUSTED_PEERS` and the sync hub
`ATLAS_SYNC_TRUSTED_PEERS`, each falling back to that list). A forwarded value
that is not an IP address counts as its own bounded source (it is never the
loopback exemption). A failure counts against the forwarded
client only, so one locked client does not lock the proxy's other clients, and
a flood through one LISTED proxy, whatever names it forwards, locks the proxy
itself after ten times the per-client limit; a proxy on loopback is the
operator's own machine and is never locked, so it must overwrite the forwarded
header (Recipe B's `header_up`), never pass a client's through. A forwarded address that is itself
loopback (`127.0.0.1`, `::1`, `[::1]`, `::ffff:127.0.0.1`), arriving from a proxy on this host, is the
operator and is never counted or locked; behind a listed proxy on another address a forwarded loopback
value is counted like any other source. Docker's published port is a NAT, not a
proxy: it adds no header, so listing the compose gateway separates nothing for
clients that reach the port directly. Sextant reads these
headers itself and starts its servers with uvicorn's own proxy-header
handling off, so this paragraph is the whole rule. Two consequences to plan
for: a proxy that strips those headers makes every client look like the
operator's own loopback, which is never locked out, so the limiter is off;
a proxy that forwards a client-supplied `Cf-Connecting-Ip` unchanged lets a
client choose the address that is counted. Configure the proxy to overwrite
that header with the real client address (Caddy: `header_up
Cf-Connecting-Ip {remote_host}`; nginx: `proxy_set_header Cf-Connecting-Ip
$remote_addr`), and set `ATLAS_FORWARDED_IP_HEADER=none` if no proxy will
ever sit in front: then every forwarded header is ignored. Keys shorter
than 16 characters are refused at startup on every service, the ntfy command
listener's shared secret included. One exemption exists: sources listed in
`ATLAS_MCP_TRUSTED_PEERS` (a NAT gateway, the Docker bridge) are never locked
out of the MCP server and their failures are not counted; never list a public
range there.

## Supported versions

The `main` branch. Fixes land there; there are no long-term support branches.
