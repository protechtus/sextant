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


_MAX_REDIRECTS = 5


def _getaddrinfo(host: str, port: int):
    """The guard's resolver (a hook: tests map a name to an address here, never through the hosts file)."""
    import socket

    return socket.getaddrinfo(host, port)


def _refuse_private_target(url: str, *, what: str = "inbox fetch", allow_env: str | None = "ATLAS_INBOX_FETCH_PRIVATE") -> None:
    """Raise ValueError unless ``url`` is http(s) to a public address.

    A captured link is chosen by whoever sent it: a page that redirects to 127.0.0.1, the LAN
    or a cloud metadata address made the worker fetch a service only this machine can reach and
    put its answer into the model prompt and the stored summary (MN86). Every hop is checked;
    ``ATLAS_INBOX_FETCH_PRIVATE=on`` allows private targets for an intranet-only inbox. The research link checker
    uses the same rule per hop (``what``/``allow_env`` name the caller; AG98).
    """
    import ipaddress
    import os
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{what} refused: only http(s) URLs are fetched ({parts.scheme or 'no'} scheme)")
    if allow_env and os.environ.get(allow_env, "").strip().lower() in ("1", "on", "true", "yes"):
        return
    try:
        infos = _getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme.lower() == "https" else 80))
    except (socket.gaierror, UnicodeError) as exc:
        raise ValueError(f"{what} refused: cannot resolve {parts.hostname!r}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise ValueError(
                f"{what} refused: {parts.hostname} resolves to a non-public address"
                + (f" (set {allow_env}=on to allow it)" if allow_env else ""))


def _default_fetch_url(url: str, timeout: float = 30.0) -> str:
    """Fetch a URL and return its raw text. Imported lazily so tests need not stub httpx.

    Redirects are followed by hand so each hop passes the same public-address check (MN86).
    """
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            _refuse_private_target(url)
            resp = client.get(url, headers={"User-Agent": "Sextant-Inbox/1.0"})
            if resp.is_redirect and resp.headers.get("location"):
                url = str(resp.url.join(resp.headers["location"]))
                continue
            resp.raise_for_status()
            return resp.text
    raise ValueError(f"inbox fetch refused: more than {_MAX_REDIRECTS} redirects")


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
