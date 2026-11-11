"""Direction D -- Weekly Dream Report: cross-session insight synthesis.

Architecture (rolling approach, keeps LLM context small):

  Nightly (Mon-Sat): Append a one-paragraph summary of yesterday's session log
  to a rolling weekly draft at .atlas/weekly-draft.md. Each append is one
  cheap LLM call (<200 tokens output).

  Weekly (Sunday 3 AM): Read the completed draft, run a synthesis LLM call,
  write the report to .memory/sessions/_week-YYYY-WNN.md, and run suggest-facts
  on the synthesis to extract high-confidence facts.

This avoids batching 7 full session logs into one large context -- instead,
each day adds one short summary to the draft.

Entry points:
  python -m atlas_plus.curation.weekly_synthesis --mode daily    # append summary
  python -m atlas_plus.curation.weekly_synthesis --mode weekly   # synthesize
  atlas-weekly-synthesis --mode daily|weekly

ATLAS_AUTONOMOUS gate:
  off -> daily appends still run (capture only, no .memory/ writes)
  off -> weekly synthesis runs but fact extraction is skipped

Report written to .atlas/weekly-synthesis-report-YYYY-MM-DD.json.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from atlas_plus import paths as atlas_paths
from atlas_plus.autonomous import autonomous_enabled

STATE_DIR = atlas_paths.state_dir()
MEMORY_ROOT = atlas_paths.memory_root()

_DRAFT_FILE = STATE_DIR / "weekly-draft.md"
_SESSIONS_DIR = MEMORY_ROOT / "sessions"


def _is_autonomous() -> bool:
    return autonomous_enabled()  # canonical allowlist gate (audit 2026-09-21)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _week_str() -> str:
    """Return YYYY-WNN format for the current ISO week."""
    dt = datetime.now(timezone.utc)
    return f"{dt.year}-W{dt.isocalendar()[1]:02d}"


def _is_sunday() -> bool:
    return datetime.now(timezone.utc).isoweekday() == 7


def _call_llm(prompt: str) -> str:
    from atlas_plus.summarizer.providers import complete, ProviderError
    try:
        text, _ = complete(prompt)
        return text.strip()
    except ProviderError as e:
        raise RuntimeError(f"LLM provider error: {e}") from e


# ---------------------------------------------------------------------------
# Daily append
# ---------------------------------------------------------------------------

_DAILY_SUMMARY_PROMPT = """\
You are reviewing a session log entry. Write a single paragraph (3-5 sentences)
summarizing what was accomplished, what decisions were made, and what remains
unresolved. Be specific about project names, tools, and key outcomes. No lists,
no headers, no markdown. Plain prose only.

SESSION LOG:
{session_text}

SUMMARY:"""


def _latest_session_text(sessions_dir: Optional[Path] = None) -> tuple[str, str]:
    """Return (date_str, session_text) for yesterday's or today's session log."""
    root = sessions_dir or _SESSIONS_DIR
    # Find the most recently modified YYYY-MM-DD.md file
    candidates = sorted(
        [f for f in root.glob("20??-??-??.md")],
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return "", ""
    latest = candidates[0]
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    # Strip frontmatter
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:].strip()
    return latest.stem, text[:3000]


def run_daily_append(
    sessions_dir: Optional[Path] = None,
    draft_file: Optional[Path] = None,
    _call_llm_fn=None,
) -> dict:
    """Append a one-paragraph summary of the latest session to the weekly draft.

    Args:
        sessions_dir: Override for .memory/sessions/ (used in tests).
        draft_file:   Override for .atlas/weekly-draft.md (used in tests).
        _call_llm_fn: Inject a mock LLM call function (used in tests).

    Returns a status dict.
    """
    draft_path = draft_file or _DRAFT_FILE
    llm = _call_llm_fn or _call_llm
    date_stem, session_text = _latest_session_text(sessions_dir)

    if not session_text.strip():
        return {"status": "no_session", "date": date_stem, "appended": False}

    prompt = _DAILY_SUMMARY_PROMPT.format(session_text=session_text)
    try:
        summary = llm(prompt)
    except RuntimeError as e:
        return {"status": "llm_error", "error": str(e), "appended": False}

    # Append to draft
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with draft_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n## {date_stem}\n\n{summary}\n")
    except OSError as e:
        return {"status": "write_error", "error": str(e), "appended": False}

    return {"status": "ok", "date": date_stem, "appended": True, "summary_chars": len(summary)}


# ---------------------------------------------------------------------------
# Weekly synthesis
# ---------------------------------------------------------------------------

_WEEKLY_SYNTHESIS_PROMPT = """\
You are synthesizing a week of AI-assisted development sessions into a structured
weekly report. Based on the daily summaries below, answer:

1. Focus areas: What projects and tasks consumed the most time?
2. Key decisions: What significant choices or designs were finalized?
3. Patterns: What recurring themes or approaches emerged?
4. Unresolved: What is still open, blocked, or unclear?
5. Insights: What would be most useful for a future assistant to know upfront?

Write in plain prose, no em-dashes, no emojis. 5-8 sentences per section.
Use ISO 8601 dates. Be specific about project names and tools.

DAILY SUMMARIES ({week}):
{draft_text}

REPORT:"""


def _version_copy(path: Path, out_dir: Path) -> None:
    """Best-effort version-before-write for an existing output file."""
    try:
        from atlas_plus.mcp.memory_server.versioning import version_before_write

        version_before_write(path, root=out_dir.parent)
    except Exception:
        pass


def run_weekly_synthesis(
    draft_file: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    suggest_facts: bool = True,
    _call_llm_fn=None,
) -> dict:
    """Synthesize the weekly draft into a structured report.

    Writes to .memory/sessions/_week-YYYY-WNN.md.
    Optionally runs suggest-facts on the synthesis to extract high-confidence facts.

    Args:
        draft_file:    Override for .atlas/weekly-draft.md (used in tests).
        output_dir:    Override for .memory/sessions/ output dir (used in tests).
        suggest_facts: Whether to run fact extraction on the synthesis.
        _call_llm_fn:  Inject a mock LLM call (used in tests).

    Returns a status dict.
    """
    draft_path = draft_file or _DRAFT_FILE
    out_dir = output_dir or _SESSIONS_DIR
    llm = _call_llm_fn or _call_llm
    autonomous = _is_autonomous()
    week = _week_str()

    if not draft_path.exists():
        return {"status": "no_draft", "week": week, "written": False}

    draft_text = draft_path.read_text(encoding="utf-8", errors="replace").strip()
    body_lines = [ln for ln in draft_text.splitlines() if ln.strip() and not ln.startswith("# Weekly draft")]
    if not draft_text or not body_lines:
        # Only the reset header: nothing happened this week (a same-week rerun used to
        # synthesize a junk -v2 from the header alone; round-B re-audit 2026-11-09, row 8).
        return {"status": "empty_draft", "week": week, "written": False}

    prompt = _WEEKLY_SYNTHESIS_PROMPT.format(week=week, draft_text=draft_text[:6000])
    try:
        synthesis = llm(prompt)
    except RuntimeError as e:
        return {"status": "llm_error", "error": str(e), "written": False}

    # Write to .memory/sessions/_week-YYYY-WNN.md
    now = _now_iso()
    header = (
        f"---\nid: week-{week}\ntitle: Weekly Synthesis {week}\n"
        f"type: session\nstatus: active\npriority: normal\nupdated: {_date_str()}\n---\n\n"
        f"# Week {week} -- Atlas Synthesis\n\n"
        f"*Generated: {_date_str()}*\n\n"
    )
    content = header + synthesis + "\n"

    out_path = out_dir / f"_week-{week}.md"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        # Never overwrite an existing synthesis (a same-week rerun used to
        # replace it with a synthesis of the already-reset draft): version it
        # and land a -v2, -v3 ... sibling instead.
        if out_path.exists():
            _version_copy(out_path, out_dir)
            n = 2
            while True:
                candidate = out_dir / f"_week-{week}-v{n}.md"
                if not candidate.exists():
                    out_path = candidate
                    break
                n += 1
        out_path.write_text(content, encoding="utf-8")
    except OSError as e:
        return {"status": "write_error", "error": str(e), "written": False}

    # Archive the draft (never truncate it away), then reset for the new week
    try:
        archive = draft_path.parent / f"weekly-draft-{week}.md"
        n = 2
        while archive.exists():
            archive = draft_path.parent / f"weekly-draft-{week}-{n}.md"
            n += 1
        archive.write_text(draft_path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        draft_path.write_text(f"# Weekly draft -- week of {_date_str()}\n", encoding="utf-8")
    except OSError:
        pass

    result: dict = {
        "status": "ok",
        "week": week,
        "written": True,
        "output_path": str(out_path),
        "synthesis_chars": len(synthesis),
        "facts_extracted": 0,
    }

    # Run suggest-facts on the synthesis (autonomous only)
    if suggest_facts and autonomous:
        try:
            from atlas_plus.curation.suggest_facts import _extract_candidates, _write_fact
            candidates = _extract_candidates(synthesis)
            written = 0
            for cand in candidates:
                if cand.get("confidence", 0) >= 0.85:
                    try:
                        _write_fact(cand["scope"], cand["fact"])
                        written += 1
                    except Exception:
                        pass
            result["facts_extracted"] = written
        except Exception as e:
            result["facts_extract_error"] = str(e)

    # Write report
    report_path = STATE_DIR / f"weekly-synthesis-report-{_date_str()}.json"
    try:
        report_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except OSError:
        pass

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point: run the weekly memory synthesis report and optionally push it to Telegram."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Weekly dream report: rolling daily summaries + Sunday synthesis.",
    )
    parser.add_argument(
        "--mode", choices=["daily", "weekly", "auto"],
        default="auto",
        help="daily=append session summary; weekly=synthesize draft; auto=daily Mon-Sat, weekly Sun",
    )
    parser.add_argument(
        "--no-facts", action="store_true",
        help="Skip suggest-facts extraction during weekly synthesis.",
    )
    args = parser.parse_args()

    mode = args.mode
    if mode == "auto":
        mode = "weekly" if _is_sunday() else "daily"

    if mode == "daily":
        print(f"[weekly-synthesis] Appending daily summary...")
        result = run_daily_append()
        if result.get("appended"):
            print(f"[weekly-synthesis] Appended {result['summary_chars']} chars for {result['date']}.")
        else:
            print(f"[weekly-synthesis] {result.get('status', 'skipped')}: {result.get('error', '')}")

    elif mode == "weekly":
        print(f"[weekly-synthesis] Synthesizing week {_week_str()}...")
        result = run_weekly_synthesis(suggest_facts=not args.no_facts)
        if result.get("written"):
            print(f"[weekly-synthesis] Report written: {result['output_path']}")
            if result.get("facts_extracted"):
                print(f"[weekly-synthesis] Facts extracted: {result['facts_extracted']}")
        else:
            print(f"[weekly-synthesis] {result.get('status')}: {result.get('error', '')}")


if __name__ == "__main__":
    main()
