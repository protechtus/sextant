"""P7-T5: Weekly cleanup report — surfaces curation findings without auto-editing."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

from atlas_plus.mcp.memory_server.config import MEMORY_ROOT
from atlas_plus.curation.scan import is_excluded_mirror
from atlas_plus import paths as atlas_paths

_CONVERGENCE_EXCLUDED = {"archive", "_sync", "versions", "_atlas"}
_DECISIONS_STALE_DAYS = 90
_REDERIVE_THRESHOLD = 0.40  # conservative threshold for the weekly report


from atlas_plus._yaml import fast_safe_load as _fast_yaml_load


def _scan_convergence(root: Path) -> list[dict]:
    """Return files with contradictions: frontmatter from the given memory root."""
    import yaml as _yaml
    flagged = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root)
        parts = rel.parts
        if any(p in _CONVERGENCE_EXCLUDED for p in parts):
            continue
        if is_excluded_mirror(parts):
            continue
        if rel.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "contradictions:" not in text:
            continue
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end == -1:
            continue
        try:
            fm = _fast_yaml_load(text[3:end]) or {}
        except Exception:
            continue
        contras = fm.get("contradictions", [])
        if contras and isinstance(contras, list) and len(contras) > 0:
            flagged.append({
                "path": str(rel).replace("\\", "/"),
                "contradictions": [str(c) for c in contras[:3]],
            })
    return flagged


def _scan_decisions(root: Path, stale_days: int = _DECISIONS_STALE_DAYS) -> list[dict]:
    """Return stale decision files from <root>/decisions/.

    A decision is stale when it has no last_reviewed field AND was made more
    than stale_days ago, OR when last_reviewed is older than stale_days.
    """
    import yaml as _yaml

    decisions_dir = root / "decisions"
    if not decisions_dir.exists():
        return []

    today = date.today()
    stale = []
    for path in sorted(decisions_dir.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.startswith("---"):
            continue
        end = text.find("\n---", 3)
        if end == -1:
            continue
        try:
            fm = _fast_yaml_load(text[3:end]) or {}
        except Exception:
            continue

        made_str = str(fm.get("made", ""))[:10]
        lr_str = str(fm.get("last_reviewed", ""))[:10]
        age_days = None
        lr_days = None
        if made_str:
            try:
                age_days = (today - date.fromisoformat(made_str)).days
            except ValueError:
                pass
        if lr_str:
            try:
                lr_days = (today - date.fromisoformat(lr_str)).days
            except ValueError:
                pass

        is_stale = (lr_days is None and age_days is not None and age_days > stale_days) or \
                   (lr_days is not None and lr_days > stale_days)
        if is_stale:
            stale.append({
                "slug": path.stem,
                "title": str(fm.get("title", path.stem))[:60],
                "project": str(fm.get("project", ""))[:30],
                "made": made_str,
                "age_days": age_days,
            })
    return stale


def _scan_rederivable(root: Path, threshold: float = _REDERIVE_THRESHOLD) -> list[dict]:
    """Return memory files that heavily overlap with project doc files.

    Delegates to `find_rederivable()` with a workspace_root derived from the
    parent of the memory root (e.g. <workspace> from <workspace>/.memory).
    Falls back to the default workspace root if derivation fails.
    """
    try:
        from atlas_plus.curation.rederive import find_rederivable
        ws_root = root.parent
        candidates = find_rederivable(threshold=threshold, workspace_root=ws_root)
        return [c.as_dict() for c in candidates[:15]]
    except Exception:
        return []


_COUNT_RULES = (
    ("Repeated facts (", "duplicates", True),
    ("Potential Duplicates (", "duplicates", True),
    ("Stale Files (", "stale", False),
    ("Convergence Flags (", "convergence", False),
    ("Stale Decisions (", "decisions", False),
    ("Re-derivable Facts (", "rederivable", False),
)


def _diverse(fact_dups: list, limit: int, per_pair: int = 3) -> list:
    """At most ``per_pair`` lines per note pair: one pair of near-copies filled the whole section
    (AG42: lines 7 to 110 were n0035 against n0633)."""
    seen: dict[tuple, int] = {}
    out = []
    for d in fact_dups:
        key = (d.get("file1"), d.get("file2"))
        if seen.get(key, 0) >= per_pair:
            continue
        seen[key] = seen.get(key, 0) + 1
        out.append(d)
        if len(out) >= limit:
            break
    return out


def curation_counts(report_text: str) -> dict[str, int]:
    """The headline numbers of a report, parsed once for the dashboard, brain chat and Telegram
    (MN25: three hand-copied parsers, two of which crashed on every run). Repeated facts and
    near-duplicate notes add up under `duplicates`."""
    import re as _re
    counts = {"duplicates": 0, "stale": 0, "convergence": 0, "decisions": 0, "rederivable": 0}
    for line in report_text.splitlines():
        for needle, key, add in _COUNT_RULES:
            if needle in line:
                m = _re.search(r"\((\d+)", line)
                if m:
                    counts[key] = (counts[key] + int(m.group(1))) if add else int(m.group(1))
                break
    return counts


def generate_report(
    memory_root: Optional[Path] = None,
    stale_months: int = 6,
    dup_threshold: float = 0.40,
    scope: str = "",
    dry_run: bool = False,
) -> tuple[str, Path]:
    """Generate a weekly curation report and write it to .memory/_atlas/.

    Returns (report_markdown, output_path).
    Passing dry_run=True returns the report without writing.
    """
    from atlas_plus.curation.duplicates import find_duplicates
    from atlas_plus.curation.stale import find_stale

    root = memory_root or MEMORY_ROOT
    today = date.today().isoformat()

    from atlas_plus.curation import duplicates as _dupmod
    dups = find_duplicates(memory_root=root, threshold=dup_threshold, scope=scope)
    stale = find_stale(memory_root=root, months=stale_months, scope=scope)
    from atlas_plus.curation.duplicates import find_fact_duplicates
    fact_dups = find_fact_duplicates(memory_root=root, scope=scope, limit=501)
    _capped = len(fact_dups) > 500  # the count read as a total; it was the cap (MN30)
    fact_dups = fact_dups[:500]
    from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
    never = [c for c in downgrade_stale_confirmations(memory_root=root, dry_run=True) if c.get("last_confirmed") == "never"]

    lines = [
        f"# Atlas Memory Curation Report -- {today}",
        "",
        "Generated by `atlas-review-memory report`. No automatic edits.",
        "",
        "---",
        "",
        f"## Repeated facts ({len(fact_dups)}{'+' if _capped else ''} found{' (capped at 500)' if _capped else ''}: "
        f"{sum(1 for d in fact_dups if d['kind'] == 'exact')} exact, "
        f"{sum(1 for d in fact_dups if d['kind'] != 'exact')} near)",
        "",
        *[f"- {d['kind']}: `{d['file1']}` and `{d['file2']}`: {d['fact'][:120]}" for d in _diverse(fact_dups, 30)],
        *([f"- ... and {len(fact_dups) - 30} more (`atlas-review-memory duplicates --facts --json`)"]
          if len(fact_dups) > 30 else []),
        "",
        f"## Never confirmed and older than 90 days ({len(never)})",
        "",
        *[f"- `{c['path']}` ({c['age_days']} days; `memory_confirm_tool` when still true, or archive)" for c in never[:30]],
        *([f"- ... and {len(never) - 30} more (`atlas-review-memory unconfirmed-decay --json`)"] if len(never) > 30 else []),
        "",
        f"## Potential Duplicates ({len(dups)}{'+' if _dupmod._LAST_CAPPED else ''} found{' (capped at ' + str(_dupmod._MAX_FILE_PAIRS) + ')' if _dupmod._LAST_CAPPED else ''}, threshold={dup_threshold})",
        "",
    ]

    if dups:
        lines.append("| File 1 | File 2 | Similarity |")
        lines.append("|--------|--------|------------|")
        for d in dups[:20]:
            lines.append(f"| `{d.file1}` | `{d.file2}` | {d.similarity:.2f} |")
        if len(dups) > 20:
            lines.append(f"| *(+{len(dups) - 20} more)* | | |")
    else:
        lines.append("*No potential duplicates found.*")

    lines += [
        "",
        "---",
        "",
        f"## Stale Files ({len(stale)} found, older than {stale_months} months)",
        "",
    ]

    if stale:
        lines.append("| File | Last Updated | Age (days) |")
        lines.append("|------|-------------|------------|")
        for s in stale[:20]:
            lines.append(f"| `{s.path}` | {s.updated} | {s.age_days} |")
        if len(stale) > 20:
            lines.append(f"| *(+{len(stale) - 20} more)* | | |")
    else:
        lines.append("*No stale files found.*")

    # Convergence flags section
    convergence_flagged = _scan_convergence(root)
    lines += [
        "",
        "---",
        "",
        f"## Convergence Flags ({len(convergence_flagged)} file(s) with unresolved contradiction flags)",
        "",
    ]
    if convergence_flagged:
        lines.append("| File | Contradicts |")
        lines.append("|------|------------|")
        for c in convergence_flagged[:20]:
            contra_str = ", ".join(f"`{p}`" for p in c["contradictions"])
            lines.append(f"| `{c['path']}` | {contra_str} |")
        if len(convergence_flagged) > 20:
            lines.append(f"| *(+{len(convergence_flagged) - 20} more)* | |")
        lines += [
            "",
            "Run `atlas-review-memory convergence` for the full report.",
        ]
    else:
        lines.append("*No unresolved contradiction flags found.*")

    # Stale decisions section
    stale_decisions = _scan_decisions(root)
    lines += [
        "",
        "---",
        "",
        f"## Stale Decisions ({len(stale_decisions)} decision(s) not reviewed in {_DECISIONS_STALE_DAYS}+ days)",
        "",
    ]
    if stale_decisions:
        lines.append("| Decision | Project | Made | Age |")
        lines.append("|----------|---------|------|-----|")
        for d in stale_decisions[:20]:
            project = d["project"] or "workspace"
            lines.append(f"| `{d['slug']}` {d['title']} | {project} | {d['made']} | {d['age_days']}d |")
        if len(stale_decisions) > 20:
            lines.append(f"| *(+{len(stale_decisions) - 20} more)* | | | |")
        lines += [
            "",
            "Run `atlas-review-memory decisions` for the full report.",
        ]
    else:
        lines.append("*No stale decisions found.*")

    # Re-derivable facts section
    rederivable = _scan_rederivable(root)
    lines += [
        "",
        "---",
        "",
        f"## Re-derivable Facts ({len(rederivable)} file(s) overlap heavily with project docs)",
        "",
    ]
    if rederivable:
        lines.append("| Memory file | Type | Matched doc | Similarity |")
        lines.append("|-------------|------|-------------|------------|")
        for r in rederivable[:20]:
            lines.append(f"| `{r['path']}` | {r['type']} | `{r['matched_doc']}` | {r['similarity']:.2f} |")
        if len(rederivable) > 20:
            lines.append(f"| *(+{len(rederivable) - 20} more)* | | | |")
        lines += [
            "",
            "Run `atlas-review-memory rederive` for the full report.",
            "To archive a re-derivable file: `atlas-review-memory archive <path>`",
        ]
    else:
        lines.append("*No re-derivable facts found above threshold.*")

    # Unconfirmed decay section (new: stale last_confirmed facts)
    try:
        from atlas_plus.mcp.memory_server.routing import downgrade_stale_confirmations
        stale_confirmed = downgrade_stale_confirmations(
            threshold_days=90, memory_root=root, dry_run=True
        )
    except Exception:
        stale_confirmed = []
    lines += [
        "",
        "---",
        "",
        f"## Unconfirmed Stale Facts ({len(stale_confirmed)} file(s) not confirmed in 90+ days)",
        "",
    ]
    if stale_confirmed:
        lines.append("| File | Old Confidence | Last Confirmed | Age |")
        lines.append("|------|---------------|---------------|-----|")
        for s in stale_confirmed[:20]:
            lines.append(
                f"| `{s['path']}` | {s['old_confidence']} "
                f"| {s['last_confirmed']} | {s['age_days']}d |"
            )
        if len(stale_confirmed) > 20:
            lines.append(f"| *(+{len(stale_confirmed) - 20} more)* | | | |")
        lines += [
            "",
            "Run `atlas-review-memory unconfirmed-decay --write` to downgrade confidence.",
        ]
    else:
        lines.append("*All confirmed facts are up to date.*")

    # Memory topic summary (new: top tags from frontmatter)
    try:
        from atlas_plus.web.app import _build_topic_index
        topic_index = _build_topic_index(root)
        top_tags = list(topic_index.items())[:15]
    except Exception:
        top_tags = []
    lines += [
        "",
        "---",
        "",
        f"## Memory Topic Coverage ({len(topic_index) if top_tags else 0} unique tags)",
        "",
    ]
    if top_tags:
        lines.append("| Tag | File Count |")
        lines.append("|-----|-----------|")
        for tag, paths in top_tags:
            lines.append(f"| `{tag}` | {len(paths)} |")
        if len(topic_index) > 15:
            lines.append(f"| *(+{len(topic_index) - 15} more tags)* | |")
    else:
        lines.append("*No tags found in frontmatter. Add `tags:` fields to enable topic coverage.*")

    # Research quality summary section (deterministic -- reads quality_score: frontmatter)
    import os as _os
    state_dir = atlas_paths.state_dir()
    reports_dir = state_dir / "reports"
    rq_total = rq_scored = rq_low = 0
    rq_low_files: list[str] = []
    if reports_dir.exists():
        import yaml as _yaml2
        for md_file in sorted(reports_dir.rglob("*.md")):
            if md_file.name.startswith("_"):
                continue
            rq_total += 1
            try:
                raw2 = md_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not raw2.startswith("---"):
                continue
            end2 = raw2.find("\n---", 3)
            if end2 == -1:
                continue
            try:
                fm2 = _yaml2.safe_load(raw2[3:end2]) or {}
            except Exception:
                continue
            qs = fm2.get("quality_score")
            if qs is None:
                continue
            rq_scored += 1
            try:
                if float(qs) < 0.5:
                    rq_low += 1
                    rq_low_files.append(md_file.name)
            except (TypeError, ValueError):
                pass

    lines += [
        "",
        "---",
        "",
        f"## Research Report Quality ({rq_scored}/{rq_total} scored, {rq_low} low-quality)",
        "",
    ]
    if rq_scored == 0:
        lines.append(
            "*No quality scores found. Run `atlas-review-memory quality --write` to score reports.*"
        )
    else:
        if rq_low > 0:
            lines.append("Reports with quality_score < 0.5 (need improvement):")
            lines.append("")
            for f in rq_low_files[:10]:
                lines.append(f"- `{f}`")
            if rq_low > 10:
                lines.append(f"- *(+{rq_low - 10} more)*")
            lines += [
                "",
                "Run `atlas-review-memory citations --write` to add citation frontmatter.",
                "Run `atlas-review-memory quality --write` to refresh scores.",
            ]
        else:
            lines.append("*All scored research reports meet the quality threshold (>= 0.5).*")
        if rq_scored < rq_total:
            lines.append(
                f"\n{rq_total - rq_scored} report(s) not yet scored. "
                "Run `atlas-review-memory quality --write` to score them."
            )

    lines += [
        "",
        "---",
        "",
        "## Actions",
        "",
        "To archive a stale file:",
        "```powershell",
        "atlas-review-memory archive <path> --yes",
        "```",
        "",
        "To list repeated facts with full paths:",
        "```powershell",
        "atlas-review-memory duplicates --facts --json",
        "```",
    ]

    report = "\n".join(lines)

    # The report is state, not memory: it used to land inside the corpus without a word (AG20).
    from atlas_plus import paths as _paths
    if memory_root is None or Path(memory_root).resolve() == Path(MEMORY_ROOT).resolve():
        atlas_dir = _paths.state_dir() / "reports"
    else:
        atlas_dir = Path(memory_root).parent / ".atlas" / "reports"   # a corpus given explicitly keeps its reports beside it
    atlas_dir.mkdir(parents=True, exist_ok=True)
    out_path = atlas_dir / f"curation-{today}.md"

    if not dry_run:
        out_path.write_text(report, encoding="utf-8")

    return report, out_path
