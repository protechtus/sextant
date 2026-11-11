"""Atlas Brain -- local-first orchestration agent.

A persistent FastAPI service wrapping Ollama that:
  - Holds conversation across sessions (SQLite history)
  - Calls Atlas MCP tools natively via direct Python import
  - Is reachable via Telegram bot and REST/WebSocket through Cloudflare tunnel
  - Respects ATLAS_AUTONOMOUS kill switch on all write tools

Default port: 8767 (ATLAS_BRAIN_PORT)
Default model: qwen3:8b (ATLAS_BRAIN_MODEL)
"""
