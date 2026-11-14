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
from ..routing import _scrub_controls, append_fact, confirm_fact, resolve_target
from ..versioning import version_before_write

log = logging.getLogger("atlas.mcp.remember")


def _has_conflict_markers(path: Path) -> bool:
    try:
        head = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    from ..routing import has_conflict_markers
    return has_conflict_markers(head)


def _routing_root():
    from .. import routing as _routing
    return _routing.MEMORY_ROOT


_UNMERGED_CACHE: dict = {"at": float("-inf"), "paths": frozenset()}


def _is_unmerged(target: Path) -> bool:
    """True when git holds this path as unmerged (status UU/AA/UD/DU). The list is cached for two seconds
    per process so a burst of writes costs one `git ls-files -u` (AG57)."""
    import time as _time
    root = Path(_routing_root())  # the writer's root, not the import-time constant (MN49)
    if not (root / ".git").exists():
        return False
    now = _time.monotonic()
    if now - _UNMERGED_CACHE["at"] > 2.0 or _UNMERGED_CACHE.get("root") != str(root):
        try:
            import subprocess as _sp
            out = _sp.run(["git", "-c", "core.quotePath=false", "ls-files", "-u", "-z"], cwd=str(root), capture_output=True, stdin=_sp.DEVNULL,
                          text=True, encoding="utf-8", errors="replace", timeout=5).stdout  # never the server's stdin (MN53)
            paths = frozenset(e.split("\t", 1)[1] for e in out.split("\0") if "\t" in e)
        except Exception:  # noqa: BLE001
            paths = frozenset()
        _UNMERGED_CACHE.update(at=now, paths=paths, root=str(root))
    if not _UNMERGED_CACHE["paths"]:
        return False
    try:
        rel = target.resolve().relative_to(root.resolve()).as_posix()
    except Exception:  # noqa: BLE001
        return False
    hit = rel in _UNMERGED_CACHE["paths"]
    if hit:
        _UNMERGED_CACHE["at"] = float("-inf")  # a refusal re-checks git on the next call: `git add` then a write within two seconds was refused again (MN53)
    return hit


def _git_autocommit(file_path: Path, scope: str, memory_root: Path = MEMORY_ROOT) -> str:
    """Stage and commit a single .memory/ file if ATLAS_MEMORY_AUTOCOMMIT=on.

    Runs `git -C <memory_root> add <relative>` then `git -C <memory_root> commit -m ...`.
    Non-fatal: any error is logged at DEBUG level and silently ignored.
    Uses a 5-second timeout per subprocess call to avoid blocking the MCP server.
    """
    if os.environ.get("ATLAS_MEMORY_AUTOCOMMIT", "off").lower() not in ("on", "1", "true"):
        return ""
    git_meta = memory_root / ".git"
    if any((git_meta / m).exists() for m in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")):
        log.debug("Memory autocommit skipped: a merge is open in %s (resolve it first)", memory_root)  # MN27
        return f"not auto-committed: a merge is open in {memory_root}; commit after you resolve it"  # the skip was silent (AG57)
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


_CJK_RE = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")


def _hint_tokens(s: str) -> set[str]:
    """Duplicate-hint tokens: Latin words (4+, no stopwords) and CJK bigrams, so a repeated
    Japanese fact gets the same hint an English one does (MN23)."""
    out = {
        w.lower() for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]*", s)
        if len(w) >= 4 and w.lower() not in _STOPWORDS
    }
    for run in re.findall(_CJK_RE.pattern + "+", s):
        out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _jaccard(a: str, b: str) -> float:
    """Jaccard similarity between word sets of two strings (stopwords removed)."""
    def tokens(s: str) -> set[str]:
        """Lowercase words of four letters or more (stopwords removed) plus every CJK bigram."""
        return _hint_tokens(s)
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
        bigrams = [t for t in _hint_tokens(fact) if _CJK_RE.search(t)][:4]
        if len(words) < 2 and len(bigrams) < 2:
            return None

        # CJK bigrams are OR-ed (a near repeat shares most of them, rarely all); Latin words AND
        query = " ".join(words) if len(words) >= 2 else "(" + " OR ".join(f'"{b}"' for b in bigrams) + ")"
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


def _same_file_repeat(fact: str, written_to: Path) -> Optional[dict]:
    """The fact's text already stood in this note before the write (two or more copies now)."""
    try:
        needle = " ".join(fact.split()).lower()  # case-insensitive (MN53)
        if len(needle) < 8:
            return None
        lines = written_to.read_text(encoding="utf-8", errors="replace").splitlines()
        hits = sum(1 for ln in lines if needle in " ".join(ln.split()).lower())
        if hits >= 2:
            try:
                rel = written_to.resolve().relative_to(Path(_routing_root()).resolve()).as_posix()
            except Exception:  # noqa: BLE001
                rel = written_to.name
            return {"path": rel, "score": 1.0}
    except Exception:  # noqa: BLE001
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

    An empty fact (nothing left once control and zero-width characters are scrubbed) is
    refused here, so the MCP tools refuse it the way the CLI does (MN20).

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
    if not _scrub_controls(fact or "").replace("\u200b", "").replace("\ufeff", "").strip():
        return {"ok": False, "error": "empty fact: nothing to remember (give some words)"}
    from ..routing import has_conflict_markers as _hcm
    if _hcm(fact or ""):
        # the guard checked the note, not the fact: a fact about resolving merges bricked its own
        # note for every later write (AG39, sweep 15)
        return {"ok": False, "file": None, "version_copy": None,
                "error": "a fact may not start a line with <<<<<<< or >>>>>>> (git conflict markers would make the note unwritable): indent those lines, or describe the merge in words"}
    # interactive=True = a user-invoked MCP tool call (explicit consent; see
    # atlas_plus.autonomous). Background callers (Brain, telegram, promotion)
    # never set it and stay behind the ATLAS_AUTONOMOUS kill switch.
    from atlas_plus.autonomous import require_write
    require_write("memory_remember", interactive=interactive)

    try:
        target, fm_template = resolve_target(scope)
    except ValueError as e:
        return {"ok": False, "file": None, "version_copy": None, "error": str(e)}
    if target.exists() and _is_unmerged(target):
        # a modify/delete or add/add conflict has no markers; `git ls-files -u` is the only trace (AG57: the write landed in the file git was holding)
        try:
            _rel = target.resolve().relative_to(Path(_routing_root()).resolve()).as_posix()
        except Exception:  # noqa: BLE001
            _rel = target.name
        return {"ok": False, "file": None, "version_copy": None,
                "error": f"{_rel} is unmerged in git (a modify/delete or add/add conflict): resolve it and `git add` it first; sextant doctor names such paths"}
    if target.exists() and _has_conflict_markers(target):
        # a git merge left markers in this note: writing into it grows a half-resolved note
        # and search would index the markers (MN27, sweep 14)
        try:
            _rel = target.resolve().relative_to(Path(_routing_root()).resolve()).as_posix()
        except Exception:  # noqa: BLE001
            _rel = target.name
        return {"ok": False, "file": None, "version_copy": None,
                "error": f"{_rel} holds git conflict markers (<<<<<<< / >>>>>>>): resolve the merge first; sextant doctor names such notes"}

    # A corpus that was never initialised is not written into: the write used to succeed by
    # creating a stray `.memory` under the home default whenever the client's env never
    # reached the server (registration without -e, GUI-launched client), so the user's facts
    # landed in a corpus nobody looks at while memory_health said degraded (sweep 2026-11-11).
    from .. import routing as _routing
    root = Path(_routing.MEMORY_ROOT)   # not target.parents[1]: a slash topic is deeper (AG22)
    if not root.exists():
        return {"ok": False, "file": None, "version_copy": None,
                "error": (f"memory root does not exist: {root} -- run sextant-init there, or pass "
                          "ATLAS_MEMORY_ROOT (or ATLAS_WORKSPACE_ROOT) to the MCP server; "
                          "`sextant setup` prints the registration with them baked in")}

    _vh: dict = {"path": None}

    def _version(t: Path) -> None:
        _vh["path"] = version_before_write(t)

    try:
        append_fact(
            target, fact, fm_template,
            source=source, confidence=confidence,
            session_id=session_id, task_id=task_id,
            interactive=interactive,
            pre_write=_version,
        )
    except Exception as e:
        vp = _vh["path"]
        return {"ok": False, "file": (str(target) if target.exists() else None), "version_copy": str(vp) if vp else None, "error": str(e)}
    version_path = _vh["path"]

    # Optional git autocommit (non-blocking, controlled by ATLAS_MEMORY_AUTOCOMMIT)
    _ac_note = _git_autocommit(target, scope)

    # Post-write duplicate check (non-blocking -- failure returns ok=True anyway)
    dup = _find_duplicate(fact, target)
    if dup is None:
        dup = _same_file_repeat(fact, target)  # a retry after a timeout stores the fact twice in the same note; the FTS check skips self (AG56)
    # A rejected idea stored again gets a warning naming the rejection (AG20, sweep 12): the
    # write stands (a note restating WHY something was rejected is a valid fact), the caller
    # decides.
    anti_hit = None
    try:
        from .anti import anti_matches
        hits = anti_matches(fact)
        if hits:
            h = hits[0]
            anti_hit = (f"this repeats a rejected idea: '{h.get('reject_pattern')}' "
                        f"(anti-memory {h.get('scope')}; reason: {h.get('reason') or 'none given'}); "
                        "review it with memory_anti_list_tool before acting on this fact")
    except Exception:  # noqa: BLE001
        anti_hit = None

    return {
        "ok": True,
        "file": str(target),
        "version_copy": str(version_path) if version_path else None,
        "error": None,
        "possible_duplicate": dup is not None,
        "autocommit_note": _ac_note or None,
        "duplicate_hint": f"Similar content at {dup['path']} (sim={dup['score']})" if dup else None,
        "anti_warning": anti_hit,
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
        from ..routing import has_conflict_markers as _hcm
        if target.exists() and _hcm(target.read_text(encoding="utf-8", errors="replace")):
            return {"ok": False, "path": path, "confirmed": None, "error": f"{path} holds git conflict markers (<<<<<<< / >>>>>>>): resolve the merge first"}  # the sibling of remember's refusal (AG47)
    except OSError:
        pass
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
