"""research_search -- query the local research RAG via its HTTP service.

Thin client of `research_api.py` (127.0.0.1:8769, in the research venv). The heavy
embedding/index dependencies live in that venv, not here; this tool just forwards
the query over localhost and returns cited, quality-ranked passages. Source-of-truth
by default (verified_only=True). Never raises -- returns {"error": ...} on failure.

Config (env): ATLAS_RESEARCH_API_URL (default http://127.0.0.1:8769), RESEARCH_API_KEY.
"""

import os

RESEARCH_API_URL = os.environ.get("ATLAS_RESEARCH_API_URL", "http://127.0.0.1:8769").rstrip("/")


def research_search(
    query: str,
    top_k: int = 5,
    verified_only: bool = True,
    min_tier: str = "B",
) -> dict:
    """Forward a query to the research RAG service and return cited passages."""
    if not query or not query.strip():
        return {"error": "empty query", "results": []}
    api_key = os.environ.get("RESEARCH_API_KEY", "")
    if not api_key:
        return {"error": "RESEARCH_API_KEY not set; research service unavailable.", "results": []}

    import httpx
    try:
        resp = httpx.get(
            f"{RESEARCH_API_URL}/search",
            params={
                "q": query.strip(),
                "k": max(1, min(int(top_k), 50)),
                "verified_only": str(bool(verified_only)).lower(),
                "min_tier": min_tier,
            },
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        if resp.status_code == 401:
            return {"error": "research service auth failed (RESEARCH_API_KEY mismatch).", "results": []}
        resp.raise_for_status()
        return resp.json()
    except httpx.ConnectError:
        return {
            "error": "optional research service not reachable (ATLAS_RESEARCH_API_URL); research search is disabled.",
            "results": [],
        }
    except Exception as exc:  # noqa: BLE001 -- best-effort, never raise out of a tool
        return {"error": f"research search failed: {exc}", "results": []}
