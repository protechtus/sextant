"""P3-T2/T5: memory_remember MCP tool -- opt-in fact writing to .memory/.

Optional git autocommit: set ATLAS_MEMORY_AUTOCOMMIT=on to automatically
`git add <file> && git commit` after every successful memory_remember write.
Requires .memory/ to be a git repository. Failures are logged and ignored --
the remember tool always succeeds regardless of git status.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from atlas_plus.autonomous import require_autonomous_writes

from ..config import MEMORY_ROOT
from ..routing import append_fact, confirm_fact, resolve_target
from ..versioning import version_before_write

log = logging.getLogger("atlas.mcp.remember")


def _git_autocommit(file_path: Path, scope: str, memory_root: Path = MEMORY_ROOT) -> None:
    """Stage and commit a single .memory/ file if ATLAS_MEMORY_AUTOCOMMIT=on.

    Runs `git -C <memory_root> add <relative>` then `git -C <memory_root> commit -m ...`.
    Non-fatal: any error is logged at DEBUG level and silently ignored.
    Uses a 5-second timeout per subprocess call to avoid blocking the MCP server.
    """
    if os.environ.get("ATLAS_MEMORY_AUTOCOMMIT", "off").lower() not in ("on", "1", "true"):
        return
    try:
        rel = str(file_path.relative_to(memory_root)).replace("\\", "/")
        git_dir = str(memory_root)
        # Stage the specific file only -- do not `git add .` to avoid capturing secrets
        # Hardened: --no-verify skips only pre-commit/commit-msg; post-commit, fsmonitor and
        # filters still ran from a repo config a sync peer could rewrite (round-B N-J 2026-11-09).
        from atlas_plus import gitsafe
        gitsafe.run(["-C", git_dir, "add", rel], None, timeout=5, text=False)
        msg = f"Atlas auto-commit: {scope} [{file_path.name}]"
        result = gitsafe.run(["-C", git_dir, "commit", "-m", msg, "--no-verify"], None, timeout=5)
        if result.returncode == 0:
            log.debug("Memory autocommit: %s", rel)
        elif "nothing to commit" in (result.stdout + result.stderr):
            log.debug("Memory autocommit: nothing new to commit for %s", rel)
        else:
            log.debug("Memory autocommit failed for %s: %s", rel, result.stderr.strip()[:80])
    except Exception as exc:
        log.debug("Memory autocommit error: %s", exc)

# Minimum Jaccard overlap to flag as a possible duplicate
_DUP_THRESHOLD = 0.45
# Stopwords excluded from duplicate-detection token comparison
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "are", "have", "from",
    "not", "use", "used", "using", "will", "been", "into", "over", "when",
    "what", "which", "such", "more", "than", "also", "any", "all", "its",
    "our", "your", "should", "always", "never", "can", "may", "must", "was",
}


def _jaccard(a: str, b: str) -> float:
    """Jaccard similarity between word sets of two strings (stopwords removed)."""
    def tokens(s: str) -> set[str]:
        """Tokenise a string into a set of lowercase alpha-numeric words (min 4 chars, stopwords removed)."""
        return {
            w.lower() for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]*", s)
            if len(w) >= 4 and w.lower() not in _STOPWORDS
        }
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _find_duplicate(fact: str, written_to: Path) -> Optional[dict]:
    """Search for a similar existing fact via FTS. Returns {path, score} or None.

    Runs only the first 6 distinctive words of the fact as the query to stay fast.
    Excludes the file we just wrote to (self-match). Returns None on any error.
    """
    try:
        from ..fts import search as fts_search
        # Extract first 6 non-stopword words as search query
        words = [
            w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]*", fact)
            if len(w) >= 4 and w.lower() not in _STOPWORDS
        ][:6]
        if len(words) < 2:
            return None

        query = " ".join(words)
        results = fts_search(query, limit=5)

        written_rel = str(written_to.relative_to(MEMORY_ROOT)).replace("\\", "/")
        for r in results:
            if r.get("path") == written_rel:
                continue  # skip self
            snippet = r.get("snippet", "") + r.get("content", "")
            sim = _jaccard(fact, snippet)
            if sim >= _DUP_THRESHOLD:
                return {"path": r["path"], "score": round(sim, 3)}
    except Exception:
        pass
    return None


def memory_remember(
    scope: str,
    fact: str,
    source: str = "agent",
    confidence: Optional[float] = None,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    interactive: bool = False,
) -> dict:
    """Write a fact to the appropriate .memory/ file.

    After writing, performs a lightweight duplicate check: if a similar fact
    already exists in another memory file (Jaccard similarity >= 0.45 on
    distinctive words), the response includes `possible_duplicate: true` and
    `duplicate_hint` with the conflicting file path. Agents should review before
    writing the fact again.

    Args:
        scope:      Where to store the fact. Examples:
                    'identity'                 -> identity/user.md
                    'preference:editor'        -> feedback/editor.md
                    'project:myapp'            -> projects/myapp.md
                    'reference:acme'           -> reference/acme.md
                    'decision:use-faiss'       -> decisions/use-faiss.md
                    'constraint:archive-only'  -> constraints/archive-only.md
                    'observation:work-pattern' -> observations/work-pattern.md
        fact:       The fact to record. One or two sentences. No markdown headers.
        source:     Provenance tag (agent | human-direct | suggest-facts | telegram).
        confidence: Numeric 0-1 score from extraction model. Mapped to high/medium/low
                    in frontmatter. None -> 'medium'. Human sources -> 'high' regardless.
        session_id: Brain/Claude session ID for traceability (stored in frontmatter).
        task_id:    Brain task ID that triggered this write (stored in frontmatter).

    Returns {ok, file, version_copy, error, possible_duplicate, duplicate_hint}.
    """
    # interactive=True = a user-invoked MCP tool call (explicit consent; see
    # atlas_plus.autonomous). Background callers (Brain, telegram, promotion)
    # never set it and stay behind the ATLAS_AUTONOMOUS kill switch.
    from atlas_plus.autonomous import require_write
    require_write("memory_remember", interactive=interactive)

    try:
        target, fm_template = resolve_target(scope)
    except ValueError as e:
        return {"ok": False, "file": None, "version_copy": None, "error": str(e)}

    # A corpus that was never initialised is not written into: the write used to succeed by
    # creating a stray `.memory` under the home default whenever the client's env never
    # reached the server (registration without -e, GUI-launched client), so the user's facts
    # landed in a corpus nobody looks at while memory_health said degraded (sweep 2026-11-11).
    root = target.parents[1]
    if not root.exists():
        return {"ok": False, "file": None, "version_copy": None,
                "error": (f"memory root does not exist: {root} -- run sextant-init there, or pass "
                          "ATLAS_MEMORY_ROOT (or ATLAS_WORKSPACE_ROOT) to the MCP server; "
                          "`sextant setup` prints the registration with them baked in")}

    version_path = version_before_write(target)

    try:
        append_fact(
            target, fact, fm_template,
            source=source, confidence=confidence,
            session_id=session_id, task_id=task_id,
            interactive=interactive,
        )
    except Exception as e:
        return {"ok": False, "file": str(target), "version_copy": str(version_path) if version_path else None, "error": str(e)}

    # Optional git autocommit (non-blocking, controlled by ATLAS_MEMORY_AUTOCOMMIT)
    _git_autocommit(target, scope)

    # Post-write duplicate check (non-blocking -- failure returns ok=True anyway)
    dup = _find_duplicate(fact, target)

    return {
        "ok": True,
        "file": str(target),
        "version_copy": str(version_path) if version_path else None,
        "error": None,
        "possible_duplicate": dup is not None,
        "duplicate_hint": f"Similar content at {dup['path']} (sim={dup['score']})" if dup else None,
    }


def memory_confirm(path: str, source: str = "agent", interactive: bool = False) -> dict:
    """Mark a .memory/ file as recently confirmed accurate.

    Updates last_confirmed: and updated: frontmatter without adding new content.
    Use this when you read a fact and verify it is still correct -- it resets the
    effective age of the fact for decay detection purposes.

    Args:
        path:   Relative path within .memory/ (e.g. 'reference/acme.md').
        source: Who confirmed it (agent | human-direct | telegram).

    Returns {ok, path, confirmed, error}.
    """
    # Same write gate as every other MCP write (audit 2026-11-09, row 26):
    # ATLAS_INTERACTIVE_WRITES=off must stop confirms too.
    from atlas_plus.autonomous import require_write
    try:
        require_write("memory_confirm", interactive=interactive)
    except RuntimeError as e:
        return {"ok": False, "path": path, "confirmed": None, "error": str(e)}
    from . import read as _read
    target = _read._safe_resolve(path)
    if target is None:
        return {"ok": False, "path": path, "confirmed": None, "error": "invalid path"}
    try:
        # Version copy before the frontmatter rewrite, like memory_remember.
        # The target is resolved, so the corpus root must be too.
        if target.exists():
            version_before_write(target, root=Path(_read.MEMORY_ROOT).resolve())
        ok = confirm_fact(target, source=source)
    except Exception as e:
        return {"ok": False, "path": path, "confirmed": None, "error": str(e)}
    if not ok:
        return {"ok": False, "path": path, "confirmed": None, "error": f"File not found: {path}"}
    from datetime import date
    confirmed = date.today().isoformat()
    return {"ok": True, "path": path, "confirmed": confirmed, "error": None}
