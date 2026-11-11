"""ATL-026 step 2: auto-summarize an inbox item via the local model.

The orchestration (load item -> get source text -> summarize -> persist) lives here with its
external dependencies injected (URL fetch + model completion), so it is fully unit-testable
without touching the network or a model. The Brain executor (_inbox_summarize) is a thin wrapper.
"""

from __future__ import annotations

import re
from typing import Callable, Optional
from pathlib import Path

from atlas_plus.autonomous import require_autonomous_writes
from atlas_plus.inbox.store import InboxItem, get_item, update_item

_MAX_SOURCE_CHARS = 6000


def _default_fetch_url(url: str, timeout: float = 30.0) -> str:
    """Fetch a URL and return its raw text. Imported lazily so tests need not stub httpx."""
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, headers={"User-Agent": "Atlas-Inbox/1.0"})
        resp.raise_for_status()
        return resp.text


def _default_complete(prompt: str) -> str:
    """Run the local summarizer (Ollama, with the provider layer's own fallbacks)."""
    from atlas_plus.summarizer import providers as summarizer

    text, _provider = summarizer.complete(prompt)
    return text


def _strip_html(html: str) -> str:
    """Crude HTML-to-text: drop script/style, strip tags, collapse whitespace."""
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _split_summary_tags(raw: str) -> tuple[str, list[str]]:
    """Split a model reply into (summary, tags), where tags come from a trailing 'Tags: a, b' line."""
    tags: list[str] = []
    lines = raw.strip().splitlines()
    kept = []
    for ln in lines:
        m = re.match(r"\s*tags\s*:\s*(.+)$", ln, flags=re.IGNORECASE)
        if m:
            tags = [t.strip() for t in re.split(r"[,;]", m.group(1)) if t.strip()][:5]
        else:
            kept.append(ln)
    return "\n".join(kept).strip(), tags


def summarize_item(
    item_id: str,
    *,
    fetch_url: Callable[[str], str] = _default_fetch_url,
    complete: Callable[[str], str] = _default_complete,
    inbox_root: Optional[Path] = None,
    max_chars: int = _MAX_SOURCE_CHARS,
) -> InboxItem:
    """Fetch the item's source, summarize it locally, and persist summary + tags.

    Gated by the autonomous kill switch (this is an autonomous .memory content write). Falls back
    to the item's captured text if the URL cannot be fetched. Raises FileNotFoundError if missing.
    """
    require_autonomous_writes("inbox_summarize")

    it = get_item(item_id, inbox_root)
    if it is None:
        raise FileNotFoundError(f"Inbox item {item_id!r} not found")

    source = ""
    if it.url:
        try:
            source = _strip_html(fetch_url(it.url))[:max_chars]
        except Exception:
            source = ""  # fall back to whatever was captured
    if not source.strip():
        source = it.body[:max_chars]

    prompt = (
        "Summarize the following in 2 to 3 sentences for a quick triage queue. "
        "On a final separate line, list up to 5 short topic tags as 'Tags: a, b, c'.\n\n"
        f"{source}"
    )
    raw = complete(prompt)
    summary, tags = _split_summary_tags(raw)
    if not summary:
        summary = raw.strip()[:500]
    return update_item(
        item_id,
        status="summarized",
        summary=summary,
        tags=tags or None,
        inbox_root=inbox_root,
    )
