"""Transcript capture sources.

Each source is an independent process or thread that writes through
`TranscriptStore`. A crash in one source cannot affect others.

Modules:
    claude_code     watchdog tailer for ~/.claude/projects/*.jsonl
    openai_proxy    forwarding HTTP proxy on :11435 for OpenAI-compatible APIs
    ollama_native   forwarding HTTP proxy on :11436 for Ollama /api/chat,/api/generate
"""
