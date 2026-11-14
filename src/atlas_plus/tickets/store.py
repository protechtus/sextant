"""P10-T1: Ticket store — read/write .memory/tickets/<project>/<id>.md."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from atlas_plus.autonomous import require_autonomous_writes
from atlas_plus import paths as atlas_paths

# Read env vars directly to avoid circular import with mcp.memory_server.config
_MEMORY_ROOT = atlas_paths.memory_root()
TICKETS_ROOT = _MEMORY_ROOT / "tickets"

VALID_STATUSES = {"todo", "in-progress", "done", "blocked", "archived"}
VALID_ASSIGNEES = {"human", "claude-code", "codex", "local", "any"}
VALID_EFFORTS = {"S", "M", "L", "XL"}


@dataclass
class Ticket:
    """Parsed representation of a ticket markdown file with YAML frontmatter."""

    id: str
    title: str
    project: str
    status: str = "todo"
    assignee: str = "human"
    priority: str = "normal"
    effort: str = "M"
    requires: Optional[str] = None
    updated: str = field(default_factory=lambda: date.today().isoformat())
    body: str = ""
    path: Optional[Path] = None
    reopen_count: int = 0
    # ISO date when the step's instructions were verified/researched as
    # ready to execute. Human-assigned (operator) steps surface this as a
    # readiness badge on the dashboard's Operator panel; None = the
    # instructions still need research before someone acts on them.
    verified: Optional[str] = None

    def as_dict(self) -> dict:
        """Return a JSON-serialisable dict of ticket fields (excludes path; includes reopen_count when >0)."""
        d = {
            "id": self.id,
            "title": self.title,
            "project": self.project,
            "status": self.status,
            "assignee": self.assignee,
            "priority": self.priority,
            "effort": self.effort,
            "requires": self.requires,
            "updated": self.updated,
        }
        if self.reopen_count > 0:
            d["reopen_count"] = self.reopen_count
        if self.verified:
            d["verified"] = self.verified
        return d

    def to_markdown(self) -> str:
        """Render ticket as a YAML-frontmatter markdown string suitable for writing to disk."""
        fm = {
            "id": self.id,
            "title": self.title,
            "type": "ticket",
            "status": self.status,
            "project": self.project,
            "assignee": self.assignee,
            "priority": self.priority,
            "effort": self.effort,
            "updated": self.updated,
        }
        if self.requires:
            fm["requires"] = self.requires
        if self.reopen_count > 0:
            fm["reopen_count"] = self.reopen_count
        if self.verified:
            fm["verified"] = self.verified
        fm_text = yaml.dump(fm, default_flow_style=False, allow_unicode=True).strip()
        body = self.body.strip() or "## What\n\n(fill in)\n\n## Acceptance criteria\n\n- [ ] TBD"
        return f"---\n{fm_text}\n---\n\n{body}\n"


def _split_frontmatter(text: str) -> Optional[tuple[list[str], int]]:
    """Split on delimiter LINES: (lines with endings kept, index of the closing ``---``).

    The block opens with a first line that is exactly ``---`` and closes at
    the next line that is exactly ``---`` (trailing whitespace tolerated,
    leading whitespace not, so an indented YAML line is never a delimiter).
    A ``---`` inside a value, such as a title ``Migrate a---b``, is content.
    Same contract as intake/frontmatter.py (audit 2026-11-09 row 9). Returns
    None when there is no well-formed block.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return lines, i
    return None


def _parse_ticket(path: Path) -> Optional[Ticket]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        split = _split_frontmatter(text)
        if split is None:
            return None
        lines, end = split
        fm = yaml.safe_load("".join(lines[1:end])) or {}
        if not isinstance(fm, dict):
            return None
        body = "".join(lines[end + 1:]).strip()
        return Ticket(
            id=fm.get("id", path.stem),
            title=fm.get("title", path.stem),
            project=fm.get("project", path.parent.name),
            status=fm.get("status", "todo"),
            assignee=fm.get("assignee", "human"),
            priority=fm.get("priority", "normal"),
            effort=fm.get("effort", "M"),
            requires=fm.get("requires"),
            updated=str(fm.get("updated", "")),
            body=body,
            path=path,
            reopen_count=int(fm.get("reopen_count", 0)),
            verified=str(fm["verified"]) if fm.get("verified") else None,
        )
    except Exception:
        return None


def _next_id(project: str, tickets_root: Path) -> str:
    return f"{project[:3].upper()}-{_next_num(project, tickets_root):03d}"


def _prefix_for(project: str, tickets_root: Path) -> str:
    """The id prefix of a project: its first three letters unless another project already owns
    them, then as many letters as make it unique (`PROJ1`), remembered in `<project>/.prefix`.
    A project with tickets and no marker keeps its three letters, so old ids stay valid."""
    pdir = tickets_root / project
    marker = pdir / ".prefix"
    try:
        if marker.is_file():
            v = re.sub(r"[^A-Za-z0-9]", "", marker.read_text(encoding="utf-8")).upper()
            if v:
                return v
    except OSError:
        pass
    base = re.sub(r"[^A-Za-z0-9]", "", project).upper() or "TKT"
    default = base[:3]
    legacy = pdir.is_dir() and any(f.suffix == ".md" and not f.name.startswith("_") for f in pdir.iterdir())
    taken: set[str] = set()
    try:
        for d in tickets_root.iterdir():
            if not d.is_dir() or d.name == project or d.name.startswith(("_", ".")):
                continue
            v = ""
            try:
                if (d / ".prefix").is_file():
                    v = re.sub(r"[^A-Za-z0-9]", "", (d / ".prefix").read_text(encoding="utf-8")).upper()
            except OSError:
                v = ""
            other = v or (re.sub(r"[^A-Za-z0-9]", "", d.name).upper() or "TKT")[:3]
            if legacy and not v and other == default and d.name > project:
                continue  # two legacy projects share the letters: the one that sorts first keeps them (MN32)
            taken.add(other)
    except OSError:
        pass
    if legacy and default not in taken:
        return default  # legacy project: its ids are already minted with these letters
    cand, k = default, 3
    while cand in taken and k < len(base):
        k += 1
        cand = base[:k]
    n = 2
    while cand in taken:
        cand = f"{default}{n}"
        n += 1
    if cand != default:
        try:
            pdir.mkdir(parents=True, exist_ok=True)
            marker.write_text(cand + "\n", encoding="utf-8")
        except OSError:
            pass
    return cand


def _next_num(project: str, tickets_root: Path) -> int:
    project_dir = tickets_root / project
    existing = []
    if project_dir.exists():
        for f in project_dir.glob("*.md"):
            if f.name.startswith("_"):
                continue
            m = re.search(r"-(\d+)", f.stem)
            if m:
                existing.append(int(m.group(1)))
    # IDs never recycle: archived tickets keep their numbers reserved, else a
    # cleared queue restarts at 001 and new tickets shadow archived history.
    archive = tickets_root / "_archive"
    if archive.exists():
        for f in archive.rglob("*.md"):
            if f.name.startswith("_") or f.parent.name != project:
                continue
            m = re.search(r"-(\d+)", f.stem)
            if m:
                existing.append(int(m.group(1)))
    return max(existing, default=0) + 1


# Windows device names: a project directory called "nul" or "CON" is not a directory.
_RESERVED_PROJECT_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"com{d}" for d in "123456789"}
    | {f"lpt{d}" for d in "123456789"}
)


def _is_safe_project(project: str) -> bool:
    """One plain directory name under the tickets root (audit 2026-11-09 row 24).

    The regex alone admits ".", ".." (a ticket written to the corpus root) and
    "..." (which Win32 trims to nothing). Also reject any leading or trailing
    dot and Windows device names.
    """
    if not isinstance(project, str) or not re.match(r'^[a-zA-Z0-9_.+-]+$', project):
        return False
    if project.startswith((".", "_")) or project.endswith("."):
        return False  # `_archive` and friends are internal folders: a ticket filed there was never listed (AG38)
    return project.split(".", 1)[0].casefold() not in _RESERVED_PROJECT_NAMES


def _atomic_write_text(path: Path, text: str) -> None:
    """tmp + os.replace in the same directory, so a crash never leaves half a ticket."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


_FM_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+):")


def _rewrite_fields(text: str, updates: dict) -> Optional[str]:
    """Rewrite top-level frontmatter scalar keys in place; every other byte is kept.

    Unknown keys (a user's ``labels:`` or ``due:``), key order, comments,
    quoting and the body all survive untouched. A key not yet present is
    added just before the closing delimiter. Returns None when the text has
    no well-formed frontmatter block.
    """
    split = _split_frontmatter(text)
    if split is None:
        return None
    lines, end = split
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    pending = dict(updates)
    for i in range(1, end):
        m = _FM_KEY_RE.match(lines[i])
        if not m or m.group(1) not in pending:
            continue
        key = m.group(1)
        ending = lines[i][len(lines[i].rstrip("\r\n")):] or newline
        lines[i] = _dump_field(key, pending.pop(key)) + ending
    extra = [_dump_field(k, v) + newline for k, v in pending.items()]
    return "".join(lines[:end] + extra + lines[end:])


def _dump_field(key: str, value) -> str:
    return yaml.safe_dump({key: value}, default_flow_style=False, allow_unicode=True).strip()


def _walk_live_ticket_files(root_dir: Path, recursive: bool = True):
    """Yield live ticket .md files, PRUNING underscore names at descent.

    Underscore DIRECTORIES (_archive/ especially) are not live tickets.
    Before this check, every archived ticket haunted every consumer:
    Helm board cards that "kept coming back", 79 phantom open tickets
    feeding the SLA checker, dispatcher views of long-dead work
    (the recurring ATL-010 sighting, 2026-08-05).

    Perf (2026-10-10): the old recursive glob ENUMERATED the whole
    _archive tree (1,815 paths for 9 live tickets) and ran
    Path.relative_to on every one before filtering -- 54ms per
    list_tickets call, paid by the session hook, the operator panel's
    60s poll, board renders, and SLA checks. Pruning at descent never
    touches archived subtrees at all. Children are visited in sorted
    name order so parse order stays deterministic.
    """
    stack = [str(root_dir)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name.startswith("_"):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if recursive:
                    stack.append(entry.path)
            elif name.endswith(".md"):
                yield Path(entry.path)


def list_tickets(
    project: Optional[str] = None,
    status: Optional[str] = None,
    assignee: Optional[str] = None,
    tickets_root: Optional[Path] = None,
) -> list[Ticket]:
    """Return tickets from TICKETS_ROOT, optionally filtered by project/status/assignee.

    Results are sorted critical -> high -> normal/low, then by updated date ascending
    so the most recently modified items surface at the bottom of each priority group.
    """
    root = tickets_root or TICKETS_ROOT
    # Confine project to a single safe component (audit HIGH-2): the read path previously let
    # list_tickets(project="../sessions") escape the tickets root. The create_ticket regex alone
    # permits bare "..", so reject "." / ".." explicitly too. Invalid -> empty (fail safe).
    if project is not None:
        if project in (".", "..") or not re.match(r'^[a-zA-Z0-9_.+-]+$', project):
            return []
    search = root / project if project else root
    if not search.exists():
        return []

    results = []
    for f in _walk_live_ticket_files(search, recursive=not project):
        t = _parse_ticket(f)
        if t is None:
            continue
        if status and t.status != status:
            continue
        if assignee and t.assignee != assignee:
            continue
        results.append(t)

    results.sort(key=lambda t: (t.priority != "critical", t.priority != "high", t.updated), reverse=False)
    return results


def get_ticket(ticket_id: str, tickets_root: Optional[Path] = None) -> Optional[Ticket]:
    """Return a Ticket by ID (e.g. 'ATL-007'), or None if not found.

    Does a recursive glob under TICKETS_ROOT so project subdirectories are transparent
    to the caller.
    """
    root = tickets_root or TICKETS_ROOT
    if "/" in ticket_id:
        # `project/ID` names one ticket when two projects minted the same id (MN29)
        proj, _, ticket_id = ticket_id.partition("/")
        if not proj or not _is_safe_project(proj):
            return None  # `C:` escaped the tickets root on Windows (AG38): the one project rule everywhere
        root = root / proj
    if not root.exists():
        return None
    direct = root / f"{ticket_id}.md"
    if "/" not in ticket_id and direct.is_file():
        t = _parse_ticket(direct)
        if t and t.id == ticket_id:
            return t  # a bare id names the ROOT-level ticket first when a folder holds the same id (AG49)
    for f in _walk_live_ticket_files(root):
        # The file name must agree with the frontmatter id: a spoofed `id:` in some other
        # file must not become the ticket a status change rewrites (round-B 2026-11-09, N-O).
        if f.stem.upper() != ticket_id.upper() and not f.stem.upper().startswith(ticket_id.upper() + "-"):
            continue
        t = _parse_ticket(f)
        if t and t.id == ticket_id:
            return t
    return None


VALID_PRIORITIES = {"critical", "high", "normal", "low"}


def create_ticket(
    title: str,
    project: str,
    assignee: str = "human",
    priority: str = "normal",
    effort: str = "M",
    body: str = "",
    requires: Optional[str] = None,
    verified: Optional[str] = None,
    tickets_root: Optional[Path] = None,
    interactive: bool = False,
) -> Ticket:
    """Create a new ticket file under TICKETS_ROOT/<project>/<id>.md.

    Validates all fields before writing.  Raises ValueError on bad input.
    Auto-assigns the next numeric ID within the project prefix (e.g. ATL-008).
    """
    # Kill switch at the write primitive (audit HIGH-3 / ATL-013): autonomous ticket creation
    # (gaps --autoticket, CLI, MCP wrapper) is fail-closed when ATLAS_AUTONOMOUS is off/unset.
    # Note: update_ticket_status is intentionally NOT gated -- status transitions are a task
    # lifecycle operation (_try_close_ticket, reopen-on-failure), not autonomous content authoring.
    from atlas_plus.autonomous import require_write
    require_write("ticket_create", interactive=interactive)
    if not _is_safe_project(project):
        raise ValueError(f"Invalid project name '{project}': only ASCII letters, digits, dots, hyphens, underscores, plus allowed "
                         f"(no leading underscore or dot, no trailing dot, not a Windows device name).")
    from atlas_plus.mcp.memory_server.routing import _scrub_controls as _scrub
    title = _scrub(str(title or "")); body = _scrub(str(body or ""))   # ESC/BEL in a title (FN6)
    if not title or not title.strip():
        raise ValueError("Ticket title must not be empty.")
    if "\n" in title or "\r" in title:
        raise ValueError("Ticket title must not contain newlines.")
    if len(title) > 200:
        raise ValueError("Ticket title must be 200 characters or fewer.")
    if assignee not in VALID_ASSIGNEES:
        raise ValueError(f"Invalid assignee {assignee!r}. Valid: {sorted(VALID_ASSIGNEES)}")
    if priority not in VALID_PRIORITIES:
        raise ValueError(f"Invalid priority {priority!r}. Valid: {sorted(VALID_PRIORITIES)}")
    if effort not in VALID_EFFORTS:
        raise ValueError(f"Invalid effort {effort!r}. Valid: {sorted(VALID_EFFORTS)}")
    if len(body) > 10_000:
        raise ValueError("Ticket body must be 10,000 characters or fewer.")
    requires = requires.strip() if isinstance(requires, str) else requires
    if requires and not re.fullmatch(r'[A-Z]{1,8}-\d+', requires):  # `$` let a trailing newline through (AG44)
        raise ValueError(f"Invalid requires value {requires!r}. Expected format: ATL-001")
    if verified and not re.match(r'^\d{4}-\d{2}-\d{2}$', verified):
        raise ValueError(f"Invalid verified value {verified!r}. Expected an ISO date: 2026-10-08")

    root = tickets_root or TICKETS_ROOT
    project_dir = root / project
    project_dir.mkdir(parents=True, exist_ok=True)

    # Claim the id with O_CREAT|O_EXCL (audit 2026-11-09 row 24): two
    # concurrent creators that compute the same next id no longer overwrite
    # each other; the loser advances to the next number.
    prefix = _prefix_for(project, root)  # unique across projects (MN29: proj1 and proj2 both minted PRO-001)
    n = _next_num(project, root)
    while True:
        ticket_id = f"{prefix}-{n:03d}"
        dest = project_dir / f"{ticket_id}.md"
        try:
            fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            n += 1
            continue
        break
    ticket = Ticket(
        id=ticket_id,
        title=title,
        project=project,
        assignee=assignee,
        priority=priority,
        effort=effort,
        body=body,
        requires=requires,
        verified=verified,
    )
    try:
        # Text mode, as write_text was: same bytes on disk as before.
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(ticket.to_markdown())
    except BaseException:
        # Never leave an empty claimed slot posing as a ticket.
        try:
            dest.unlink()
        except OSError:
            pass
        raise
    ticket.path = dest
    try:
        from atlas_plus import activity
        activity.record("tickets", "ticket_created", ticket_id=ticket_id,
                        project=project, assignee=assignee,
                        ref=f"tickets/{project}/{ticket_id}.md")
    except Exception:
        pass
    return ticket


def _version_or_raise(path: Path) -> None:
    from atlas_plus.mcp.memory_server.versioning import version_before_write
    try:
        version_before_write(path)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"refusing to rewrite {path.name}: version copy failed ({exc})") from exc


def update_ticket_fields(ticket_id: str, updates: dict, tickets_root: Optional[Path] = None) -> Ticket:
    """Rewrite only the given frontmatter fields (plus ``updated``), keeping unknown keys and the
    body byte for byte; versioned first, written atomically. Status changes go through
    update_ticket_status (reopen accounting). Raises FileNotFoundError / ValueError."""
    if "status" in updates:
        raise ValueError("use update_ticket_status for status changes")
    t = get_ticket(ticket_id, tickets_root)
    if t is None or t.path is None:
        raise FileNotFoundError(f"Ticket '{ticket_id}' not found")
    fields = dict(updates)
    fields["updated"] = date.today().isoformat()
    _version_or_raise(t.path)
    raw = t.path.read_bytes().decode("utf-8", errors="replace")
    new_text = _rewrite_fields(raw, fields)
    if new_text is None:
        raise ValueError(f"Ticket '{ticket_id}' has no frontmatter block")
    _atomic_write_text(t.path, new_text)
    for k, v in fields.items():
        if hasattr(t, k):
            setattr(t, k, v)
    return t


def update_ticket_status(
    ticket_id: str,
    status: str,
    tickets_root: Optional[Path] = None,
) -> Ticket:
    """Change the status of a ticket and write it back to disk.

    Called by tasks.py _try_close_ticket() when a Brain task reaches a terminal state,
    and by dispatcher.reopen_ticket_if_low_quality / _reopen_ticket_from_verify_failure
    when a quality gate or verify failure sends a ticket back to todo.

    Automatically increments reopen_count when the ticket transitions back to todo
    from any other status. This provides a production-quality signal for how many
    times a ticket has looped through the automation cycle.

    Raises ValueError on invalid status; raises FileNotFoundError if the ticket is missing.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Valid: {VALID_STATUSES}")
    t = get_ticket(ticket_id, tickets_root)
    if t is None:
        raise FileNotFoundError(f"Ticket '{ticket_id}' not found")

    # Increment reopen_count when any ticket is sent back to todo from a non-todo state.
    # This tracks automation loop retries regardless of the cause (quality gate, verify, etc.).
    if status == "todo" and t.status != "todo":
        t.reopen_count += 1

    # Version before rewrite -- house rule; a failed version copy stops the write
    # (round-B re-audit 2026-11-09, row 9: never rewrite what you could not copy).
    assert t.path is not None
    _version_or_raise(t.path)

    t.status = status
    t.updated = date.today().isoformat()
    assert t.path is not None
    # Rewrite only the changed frontmatter lines (audit 2026-11-09 row 9):
    # unknown keys, values containing "---" and the body survive byte for
    # byte. Atomic (tmp + replace) so a crash never truncates the ticket.
    updates = {"status": t.status, "updated": t.updated}
    if t.reopen_count > 0:
        updates["reopen_count"] = t.reopen_count
    raw = t.path.read_bytes().decode("utf-8", errors="replace")
    new_text = _rewrite_fields(raw, updates)
    if new_text is None:  # pragma: no cover - _parse_ticket already required a block
        new_text = t.to_markdown()
    _atomic_write_text(t.path, new_text)
    try:
        from atlas_plus import activity
        activity.record("tickets", "ticket_status", ticket_id=ticket_id,
                        status=status, project=t.project,
                        ref=f"tickets/{t.project}/{ticket_id}.md")
    except Exception:
        pass
    return t
