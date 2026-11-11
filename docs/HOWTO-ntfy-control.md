---
title: HOWTO -- Control Brain from the ntfy phone app (inbound channel)
updated: 2026-05-28
---

# ntfy inbound: drive Brain from your phone

Atlas pushes notifications to the ntfy app (outbound). This adds the reverse
direction: send a message from the ntfy app and it routes to Brain, with the
reply pushed back to your phone. It mirrors the Telegram two-way bridge but over
ntfy, and it works even when the Cloudflare tunnel is down, because the listener
makes an outbound connection to ntfy.sh (it opens no inbound port).

Service: `atlas-ntfy-listener` (module `atlas_plus.ntfy.inbound`) -- a plain
long-running console process; Setup below covers running and auto-starting it.

## Security model

A command channel is a control surface, so it is locked down by default:

- It is OPT-IN. With `ATLAS_NTFY_CMD_TOPIC` or `ATLAS_NTFY_CMD_SECRET` unset, the
  listener refuses to start.
- Two factors. The command topic is separate from the notify topic and must be
  secret and high-entropy (ntfy.sh topics are world-publishable, so the topic
  name is only a weak first factor). Every message must then begin with the
  shared secret, checked in constant time (`hmac.compare_digest`).
- Failed auth is logged and dropped with NO reply, so the channel cannot be used
  as an oracle to guess the secret.
- Commands hit Brain with the Brain API key, and Brain's own `ATLAS_AUTONOMOUS`
  gate still applies. A breached channel cannot trigger write tools unless
  autonomous mode is separately enabled. Defense in depth.

## Setup

1. Generate a command topic and a secret:
   ```powershell
   python -c "import secrets; print('atlas-cmd-'+secrets.token_urlsafe(8)); print(secrets.token_urlsafe(24))"
   ```
2. Set both values in the environment the listener will run in (your
   shell, or the service definition if you register one):
   ```powershell
   $env:ATLAS_NTFY_CMD_TOPIC  = "atlas-cmd-XXXXXXXX"   # first line above
   $env:ATLAS_NTFY_CMD_SECRET = "YYYYYYYYYYYYYYYY"      # second line above
   ```
   `ATLAS_NTFY_TOPIC` (your existing notify topic) is reused for replies.
3. In the ntfy phone app, subscribe to BOTH topics: your notify topic (to receive
   replies) and the new command topic (so you can publish to it).
4. Run the listener (a plain long-running console process; keep it running):
   ```powershell
   .\.venv\Scripts\atlas-ntfy-listener.exe
   # or equivalently: .\.venv\Scripts\python.exe -m atlas_plus.ntfy.inbound
   ```
   To start it automatically, register that command with your platform's
   scheduler (Windows Task Scheduler, systemd, etc.) with the two
   environment variables from step 2 in its environment.

## Usage

From the ntfy app, publish to the command topic. Every message starts with the secret:

```
<secret> status                 -> Brain health summary
<secret> task <instruction>     -> queue a Brain task (completion is pushed when done)
<secret> chat <message>         -> interactive Brain chat, reply pushed back
<secret> <anything else>        -> queued as a Brain task (the safe async default)
```

Replies arrive as ntfy notifications on your notify topic, titled "Atlas reply".

## Notes

- The console script and the `python -m atlas_plus.ntfy.inbound` form are
  equivalent; both exist after the normal install.
- Logs go to the console (stderr); redirect to a file in your service
  definition if you run it in the background.
- Outbound completion pushes are deduped (see the `ntfy_sent` ledger), so a task
  queued from your phone produces exactly one completion notification.
