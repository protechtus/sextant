"""P3-T6: atlas suggest-facts -- scan a transcript and propose memory facts.

Reads recent session turns, asks an LLM to extract candidate facts, deduplicates
against existing .memory/ content (Jaccard), then presents each candidate for
interactive approve/skip/edit before writing.

Usage:
    atlas-suggest-facts                          # scan today's transcript
    atlas-suggest-facts --since 4h              # last 4 hours
    atlas-suggest-facts --since 2026-05-04      # specific day
    atlas-suggest-facts --auto                  # approve all high-confidence (>=0.95)
    atlas-suggest-facts --dry-run               # print candidates, write nothing
    atlas-suggest-facts --skip-similar 0.6      # skip candidates already well-covered

Env vars:
    ATLAS_SUGGEST_CONFIDENCE_THRESHOLD  float, default 0.70. LLM-assigned confidence
                                        below this value causes the candidate to be
                                        rejected and counted in rejected_low_confidence.
"""

from __future__ import annotations

import json
import os
import re
import sys

from .scan import is_excluded_mirror
from pathlib import Path
from typing import Optional
from atlas_plus import paths as atlas_paths

MEMORY_ROOT = atlas_paths.memory_root()

_EXCLUDED_DIRS = {"archive", "_sync", "versions"}
_DUP_THRESHOLD = 0.40

# Minimum LLM-assigned confidence to accept a candidate fact.
# Override via ATLAS_SUGGEST_CONFIDENCE_THRESHOLD env var (float, 0.0-1.0).
_DEFAULT_CONFIDENCE_THRESHOLD = 0.70

# Map LLM output scope aliases to canonical routing scopes
_SCOPE_ALIASES = {
    "best_practice": "preference",
}

# ---------------------------------------------------------------------------
# Transcript loading helpers
# ---------------------------------------------------------------------------

def _load_transcript_text(since: str = "today", source: str = "claude-code") -> str:
    """Load turns and return a condensed plain-text transcript."""
    from atlas_plus.summarizer.loader import load_turns
    turns = load_turns(source=source, since=since)

    lines: list[str] = []
    for t in turns:
        role = t.role.upper()
        content = t.content
        if isinstance(content, list):
            # Flatten content blocks, skip tool_result noise
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool:{block.get('name','?')}]")
            content = " ".join(parts)
        if content and str(content).strip():
            lines.append(f"{role}: {str(content).strip()[:400]}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fact extraction prompt
# ---------------------------------------------------------------------------

_EXTRACT_PROMPT = """\
You are reviewing a software development session transcript. Your job is to
extract facts worth saving in a persistent memory system.

Return ONLY valid JSON (no markdown, no preamble):
[
  {{
    "type": "feedback",
    "scope": "preference:<topic>",
    "title": "Short imperative rule (under 60 chars)",
    "fact": "1-3 sentences. Include WHY if mentioned. No em-dashes.",
    "confidence": 0.9
  }}
]

TYPES:
- "feedback": Rules and corrections about how to approach work (most common)
- "project": Status, decisions, goals for a specific project
- "user": Facts about the developer's role, expertise, preferences
- "reference": Pointers to external resources or systems

SCOPE FORMAT (use colon separator, not slash):
- preference:<topic>   e.g. preference:testing, preference:editor, preference:git
- project:<name>       e.g. project:my-app, project:sextant
- identity             for persistent user facts (identity/user.md)
- reference:<topic>    e.g. reference:acme, reference:machines

INCLUDE a fact only when ALL of these are true:
1. Non-obvious (not derivable from just reading the code)
2. Persistent (still true in 6 months, not a one-off workaround)
3. Specific (not a vague generalization like "test everything")
4. Memorable (a future session would benefit from knowing it upfront)

CONFIDENCE SCALE:
- 0.95+ = critical rule, must save (very rare)
- 0.85-0.94 = definitely worth saving
- 0.75-0.84 = probably worth saving, borderline
- 0.70-0.74 = borderline -- include only if highly specific and unusual
- below 0.70 = DO NOT include -- omit candidates below this threshold entirely

CRITICAL: Only return candidates with confidence >= 0.70. Do not include
candidates below this threshold -- they will be rejected automatically.
Return [] if no candidates meet the bar.

TRANSCRIPT:
{transcript}

JSON:"""


def _extract_candidates(
    transcript_text: str,
    confidence_threshold: Optional[float] = None,
) -> tuple[list[dict], int]:
    """Call LLM to extract candidate facts.

    Returns (candidates, rejected_low_confidence) where rejected_low_confidence
    counts how many LLM proposals were dropped for falling below the threshold.

    confidence_threshold defaults to ATLAS_SUGGEST_CONFIDENCE_THRESHOLD env var
    or _DEFAULT_CONFIDENCE_THRESHOLD (0.70) when not provided.
    """
    if not transcript_text.strip():
        return [], 0

    threshold = confidence_threshold
    if threshold is None:
        threshold = float(os.environ.get("ATLAS_SUGGEST_CONFIDENCE_THRESHOLD", str(_DEFAULT_CONFIDENCE_THRESHOLD)))

    # Trim transcript to fit reasonable context (~6000 chars)
    trimmed = transcript_text[-6000:] if len(transcript_text) > 6000 else transcript_text
    prompt = _EXTRACT_PROMPT.format(transcript=trimmed)

    from atlas_plus.summarizer.providers import complete, ProviderError
    try:
        text, _ = complete(prompt)
    except ProviderError as e:
        raise RuntimeError(f"LLM provider error: {e}") from e

    # Extract JSON from response
    text = text.strip()
    # Find the first '[' and last ']'
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        return [], 0
    try:
        candidates = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return [], 0

    if not isinstance(candidates, list):
        return [], 0

    # Filter and normalize
    valid = []
    rejected = 0
    for c in candidates:
        if not isinstance(c, dict):
            continue
        confidence = float(c.get("confidence", 0))
        if confidence < threshold:
            rejected += 1
            continue
        fact = str(c.get("fact", "")).strip()
        scope = str(c.get("scope", "")).strip()
        if not fact or not scope:
            continue
        # Normalize scope aliases (e.g. best_practice -> preference)
        if ":" in scope:
            key, param = scope.split(":", 1)
            if key in _SCOPE_ALIASES:
                scope = f"{_SCOPE_ALIASES[key]}:{param}"
        valid.append({
            "type": c.get("type", "feedback"),
            "scope": scope,
            "title": str(c.get("title", scope)).strip()[:80],
            "fact": fact,
            "confidence": confidence,
        })
    return valid, rejected


# ---------------------------------------------------------------------------
# Deduplication against existing memory
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    _STOPS = {
        "the", "a", "an", "and", "or", "of", "to", "in", "is", "it",
        "that", "this", "for", "be", "with", "on", "at", "by", "from",
        "not", "are", "was", "were", "as", "if", "but", "all", "any",
        "has", "have", "had", "do", "does", "did", "will", "would",
        "can", "could", "should", "may", "might", "must", "shall",
    }
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3:]
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _load_memory_corpus(root: Path) -> list[tuple[str, set[str]]]:
    """Return (relative_path, token_set) for all non-excluded .memory/ files."""
    corpus = []
    for f in root.rglob("*.md"):
        parts = f.relative_to(root).parts
        if any(p in _EXCLUDED_DIRS for p in parts) or is_excluded_mirror(parts):
            continue
        if f.name.startswith("_"):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tokens = _tokenize(text)
        if tokens:
            corpus.append((f.relative_to(root).as_posix(), tokens))
    return corpus


def _find_similar(fact_text: str, corpus: list[tuple[str, set[str]]]) -> list[tuple[float, str]]:
    """Return list of (similarity, path) pairs above threshold, sorted desc."""
    fact_tokens = _tokenize(fact_text)
    results = []
    for path, tokens in corpus:
        sim = _jaccard(fact_tokens, tokens)
        if sim >= _DUP_THRESHOLD:
            results.append((sim, path))
    results.sort(reverse=True)
    return results


# ---------------------------------------------------------------------------
# Fact writing
# ---------------------------------------------------------------------------

def _write_fact(scope: str, fact: str, confidence: float = None) -> dict:
    """Write a fact to .memory/ via the routing + append_fact machinery."""
    from atlas_plus.mcp.memory_server.routing import resolve_target, append_fact
    from atlas_plus.mcp.memory_server.versioning import version_before_write

    target, fm_template = resolve_target(scope)
    version_before_write(target)
    append_fact(target, fact, fm_template, source="suggest-facts", confidence=confidence)
    return {"ok": True, "file": str(target)}


# ---------------------------------------------------------------------------
# Interactive review
# ---------------------------------------------------------------------------

def _review_candidates(
    candidates: list[dict],
    corpus: list[tuple[str, set[str]]],
    dry_run: bool = False,
    auto_approve_threshold: float = 1.1,  # disabled by default
    auto_write_cap: int = 0,
    skip_similar_threshold: float = 0.0,  # 0.0 = disabled
    non_interactive: bool = False,
) -> dict:
    """Present each candidate for interactive review. Returns summary stats.

    non_interactive: candidates that do not auto-approve are SKIPPED instead
    of prompting. --auto runs are headless (Brain scheduled task): a
    sub-threshold candidate used to fall through to input() and hang until
    the worker's 1200s timeout killed the run (the recurring
    suggest_facts_auto failures, found 2026-10-12).

    auto_write_cap is the maximum number of auto-approved writes in one run.
    Beyond the cap, remaining candidates fall through to interactive review
    (or skip in dry-run). This is the per-run rate limit defense against
    memory poisoning bursts (Audit 2026-05-18 HIGH-2).

    skip_similar_threshold: when > 0, candidates whose top Jaccard similarity
    to an existing memory file meets or exceeds this value are automatically
    skipped without human review. Useful for deduplication in --auto mode.
    """
    approved = skipped = written = 0
    auto_written = 0
    total = len(candidates)

    try:
        from rich.console import Console
        from rich.panel import Panel
        console = Console()
    except ImportError:
        console = None  # type: ignore

    def _print(msg: str) -> None:
        if console:
            console.print(msg)
        else:
            print(msg)

    _print(f"\n[bold]Found {total} candidate fact(s) from transcript.[/bold]\n" if console else f"\nFound {total} candidate fact(s).\n")

    for i, cand in enumerate(candidates, 1):
        similar = _find_similar(cand["fact"], corpus)
        sim_note = ""
        if similar:
            top = similar[0]
            sim_note = f" | Similar to: {top[1]} ({top[0]:.2f})"

        # Auto-skip when the candidate is already well-covered in memory
        if skip_similar_threshold > 0.0 and similar:
            top_score = similar[0][0]
            if top_score >= skip_similar_threshold:
                _print(
                    f"[dim]Auto-skipped (similar {top_score:.2f} >= {skip_similar_threshold:.2f}): "
                    f"{similar[0][1]}[/dim]"
                    if console
                    else f"Auto-skipped (similar {top_score:.2f} >= {skip_similar_threshold:.2f}): {similar[0][1]}"
                )
                skipped += 1
                continue

        if console:
            body = (
                f"[cyan]{cand['scope']}[/cyan]\n"
                f"[bold]{cand['title']}[/bold]\n\n"
                f"{cand['fact']}\n\n"
                f"[dim]Confidence: {cand['confidence']:.2f}{sim_note}[/dim]"
            )
            console.print(Panel(body, title=f"[{i}/{total}]", expand=False))
        else:
            print(f"\n[{i}/{total}] {cand['scope']}")
            print(f"  {cand['title']}")
            print(f"  {cand['fact']}")
            print(f"  Confidence: {cand['confidence']:.2f}{sim_note}")

        # Auto-approve high-confidence facts (gated by threshold + per-run cap)
        if cand["confidence"] >= auto_approve_threshold and not dry_run:
            if auto_written >= auto_write_cap:
                _print(
                    f"[yellow]Auto-write cap reached ({auto_write_cap}); skipping rest.[/yellow]"
                    if console else f"Auto-write cap reached ({auto_write_cap}); skipping rest."
                )
                skipped += 1
                continue
            _print("[green]Auto-approved.[/green]" if console else "Auto-approved.")
            try:
                _write_fact(cand["scope"], cand["fact"], confidence=cand.get("confidence"))
                written += 1
                approved += 1
                auto_written += 1
            except Exception as e:
                _print(f"[red]Write error: {e}[/red]" if console else f"Write error: {e}")
            continue

        if dry_run:
            _print("[dim](dry-run)[/dim]" if console else "(dry-run)")
            skipped += 1
            continue

        if non_interactive:
            skipped += 1
            _print(
                "[dim]Skipped (below auto-approve threshold; non-interactive run).[/dim]"
                if console else "Skipped (below auto-approve threshold; non-interactive run)."
            )
            continue

        # Interactive prompt
        while True:
            try:
                choice = input("  [a]pprove  [s]kip  [e]dit  [q]uit ? ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                _print("\n[yellow]Aborted.[/yellow]" if console else "\nAborted.")
                return {"approved": approved, "skipped": skipped, "written": written}

            if choice == "q":
                _print("[yellow]Quit.[/yellow]" if console else "Quit.")
                return {"approved": approved, "skipped": skipped, "written": written}
            elif choice == "s":
                skipped += 1
                _print("[dim]Skipped.[/dim]" if console else "Skipped.")
                break
            elif choice == "a":
                try:
                    _write_fact(cand["scope"], cand["fact"], confidence=cand.get("confidence"))
                    written += 1
                    approved += 1
                    _print("[green]Written.[/green]" if console else "Written.")
                except Exception as e:
                    _print(f"[red]Write error: {e}[/red]" if console else f"Write error: {e}")
                break
            elif choice == "e":
                print(f"  Current: {cand['fact']}")
                try:
                    new_fact = input("  New text: ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if new_fact:
                    cand["fact"] = new_fact
                # Loop back to re-prompt with edited fact
                _print(f"[dim]Edited. Re-prompting...[/dim]" if console else "Edited. Re-prompting...")
                continue
            else:
                _print("[dim]Type a, s, e, or q.[/dim]" if console else "Type a, s, e, or q.")

    _print(
        f"\n[green]Done.[/green] {approved} approved, {skipped} skipped, {written} written."
        if console
        else f"\nDone. {approved} approved, {skipped} skipped, {written} written."
    )
    return {"approved": approved, "skipped": skipped, "written": written}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Typer-based CLI entry point: atlas-suggest-facts."""
    import typer

    app = typer.Typer(help="Scan a session transcript and propose facts to add to .memory/.", add_completion=False)

    @app.command()
    def _cmd(
        since: str = typer.Option("today", "--since", "-s", help="Time window: 'today', 'Nh', 'Nm', or ISO timestamp"),
        source: str = typer.Option("claude-code", "--source", help="Transcript source: claude-code | atlas | auto"),
        dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Print candidates without writing"),
        auto: bool = typer.Option(False, "--auto", "-a", help="Auto-approve facts with confidence >= 0.95"),
        skip_similar: float = typer.Option(0.0, "--skip-similar", help="Auto-skip candidates with Jaccard similarity >= threshold (0.0 = off, e.g. 0.6)"),
        threshold: float = typer.Option(0.0, "--threshold", help="Override confidence threshold (0.0 = use env default 0.70)"),
    ):
        try:
            suggest_facts(
                since=since,
                source=source,
                dry_run=dry_run,
                auto=auto,
                skip_similar_threshold=skip_similar,
                confidence_threshold=threshold if threshold > 0.0 else None,
            )
        except RuntimeError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(1)

    app()


def suggest_facts(
    since: str = "today",
    source: str = "claude-code",
    dry_run: bool = False,
    auto: bool = False,
    memory_root: Optional[Path] = None,
    skip_similar_threshold: float = 0.0,
    confidence_threshold: Optional[float] = None,
) -> dict:
    """Full suggest-facts pipeline. Returns summary stats dict.

    skip_similar_threshold: Jaccard similarity cutoff (0.0 = disabled).
    Candidates above this threshold are auto-skipped as likely duplicates.

    confidence_threshold: LLM-confidence minimum (None = use ATLAS_SUGGEST_CONFIDENCE_THRESHOLD
    env var, default 0.70). Candidates below this are rejected and counted in
    returned['rejected_low_confidence'].
    """
    root = memory_root or MEMORY_ROOT

    print(f"Loading transcript since '{since}'...", flush=True)
    transcript_text = _load_transcript_text(since=since, source=source)
    if not transcript_text.strip():
        print("No transcript content found for the given window.")
        return {"candidates": 0, "approved": 0, "skipped": 0, "written": 0}

    print(f"Transcript: {len(transcript_text)} chars. Extracting facts...", flush=True)
    candidates, rejected_low_confidence = _extract_candidates(transcript_text, confidence_threshold=confidence_threshold)
    if rejected_low_confidence > 0:
        effective_threshold = confidence_threshold or float(os.environ.get("ATLAS_SUGGEST_CONFIDENCE_THRESHOLD", str(_DEFAULT_CONFIDENCE_THRESHOLD)))
        print(f"Rejected {rejected_low_confidence} low-confidence candidate(s) (below {effective_threshold:.2f}).", flush=True)
    if not candidates:
        print("No candidate facts extracted.")
        return {"candidates": 0, "approved": 0, "skipped": 0, "written": 0, "rejected_low_confidence": rejected_low_confidence}

    corpus = _load_memory_corpus(root)

    # Audit 2026-05-18 HIGH-2: tighten auto-approve gates.
    # The threshold change (0.85 -> 0.95) plus the source-authority gate plus
    # the per-run write cap is the layered defense against memory poisoning.
    # Source authority: only the claude-code source (user-typed JSONL) gets
    # to drive auto-approve. Any proxy-captured source (openai, ollama,
    # transcript-content) requires human review regardless of confidence.
    _trusted_auto_sources = {"claude-code"}
    if auto and source in _trusted_auto_sources:
        auto_threshold = float(os.environ.get("ATLAS_SUGGEST_AUTO_THRESHOLD", "0.95"))
        auto_write_cap = int(os.environ.get("ATLAS_SUGGEST_AUTO_CAP", "5"))
    else:
        auto_threshold = 1.1  # never auto-approve
        auto_write_cap = 0
        if auto and source not in _trusted_auto_sources:
            print(
                f"[suggest-facts] --auto requested with source={source!r}; "
                f"auto-approve disabled (only {sorted(_trusted_auto_sources)} are trusted).",
                flush=True,
            )

    stats = _review_candidates(
        candidates=candidates,
        corpus=corpus,
        dry_run=dry_run,
        auto_approve_threshold=auto_threshold,
        auto_write_cap=auto_write_cap,
        skip_similar_threshold=skip_similar_threshold,
        non_interactive=auto,  # --auto runs headless; never block on input()
    )
    return {"candidates": len(candidates), "rejected_low_confidence": rejected_low_confidence, **stats}


if __name__ == "__main__":
    main()
