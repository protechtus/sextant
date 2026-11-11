"""memory_decision_tool -- write structured decisions with conditions and revisit triggers.

A decision is distinct from a preference or fact:
  - It was made under specific conditions.
  - Those conditions can change, making the decision stale.
  - It should be revisited when `revisit_if` conditions are met.

Each decision becomes its own .memory/decisions/<slug>.md file.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

from atlas_plus.autonomous import require_autonomous_writes


def _search_dirty() -> None:
    """Void the FTS freshness window after a direct corpus write (expert
    pass 2026-10-05, M1: decision-then-search missed the debounce hook).
    Best-effort -- never fail a write over the search index."""
    try:
        from atlas_plus.mcp.memory_server.fts import mark_dirty
        mark_dirty()
    except Exception:  # pragma: no cover
        pass

from ..config import MEMORY_ROOT
from ..routing import _atomic_write_text
from ..versioning import version_before_write  # noqa: F401 -- kept for callers/tests that patch it


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower().strip())[:60].strip("-")


def _hash_slug(text: str) -> str:
    """Fallback slug for titles with no [a-z0-9] characters (non-Latin
    scripts, punctuation only), which previously all collapsed to '.md'."""
    return "decision-" + hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]


_MAX_SUFFIX = 1000


def _claim_slug(decisions_dir: Path, base: str) -> tuple[str, Path]:
    """Atomically claim ``<base>.md`` or the first free ``<base>-N.md``.

    O_CREAT|O_EXCL makes the claim race-free across processes, so two
    writers with the same title get two files instead of one lost decision.
    """
    for n in range(1, _MAX_SUFFIX + 1):
        slug = base if n == 1 else f"{base}-{n}"
        target = decisions_dir / f"{slug}.md"
        try:
            fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(fd)
        return slug, target
    raise FileExistsError(f"no free decision slug for {base!r} after {_MAX_SUFFIX} tries")


def memory_remember_decision(
    title: str,
    decision: str,
    conditions: list[str] | None = None,
    revisit_if: list[str] | None = None,
    project: str = "",
    alternatives_rejected: str = "",
    source: str = "agent",
    interactive: bool = False,
) -> dict:
    """Record a technical or strategic decision in .memory/decisions/.

    Args:
        title: Short title, e.g. "Use FAISS for VoIP corpus".
        decision: The decision text -- what was chosen and why.
        conditions: Assumptions that held when the decision was made.
        revisit_if: Conditions that should trigger re-evaluation.
        project: Project this decision belongs to (optional).
        alternatives_rejected: What was considered and discarded (optional).
        source: Write source for provenance (agent, human-direct, telegram).

    Returns {ok, file, slug, error}.
    """
    # interactive=True = the user asked for it in a session (same consent as memory_remember);
    # background callers stay behind the kill switch (power-user pass 2026-11-10).
    from atlas_plus.autonomous import require_write
    require_write("memory_remember_decision", interactive=interactive)

    if not title.strip():
        return {"ok": False, "file": None, "slug": None, "error": "title is required"}
    if not decision.strip():
        return {"ok": False, "file": None, "slug": None, "error": "decision text is required"}

    decisions_dir = MEMORY_ROOT / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    # Never overwrite an existing decision (audit 2026-11-09, row 15): the
    # slug is claimed with O_CREAT|O_EXCL, and a taken slug gets -2, -3, ...
    try:
        slug, target = _claim_slug(decisions_dir, _slug(title) or _hash_slug(title))
    except OSError as exc:
        return {"ok": False, "file": None, "slug": None, "error": f"could not claim decision file: {exc}"}

    # `made` is the LOCAL date, like every other date in the corpus (facts, anti-memories);
    # the full timestamp stays UTC-explicit (persona rerun 2026-11-10: "today" was two dates).
    now_iso = date.today().isoformat()
    now_full = datetime.now(timezone.utc).isoformat()

    # yaml.safe_dump, never an f-string: a title with ": " or a newline in project used to
    # break or rewrite the frontmatter (round-B re-audit 2026-11-09, N-C).
    import yaml as _yaml
    fm_fields = {
        "id": slug,
        "title": str(title),
        "type": "decision",
        "status": "active",
        "priority": "high",
        "project": str(project or "workspace").replace("\n", " ").strip(),
        "made": now_iso,
        "source": str(source),
        "revisit_if": [str(r) for r in revisit_if] if revisit_if else ["(none recorded)"],
        "conditions": [str(c) for c in conditions] if conditions else ["(none recorded)"],
    }
    fm = "---\n" + _yaml.safe_dump(fm_fields, sort_keys=False, allow_unicode=True,
                                   default_flow_style=False, width=10**6) + "---"

    body_parts = [f"## Decision\n\n{decision.strip()}"]
    if alternatives_rejected.strip():
        body_parts.append(f"\n## Alternatives rejected\n\n{alternatives_rejected.strip()}")
    body_parts.append(f"\n## Conditions when made\n\n{chr(10).join('- ' + c for c in (conditions or ['(none recorded)']))}")
    body_parts.append(f"\n## Revisit when\n\n{chr(10).join('- ' + r for r in (revisit_if or ['(none recorded)']))}")
    body_parts.append(f"\n---\n*Recorded {now_full} via {source}*")

    content = fm + "\n\n" + "\n".join(body_parts) + "\n"
    try:
        _atomic_write_text(target, content)
    except OSError as exc:
        # Release the empty placeholder we claimed; it holds no user data.
        try:
            if target.stat().st_size == 0:
                target.unlink()
        except OSError:
            pass
        return {"ok": False, "file": None, "slug": None, "error": f"write failed: {exc}"}
    _search_dirty()

    return {
        "ok": True,
        "file": str(target),
        "path": str(target),
        "slug": slug,
        "replaced": False,
        "error": None,
    }


def list_decisions(project: str = "") -> list[dict]:
    """Return all decisions, optionally filtered by project."""
    decisions_dir = MEMORY_ROOT / "decisions"
    if not decisions_dir.exists():
        return []
    results = []
    for f in sorted(decisions_dir.glob("*.md")):
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
            fm: dict = {}
            if raw.startswith("---"):
                end = raw.find("\n---", 3)
                if end != -1:
                    import yaml
                    fm = yaml.safe_load(raw[3:end]) or {}
            if project and fm.get("project", "") != project:
                continue
            results.append({
                "slug": f.stem,
                "title": fm.get("title", f.stem),
                "project": fm.get("project", ""),
                "made": fm.get("made", ""),
                "status": fm.get("status", "active"),
                "revisit_if": fm.get("revisit_if", []),
                "conditions": fm.get("conditions", []),
            })
        except Exception:
            pass
    return results
