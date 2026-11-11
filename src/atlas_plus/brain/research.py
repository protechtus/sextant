"""Atlas Brain research agent -- autonomous investigation loop (ATL-N05).

Takes a research topic, searches memory for existing context, synthesizes
findings using the brain model, and writes a structured report to
.atlas/reports/research-<slug>-<date>.md.

Can be triggered via:
  - Telegram /research <topic>
  - Brain task with agent=research
  - POST /task with agent=research

Optional web search (requires ATLAS_RESEARCH_WEB_SEARCH=on):
  - DuckDuckGo Instant Answer API (no API key required)
  - SearXNG (requires ATLAS_RESEARCH_SEARXNG_URL)
  Results are cached in .atlas/research-cache/ and included in the prompt.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from atlas_plus import paths as atlas_paths
from atlas_plus.autonomous import autonomous_enabled

log = logging.getLogger("atlas.brain.research")

_ATLAS_DIR = atlas_paths.state_dir()
_MEMORY_ROOT = atlas_paths.memory_root()
_RESEARCH_CACHE_DIR = _ATLAS_DIR / "research-cache"

_DDG_API = "https://api.duckduckgo.com/"
_WEB_SEARCH_ENABLED = os.environ.get("ATLAS_RESEARCH_WEB_SEARCH", "").lower() in ("on", "1", "true")
_SEARXNG_URL = os.environ.get("ATLAS_RESEARCH_SEARXNG_URL", "").rstrip("/")
_WEB_SEARCH_TTL = int(os.environ.get("ATLAS_RESEARCH_CACHE_TTL", "3600"))  # seconds

# URL patterns for citation extraction
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^\)\s]+)\)")
_BARE_URL_RE = re.compile(r"(?<!\()https?://[^\s\)\]\>\"\'\`,]+")


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[\s_]+", "-", text)[:40].strip("-")


def _cache_path(slug: str) -> Path:
    _RESEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _RESEARCH_CACHE_DIR / f"{slug}.json"


def _load_cache(slug: str) -> Optional[list[str]]:
    p = _cache_path(slug)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - data.get("ts", 0) < _WEB_SEARCH_TTL:
            return data.get("results", [])
    except Exception:
        pass
    return None


def _save_cache(slug: str, results: list[str]) -> None:
    try:
        _cache_path(slug).write_text(
            json.dumps({"ts": time.time(), "results": results}), encoding="utf-8"
        )
    except Exception as exc:
        log.debug("Research cache write failed for %s: %s", slug, exc)


def _web_search_duckduckgo(query: str, max_results: int = 6) -> list[str]:
    """Fetch DuckDuckGo Instant Answer API results. Returns snippet strings.

    Uses the free DDG JSON API (no API key). Returns abstract + related topics.
    Empty list on any failure -- callers treat web search as best-effort.
    """
    import httpx
    try:
        resp = httpx.get(
            _DDG_API,
            params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            timeout=8.0,
            follow_redirects=True,
            headers={"User-Agent": "Atlas-Research-Agent/1.0"},
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception as exc:
        log.debug("DDG web search error for %r: %s", query, exc)
        return []

    snippets: list[str] = []

    abstract = data.get("AbstractText", "").strip()
    abstract_url = data.get("AbstractURL", "").strip()
    if abstract:
        src = f" [{abstract_url}]" if abstract_url else ""
        snippets.append(f"[DDG Abstract{src}] {abstract}")

    for topic in data.get("RelatedTopics", []):
        if len(snippets) >= max_results:
            break
        if isinstance(topic, dict) and topic.get("Text"):
            url = topic.get("FirstURL", "")
            src = f" [{url}]" if url else ""
            snippets.append(f"[DDG Related{src}] {topic['Text'][:200]}")

    for result in data.get("Results", []):
        if len(snippets) >= max_results:
            break
        if isinstance(result, dict) and result.get("Text"):
            url = result.get("FirstURL", "")
            src = f" [{url}]" if url else ""
            snippets.append(f"[DDG Result{src}] {result['Text'][:200]}")

    return snippets[:max_results]


def _web_search_searxng(query: str, max_results: int = 6) -> list[str]:
    """Fetch results from a local SearXNG instance.

    Requires ATLAS_RESEARCH_SEARXNG_URL set to base URL (e.g. http://localhost:8888).
    Returns empty list when SearXNG is unreachable.
    """
    if not _SEARXNG_URL:
        return []
    import httpx
    try:
        resp = httpx.get(
            f"{_SEARXNG_URL}/search",
            params={"q": query, "format": "json", "engines": "general"},
            timeout=10.0,
            headers={"User-Agent": "Atlas-Research-Agent/1.0"},
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except Exception as exc:
        log.debug("SearXNG search error for %r: %s", query, exc)
        return []

    snippets = []
    for r in data.get("results", [])[:max_results]:
        url = r.get("url", "")
        title = r.get("title", "")
        content = r.get("content", "")[:200]
        src = f" [{url}]" if url else ""
        text = f"{title}: {content}" if title and content else (title or content)
        if text:
            snippets.append(f"[SearXNG{src}] {text}")
    return snippets


def _web_search(topic: str) -> list[str]:
    """Dispatch to configured web search backend with caching.

    Returns empty list when ATLAS_RESEARCH_WEB_SEARCH is not 'on'.
    """
    if not _WEB_SEARCH_ENABLED:
        return []

    slug = _slugify(topic)
    cached = _load_cache(slug)
    if cached is not None:
        log.debug("Web search cache hit for %r (%d results)", topic, len(cached))
        return cached

    results: list[str] = []
    if _SEARXNG_URL:
        results = _web_search_searxng(topic)
        log.debug("SearXNG returned %d results for %r", len(results), topic)

    if not results:
        results = _web_search_duckduckgo(topic)
        log.debug("DDG returned %d results for %r", len(results), topic)

    _save_cache(slug, results)
    return results


def _gather_memory_context(topic: str) -> list[str]:
    """Search .memory/ for relevant existing context. Returns list of snippet strings."""
    try:
        from atlas_plus.mcp.memory_server.fts import search as mem_search
        results = mem_search(topic, limit=8)
        return [f"[{r['path']}] {r['snippet']}" for r in results]
    except Exception as exc:
        log.warning("Memory search failed for topic %r: %s", topic, exc)
        return []


def _gather_temporal_context(topic: str, limit: int = 5) -> list[str]:
    """Query the temporal graph for historical facts related to the topic.

    Returns empty list when the graph is empty or the query fails.
    """
    try:
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        provider = FileTemporalProvider()
        if provider.stats().get("facts_active", 0) == 0:
            return []
        results = provider.search_current(topic, limit=limit)
        snippets = []
        for r in results:
            entity = r.entity
            subj = entity.name if entity else r.fact.subject_id
            snippets.append(
                f"[temporal:{r.fact.valid_from[:10]}] {subj}: {r.fact.fact_text}"
            )
        return snippets
    except Exception as exc:
        log.debug("Temporal context failed for topic %r: %s", topic, exc)
        return []


_DEDUP_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "are", "have", "from",
    "not", "use", "used", "will", "been", "into", "over", "when", "what",
    "which", "such", "more", "than", "also", "any", "all", "its", "our",
    "should", "always", "never", "can", "may", "must", "was", "how",
}


def _topic_tokens(text: str) -> set[str]:
    # Split on non-alpha chars so hyphenated slugs become separate words:
    # "sip-registration-flow" -> ["sip", "registration", "flow"]
    return {
        w.lower() for w in re.findall(r"[a-zA-Z]{4,}", text)
        if w.lower() not in _DEDUP_STOPWORDS
    }


def find_similar_research_reports(
    topic: str,
    threshold: float = 0.5,
    max_age_days: int = 30,
) -> list[dict]:
    """Search .atlas/reports/ for research reports similar to the given topic.

    Uses two complementary signals (max of the two):
      - Primary: Jaccard similarity on title + slug tokens (>= 4 chars, stopwords removed)
      - Secondary: body recall (fraction of topic tokens in first ~300 body words) * 0.6
        Catches cases where the title differs but the body covers the same topic.

    Returns list of {file, title, date, score} sorted by similarity descending,
    filtered to reports within max_age_days and above threshold.

    Useful for deduplication checks before starting a new research task.
    Returns [] when no similar reports exist or the reports directory is missing.
    """
    reports_dir = _ATLAS_DIR / "reports"
    if not reports_dir.exists():
        return []

    topic_tokens = _topic_tokens(topic)
    if not topic_tokens:
        return []

    from datetime import date
    today = date.today()
    cutoff_ordinal = today.toordinal() - max_age_days
    results = []
    import yaml as _yaml

    for md_file in sorted(reports_dir.glob("*.md"), reverse=True):
        if md_file.name.startswith("_"):
            continue
        try:
            raw = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not raw.startswith("---"):
            continue
        end = raw.find("\n---", 3)
        if end == -1:
            continue
        try:
            fm = _yaml.safe_load(raw[3:end]) or {}
        except Exception:
            continue

        # Age filter
        date_str = str(fm.get("date") or fm.get("report_date") or "")[:10]
        if date_str:
            try:
                report_date = date.fromisoformat(date_str)
                if report_date.toordinal() < cutoff_ordinal:
                    continue
            except ValueError:
                pass

        # Primary: Jaccard similarity on title + slug
        title = str(fm.get("title", md_file.stem))
        slug = str(fm.get("slug", ""))
        candidate_tokens = _topic_tokens(title + " " + slug)
        if not candidate_tokens:
            continue

        intersection = len(topic_tokens & candidate_tokens)
        union = len(topic_tokens | candidate_tokens)
        score = intersection / union if union else 0.0

        # Secondary: body recall -- fraction of topic tokens appearing in first 300 body words.
        # Catches cases where the title differs but the body covers the same topic.
        body_section = raw[end + 4:end + 4 + 2000]  # ~300-400 words
        body_tokens = _topic_tokens(body_section)
        body_recall = len(topic_tokens & body_tokens) / len(topic_tokens) if topic_tokens else 0.0

        # Include if primary Jaccard meets threshold OR body coverage is high
        # with a weak primary signal (topic appears throughout the body but differently titled).
        combined_score = max(score, body_recall * 0.6)  # body recall weighted at 60%

        if combined_score >= threshold:
            results.append({
                "file": md_file.name,
                "title": title,
                "date": date_str,
                "score": round(combined_score, 3),
            })

    results.sort(key=lambda r: -r["score"])
    return results


_QUALITY_GATE_MIN_WORDS = int(os.environ.get("ATLAS_RESEARCH_MIN_WORDS", "200"))
_QUALITY_GATE_WARN_CITATIONS = True  # warn when 0 references in frontmatter


def _apply_quality_gate(report_path: Path, report_body: str) -> None:
    """Add quality_gate_passed/quality_gate_flags frontmatter after report is written.

    Flags are informational -- the report is not deleted. Downstream tools
    (scheduler, Telegram /research_index) can use these fields to highlight
    low-quality reports for re-investigation.

    Checks:
      - word_count_low: body < ATLAS_RESEARCH_MIN_WORDS (default 200) words
      - no_citations:   references: frontmatter list is empty or absent
    """
    import re as _re
    import yaml as _yaml

    min_words = int(os.environ.get("ATLAS_RESEARCH_MIN_WORDS", str(_QUALITY_GATE_MIN_WORDS)))

    try:
        raw = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return

    # Parse frontmatter
    if not raw.startswith("---"):
        return
    end = raw.find("\n---", 3)
    if end == -1:
        return
    try:
        fm = _yaml.safe_load(raw[3:end]) or {}
    except Exception:
        return

    body = raw[end + 4:]
    word_count = len(_re.findall(r"\b\w+\b", body))
    citation_count = len(fm.get("references") or [])

    flags: list[str] = []
    if word_count < min_words:
        flags.append("word_count_low")
        log.warning(
            "Research quality gate: %s word count %d < %d (ATLAS_RESEARCH_MIN_WORDS)",
            report_path.name, word_count, min_words,
        )
    if citation_count == 0:
        flags.append("no_citations")
        log.warning("Research quality gate: %s has no citations", report_path.name)

    gate_passed = len(flags) == 0

    # Inject quality_gate_* fields idempotently
    new_lines = []
    in_fm = False
    injected = False
    for line in raw.split("\n"):
        if line.startswith("---") and not in_fm:
            in_fm = True
            new_lines.append(line)
            continue
        if line == "---" and in_fm:
            if not injected:
                new_lines.append(f"quality_gate_passed: {'true' if gate_passed else 'false'}")
                if flags:
                    new_lines.append("quality_gate_flags:")
                    for f in flags:
                        new_lines.append(f"  - {f}")
                injected = True
            in_fm = False
            new_lines.append(line)
            continue
        if in_fm and line.startswith("quality_gate_"):
            continue  # drop old gate fields
        new_lines.append(line)

    if injected:
        try:
            report_path.write_text("\n".join(new_lines), encoding="utf-8")
        except OSError as exc:
            log.warning("Quality gate write failed for %s: %s", report_path.name, exc)


def run_research(topic: str, session_id: str, force: bool = False) -> tuple[str, Path]:
    """Run a research investigation on a topic.

    Steps:
      1. Deduplication check: if a similar report exists (<= 30d, sim >= 0.5)
         and force=False, raise ValueError with the existing report path.
      2. Search .memory/ for existing context
      3. Synthesize with brain model
      4. Write structured report to .atlas/reports/
      5. Link from .memory/reference/ when autonomous
      6. Inject citation URLs into frontmatter references: field
      7. Inject quality_score + quality_factors into frontmatter
     10. Apply quality gate: flag word_count_low / no_citations (quality_gate_passed frontmatter)

    Args:
        topic:      Research topic text.
        session_id: Brain/Claude session identifier.
        force:      Skip deduplication check and always create a new report.

    Returns (report_markdown_body, report_path).
    Raises ValueError when a similar recent report exists and force=False.
    """
    # Step 0: deduplication check (skip when force=True or no similar reports found)
    _dedup_days = int(os.environ.get("ATLAS_RESEARCH_DEDUP_DAYS", "30"))
    if not force:
        similar = find_similar_research_reports(topic, threshold=0.5, max_age_days=_dedup_days)
        if similar:
            best = similar[0]
            raise ValueError(
                f"Similar research report already exists (sim={best['score']:.2f}, "
                f"dated {best['date']}): {best['file']}. "
                f"Read it first or call run_research(..., force=True) to create a new report."
            )

    from .agent import chat

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = _slugify(topic)
    report_dir = _ATLAS_DIR / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"research-{slug}-{date_str}.md"
    # A forced same-day rerun must not overwrite the earlier report.
    _n = 2
    while report_path.exists():
        report_path = report_dir / f"research-{slug}-{date_str}-{_n}.md"
        _n += 1

    # Step 1: gather existing memory context
    hits = _gather_memory_context(topic)
    memory_context = "\n".join(hits) if hits else "(no existing memory entries found)"

    # Step 1b: temporal graph context (historical facts)
    temporal_hits = _gather_temporal_context(topic)
    temporal_context_block = ""
    if temporal_hits:
        temporal_context_block = (
            f"\nHistorical context from knowledge graph:\n"
            + "\n".join(temporal_hits)
            + "\n"
        )

    # Step 1c: optional web search
    web_hits = _web_search(topic)
    web_context_block = ""
    if web_hits:
        web_context_block = (
            f"\nRelevant web search results (use as additional context, verify before citing):\n"
            + "\n".join(web_hits)
            + "\n"
        )

    # Step 2: build synthesis prompt
    prompt = (
        f"You are a research analyst for a software engineer's personal knowledge system. "
        f"Investigate the following topic thoroughly and write a structured report.\n\n"
        f"Topic: {topic}\n\n"
        f"Relevant existing memory context:\n{memory_context}\n"
        + temporal_context_block
        + web_context_block +
        f"\nWrite a structured Markdown report with exactly these sections:\n\n"
        f"## Summary\n"
        f"2-3 sentence overview of the topic.\n\n"
        f"## Key Findings\n"
        f"Bullet points: specific, concrete, actionable findings.\n\n"
        f"## Context\n"
        f"Background, related concepts, connections to existing knowledge.\n\n"
        f"## Gaps and Open Questions\n"
        f"What is still unknown or needs further investigation.\n\n"
        f"## Recommendations\n"
        f"Concrete next steps and actionable items.\n\n"
        f"## References\n"
        f"List any sources you referenced as Markdown links: [Title](https://url). "
        f"If no external sources were used, write 'No external references.'\n\n"
        f"Be specific and factual. Do not pad with generic caveats. "
        f"Reference the memory context where relevant."
    )

    # Step 3: synthesize via brain model
    model = os.environ.get("ATLAS_BRAIN_MODEL", "qwen3:8b")
    result = chat(session_id=session_id, user_message=prompt, model=model)
    report_body = result.text

    # Step 4: write report file
    web_note = f"  \n*Web search hits: {len(web_hits)}*" if web_hits else ""
    temporal_note = f"  \n*Temporal facts: {len(temporal_hits)}*" if temporal_hits else ""
    full_report = (
        f"---\n"
        f"title: \"Research: {topic}\"\n"
        f"date: {date_str}\n"
        f"session: {session_id}\n"
        f"slug: {slug}\n"
        f"---\n\n"
        f"# Research: {topic}\n\n"
        f"{report_body}\n\n"
        f"---\n"
        f"*Generated by Atlas Research Agent on {date_str}*  \n"
        f"*Memory hits: {len(hits)}*{temporal_note}{web_note}\n"
    )
    report_path.write_text(full_report, encoding="utf-8")
    log.info("Research report written: %s", report_path)

    # Step 5: link from .memory/reference/ if autonomous writes are allowed
    _try_link_reference(topic, slug, date_str, report_path)

    # Step 6: inject any discovered citations into frontmatter references: field
    inject_references_frontmatter(report_path)

    # Step 7: cross-link citations to .memory/reference/ files
    inject_cross_refs(report_path)

    # Step 8: extract and store the Summary section as an abstract: frontmatter field
    inject_abstract(report_path)

    # Step 9: compute and store deterministic quality score in frontmatter
    scored = inject_quality_score(report_path)
    initial_score = scored.get("score", 1.0) if scored else 1.0

    # Step 10: quality gate -- flag reports that don't meet minimum standards
    _apply_quality_gate(report_path, report_body)

    # Step 11: retry synthesis on very low quality results (opt-in, ATLAS_RESEARCH_RETRY=on)
    _retry_threshold = float(os.environ.get("ATLAS_RESEARCH_RETRY_THRESHOLD", "0.35"))
    if (
        os.environ.get("ATLAS_RESEARCH_RETRY", "off").lower() in ("on", "1", "true")
        and initial_score < _retry_threshold
    ):
        retry_body = _retry_low_quality(
            topic, session_id, hits, temporal_hits, web_hits, report_body, initial_score
        )
        if retry_body:
            report_body = retry_body
            # Re-write the report file with the new body
            full_report = (
                f"---\n"
                f"title: \"Research: {topic}\"\n"
                f"date: {date_str}\n"
                f"session: {session_id}\n"
                f"slug: {slug}\n"
                f"---\n\n"
                f"# Research: {topic}\n\n"
                f"{report_body}\n\n"
                f"---\n"
                f"*Generated by Atlas Research Agent on {date_str} (retry after quality={initial_score:.2f})*  \n"
                f"*Memory hits: {len(hits)}*\n"
            )
            report_path.write_text(full_report, encoding="utf-8")
            # Re-run pipeline steps on the new body
            inject_references_frontmatter(report_path)
            inject_cross_refs(report_path)
            inject_abstract(report_path)
            inject_quality_score(report_path)
            _apply_quality_gate(report_path, report_body)
            log.info("Research retry improved quality from %.2f for topic %r", initial_score, topic)

    return report_body, report_path


def _retry_low_quality(
    topic: str,
    session_id: str,
    memory_hits: list,
    temporal_hits: list,
    web_hits: list,
    first_body: str,
    first_score: float,
) -> Optional[str]:
    """Run a second synthesis attempt with a more demanding prompt.

    Returns the new body text if the attempt produces a better result, None otherwise.
    Called only when ATLAS_RESEARCH_RETRY=on and quality_score < threshold.
    """
    model = os.environ.get("ATLAS_BRAIN_MODEL", "qwen3:8b")
    memory_context = "\n".join(memory_hits) if memory_hits else "(no existing memory)"
    temporal_block = ("\nHistorical context:\n" + "\n".join(temporal_hits)) if temporal_hits else ""
    web_block = ("\nWeb results:\n" + "\n".join(web_hits)) if web_hits else ""

    retry_prompt = (
        f"Your previous research report on '{topic}' scored {first_score:.2f}/1.0 -- too short "
        f"or missing required sections. Please write a complete, detailed report with ALL "
        f"five sections: Summary, Key Findings (at least 5 bullets), Context, "
        f"Gaps and Open Questions, Recommendations, and References.\n\n"
        f"Topic: {topic}\n\n"
        f"Memory context:\n{memory_context}\n"
        + temporal_block + web_block +
        f"\n\nWrite a thorough report of at least 500 words with concrete findings and references."
    )
    try:
        from .agent import chat
        result = chat(session_id=f"{session_id}-retry", user_message=retry_prompt, model=model)
        return result.text if result.text.strip() else None
    except Exception as exc:
        log.warning("Research retry failed: %s", exc)
        return None


def extract_report_citations(text: str) -> list[str]:
    """Extract unique HTTP/HTTPS URLs from markdown text.

    Finds both Markdown-link format [Title](url) and bare URLs.
    Deduplicates and sorts. Strips trailing punctuation that may be part of
    surrounding prose rather than the URL itself.
    Returns [] when no URLs are found.
    """
    urls: set[str] = set()
    _strip_chars = ".,;:)>'\"`"
    for _, url in _MD_LINK_RE.findall(text):
        url = url.rstrip(_strip_chars)
        if len(url) > 12:
            urls.add(url)
    for url in _BARE_URL_RE.findall(text):
        url = url.rstrip(_strip_chars)
        if len(url) > 12:
            urls.add(url)
    return sorted(urls)


def inject_references_frontmatter(report_path: Path) -> int:
    """Extract URLs from a research report and write them to its references: frontmatter.

    Idempotent: if the current references: list already contains every discovered URL,
    the file is not modified.  Removes the old block and writes the new complete list so
    that re-runs after the model adds more links stay consistent.

    Returns the number of unique URLs written (0 = no URLs found or already complete).
    """
    try:
        raw = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.debug("Citation extraction: cannot read %s: %s", report_path, exc)
        return 0

    if not raw.startswith("---"):
        return 0
    end = raw.find("\n---", 3)
    if end == -1:
        return 0

    fm_block = raw[3:end]
    # Extract URLs from body only -- not from existing frontmatter to avoid
    # circular self-reference where old references: values bootstrap new ones.
    body_text = raw[end + 4:]
    urls = extract_report_citations(body_text)
    if not urls:
        return 0

    # Check idempotency: are all discovered URLs already in references:?
    try:
        import yaml as _yaml
        fm = _yaml.safe_load(fm_block) or {}
    except Exception:
        fm = {}
    existing_refs = fm.get("references") or []
    if isinstance(existing_refs, list) and set(str(u) for u in existing_refs) >= set(urls):
        return 0

    # Rebuild frontmatter lines, removing any existing references: block
    new_fm_lines: list[str] = []
    inside_refs = False
    for line in fm_block.splitlines():
        if line.startswith("references:"):
            inside_refs = True
            continue
        if inside_refs:
            stripped = line.lstrip()
            if stripped.startswith("- "):
                continue  # list item continuation
            inside_refs = False
        new_fm_lines.append(line)

    # Append updated references block
    new_fm_lines.append("references:")
    for url in urls:
        new_fm_lines.append(f"  - {url}")

    new_raw = "---" + "\n".join(new_fm_lines) + "\n---" + raw[end + 4:]
    try:
        report_path.write_text(new_raw, encoding="utf-8")
        log.info("Citations injected: %d URL(s) -> %s", len(urls), report_path.name)
    except OSError as exc:
        log.warning("Citation frontmatter write failed for %s: %s", report_path, exc)
        return 0

    return len(urls)


def inject_abstract(report_path: Path) -> bool:
    """Extract the Summary section from a research report and store it as abstract: frontmatter.

    The abstract is the 2-3 sentence Summary section of the report, truncated to 500 chars.
    It's stored in frontmatter so Telegram /research_index and Web UI can show a preview
    without reading the full report body.

    Idempotent: skips if abstract: already exists in frontmatter.
    Returns True if the abstract was written, False otherwise.
    """
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
        import yaml as _yaml
        fm = _yaml.safe_load(raw[3:end]) or {}
    except Exception:
        return False

    if fm.get("abstract"):
        return False  # already has an abstract

    # Extract Summary section from body
    body = raw[end + 4:]
    summary_match = re.search(r"##\s+Summary\s*\n+(.*?)(?=\n##|\Z)", body, re.DOTALL | re.IGNORECASE)
    if not summary_match:
        return False

    abstract_text = summary_match.group(1).strip()
    # Collapse multiple whitespace/newlines to single space
    abstract_text = re.sub(r"\s+", " ", abstract_text).strip()
    if len(abstract_text) < 20:
        return False

    abstract_text = abstract_text[:500]

    # Inject into frontmatter
    new_fm_lines = raw[3:end].splitlines()
    new_fm_lines.append(f'abstract: "{abstract_text.replace(chr(34), chr(39))}"')

    new_raw = "---" + "\n".join(new_fm_lines) + "\n---" + raw[end + 4:]
    try:
        report_path.write_text(new_raw, encoding="utf-8")
        log.debug("Abstract injected into %s (%d chars)", report_path.name, len(abstract_text))
        return True
    except OSError:
        return False


def inject_cross_refs(report_path: Path, memory_root: Optional[Path] = None) -> int:
    """Cross-link research report citations to .memory/reference/ files.

    For each URL in the report's references: frontmatter, scans .memory/reference/
    for files whose body mentions that URL (exact substring match). When a match is
    found, the relative path is added to a cross_refs: frontmatter list so agents
    can follow the link from research report to structured memory.

    Idempotent: reads the existing cross_refs: list and only adds new entries.
    Returns the number of new cross-references injected (0 = nothing new found).
    """
    mem_root = memory_root or _MEMORY_ROOT
    ref_dir = mem_root / "reference"
    if not ref_dir.exists():
        return 0

    try:
        raw = report_path.read_text(encoding="utf-8")
    except OSError:
        return 0

    if not raw.startswith("---"):
        return 0
    end = raw.find("\n---", 3)
    if end == -1:
        return 0

    try:
        import yaml as _yaml
        fm = _yaml.safe_load(raw[3:end]) or {}
    except Exception:
        return 0

    # Get the report's reference URLs (set by inject_references_frontmatter)
    ref_urls: list[str] = fm.get("references") or []
    if not ref_urls:
        return 0

    existing_cross: set[str] = set(fm.get("cross_refs") or [])

    # Scan reference files for URL matches
    new_cross: list[str] = []
    for ref_file in sorted(ref_dir.rglob("*.md")):
        if ref_file.name.startswith("_"):
            continue
        try:
            ref_content = ref_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel_path = str(ref_file.relative_to(mem_root)).replace("\\", "/")
        if rel_path in existing_cross:
            continue  # already linked
        for url in ref_urls:
            if url in ref_content:
                new_cross.append(rel_path)
                break  # one match per file is enough

    if not new_cross:
        return 0

    # Rebuild frontmatter with updated cross_refs list
    all_cross = sorted(existing_cross | set(new_cross))
    new_fm_lines: list[str] = []
    inside_cross = False
    for line in raw[3:end].splitlines():
        if line.startswith("cross_refs:"):
            inside_cross = True
            continue
        if inside_cross:
            if line.lstrip().startswith("- "):
                continue
            inside_cross = False
        new_fm_lines.append(line)

    new_fm_lines.append("cross_refs:")
    for path_str in all_cross:
        new_fm_lines.append(f"  - {path_str}")

    new_raw = "---" + "\n".join(new_fm_lines) + "\n---" + raw[end + 4:]
    try:
        report_path.write_text(new_raw, encoding="utf-8")
        log.info("Cross-refs injected: %d -> %s", len(new_cross), report_path.name)
    except OSError as exc:
        log.warning("Cross-ref write failed for %s: %s", report_path, exc)
        return 0

    return len(new_cross)


def compute_freshness_score(date_str: str) -> Optional[float]:
    """Compute a freshness score (0-1) for a research report based on its date.

    Decays linearly from 1.0 (today) to 0.0 at 365 days. Milestones:
      0-30d:   >= 0.92  (fresh)
      30-90d:  0.75-0.92
      90-180d: 0.51-0.75
      180-365d: 0.01-0.50
      > 365d:  0.0

    Returns None when date_str is empty or cannot be parsed.
    """
    if not date_str or len(date_str) < 10:
        return None
    try:
        from datetime import date
        report_date = date.fromisoformat(date_str[:10])
        age_days = (date.today() - report_date).days
        if age_days < 0:
            return 1.0  # future-dated (typo) -> treat as fresh
        score = max(0.0, 1.0 - age_days / 365.0)
        return round(score, 3)
    except (ValueError, TypeError):
        return None


_REQUIRED_SECTIONS = {"summary", "key findings", "context", "gaps and open questions", "recommendations"}
_HEADING_RE = re.compile(r"^##\s+(.+)", re.MULTILINE)
_BULLET_RE = re.compile(r"^[-*]\s+.+", re.MULTILINE)

_SCORE_WEIGHTS = {
    "citations":  0.25,
    "sections":   0.30,
    "word_count": 0.20,
    "findings":   0.25,
}


def score_research_report(text: str) -> dict:
    """Compute a deterministic quality score for a research report body.

    Scoring factors (all 0-1, weights sum to 1.0):
      citations:  0 URLs -> 0, 1-2 -> 0.5, 3+ -> 1.0  (weight 0.25)
      sections:   required headings present / 5         (weight 0.30)
      word_count: <200 -> 0, 200-500 -> 0.5, 500+ -> 1.0 (weight 0.20)
      findings:   bullet count under ## Key Findings     (weight 0.25)
                  0 -> 0, 1-2 -> 0.5, 3+ -> 1.0

    Returns {score: float (0-1), factors: {citations, sections, word_count, findings},
             raw: {citation_count, section_count, word_count, findings_count}}.
    """
    # Citations from body
    citation_count = len(extract_report_citations(text))
    citations_score = 1.0 if citation_count >= 3 else (0.5 if citation_count >= 1 else 0.0)

    # Sections
    headings = {h.strip().lower() for h in _HEADING_RE.findall(text)}
    matched = sum(1 for req in _REQUIRED_SECTIONS if any(req in h for h in headings))
    sections_score = matched / len(_REQUIRED_SECTIONS)

    # Word count (body text, excluding frontmatter markers and headings)
    words = len(re.findall(r"\b\w+\b", text))
    word_score = 1.0 if words >= 500 else (0.5 if words >= 200 else 0.0)

    # Key findings bullet points
    findings_count = 0
    findings_match = re.search(r"## Key Findings\s*\n(.*?)(?=\n##|\Z)", text, re.DOTALL)
    if findings_match:
        findings_count = len(_BULLET_RE.findall(findings_match.group(1)))
    findings_score = 1.0 if findings_count >= 3 else (0.5 if findings_count >= 1 else 0.0)

    composite = round(
        citations_score * _SCORE_WEIGHTS["citations"]
        + sections_score * _SCORE_WEIGHTS["sections"]
        + word_score * _SCORE_WEIGHTS["word_count"]
        + findings_score * _SCORE_WEIGHTS["findings"],
        3,
    )

    return {
        "score": composite,
        "factors": {
            "citations": round(citations_score, 2),
            "sections": round(sections_score, 2),
            "word_count": round(word_score, 2),
            "findings": round(findings_score, 2),
        },
        "raw": {
            "citation_count": citation_count,
            "section_count": matched,
            "word_count": words,
            "findings_count": findings_count,
        },
    }


def inject_quality_score(report_path: Path) -> dict:
    """Score the report and write quality_score + quality_factors to its frontmatter.

    Idempotent: if the file already has quality_score and it is current, skips the write.
    Called automatically by run_research() after inject_references_frontmatter().
    Returns the score dict {score, factors, raw} or empty dict on error.
    """
    try:
        raw_text = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.debug("Quality score: cannot read %s: %s", report_path, exc)
        return {}

    if not raw_text.startswith("---"):
        return {}
    end = raw_text.find("\n---", 3)
    if end == -1:
        return {}

    body_text = raw_text[end + 4:]
    result = score_research_report(body_text)
    score = result["score"]
    factors = result["factors"]

    # Build updated frontmatter
    fm_block = raw_text[3:end]
    new_fm_lines = []
    skip_block = False
    skip_keys = {"quality_score", "quality_factors"}
    for line in fm_block.splitlines():
        stripped_key = line.split(":")[0].strip().lower()
        if stripped_key in skip_keys:
            skip_block = True
            continue
        if skip_block:
            if line.startswith("  ") or (line.lstrip().startswith("- ")):
                continue
            skip_block = False
        new_fm_lines.append(line)

    new_fm_lines.append(f"quality_score: {score}")
    new_fm_lines.append(
        f"quality_factors: {{citations: {factors['citations']}, "
        f"sections: {factors['sections']}, "
        f"word_count: {factors['word_count']}, "
        f"findings: {factors['findings']}}}"
    )

    new_raw = "---" + "\n".join(new_fm_lines) + "\n---" + raw_text[end + 4:]
    try:
        report_path.write_text(new_raw, encoding="utf-8")
        log.info("Quality score: %.3f -> %s", score, report_path.name)
    except OSError as exc:
        log.warning("Quality score write failed for %s: %s", report_path, exc)
        return {}

    return result


def _try_link_reference(topic: str, slug: str, date_str: str, report_path: Path) -> None:
    """Write a reference stub to .memory/reference/ pointing at the report."""
    autonomous = autonomous_enabled()  # was denylist: WROTE on ATLAS_AUTONOMOUS=yes (audit 2026-09-21)
    session_auto = os.environ.get("ATLAS_SESSION_AUTONOMOUS", "").lower() in ("on", "1", "true")
    if not (autonomous or session_auto):
        log.debug("Autonomous off -- skipping reference link for %s", slug)
        return

    ref_dir = _MEMORY_ROOT / "reference"
    ref_path = ref_dir / f"research-{slug}.md"
    if ref_path.exists():
        return

    content = (
        f"---\n"
        f"type: reference\n"
        f"scope: research/{slug}\n"
        f"created: {date_str}\n"
        f"---\n\n"
        f"# Research: {topic}\n\n"
        f"Report: `{report_path}`\n"
    )
    try:
        ref_dir.mkdir(parents=True, exist_ok=True)
        ref_path.write_text(content, encoding="utf-8")
        log.info("Reference link written: %s", ref_path)
    except Exception as exc:
        log.warning("Failed to write reference link %s: %s", ref_path, exc)
