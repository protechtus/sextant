# Security policy

## Reporting a vulnerability

Please do NOT open a public issue for security problems. Use GitHub's private
vulnerability reporting on this repository ("Report a vulnerability" under the
Security tab). You will get an acknowledgement within a few days.

If that button is missing (GitHub only offers it once the repository is public and the maintainer has enabled it), open an issue from any template (blank issues are off) titled `security contact request` with no details in it; the maintainer replies within a few days with a private channel. Never put the vulnerability itself in a public issue.

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
appending proxy leaves the first hop client-chosen). Sextant reads these
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
than 16 characters are refused at startup on every service.

## Supported versions

The `main` branch. Fixes land there; there are no long-term support branches.
