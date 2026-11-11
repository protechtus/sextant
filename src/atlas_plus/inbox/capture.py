"""ATL-026 step 3: build a POST /inbox payload from a free-form capture message.

Pure helpers (no IO) shared by the Telegram command and the bare-URL auto-capture, so the
parsing is unit-tested without mocking Telegram or httpx.
"""

from __future__ import annotations

import re

_URL_RE = re.compile(r"https?://\S+")
_BARE_URL_RE = re.compile(r"^https?://\S+$")
# Leading keyword that flags an item for the deep-research pass.
_RESEARCH_PREFIXES = ("research", "deep", "dig")

_MAX_TEXT = 20000


def is_bare_url(text: str) -> bool:
    """True if the whole message is just a single URL (used for auto-capture of dropped links)."""
    return bool(_BARE_URL_RE.match((text or "").strip()))


def build_capture_payload(text: str, source: str = "telegram") -> dict:
    """Turn a free-form message into a POST /inbox JSON payload.

    - A leading 'research'/'deep'/'dig' keyword sets research=true and is stripped.
    - A message that is exactly a URL becomes {url: ...}.
    - Otherwise the message becomes {text: ...}; if it also contains a URL, that is set too.
    Returns {} when there is nothing capturable.
    """
    raw = (text or "").strip()
    research = False
    parts = raw.split(None, 1)
    if parts and parts[0].lower() in _RESEARCH_PREFIXES:
        research = True
        raw = parts[1].strip() if len(parts) > 1 else ""
    if not raw:
        return {}

    payload: dict = {"source": source, "research": research}
    if _BARE_URL_RE.match(raw):
        payload["url"] = raw
    else:
        payload["text"] = raw[:_MAX_TEXT]
        m = _URL_RE.search(raw)
        if m:
            payload["url"] = m.group(0)
    return payload
