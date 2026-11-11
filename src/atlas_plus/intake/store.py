"""Intake state: the ledger (authority), run reports, and the rendered manifest.

Everything lives under ``<state>/intake/``. The ledger is append-only
JSONL, one line per file outcome; it is what makes intake idempotent
(same sha256 never imported twice) and what the manifest is rendered
from. Unlike the sync audit log it is NOT trimmed: it is the provenance
record for every file that ever came through the door.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from atlas_plus import paths as atlas_paths


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def intake_dir() -> Path:
    return atlas_paths.state_dir() / "intake"


def ledger_path(root: Path | None = None) -> Path:
    return (root or intake_dir()) / "ledger.jsonl"


def runs_dir(root: Path | None = None) -> Path:
    return (root or intake_dir()) / "runs"


def atomic_write_bytes(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".intake-tmp")
    tmp.write_bytes(content)
    os.replace(tmp, target)


def atomic_write_text(target: Path, text: str) -> None:
    atomic_write_bytes(target, text.encode("utf-8"))


# -- Records -------------------------------------------------------------------

STATUSES = (
    "imported",            # copied, verified, ledgered
    "duplicate",           # same sha256 already in the ledger as imported
    "already-at-dest",     # destination holds identical bytes; nothing written
    "quarantined",         # unsafe destination; parked under landing/_quarantine
    "unstable",            # still syncing / unreadable; retry next run
    "skipped",             # ignored by name/suffix (native gdoc, temp, empty)
    "error",               # exception during copy/verify; original untouched
    "dry-run",             # what WOULD happen; nothing written
)


@dataclass
class Record:
    ts: str
    status: str
    source_root: str
    source_rel: str
    size: int = 0
    sha256: str = ""
    rule: str = ""
    dest: str = ""
    reason: str = ""
    frontmatter: dict = field(default_factory=dict)
    versioned: str = ""        # path of the version copy made before overwrite
    archived_to: str = ""      # where the original was parked
    actions: dict = field(default_factory=dict)  # post-import action outcomes
    run_id: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)


def ledger_append(record: Record, root: Path | None = None) -> None:
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(record.to_json() + "\n")


def ledger_load(root: Path | None = None) -> list[dict]:
    path = ledger_path(root)
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def imported_hashes(root: Path | None = None) -> dict[str, dict]:
    """sha256 -> the ledger record that imported those bytes."""
    seen: dict[str, dict] = {}
    for d in ledger_load(root):
        if d.get("status") in ("imported", "already-at-dest") and d.get("sha256"):
            seen.setdefault(d["sha256"], d)
    return seen


# -- Run reports ---------------------------------------------------------------

@dataclass
class RunReport:
    run_id: str
    started_utc: str
    source: str
    dry_run: bool
    finished_utc: str = ""
    records: list[Record] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c = Counter(r.status for r in self.records)
        if self.skipped:
            c["skipped"] += len(self.skipped)
        return dict(sorted(c.items()))

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "source": self.source,
            "dry_run": self.dry_run,
            "counts": self.counts(),
            "records": [asdict(r) for r in self.records],
            "skipped": [list(s) for s in self.skipped],
            "notes": list(self.notes),
        }

    def summary_line(self) -> str:
        parts = [f"{k}={v}" for k, v in self.counts().items()]
        tag = " (dry-run)" if self.dry_run else ""
        return f"intake {self.run_id}{tag}: " + (", ".join(parts) if parts else "nothing to do")


def save_run(report: RunReport, root: Path | None = None) -> Path:
    path = runs_dir(root) / f"{report.run_id}.json"
    atomic_write_text(path, json.dumps(report.to_dict(), indent=2, sort_keys=True, ensure_ascii=False))
    return path


def last_run(root: Path | None = None) -> dict | None:
    d = runs_dir(root)
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# -- Manifest ------------------------------------------------------------------

def _cell(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def render_manifest(records: list[dict], *, source: str = "", title: str = "Intake manifest") -> str:
    """Markdown view of the ledger, newest first, grouped by UTC day.

    Written next to the archived originals so it can be read from the same
    place the files were dropped. The ledger stays the authority.
    """
    lines = [f"# {title}", ""]
    if source:
        lines += [f"Source folder: `{source}`", ""]
    lines += [
        f"Regenerated {now_utc()} from the intake ledger. Newest first. Paths are",
        "workspace-local; the original of every imported file is under `_imported/<date>/`.",
        "",
    ]
    if not records:
        lines += ["Nothing imported yet.", ""]
        return "\n".join(lines)
    by_day: dict[str, list[dict]] = {}
    for r in records:
        by_day.setdefault(str(r.get("ts", ""))[:10] or "unknown", []).append(r)
    for day in sorted(by_day, reverse=True):
        lines += [f"## {day}", "", "| Time (UTC) | File | Status | Rule | Destination | Note |", "| --- | --- | --- | --- | --- | --- |"]
        for r in sorted(by_day[day], key=lambda d: d.get("ts", ""), reverse=True):
            note = r.get("reason", "")
            if not note and r.get("frontmatter"):
                fm = r["frontmatter"]
                bits = [f"{k}={fm[k]}" for k in ("project", "artifact_type", "intended_consumer") if k in fm]
                note = "; ".join(bits)
            acts = r.get("actions") or {}
            if acts:
                note = (note + "; " if note else "") + ", ".join(f"{k}:{v}" for k, v in acts.items())
            lines.append(
                "| " + " | ".join([
                    _cell(str(r.get("ts", ""))[11:19]),
                    _cell(r.get("source_rel", "")),
                    _cell(r.get("status", "")),
                    _cell(r.get("rule", "")),
                    _cell(r.get("dest", ""), 110),
                    _cell(note, 140),
                ]) + " |"
            )
        lines.append("")
    return "\n".join(lines)
