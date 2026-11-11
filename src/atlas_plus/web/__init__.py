"""Atlas web UI — FastAPI PWA served over Cloudflare Tunnel.

STATUS: Scaffold only. Implement after ATL-001 (Cloudflare tunnel) is complete.

Architecture (approved 2026-05-04, see docs/adr/013-remote-access-architecture.md):
  - FastAPI backend serving a mobile-first PWA
  - Same Cloudflare Tunnel as the MCP server (or dedicated tunnel)
  - Telegram bot handles interactive commands; web UI handles structured views
  - Claude Code Routines handle scheduled/nightly automation
  - ntfy.sh for push notifications (job done, alerts)

Entry point: atlas-web-ui (port 8766 by default)
"""
