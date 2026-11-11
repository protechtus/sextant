"""Direction F -- Dream Mode: on-demand meta-cognitive reflection.

An LLM reads the full .memory/ knowledge base and generates a structured
self-reflection: what is known with confidence, what is uncertain, what
patterns exist, what gaps need filling, and what questions to ask next session.

Inspired by:
  - Letta's sleep-time compute (agents reason during idle periods)
  - Hippocampal replay (offline consolidation of fast-buffer experiences)
  - Reflexion's episodic memory buffers (verbal self-critique)

This is NOT scheduled by default. It is on-demand only:
  atlas-dream               # full dream from all .memory/ content
  atlas-dream --scope feedback reference   # focus on specific scopes
  atlas-dream --dry-run     # show corpus stats, skip LLM

Output: .memory/sessions/_dream-YYYY-MM-DD.md

ATLAS_AUTONOMOUS gate:
  off -> dream still runs (read-only), but output is written to
         .atlas/dream-YYYY-MM-DD.md instead of .memory/sessions/
         (avoids writing to .memory/ without explicit permission)
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from atlas_plus import paths as atlas_paths
from atlas_plus.autonomous import autonomous_enabled

MEMORY_ROOT = atlas_paths.memory_root()
STATE_DIR = atlas_paths.state_dir()

_EXCLUDED_DIRS = {"archive", "_sync", "versions", "_atlas", "sessions", "tickets", "personas"}
_DEFAULT_SCOPES = ["feedback", "identity", "projects", "reference"]
# Caps raised 2026-07-30: at 400/8000 the corpus truncated after ~20 files in
# a FIXED alphabetical order, so every nightly dream read the same slice and
# asked the same questions (verified across _dream-2026-07-25/28/30).
_MAX_CHARS_PER_FILE = 600    # per file snippet in LLM context
_MAX_TOTAL_CHARS = 24000     # total corpus chars sent to LLM (qwen3:8b ctx is ample)


def _is_autonomous() -> bool:
    return autonomous_enabled()  # canonical allowlist gate (audit 2026-09-21)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ascii_safe(text: str) -> str:
    return "".join(c if ord(c) < 128 and c >= " " else " " for c in text).strip()


# ---------------------------------------------------------------------------
# Corpus builder
# ---------------------------------------------------------------------------

def _read_stripped(path: Path, max_chars: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                text = text[end + 3:].strip()
        return _ascii_safe(text.strip()[:max_chars])
    except OSError:
        return ""


def _parse_frontmatter_title(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                for line in text[3:end].splitlines():
                    if line.startswith("title:"):
                        return _ascii_safe(line.split(":", 1)[1].strip())
    except OSError:
        pass
    return path.stem


def build_corpus(
    memory_root: Optional[Path] = None,
    scopes: Optional[list[str]] = None,
    rotation_key: Optional[int] = None,
) -> tuple[str, dict]:
    """Build a compact text corpus from .memory/ files for LLM consumption.

    Selection order (2026-07-30 redesign): STALEST FILES FIRST (oldest mtime),
    then rotated by rotation_key so consecutive dreams start at different
    offsets even when nothing changed. Rationale: a dream's job is to probe
    what might be stale or forgotten; the old alphabetical order fed the same
    fresh-ish slice every night and the reflections repeated themselves.

    rotation_key: any int (defaults to today's ordinal). Same key = same
    corpus (deterministic for tests); consecutive days walk the corpus.

    Returns (corpus_text, stats_dict).
    """
    root = memory_root or MEMORY_ROOT
    scopes = scopes or _DEFAULT_SCOPES
    if rotation_key is None:
        rotation_key = datetime.now(timezone.utc).toordinal()
    stats: dict = {"scopes": scopes, "files": 0, "total_chars": 0, "truncated": False}

    # Collect every candidate across scopes.
    candidates: list[tuple[float, str, Path]] = []
    for scope in scopes:
        scope_dir = root / scope
        if not scope_dir.exists():
            continue
        for path in sorted(scope_dir.rglob("*.md")):
            if path.name.startswith("_"):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            candidates.append((mtime, scope, path))

    # Stalest first, then rotate the whole ordering by the daily key.
    candidates.sort(key=lambda t: (t[0], t[2].as_posix()))
    if candidates:
        off = rotation_key % len(candidates)
        candidates = candidates[off:] + candidates[:off]

    # Select under the char budget, then group by scope for readable output.
    selected: dict[str, list[str]] = {s: [] for s in scopes}
    total_chars = 0
    for _mtime, scope, path in candidates:
        rel = path.relative_to(root).as_posix()
        title = _parse_frontmatter_title(path)
        snippet = _read_stripped(path, _MAX_CHARS_PER_FILE)
        if not snippet:
            continue
        entry = f"[{rel}] {title}:\n{snippet}"
        selected[scope].append(entry)
        stats["files"] += 1
        total_chars += len(entry)
        if total_chars >= _MAX_TOTAL_CHARS:
            stats["truncated"] = True
            break

    sections = [
        f"=== {scope.upper()} ===\n\n" + "\n\n".join(selected[scope])
        for scope in scopes if selected.get(scope)
    ]
    stats["total_chars"] = total_chars
    return "\n\n".join(sections), stats


# ---------------------------------------------------------------------------
# Dream prompt
# ---------------------------------------------------------------------------

_DREAM_PROMPT = """\
You are performing a meta-cognitive review of a personal AI knowledge base.
Read the files below and answer each of the five questions in 3-6 sentences of
plain prose. No lists, no em-dashes, no emojis. Be specific about names, tools,
and projects. Use ISO 8601 dates when referring to time.

Questions:
1. HIGH CONFIDENCE: What does this knowledge base know with confidence?
2. UNCERTAIN: What seems stale, ambiguous, or possibly wrong?
3. PATTERNS: What recurring rules, preferences, or approaches appear?
4. GAPS: What would a deeply knowledgeable assistant want to know that is missing?
5. QUESTIONS: What 3-5 specific questions should be asked in the next session?

KNOWLEDGE BASE:
{corpus}

REFLECTION:"""


def _call_llm(prompt: str) -> str:
    from atlas_plus.summarizer.providers import complete, ProviderError
    try:
        text, _ = complete(prompt)
        return text.strip()
    except ProviderError as e:
        raise RuntimeError(f"LLM provider error: {e}") from e


def _previous_corpus_hash(root: Path) -> Optional[str]:
    """corpus_hash from the newest existing dream file (sessions/ or state dir).

    Best-effort: returns None when no prior dream or no hash recorded.
    """
    candidates: list[Path] = []
    for d in (root / "sessions", STATE_DIR):
        try:
            candidates.extend(d.glob("_dream-*.md"))
        except OSError:
            pass
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.name)
    try:
        text = newest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines()[:12]:
        if line.startswith("corpus_hash:"):
            return line.split(":", 1)[1].strip() or None
    return None


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run_dream(
    memory_root: Optional[Path] = None,
    scopes: Optional[list[str]] = None,
    dry_run: bool = False,
    _call_llm_fn=None,
) -> dict:
    """Run a dream session against .memory/ and write the output.

    Args:
        memory_root:  Override .memory/ root (used in tests).
        scopes:       Which subdirs to include. Defaults to feedback, identity, projects, reference.
        dry_run:      Build corpus and report stats, but skip LLM and don't write output.
        _call_llm_fn: Inject a mock LLM function (used in tests).

    Returns a status dict.
    """
    root = memory_root or MEMORY_ROOT
    autonomous = _is_autonomous()
    llm = _call_llm_fn or _call_llm
    date = _date_str()

    corpus, stats = build_corpus(root, scopes)

    result: dict = {
        "date": date,
        "autonomous": autonomous,
        "dry_run": dry_run,
        "corpus_files": stats["files"],
        "corpus_chars": stats["total_chars"],
        "corpus_truncated": stats["truncated"],
        "written": False,
        "output_path": None,
    }

    if dry_run:
        print(f"[atlas-dream] Corpus: {stats['files']} files, {stats['total_chars']} chars"
              f"{' (truncated)' if stats['truncated'] else ''}.")
        return result

    if not corpus.strip():
        result["error"] = "Empty corpus -- no .memory/ files found in requested scopes."
        return result

    # Novelty guard (2026-07-30): if the selected corpus is byte-identical to
    # the previous dream's, a new reflection would repeat it. Skip the LLM
    # call and the write; the scheduler run still counts as ok.
    corpus_hash = hashlib.sha256(corpus.encode("utf-8")).hexdigest()[:16]
    result["corpus_hash"] = corpus_hash
    prev = _previous_corpus_hash(root)
    if prev is not None and prev == corpus_hash:
        result["skipped"] = True
        result["note"] = "corpus unchanged since previous dream; skipped LLM + write"
        return result

    prompt = _DREAM_PROMPT.format(corpus=corpus)
    try:
        reflection = llm(prompt)
    except RuntimeError as e:
        result["error"] = str(e)
        return result

    # Build output document
    now = _now_iso()
    frontmatter = (
        f"---\nid: dream-{date}\ntitle: Dream {date}\ntype: session\n"
        f"status: active\npriority: normal\nupdated: {date}\n"
        f"corpus_hash: {corpus_hash}\n---\n\n"
    )
    header = f"# Atlas Dream -- {date}\n\n*Generated from {stats['files']} memory files.*\n\n"
    content = frontmatter + header + _ascii_safe(reflection) + "\n"

    # Write output
    if autonomous:
        # Write to .memory/sessions/ (the canonical location)
        out_dir = root / "sessions"
    else:
        # Write to .atlas/ (no .memory/ writes without autonomous)
        out_dir = STATE_DIR
    out_path = out_dir / f"_dream-{date}.md"

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        # Same-day reruns must not overwrite an earlier dream.
        n = 2
        while out_path.exists():
            out_path = out_dir / f"_dream-{date}-{n}.md"
            n += 1
        out_path.write_text(content, encoding="utf-8")
        result["written"] = True
        result["output_path"] = str(out_path)
    except OSError as e:
        result["error"] = f"Write failed: {e}"
        return result

    if not autonomous:
        result["note"] = (
            f"ATLAS_AUTONOMOUS=off: dream written to {out_path} "
            "(not to .memory/sessions/ -- set ATLAS_AUTONOMOUS=on to enable)"
        )

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for the `atlas-dream` CLI command.

    Parses --scope, --dry-run, --force args and calls run_dream(). Writes output
    to .memory/sessions/_dream-YYYY-MM-DD.md (or .atlas/ when not autonomous).
    Exit code 0 on success, 1 on error (empty corpus, LLM failure, write error).
    """
    import argparse

    from atlas_plus.branding import product
    parser = argparse.ArgumentParser(
        description=f"{product()} Dream Mode: on-demand meta-cognitive reflection over the corpus.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Output: .memory/sessions/_dream-YYYY-MM-DD.md\n"
            "       (or .atlas/_dream-YYYY-MM-DD.md when ATLAS_AUTONOMOUS=off)\n\n"
            "Examples:\n"
            "  atlas-dream                         # full reflection\n"
            "  atlas-dream --scope feedback        # focus on feedback rules\n"
            "  atlas-dream --dry-run               # show corpus stats only\n"
        ),
    )
    parser.add_argument(
        "--scope", action="append", dest="scopes",
        help="Scope to include (repeatable). Defaults to: feedback identity projects reference",
    )
    parser.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Show corpus stats without calling the LLM or writing output.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print the full reflection to stdout in addition to writing the file.",
    )
    args = parser.parse_args()

    autonomous = _is_autonomous()
    scopes = args.scopes or _DEFAULT_SCOPES
    print(f"[atlas-dream] Scopes: {scopes} | autonomous: {autonomous} | dry-run: {args.dry_run}")

    result = run_dream(scopes=scopes, dry_run=args.dry_run)

    if result.get("error"):
        print(f"[atlas-dream] Error: {result['error']}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        return

    if result.get("written"):
        print(f"[atlas-dream] Dream written: {result['output_path']}")
        if not autonomous:
            print(f"[atlas-dream] {result.get('note', '')}")
        if args.verbose:
            print("\n" + Path(result["output_path"]).read_text(encoding="utf-8"))
    else:
        print(f"[atlas-dream] Not written: {result}", file=sys.stderr)


if __name__ == "__main__":
    main()
