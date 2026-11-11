"""One answer to "where is Ollama": ATLAS_BRAIN_OLLAMA_URL, else the standard OLLAMA_HOST
(host:port or a URL, as the Ollama CLI reads it), else the local default."""
from __future__ import annotations

import os

DEFAULT = "http://localhost:11434"


def default_ollama_url() -> str:
    explicit = os.environ.get("ATLAS_BRAIN_OLLAMA_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    # OLLAMA_BASE_URL is what the dashboard's Map tab reads and what the docs named for the
    # Brain too (self-hoster pass 2026-11-10): honour it before the Ollama CLI's OLLAMA_HOST.
    base = os.environ.get("OLLAMA_BASE_URL", "").strip()
    if base:
        return base.rstrip("/")
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if host:
        if "://" not in host:
            host = "http://" + host
        return host.rstrip("/")
    return DEFAULT
