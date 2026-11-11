"""Research report URL validation -- check that references: frontmatter links are still reachable.

Uses httpx for concurrent HTTP HEAD requests with a short timeout. URLs returning
4xx/5xx or connection errors are flagged as broken. 403 responses are flagged as
"gated" (auth-required, not necessarily broken).

Designed to run against .atlas/reports/ after inject_references_frontmatter() has
populated references: lists. Can optionally remove dead URLs from frontmatter.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# Skip domains known to require browser/auth (block bots regardless of URL validity)
_SKIP_DOMAINS = {
    "doi.org", "arxiv.org", "linkedin.com", "twitter.com", "x.com",
    "facebook.com", "instagram.com", "researchgate.net", "jstor.org",
    "springer.com", "sciencedirect.com", "acm.org",
}
_TIMEOUT_SECS = 5.0
_MAX_WORKERS = 8


def _should_skip(url: str) -> bool:
    """Return True for domains that require auth or block automated checks."""
    try:
        match = re.search(r"https?://([^/]+)", url)
        if not match:
            return True
        domain = match.group(1).lower().lstrip("www.")
        return any(domain == s or domain.endswith("." + s) for s in _SKIP_DOMAINS)
    except Exception:
        return True


def check_url(url: str) -> dict:
    """Perform a HEAD (then GET fallback) request for url.

    Returns {url, status, ok, gated, skipped, error}.
      ok:      HTTP 200-399 (reachable)
      gated:   HTTP 401/403 (auth-required, may still exist)
      skipped: domain on skip list
      error:   connection/timeout error text, or None
    """
    if _should_skip(url):
        return {"url": url, "status": None, "ok": True, "gated": False, "skipped": True, "error": None}

    try:
        import httpx
        headers = {
            "User-Agent": "Atlas-LinkChecker/1.0 (research corpus health check)",
            "Accept": "*/*",
        }
        with httpx.Client(timeout=_TIMEOUT_SECS, follow_redirects=True) as client:
            try:
                r = client.head(url, headers=headers)
                status = r.status_code
            except httpx.RequestError:
                # Some servers reject HEAD -- fall back to GET with a small range
                r = client.get(url, headers={**headers, "Range": "bytes=0-0"})
                status = r.status_code

        if status in (401, 403):
            return {"url": url, "status": status, "ok": True, "gated": True, "skipped": False, "error": None}
        ok = 200 <= status < 400
        return {"url": url, "status": status, "ok": ok, "gated": False, "skipped": False, "error": None}
    except Exception as exc:
        return {"url": url, "status": None, "ok": False, "gated": False, "skipped": False,
                "error": str(exc)[:120]}


def check_report_links(report_path: Path, workers: int = _MAX_WORKERS) -> list[dict]:
    """Check all references: URLs in a research report's frontmatter.

    Runs URL checks concurrently. Returns list of check result dicts
    (one per non-skipped URL), sorted with broken links first.
    Returns [] when the file has no references: frontmatter or cannot be read.
    """
    import yaml as _yaml
    from concurrent.futures import ThreadPoolExecutor, as_completed

    try:
        raw = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    if not raw.startswith("---"):
        return []
    end = raw.find("\n---", 3)
    if end == -1:
        return []
    try:
        fm = _yaml.safe_load(raw[3:end]) or {}
    except Exception:
        return []

    refs = fm.get("references") or []
    if not isinstance(refs, list) or not refs:
        return []

    urls = [str(u) for u in refs if u]
    results = []
    with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as ex:
        futures = {ex.submit(check_url, url): url for url in urls}
        for fut in as_completed(futures):
            results.append(fut.result())

    results.sort(key=lambda r: (r["ok"], r.get("gated", False)))
    return results


def remove_broken_links(report_path: Path, broken_urls: list[str]) -> bool:
    """Remove specified broken URLs from a report's references: frontmatter.

    Idempotent: URLs not present are silently ignored.
    Returns True if the file was modified, False otherwise.
    """
    if not broken_urls:
        return False
    import yaml as _yaml

    try:
        raw = report_path.read_text(encoding="utf-8")
    except OSError:
        return False

    if not raw.startswith("---"):
        return False
    end = raw.find("\n---", 3)
    if end == -1:
        return False

    try:
        fm = _yaml.safe_load(raw[3:end]) or {}
    except Exception:
        return False

    existing_refs = list(fm.get("references") or [])
    broken_set = set(broken_urls)
    new_refs = [r for r in existing_refs if str(r) not in broken_set]

    if len(new_refs) == len(existing_refs):
        return False  # nothing to remove

    # Rebuild frontmatter without old references block
    fm_lines = raw[3:end].splitlines()
    new_fm_lines = []
    inside_refs = False
    for line in fm_lines:
        if line.startswith("references:"):
            inside_refs = True
            continue
        if inside_refs:
            stripped = line.lstrip()
            if stripped.startswith("- "):
                continue
            inside_refs = False
        new_fm_lines.append(line)

    if new_refs:
        new_fm_lines.append("references:")
        for url in new_refs:
            new_fm_lines.append(f"  - {url}")

    new_raw = "---" + "\n".join(new_fm_lines) + "\n---" + raw[end + 4:]
    try:
        report_path.write_text(new_raw, encoding="utf-8")
        return True
    except OSError:
        return False
