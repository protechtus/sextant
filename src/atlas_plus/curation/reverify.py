"""Autonomous re-verification: the action half of decay-as-behavior.

When the decay scan flags a memory as STALE/EXPIRED (curation.decay.reverify_queue),
the system does not merely report it - it re-verifies. `reverify_fact()` asks the
model whether a stored fact still holds; the verdict prescribes an action, and
`run_reverification()` files each one into the run report:

    STILL_VALID  -> reset the decay clock (bump `updated:` + `last_verified_at:`;
                    applied only behind the caller's autonomous gate)
    OUTDATED     -> `quarantine` list in the report (do not assert; nothing is
                    moved or deleted -- acting on the list is the operator's call)
    NEEDS_HUMAN  -> `human_review` list in the report

The model call is injected, so the logic is fully unit-testable without a model,
and routes through the LLM provider layer by default (role 'brain') - so
re-verification is itself LLM-agnostic. `reset_clock()` is the ONLY memory
mutation here and is performed only when the caller explicitly invokes it (the
caller gates it behind the autonomous kill-switch); reverify_fact() never writes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional


class ReverifyOutcome(str, Enum):
    STILL_VALID = "still_valid"
    OUTDATED = "outdated"
    NEEDS_HUMAN = "needs_human"


# What each outcome tells the system to do.
_OUTCOME_ACTION = {
    ReverifyOutcome.STILL_VALID: "reset_clock",
    ReverifyOutcome.OUTDATED: "quarantine",
    ReverifyOutcome.NEEDS_HUMAN: "human_review",
}


@dataclass
class ReverifyVerdict:
    """The result of re-verifying one stored memory."""

    path: str
    outcome: ReverifyOutcome
    action: str           # reset_clock | quarantine | human_review
    reasoning: str
    raw: str = ""

    @property
    def resets_clock(self) -> bool:
        return self.outcome is ReverifyOutcome.STILL_VALID

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "outcome": self.outcome.value,
            "action": self.action,
            "reasoning": self.reasoning,
        }


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

_PROMPT = """You are auditing one stored memory for staleness. Decide whether it is still accurate.

MEMORY
  title: {title}
  last updated: {updated} ({age} days ago)
  half-life: {half_life} days
---
{content}
---

Answer with EXACTLY one of these tokens on the first line:
  STILL_VALID   the fact is almost certainly still true
  OUTDATED      the fact is likely no longer true
  NEEDS_HUMAN   cannot be determined without asking the user
Then one short sentence of justification. Be conservative: if unsure, answer NEEDS_HUMAN. /no_think"""


def _strip_think(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


def parse_outcome(raw: str) -> ReverifyOutcome:
    """Extract the verdict token from a model reply. Ambiguous/empty -> NEEDS_HUMAN (safe)."""
    text = _strip_think(raw).upper()
    if not text:
        return ReverifyOutcome.NEEDS_HUMAN
    # Match whole tokens; check the safest-to-act-on order, but prefer the first that appears.
    positions = []
    for token, outcome in (
        ("STILL_VALID", ReverifyOutcome.STILL_VALID),
        ("OUTDATED", ReverifyOutcome.OUTDATED),
        ("NEEDS_HUMAN", ReverifyOutcome.NEEDS_HUMAN),
    ):
        idx = text.find(token)
        if idx != -1:
            positions.append((idx, outcome))
    if not positions:
        return ReverifyOutcome.NEEDS_HUMAN
    positions.sort(key=lambda p: p[0])
    return positions[0][1]


GenerateFn = Callable[[str], str]


def _default_generate(prompt: str) -> str:
    from atlas_plus.llm import get_provider
    return get_provider("brain").generate(prompt, temperature=0.1, timeout=90.0)


def reverify_fact(
    *,
    path: str,
    title: str,
    content: str,
    updated: str = "",
    age_days: int = 0,
    half_life_days: int = 0,
    generate: Optional[GenerateFn] = None,
) -> ReverifyVerdict:
    """Re-verify one stored memory via the model. Pure w.r.t. the filesystem: never writes.

    `generate` is injected (default: provider-layer role 'brain'), so this is testable
    without a model and LLM-agnostic in production."""
    gen = generate or _default_generate
    prompt = _PROMPT.format(
        title=title or path,
        updated=updated or "unknown",
        age=age_days,
        half_life=half_life_days,
        content=(content or "").strip()[:4000],
    )
    try:
        raw = gen(prompt) or ""
    except Exception as exc:  # a model failure must not crash the scan; defer to human
        return ReverifyVerdict(
            path=path, outcome=ReverifyOutcome.NEEDS_HUMAN, action="human_review",
            reasoning=f"re-verification model call failed: {exc}", raw="",
        )
    outcome = parse_outcome(raw)
    reasoning = _strip_think(raw)
    # one-line justification after the token, if present
    lines = [ln.strip() for ln in reasoning.splitlines() if ln.strip()]
    just = lines[1] if len(lines) > 1 else (lines[0] if lines else "")
    return ReverifyVerdict(
        path=path, outcome=outcome, action=_OUTCOME_ACTION[outcome],
        reasoning=just[:300], raw=raw,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def reset_clock(path: Path, *, today: Optional[str] = None, now_iso: Optional[str] = None) -> bool:
    """Reset a memory's decay clock: set `updated:` to today and `last_verified_at:` to now,
    preserving the rest of the frontmatter and the body. Returns True if rewritten.

    This is the only memory mutation in this module. Caller is responsible for the
    autonomous-write gate and version-before-edit."""
    today = today or date.today().isoformat()
    now_iso = now_iso or _now_iso()
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if not raw.startswith("---"):
        return False
    end = raw.find("\n---", 3)
    if end == -1:
        return False

    fm_lines = raw[3:end].lstrip("\n").split("\n")
    body = raw[end + 4:]  # everything after the closing '\n---'

    out: list[str] = []
    saw_updated = saw_verified = False
    for ln in fm_lines:
        if ln.startswith("updated:"):
            out.append(f"updated: {today}")
            saw_updated = True
        elif ln.startswith("last_verified_at:"):
            out.append(f"last_verified_at: {now_iso}")
            saw_verified = True
        else:
            out.append(ln)
    if not saw_updated:
        out.append(f"updated: {today}")
    if not saw_verified:
        out.append(f"last_verified_at: {now_iso}")

    rebuilt = "---\n" + "\n".join(out) + "\n---" + body
    if rebuilt == raw:
        return False
    path.write_text(rebuilt, encoding="utf-8")
    return True


@dataclass
class ReverifyRun:
    """Outcome of one autonomous re-verification pass over the decay queue."""

    checked: int = 0
    reset: list = field(default_factory=list)        # clock reset (still valid + autonomous on)
    would_reset: list = field(default_factory=list)  # still valid but reset gated off
    quarantine: list = field(default_factory=list)   # outdated -> queued for archive/correction
    human: list = field(default_factory=list)        # needs human confirmation
    verdicts: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "reset": list(self.reset),
            "would_reset": list(self.would_reset),
            "quarantine": list(self.quarantine),
            "human": list(self.human),
        }

    def summary_md(self) -> str:
        lines = [
            "# Decay re-verification run",
            "",
            f"- checked: {self.checked}",
            f"- clock reset (still valid): {len(self.reset)}",
            f"- would reset (autonomous off): {len(self.would_reset)}",
            f"- quarantine (outdated): {len(self.quarantine)}",
            f"- needs human: {len(self.human)}",
        ]
        for v in self.verdicts:
            lines.append(f"\n## {v.path} -> {v.outcome.value} ({v.action})\n{v.reasoning}")
        return "\n".join(lines)


def run_reverification(
    candidates: list,
    *,
    autonomous: bool = False,
    limit: int = 10,
    generate: Optional[GenerateFn] = None,
    load_content: Optional[Callable] = None,
    reset: Optional[Callable] = None,
    today: Optional[str] = None,
    now_iso: Optional[str] = None,
) -> ReverifyRun:
    """Autonomous decay re-verification loop: re-verify each queued candidate and ACT.

    - STILL_VALID: reset the decay clock - but only if `autonomous` (the kill switch);
      otherwise it is recorded as would_reset, never mutated.
    - OUTDATED:    queued for quarantine. Never auto-deleted (archive-not-delete).
    - NEEDS_HUMAN: queued for human confirmation.

    Collaborators (generate / load_content / reset) are injectable for tests; the
    defaults read and mutate files under MEMORY_ROOT."""
    gen = generate or _default_generate

    if load_content is None:
        def load_content(c):
            from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
            try:
                return (MEMORY_ROOT / c.path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""

    if reset is None:
        def reset(c):
            from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
            return reset_clock(MEMORY_ROOT / c.path, today=today, now_iso=now_iso)

    run = ReverifyRun()
    for c in list(candidates)[:limit]:
        content = load_content(c)
        age = getattr(c, "days_overdue", 0) + getattr(c, "half_life_days", 0)
        verdict = reverify_fact(
            path=c.path, title=getattr(c, "title", c.path), content=content,
            updated=getattr(c, "updated", ""), age_days=age,
            half_life_days=getattr(c, "half_life_days", 0), generate=gen,
        )
        run.verdicts.append(verdict)
        run.checked += 1
        if verdict.outcome is ReverifyOutcome.STILL_VALID:
            if autonomous and reset(c):
                run.reset.append(c.path)
            else:
                run.would_reset.append(c.path)
        elif verdict.outcome is ReverifyOutcome.OUTDATED:
            run.quarantine.append(c.path)
        else:
            run.human.append(c.path)
    return run
