"""Promotion gate: move a quarantined, unverified idea to a confirmed memory fact.

This closes the Inspiration Engine's Verify -> Promote step (see
docs/INSPIRATION-ENGINE.md). An idea produced by `inspire()` lives unverified in the
quarantine store (.atlas/brain/ideas/). It can leave quarantine in exactly three ways:

  - PROMOTED: written into .memory/ as a real fact (via memory_remember), but only if
    it does not contradict. This is the verifier saying "yes."
  - DEMOTED: a contradiction was found, so it is NOT written; it stays an idea.
  - REJECTED: recorded to anti-memory so the system stops re-proposing it.

Contradiction guard: there is no standalone temporal-graph checker, and ideas are free
text rather than clean subject-predicate-object triples, so the realistic, lightweight
guard for prose is the ANTI-MEMORY match (do not re-promote something already rejected).
When an idea IS expressed as a structured fact, `temporal_contradiction(TemporalFact)`
runs the real graph overlap check (it loads the graph, so it is opt-in/expensive).

Everything here is dependency-injected (write_fact / check_contradiction / record_anti)
so the logic is unit-testable without touching the live memory corpus or graph; the
`default_*` adapters wire the real Atlas subsystems.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .inspiration import normalize_answer, _tokens, jaccard

# write_fact(scope, fact, source, confidence) -> path-ish
WriteFactFn = Callable[[str, str, str, float], object]
# check_contradiction(idea_text) -> reason str if it conflicts, else None
CheckContradictionFn = Callable[[str], Optional[str]]
# record_anti(scope, reject_pattern) -> anything
RecordAntiFn = Callable[[str, str], object]


@dataclass
class PromotionResult:
    """Outcome of a promotion attempt."""

    action: str                       # "promoted" | "demoted" | "rejected"
    idea_text: str
    detail: str
    memory_path: Optional[str] = None


# --- core logic (pure; dependencies injected) ------------------------------

def promote_idea(
    idea_text: str,
    *,
    scope: str,
    write_fact: WriteFactFn,
    check_contradiction: CheckContradictionFn,
    source: str = "brain-inspire-promote",
    confidence: float = 0.9,
) -> PromotionResult:
    """Promote an idea to a confirmed memory fact unless it contradicts.

    Calls check_contradiction(idea_text); a non-None return demotes (no write). Otherwise
    write_fact(scope, idea_text, source, confidence) persists it and the idea is promoted.
    """
    idea_text = (idea_text or "").strip()
    if not idea_text:
        return PromotionResult("demoted", "", "empty idea")
    conflict = check_contradiction(idea_text)
    if conflict:
        return PromotionResult("demoted", idea_text, f"contradiction: {conflict}")
    path = write_fact(scope, idea_text, source, confidence)
    return PromotionResult("promoted", idea_text, "written to memory", memory_path=str(path))


def reject_idea(
    idea_text: str,
    *,
    record_anti: RecordAntiFn,
    reason: str = "",
    scope: str = "ideas",
) -> PromotionResult:
    """Reject an idea: record it to anti-memory so it is not re-proposed."""
    idea_text = (idea_text or "").strip()
    pattern = idea_text if not reason else f"{idea_text} -- {reason}"
    record_anti(scope, pattern)
    return PromotionResult("rejected", idea_text, reason or "rejected")


def matches_rejected(idea_text: str, reject_patterns: list[str], *, threshold: float = 0.5) -> Optional[str]:
    """Return a reason if idea_text matches a prior anti-memory reject pattern, else None.

    Pure and testable. Match = normalized-equal, or token-set Jaccard >= threshold.
    """
    it = _tokens(idea_text)
    for pat in reject_patterns:
        if not pat:
            continue
        if normalize_answer(idea_text) == normalize_answer(pat):
            return f"matches rejected idea: {pat[:80]}"
        if jaccard(it, _tokens(pat)) >= threshold:
            return f"matches rejected pattern: {pat[:80]}"
    return None


# --- default adapters (wire the real Atlas subsystems; lazy imports) -------

def default_write_fact(scope: str, fact: str, source: str, confidence: float):
    """Write a confirmed fact into .memory/ via the memory_remember routing."""
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    res = memory_remember(scope=scope, fact=fact, source=source, confidence=confidence)
    if isinstance(res, dict):
        return res.get("path") or res.get("target") or res
    return res


def default_record_anti(scope: str, reject_pattern: str, *, applies_to: str = "workspace", project: str = ""):
    """Record a rejection in anti-memory so the idea is not re-proposed."""
    from atlas_plus.mcp.memory_server.tools.anti import anti_remember
    return anti_remember(scope=scope, reject_pattern=reject_pattern, applies_to=applies_to, project=project)


def default_check_contradiction(idea_text: str) -> Optional[str]:
    """Free-text contradiction guard: does the idea match an existing anti-memory entry?

    Fail-open: if the anti list cannot be read, return None (do not block a legitimate
    promotion on a broken check).
    """
    try:
        from atlas_plus.mcp.memory_server.tools.anti import list_anti
        entries = list_anti() or []
        patterns: list[str] = []
        for e in entries:
            if isinstance(e, dict):
                p = e.get("reject_pattern") or e.get("pattern") or ""
                if p:
                    patterns.append(p)
        return matches_rejected(idea_text, patterns)
    except Exception:
        return None


def temporal_contradiction(fact) -> list[str]:
    """Run the real temporal-graph overlap check for a structured TemporalFact.

    Loads the graph and calls FileTemporalProvider.add_fact (in-memory only; not
    persisted). Returns the list of contradiction warnings (empty = no conflict).
    Opt-in and relatively expensive; only used when an idea is a structured triple.
    """
    from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
    provider = FileTemporalProvider()
    _fact_id, warnings = provider.add_fact(fact)
    return list(warnings or [])


# --- quarantine-file helpers -----------------------------------------------

def read_idea_body(path: str | Path) -> str:
    """Return the idea body (text after YAML frontmatter)."""
    text = Path(path).read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return text.strip()


def update_idea_status(path: str | Path, status: str, **extra: object) -> None:
    """Set the frontmatter `status` of a quarantine idea file and add/replace `extra` fields."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    extra_keys = set(extra.keys())
    if not text.startswith("---"):
        fm = [f"status: {status}"] + [f"{k}: {v}" for k, v in extra.items()]
        p.write_text("---\n" + "\n".join(fm) + "\n---\n\n" + text, encoding="utf-8")
        return
    parts = text.split("---", 2)
    fm_block = parts[1]
    body = parts[2] if len(parts) >= 3 else ""
    out: list[str] = []
    seen_status = False
    for ln in fm_block.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("status:"):
            out.append(f"status: {status}")
            seen_status = True
        elif any(s.startswith(f"{k}:") for k in extra_keys):
            continue  # replaced below
        else:
            out.append(ln)
    if not seen_status:
        out.append(f"status: {status}")
    for k, v in extra.items():
        out.append(f"{k}: {v}")
    p.write_text("---\n" + "\n".join(out) + "\n---\n" + body.lstrip("\n"), encoding="utf-8")


def promote_idea_file(
    path: str | Path,
    *,
    scope: str,
    write_fact: Optional[WriteFactFn] = None,
    check_contradiction: Optional[CheckContradictionFn] = None,
    source: str = "brain-inspire-promote",
    confidence: float = 0.9,
) -> PromotionResult:
    """Promote a quarantined idea file: write to memory unless it contradicts, then
    stamp the file's status (promoted | demoted)."""
    body = read_idea_body(path)
    result = promote_idea(
        body,
        scope=scope,
        write_fact=write_fact or default_write_fact,
        check_contradiction=check_contradiction or default_check_contradiction,
        source=source,
        confidence=confidence,
    )
    if result.action == "promoted":
        update_idea_status(path, "promoted", memory_path=result.memory_path or "")
    else:
        update_idea_status(path, "demoted", reason=result.detail)
    return result


def reject_idea_file(
    path: str | Path,
    *,
    reason: str = "",
    scope: str = "ideas",
    record_anti: Optional[RecordAntiFn] = None,
) -> PromotionResult:
    """Reject a quarantined idea file: record to anti-memory and stamp status=rejected."""
    body = read_idea_body(path)
    result = reject_idea(body, record_anti=record_anti or default_record_anti, reason=reason, scope=scope)
    update_idea_status(path, "rejected", reason=reason or "rejected")
    return result
