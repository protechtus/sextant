"""Brain learning loop (ATL-073): session digests, proposals, the session-close note.

The loop, as redesigned on 2026-11-20 (docs/BRAIN-LEARNING-LOOP-REVIEW-2026-11-20.md):

* Brain WATCHES AND MEASURES. Every night the ``session_digest`` lane writes a deterministic
  digest of the previous local day: session-log entries, updates entries, session-close notes,
  commits per repository, Brain jobs by kind and outcome, scheduler heartbeats, queue counts.
  No LLM. ``.atlas/brain/digests/<date>.json`` and ``<date>.md``.
* Claude SYNTHESIZES WEEKLY. The Monday supervisor (executors._claude_supervisor) reads the last
  seven digests, the lessons ledger and the proposals already made, and ends its report with a
  fenced JSON block of proposals (lesson rows, tickets, facts).
* Every session READS the result: the SessionStart ``## Brain`` section shows the newest digest,
  the pending proposals and the supervisor's recommendations.
* Writes go through the PROPOSALS DESK: ``brain_proposals`` rows the operator approves, edits or
  rejects in Helm (Memory tab). Approval is the only path from a proposal to the ledger, the
  ticket store or the memory corpus. ``ATLAS_AUTONOMOUS`` stays off.

Also here: ``add_session_note`` (the session-close skill drops a one-paragraph note the next
digest folds in, so what Claude sessions did reaches Brain without an HTTP dependency).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from atlas_plus import paths as atlas_paths
from atlas_plus._fsutil import make_private_dir

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

PROPOSAL_KINDS = ("lesson", "ticket", "fact")
PROPOSAL_STATUSES = ("pending", "approved", "rejected")
_ENTRY_RE = re.compile(r"^## (\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}))?\s+-{1,2}\s+(.+?)\s*$", re.MULTILINE)
_SUMMARY_CHARS = 400
_MAX_ENTRIES_PER_SOURCE = 40
_MAX_REPOS = 25
_MAX_COMMITS_PER_REPO = 15
_GIT_TIMEOUT = 8
_DIGEST_BLOCK_CHARS = 6000


def digests_dir(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or atlas_paths.state_dir()) / "brain" / "digests"


def notes_path(state_dir: Optional[Path] = None) -> Path:
    return (state_dir or atlas_paths.state_dir()) / "brain" / "session-notes.jsonl"


def _db_path(db_path: Optional[Path] = None) -> Path:
    return db_path or (atlas_paths.state_dir() / "state.sqlite")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _local_yesterday() -> _date:
    return (datetime.now().astimezone() - timedelta(days=1)).date()


# ---------------------------------------------------------------------------
# Session-close notes (P6): the skill drops a note, the digest folds it in
# ---------------------------------------------------------------------------

def add_session_note(title: str, text: str, *, project: Optional[str] = None,
                     state_dir: Optional[Path] = None) -> Path:
    """Append one session-close note (JSON line). Works with Brain down: it is a file."""
    path = notes_path(state_dir)
    make_private_dir(path.parent)
    row = {
        "ts": _now_iso(),
        "date": datetime.now().astimezone().date().isoformat(),
        "title": (title or "").strip()[:200],
        "text": (text or "").strip()[:2000],
        "project": (project or "").strip()[:80] or None,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _notes_for(day: str, state_dir: Optional[Path]) -> list[dict]:
    path = notes_path(state_dir)
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("date") == day:
            out.append({"title": row.get("title", ""), "text": row.get("text", ""), "project": row.get("project")})
    return out[-_MAX_ENTRIES_PER_SOURCE:]


# ---------------------------------------------------------------------------
# Digest inputs
# ---------------------------------------------------------------------------

def parse_day_entries(text: str, day: str) -> list[dict]:
    """``## YYYY-MM-DD[ HH:MM] -- title`` entries for one day, each with its first paragraph.

    Accepts the modern heading (time, two dashes) and the legacy one (one dash, no time): the
    same two shapes session_patterns learned to read on 2026-11-20.
    """
    matches = list(_ENTRY_RE.finditer(text))
    out: list[dict] = []
    for i, m in enumerate(matches):
        if m.group(1) != day:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        para = ""
        for chunk in re.split(r"\n\s*\n", body):
            chunk = " ".join(ln.strip() for ln in chunk.splitlines() if ln.strip())
            if chunk:
                para = chunk
                break
        out.append({"time": m.group(2) or "", "title": m.group(3).strip()[:200], "summary": para[:_SUMMARY_CHARS]})
    return out[:_MAX_ENTRIES_PER_SOURCE]


def _read(path: Path, limit: int = 2_000_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _git_repos(workspace_root: Path, memory_root: Path) -> list[Path]:
    roots: list[Path] = []
    for cand in (workspace_root, memory_root):
        if (cand / ".git").exists():
            roots.append(cand)
    projects = workspace_root / "projects"
    if projects.is_dir():
        for p in sorted(projects.iterdir()):
            if (p / ".git").exists():
                roots.append(p)
    return roots[:_MAX_REPOS]


def _git_commits(day: str, repos: list[Path]) -> dict[str, list[str]]:
    """Commit subjects per repository for the local day. Bounded, timeout, no stdin (LES-002)."""
    start = f"{day}T00:00:00"
    until = (datetime.fromisoformat(day) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    out: dict[str, list[str]] = {}
    for repo in repos:
        try:
            cp = subprocess.run(
                ["git", "-C", str(repo), "log", f"--since={start}", f"--until={until}",
                 "--format=%h %s", "--no-merges", f"-n{_MAX_COMMITS_PER_REPO}"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=_GIT_TIMEOUT, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if cp.returncode != 0:
            continue
        lines = [ln.strip()[:160] for ln in cp.stdout.splitlines() if ln.strip()]
        if lines:
            out[repo.name] = lines
    return out


def _brain_ledger(day: str, db_path: Path) -> dict:
    """Brain jobs finished on the day (by kind and outcome, minutes spent) and scheduler heartbeats."""
    result: dict = {"jobs": {}, "failed": [], "scheduler": {}, "pending_facts": 0, "pending_proposals": 0}
    if not db_path.exists():
        return result
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return result
    try:
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "select kind, status, count(*) as n, "
                "sum((julianday(finished_at) - julianday(started_at)) * 1440.0) as minutes "
                "from brain_tasks where substr(finished_at, 1, 10) = ? group by kind, status", (day,)).fetchall()
            for r in rows:
                slot = result["jobs"].setdefault(r["kind"] or "agent", {"done": 0, "failed": 0, "blocked": 0, "minutes": 0.0})
                slot[r["status"] if r["status"] in ("done", "failed", "blocked") else "done"] += int(r["n"])
                slot["minutes"] = round(float(slot["minutes"]) + float(r["minutes"] or 0.0), 1)
            rows = con.execute(
                "select task_id, kind, status, substr(coalesce(error, result, ''), 1, 200) as why "
                "from brain_tasks where substr(finished_at, 1, 10) = ? and status in ('failed', 'blocked') "
                "order by finished_at desc limit 12", (day,)).fetchall()
            result["failed"] = [dict(r) for r in rows]
        except sqlite3.Error:
            pass
        try:
            rows = con.execute(
                "select job_id, outcome, run_count from scheduler_runs where substr(last_run_at, 1, 10) = ?", (day,)).fetchall()
            result["scheduler"] = {r["job_id"]: r["outcome"] for r in rows}
        except sqlite3.Error:
            pass
        for key, sql in (("pending_facts", "select count(*) from pending_facts where status='pending'"),
                         ("pending_proposals", "select count(*) from brain_proposals where status='pending'")):
            try:
                result[key] = int(con.execute(sql).fetchone()[0])
            except sqlite3.Error:
                pass
    finally:
        con.close()
    return result


def tickets_snapshot(memory_root: Optional[Path] = None, *, stale_days: int = 7) -> dict:
    """Open tickets right now: counts by assignee, agent tickets untouched for ``stale_days``, done but not
    archived. Read-only; an unreadable store yields zeros. 2026-11-21: the digest counted everything but the
    tickets nobody was closing."""
    out: dict = {"open": 0, "by_assignee": {}, "stale": [], "done_unarchived": 0, "stale_days": stale_days}
    try:
        from atlas_plus.tickets.store import list_tickets
        root = (memory_root or atlas_paths.memory_root()) / "tickets"
        if not root.exists():
            return out
        rows = list_tickets(tickets_root=root)
    except Exception:  # noqa: BLE001 - the digest never fails on the ticket store
        return out
    today = datetime.now().date()
    for t in rows:
        status = str(getattr(t, "status", ""))
        if status == "done":
            out["done_unarchived"] += 1
            continue
        if status not in ("todo", "in-progress", "in_progress", "blocked"):
            continue
        out["open"] += 1
        who = str(getattr(t, "assignee", "") or "unassigned")
        out["by_assignee"][who] = out["by_assignee"].get(who, 0) + 1
        try:
            age = (today - datetime.fromisoformat(str(getattr(t, "updated", ""))[:10]).date()).days
        except ValueError:
            age = -1
        if who != "human" and age > stale_days:
            out["stale"].append({"id": t.id, "title": str(getattr(t, "title", ""))[:90], "assignee": who,
                                 "status": status, "days": age})
    out["stale"].sort(key=lambda r: -r["days"])
    out["stale"] = out["stale"][:15]
    return out


def tickets_block(snap: dict) -> str:
    if not snap or (not snap.get("open") and not snap.get("done_unarchived")):
        return "  (no open tickets)"
    who = ", ".join(f"{k} {v}" for k, v in sorted(snap.get("by_assignee", {}).items()))
    lines = [f"  open {snap.get('open', 0)} ({who}); done but not archived {snap.get('done_unarchived', 0)}"]
    for r in snap.get("stale", []):
        lines.append(f"  - {r['id']} [{r['assignee']}, {r['status']}, {r['days']}d]: {r['title']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Digest build / write / read
# ---------------------------------------------------------------------------

def build_session_digest(day: Optional[str] = None, *, memory_root: Optional[Path] = None,
                         state_dir: Optional[Path] = None, workspace_root: Optional[Path] = None,
                         db_path: Optional[Path] = None, updates_file: Optional[Path] = None) -> dict:
    """The deterministic digest of one local day. Every input is optional and bounded."""
    day = day or _local_yesterday().isoformat()
    memory_root = memory_root or atlas_paths.memory_root()
    state_dir = state_dir or atlas_paths.state_dir()
    workspace_root = workspace_root or atlas_paths.workspace_root()
    updates_file = updates_file or atlas_paths.updates_file()
    db = db_path or (state_dir / "state.sqlite")

    sessions = parse_day_entries(_read(memory_root / "sessions" / f"{day}.md"), day)
    history = parse_day_entries(_read(memory_root / "sessions" / "_history.md"), day)
    # paths.updates_file() is the state-dir copy; the house-rule log is <workspace>/updates.txt. Read both.
    updates_candidates = [updates_file, workspace_root / "updates.txt"]
    updates: list[dict] = []
    seen_titles: set[str] = set()
    for cand in updates_candidates:
        for e in parse_day_entries(_read(cand), day):
            if e["title"] not in seen_titles:
                seen_titles.add(e["title"])
                updates.append(e)
    notes = _notes_for(day, state_dir)
    commits = _git_commits(day, _git_repos(workspace_root, memory_root))
    ledger = _brain_ledger(day, db)

    titles = {e["title"] for e in sessions} | {e["title"] for e in history}
    projects = sorted({c for c in commits})
    digest = {
        "date": day,
        "generated_at": _now_iso(),
        "sessions": sessions,
        "history": [e for e in history if e["title"] not in {s["title"] for s in sessions}],
        "updates": [e for e in updates if e["title"] not in titles],
        "notes": notes,
        "commits": commits,
        "brain": ledger,
        "tickets": tickets_snapshot(memory_root),
        "counts": {
            "session_entries": len(titles | {e["title"] for e in updates}),
            "notes": len(notes),
            "repos_touched": len(projects),
            "commits": sum(len(v) for v in commits.values()),
            "brain_jobs": sum(sum(v[k] for k in ("done", "failed", "blocked")) for v in ledger["jobs"].values()),
            "brain_failed": sum(v["failed"] + v["blocked"] for v in ledger["jobs"].values()),
            "brain_minutes": round(sum(v["minutes"] for v in ledger["jobs"].values()), 1),
        },
    }
    return digest


def render_digest_md(d: dict) -> str:
    c = d["counts"]
    lines = [
        "---",
        f"id: session-digest-{d['date']}",
        f"title: Session digest {d['date']}",
        "type: brain-digest",
        f"updated: {d['date']}",
        "---",
        "",
        f"# Session digest {d['date']}",
        "",
        f"{c['session_entries']} session entries, {c['notes']} close notes, {c['commits']} commits in "
        f"{c['repos_touched']} repositories, {c['brain_jobs']} Brain jobs ({c['brain_failed']} failed or blocked, "
        f"{c['brain_minutes']} worker minutes).",
        "",
        "## Sessions",
        "",
    ]
    entries = d["sessions"] + d["history"] + d["updates"]
    if not entries:
        lines.append("(no session-log entries for this day)")
    for e in entries:
        stamp = f"{e['time']} " if e.get("time") else ""
        lines.append(f"- {stamp}{e['title']}")
        if e.get("summary"):
            lines.append(f"  {e['summary']}")
    if d["notes"]:
        lines += ["", "## Session-close notes", ""]
        for n in d["notes"]:
            proj = f" [{n['project']}]" if n.get("project") else ""
            lines.append(f"- {n['title']}{proj}: {n['text']}")
    lines += ["", "## Commits", ""]
    if not d["commits"]:
        lines.append("(none found)")
    for repo, subjects in d["commits"].items():
        lines.append(f"- {repo}: {len(subjects)}")
        for s in subjects[:6]:
            lines.append(f"  - {s}")
    if d.get("tickets"):
        lines += ["", "## Tickets", "", tickets_block(d["tickets"])]
    lines += ["", "## Brain", ""]
    if not d["brain"]["jobs"]:
        lines.append("(no Brain jobs finished)")
    for kind, v in sorted(d["brain"]["jobs"].items()):
        lines.append(f"- {kind}: done {v['done']}, failed {v['failed']}, blocked {v['blocked']}, {v['minutes']} min")
    for f in d["brain"]["failed"]:
        lines.append(f"  - {f['status']} {f['task_id']} ({f['kind']}): {f['why']}")
    if d["brain"]["scheduler"]:
        errs = [j for j, o in d["brain"]["scheduler"].items() if o != "ok"]
        lines.append(f"- scheduler lanes whose latest heartbeat is this day: {len(d['brain']['scheduler'])}" + (f", errors: {', '.join(errs)}" if errs else ", all ok"))
    lines.append(f"- queues: {d['brain']['pending_facts']} pending facts, {d['brain']['pending_proposals']} pending proposals")
    return "\n".join(lines) + "\n"


def write_session_digest(day: Optional[str] = None, *, state_dir: Optional[Path] = None, **kwargs) -> tuple[Path, Path, dict]:
    """Build and write ``<date>.json`` + ``<date>.md``; a rerun for the same day overwrites (deterministic)."""
    state_dir = state_dir or atlas_paths.state_dir()
    digest = build_session_digest(day, state_dir=state_dir, **kwargs)
    out = digests_dir(state_dir)
    make_private_dir(out)
    json_path = out / f"{digest['date']}.json"
    md_path = out / f"{digest['date']}.md"
    json_path.write_text(json.dumps(digest, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_digest_md(digest), encoding="utf-8")
    return json_path, md_path, digest


def load_recent_digests(days: int = 7, *, state_dir: Optional[Path] = None) -> list[dict]:
    """The newest ``days`` digests, oldest first. Missing or unreadable files are skipped."""
    out: list[dict] = []
    try:
        files = sorted(digests_dir(state_dir).glob("????-??-??.json"))[-days:]
    except OSError:
        return out
    for p in files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("date"):
            out.append(d)
    return out


def digest_block(digests: list[dict], max_chars: int = _DIGEST_BLOCK_CHARS) -> str:
    """A compact prose block of the digests for a prompt (dream, supervisor). Bounded."""
    if not digests:
        return ""
    parts: list[str] = []
    for d in digests:
        c = d.get("counts", {})
        head = (f"{d['date']}: {c.get('session_entries', 0)} session entries, {c.get('commits', 0)} commits in "
                f"{c.get('repos_touched', 0)} repos, {c.get('brain_jobs', 0)} Brain jobs ({c.get('brain_failed', 0)} failed)")
        items = [head]
        for e in (d.get("sessions") or []) + (d.get("history") or []) + (d.get("updates") or []):
            items.append(f"  - {e.get('title', '')}" + (f": {e['summary'][:220]}" if e.get("summary") else ""))
        for n in d.get("notes") or []:
            items.append(f"  - note: {n.get('title', '')}: {n.get('text', '')[:220]}")
        for repo, subjects in (d.get("commits") or {}).items():
            items.append(f"  - {repo}: " + "; ".join(s.split(" ", 1)[-1][:80] for s in subjects[:4]))
        for f in (d.get("brain") or {}).get("failed", []):
            items.append(f"  - Brain {f.get('status')} {f.get('kind')}: {str(f.get('why', ''))[:120]}")
        parts.append("\n".join(items))
    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n  (digest block truncated)"
    return text


# ---------------------------------------------------------------------------
# Proposals: the desk's rows
# ---------------------------------------------------------------------------

_CREATE_PROPOSALS = """
CREATE TABLE IF NOT EXISTS brain_proposals (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    source      TEXT NOT NULL,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',
    reviewed_at TEXT,
    result      TEXT
);
"""
_CREATE_PROPOSALS_INDEX = "CREATE INDEX IF NOT EXISTS idx_brain_proposals_status ON brain_proposals(status, created_at DESC);"


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = _db_path(db_path)
    make_private_dir(path.parent)
    con = sqlite3.connect(str(path), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(_CREATE_PROPOSALS)
    con.execute(_CREATE_PROPOSALS_INDEX)
    con.commit()
    return con


def _row_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["payload"] = json.loads(d.get("payload") or "{}")
    except ValueError:
        d["payload"] = {}
    return d


def add_proposal(source: str, kind: str, title: str, body: str, payload: Optional[dict] = None, *,
                 db_path: Optional[Path] = None, dedupe_days: int = 30) -> dict:
    """File a proposal for the desk. Same kind + title within ``dedupe_days`` returns the existing row."""
    kind = (kind or "").strip().lower()
    if kind not in PROPOSAL_KINDS:
        raise ValueError(f"unknown proposal kind {kind!r}; expected one of {PROPOSAL_KINDS}")
    title = " ".join((title or "").split())[:200]
    body = (body or "").strip()[:4000]
    if not title or not body:
        raise ValueError("a proposal needs a title and a body")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=dedupe_days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    with _connect(db_path) as con:
        row = con.execute(
            "select * from brain_proposals where kind=? and lower(title)=lower(?) and created_at>=? "
            "order by created_at desc limit 1", (kind, title, cutoff)).fetchone()
        if row:
            d = _row_dict(row)
            d["deduplicated"] = True
            return d
        pid = uuid.uuid4().hex
        con.execute(
            "insert into brain_proposals(id, created_at, source, kind, title, body, payload, status) "
            "values (?, ?, ?, ?, ?, ?, ?, 'pending')",
            (pid, _now_iso(), (source or "unknown")[:80], kind, title, body, json.dumps(payload or {}, ensure_ascii=False)))
        row = con.execute("select * from brain_proposals where id=?", (pid,)).fetchone()
    d = _row_dict(row)
    d["deduplicated"] = False
    return d


def list_proposals(status: str = "pending", limit: int = 50, *, db_path: Optional[Path] = None) -> list[dict]:
    with _connect(db_path) as con:
        if status == "all":
            rows = con.execute("select * from brain_proposals order by created_at desc, rowid desc limit ?", (limit,)).fetchall()
        else:
            rows = con.execute("select * from brain_proposals where status=? order by created_at desc, rowid desc limit ?",
                               (status, limit)).fetchall()
    return [_row_dict(r) for r in rows]


def pending_proposal_count(*, db_path: Optional[Path] = None) -> int:
    with _connect(db_path) as con:
        return int(con.execute("select count(*) from brain_proposals where status='pending'").fetchone()[0])


def reject_proposal(proposal_id: str, *, db_path: Optional[Path] = None) -> dict:
    with _connect(db_path) as con:
        cur = con.execute("update brain_proposals set status='rejected', reviewed_at=? where id=? and status='pending'",
                          (_now_iso(), proposal_id))
    return {"ok": cur.rowcount == 1, "id": proposal_id}


def _default_lessons_path() -> Path:
    """ATLAS_LESSONS_LEDGER, else the repo ledger (docs/lessons/LESSONS.md) when this is a checkout, else
    <state>/brain/LESSONS.md so an installed package never writes beside site-packages."""
    env = os.environ.get("ATLAS_LESSONS_LEDGER", "").strip()
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[3] / "docs" / "lessons" / "LESSONS.md"
    if repo.exists():
        return repo
    return atlas_paths.state_dir() / "brain" / "LESSONS.md"


def _next_lesson_id(text: str) -> str:
    nums = [int(m) for m in re.findall(r"\|\s*LES-(\d{3,})\s*\|", text)]
    return f"LES-{(max(nums) + 1) if nums else 1:03d}"


def _append_lesson_row(lessons_path: Path, title: str, rule: str, scope: str, source: str) -> str:
    """Append a ``proposed`` row to the lessons ledger (docs/lessons/LESSONS.md); returns the new id."""
    text = lessons_path.read_text(encoding="utf-8") if lessons_path.exists() else ""
    if "| id | pattern | rule |" not in text:
        text = (text.rstrip("\n") + "\n\n| id | pattern | rule | scope | check | status | sources |\n"
                "|---|---|---|---|---|---|---|\n")
    les = _next_lesson_id(text)
    clean = lambda s: " ".join(str(s).replace("|", "/").split())  # noqa: E731
    row = f"| {les} | {clean(title)[:160]} | {clean(rule)[:400]} | {clean(scope)[:60] or 'process'} | none yet | proposed | {clean(source)[:60]} |"
    if not text.endswith("\n"):
        text += "\n"
    lessons_path.write_text(text + row + "\n", encoding="utf-8")
    return les


_ASSIGNEE_ALIASES = {"": "claude-code", "agent": "claude-code", "brain": "local", "operator": "human"}
_STORE_ASSIGNEES = {"any", "claude-code", "codex", "human", "local"}


def _ticket_assignee(value) -> str:
    """The ticket store's assignee vocabulary (any, claude-code, codex, human, local); proposal payloads
    say "agent" and the supervisor may say "operator" (2026-11-21: Approve failed on 'agent')."""
    v = str(value or "").strip().lower()
    v = _ASSIGNEE_ALIASES.get(v, v)
    # anything else is a person's name (the supervisor sometimes writes the operator's): a human
    return v if v in _STORE_ASSIGNEES else "human"


def approve_proposal(proposal_id: str, *, edited_body: Optional[str] = None, db_path: Optional[Path] = None,
                     lessons_path: Optional[Path] = None, tickets_root: Optional[Path] = None,
                     remember: Optional[Callable[..., dict]] = None) -> dict:
    """Apply one proposal under the operator's click. The only write path of the loop."""
    with _connect(db_path) as con:
        row = con.execute("select * from brain_proposals where id=?", (proposal_id,)).fetchone()
    if not row:
        return {"ok": False, "error": f"proposal {proposal_id!r} not found"}
    p = _row_dict(row)
    if p["status"] != "pending":
        return {"ok": False, "error": f"proposal is already {p['status']}"}
    body = (edited_body or "").strip() or p["body"]
    payload = p.get("payload") or {}
    kind = p["kind"]
    result: dict
    try:
        if kind == "lesson":
            path = lessons_path or _default_lessons_path()
            les = _append_lesson_row(path, p["title"], body, str(payload.get("scope") or "process"), p["source"])
            result = {"ok": True, "lesson_id": les, "file": str(path)}
        elif kind == "ticket":
            from atlas_plus.tickets.store import create_ticket
            kwargs = dict(
                title=p["title"], project=str(payload.get("project") or "atlas-memory+"),
                assignee=_ticket_assignee(payload.get("assignee")), priority=str(payload.get("priority") or "normal"),
                effort=str(payload.get("effort") or "M"), body=body,
                interactive=True,  # the desk click IS the consent (2026-11-21): the background gate
                # (ATLAS_AUTONOMOUS) is for unattended writers, and Approve on Helm is a person
            )
            if tickets_root is not None:
                kwargs["tickets_root"] = tickets_root
            ticket = create_ticket(**kwargs)
            result = {"ok": True, "ticket_id": getattr(ticket, "id", None) or getattr(ticket, "ticket_id", None)}
        elif kind == "fact":
            if remember is None:
                from atlas_plus.mcp.memory_server.tools.remember import memory_remember as remember  # type: ignore[assignment]
            res = remember(scope=str(payload.get("scope") or "reference:brain-proposal"), fact=body, source="agent")
            result = {"ok": True, "file": (res or {}).get("file") if isinstance(res, dict) else None}
        else:
            return {"ok": False, "error": f"unknown kind {kind!r}"}
    except Exception as exc:  # noqa: BLE001 - the desk shows the failure; the row stays pending
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
    with _connect(db_path) as con:
        con.execute("update brain_proposals set status='approved', reviewed_at=?, result=?, body=? where id=?",
                    (_now_iso(), json.dumps(result, ensure_ascii=False), body, proposal_id))
    result["id"] = proposal_id
    result["kind"] = kind
    return result


# ---------------------------------------------------------------------------
# Supervisor support: the context it reads and the proposals it writes
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def parse_proposals(text: str) -> list[dict]:
    """The proposals in the LAST fenced JSON block of a supervisor report: ``{"proposals": [...]}``.

    Each item: kind (lesson|ticket|fact), title, body, optional scope/project/assignee/priority/effort.
    Unknown kinds and items without a title or body are dropped; nothing raises.
    """
    found: list[dict] = []
    for m in reversed(_FENCE_RE.findall(text or "")):
        try:
            data = json.loads(m)
        except ValueError:
            continue
        items = data.get("proposals") if isinstance(data, dict) else data
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            kind = str(it.get("kind", "")).strip().lower()
            title = " ".join(str(it.get("title", "")).split())[:200]
            body = str(it.get("body", "")).strip()[:4000]
            if kind in PROPOSAL_KINDS and title and body:
                payload = {k: it[k] for k in ("scope", "project", "assignee", "priority", "effort") if k in it}
                found.append({"kind": kind, "title": title, "body": body, "payload": payload})
        if found:
            break
    return found


def file_proposals(items: list[dict], source: str, *, db_path: Optional[Path] = None) -> list[dict]:
    out: list[dict] = []
    for it in items:
        try:
            out.append(add_proposal(source, it["kind"], it["title"], it["body"], it.get("payload"), db_path=db_path))
        except ValueError:
            continue
    return out


def lessons_summary(lessons_path: Optional[Path] = None, limit: int = 80) -> str:
    """``LES-id: pattern`` per row so the supervisor proposes lessons the ledger lacks."""
    path = lessons_path or _default_lessons_path()
    text = _read(path)
    rows = re.findall(r"^\|\s*(LES-\d+)\s*\|\s*([^|]+?)\s*\|", text, re.MULTILINE)
    return "\n".join(f"{i}: {p[:110]}" for i, p in rows[:limit])


def proposals_history(days: int = 30, *, db_path: Optional[Path] = None, limit: int = 40) -> str:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    try:
        with _connect(db_path) as con:
            rows = con.execute("select kind, title, status from brain_proposals where created_at>=? "
                               "order by created_at desc, rowid desc limit ?", (cutoff, limit)).fetchall()
    except sqlite3.Error:
        return ""
    return "\n".join(f"[{r['status']}] {r['kind']}: {r['title']}" for r in rows)


def supervisor_context(days: int = 7, *, state_dir: Optional[Path] = None, db_path: Optional[Path] = None,
                       lessons_path: Optional[Path] = None) -> str:
    """Digests + lessons ledger + proposals history, the input the weekly synthesis reads."""
    parts: list[str] = []
    block = digest_block(load_recent_digests(days, state_dir=state_dir))
    parts.append("RECENT WORK (nightly session digests, oldest first):\n" + (block or "  (no digests yet: the session_digest lane has not run)"))
    lessons = lessons_summary(lessons_path)
    parts.append("LESSONS LEDGER (ids and patterns already recorded; do not propose these again):\n" + (lessons or "  (empty)"))
    parts.append("OPEN TICKETS NOW (agent tickets untouched for a week are the ones to ask about):\n"
                 + tickets_block(tickets_snapshot()))
    hist = proposals_history(db_path=db_path)
    parts.append("PROPOSALS ALREADY MADE in the last 30 days (status is the operator's decision; "
                 "do not repeat a rejected one):\n" + (hist or "  (none)"))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# CLI: python -m atlas_plus.brain.learning digest|note|proposals
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="atlas-brain-learning", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("digest", help="write the session digest for a local day (default: yesterday)")
    d.add_argument("--date", help="YYYY-MM-DD")
    n = sub.add_parser("note", help="append a session-close note the next digest folds in")
    n.add_argument("--title", required=True)
    n.add_argument("--text", required=True)
    n.add_argument("--project")
    p = sub.add_parser("proposals", help="list proposals")
    p.add_argument("--status", default="pending", choices=("pending", "approved", "rejected", "all"))
    args = ap.parse_args(argv)

    if args.cmd == "digest":
        json_path, md_path, digest = write_session_digest(args.date)
        c = digest["counts"]
        print(f"digest {digest['date']}: {c['session_entries']} session entries, {c['commits']} commits, "
              f"{c['brain_jobs']} Brain jobs -> {json_path}")
        return 0
    if args.cmd == "note":
        path = add_session_note(args.title, args.text, project=args.project)
        print(f"note appended -> {path}")
        return 0
    rows = list_proposals(args.status, limit=100)
    for r in rows:
        print(f"[{r['status']}] {r['id'][:8]} {r['kind']}: {r['title']}")
    print(f"{len(rows)} proposal(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
