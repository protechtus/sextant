"""P10-T1: Ticket store — read/write .memory/tickets/<project>/<id>.md."""

from __future__ import annotations

import os
import re
import time
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
        # After a folder rename the frontmatter still names the old project: JSON readers (list --json, the MCP
        # list, the dashboard) got a project that `project/ID` no longer finds. Name the folder too (SEX-026).
        folder = ticket_folder(self) if self.path else ""
        if folder and folder != self.project:
            d["folder"] = folder
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
        from atlas_plus._yaml import fast_safe_load as _fsl
        fm = _fsl("".join(lines[1:end])) or {}  # the C loader: 9.9 of 10.9 s listing 5,000 tickets was the pure one (AG75)
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


_TICKET_NAME = re.compile(r"([A-Za-z0-9]+)-(\d+)")
_RACY_NS = 1_000_000_000  # a stamp younger than this may hide a second change in the same tick: re-read
# Folder path -> _DirInfo, re-read only when the folder's stamp moved; a create updates its own folder's
# entry, so N creates in one process stay linear (AG84). `.clear()` forgets it (and the high-water marks).
_NEXT_MEMO: dict = {}
_ARCHIVE_FULL_AT: dict = {}  # tickets root -> when every _archive folder was last stat-checked
_ARCHIVE_RECHECK_S = 2.0


class _DirInfo:
    """One folder's ticket names: highest `-<n>` (anymax), highest per PREFIX, subfolders, `.prefix`."""
    __slots__ = ("mtime", "racy", "anymax", "by_prefix", "has_md", "first", "subdirs", "marker", "fm_project")

    def __init__(self, mtime: int):
        self.mtime, self.racy, self.anymax, self.by_prefix, self.has_md = mtime, False, 0, {}, False
        self.first, self.subdirs, self.marker, self.fm_project = "", (), "", None


def _dir_info(path: str) -> Optional["_DirInfo"]:
    try:
        m = os.stat(path).st_mtime_ns
    except OSError:
        _NEXT_MEMO.pop(path, None)
        return None
    info = _NEXT_MEMO.get(path)
    if info is not None and info.mtime == m and not info.racy:
        return info
    info, subs = _DirInfo(m), []
    try:
        with os.scandir(path) as it:
            for e in it:
                name = e.name
                if e.is_dir(follow_symlinks=False):
                    if not name.startswith("."):
                        subs.append(name)
                elif name == ".prefix":
                    try:
                        with open(e.path, encoding="utf-8") as fh:
                            info.marker = re.sub(r"[^A-Za-z0-9]", "", fh.read(64)).upper()
                    except (OSError, ValueError):
                        pass
                elif name.endswith(".md") and not name.startswith("_"):
                    info.has_md = True
                    info.first = min(info.first or name, name)
                    a, p = re.search(r"-(\d+)", name[:-3]), _TICKET_NAME.match(name)
                    if a:
                        info.anymax = max(info.anymax, int(a.group(1)))
                    if p:
                        k = p.group(1).upper()
                        info.by_prefix[k] = max(info.by_prefix.get(k, 0), int(p.group(2)))
    except OSError:
        return None
    info.subdirs, info.racy = tuple(sorted(subs)), (time.time_ns() - m) < _RACY_NS
    _NEXT_MEMO[path] = info
    return info


def _snapshot(tickets_root: Path) -> list:
    """[(kind, path, name, info)]: the root ("root"), each live project folder ("project") and its
    non-underscore subfolders ("live"), every one stat-checked now."""
    top = str(tickets_root)
    rinfo = _dir_info(top)
    out: list = [("root", top, os.path.basename(top), rinfo)] if rinfo else []
    # Underscore folders other than the root `_archive` (walked by _archive_dirs) count too: a ticket moved
    # into `alpha/_archive/` or `_done/` had its id issued again (MN79)
    stack = [(os.path.join(top, n), n, "archive" if n.startswith("_") else "project")
             for n in (rinfo.subdirs if rinfo else ()) if n != "_archive"]
    while stack:
        p, base, k = stack.pop()
        info = _dir_info(p)
        if info is not None:
            out.append((k, p, base, info))
            stack += [(os.path.join(p, s), s, "archive" if (k == "archive" or s.startswith("_")) else "live")
                      for s in info.subdirs]
    return out


def _archive_dirs(tickets_root: Path, project: str, prefix: str) -> list:
    """Every ``_archive`` folder at any depth. ``_archive``, folders named after the project, folders
    holding this prefix and folders not seen before are stat-checked on every call, the rest every
    _ARCHIVE_RECHECK_S seconds (hundreds of archive folders cost one stat each per create otherwise);
    a ticket moved into a skipped folder was counted where it lived (the high-water mark in _next_num)."""
    root = str(tickets_root)
    now = time.monotonic()
    full = now - _ARCHIVE_FULL_AT.get(root, -1e9) >= _ARCHIVE_RECHECK_S
    if full:
        _ARCHIVE_FULL_AT[root] = now
    key, want, out = prefix.upper(), os.path.normcase(project), []
    stack = [(os.path.join(root, "_archive"), "_archive")]
    while stack:
        p, base = stack.pop()
        info = _NEXT_MEMO.get(p)
        if full or info is None or info.racy or key in info.by_prefix or base in ("_archive",) \
                or os.path.normcase(base) == want:
            info = _dir_info(p)
        if info is not None:
            out.append(("archive", p, base, info))
            stack += [(os.path.join(p, s), s) for s in info.subdirs]
    return out


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
    legacy = False
    try:
        with os.scandir(pdir) as _it:  # stops at the first ticket: iterdir() listed the whole folder per create (AG84)
            legacy = any(os.path.splitext(e.name)[1] == ".md" and not e.name.startswith("_") for e in _it)
    except OSError:
        legacy = False
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
    own = _dir_info(str(pdir)) if legacy else None
    inherited = next(iter(own.by_prefix)) if own and len(own.by_prefix) == 1 else default
    if inherited != default and inherited not in taken:
        if own.fm_project is None:
            t = _parse_ticket(pdir / own.first)
            own.fm_project = (t.project if t else "") or ""
        if own.fm_project and own.fm_project != project:
            try:  # a renamed folder (its tickets name another project) keeps their prefix: not two (AG86)
                marker.write_text(inherited + "\n", encoding="utf-8")
            except OSError:
                pass
            return inherited
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
    """The next free number: above any ticket file of the project folder, any ticket in an ``_archive``
    folder named after the project, and every `PREFIX-<n>` anywhere else, live or archived, at any
    depth: a new folder under a renamed project's old name continued nothing and reissued its ids
    (MN73 F7). The highest number seen is kept per project, so a ticket moved out of sight is not
    issued again; create_ticket's O_EXCL claim still decides in the project folder."""
    prefix = _prefix_for(project, tickets_root)
    key, own_key = prefix.upper(), os.path.normcase(str(tickets_root / project))
    best = 0
    for kind, p, name, info in _snapshot(tickets_root) + _archive_dirs(tickets_root, project, prefix):
        v = info.by_prefix.get(key, 0)
        if (kind == "project" and os.path.normcase(p) == own_key) or (
                kind == "archive" and os.path.normcase(name) == os.path.normcase(project)):
            v = max(v, info.anymax)
        best = max(best, v)
    hk = ("high", str(tickets_root), os.path.normcase(project), key)
    _NEXT_MEMO[hk] = best = max(best, _NEXT_MEMO.get(hk, 0))
    return best + 1


def _remember_own_create(project: str, tickets_root: Path, prefix: str, n: int, name: str) -> None:
    """Our own file moved the project folder's stamp: record it instead of re-reading the folder."""
    hk = ("high", str(tickets_root), os.path.normcase(project), prefix.upper())
    _NEXT_MEMO[hk] = max(_NEXT_MEMO.get(hk, 0), n)
    p = str(tickets_root / project)
    info = _NEXT_MEMO.get(p)
    try:
        info.mtime, info.racy = os.stat(p).st_mtime_ns, False  # O_EXCL guards this folder itself
    except (OSError, AttributeError):
        _NEXT_MEMO.pop(p, None)
        return
    info.has_md, info.anymax, info.first = True, max(info.anymax, n), min(info.first or name, name)
    info.by_prefix[prefix.upper()] = max(info.by_prefix.get(prefix.upper(), 0), n)


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
    # `list <project>` walked the folder flat and missed `<project>/done/`, which plain `list` showed (SEX-026)
    for f in _walk_live_ticket_files(search, recursive=True):
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


import threading as _threading


class _PerThread(_threading.local):
    def __init__(self):
        self.d = {}


_AMBIG = _PerThread()  # AG67: one module-global dict was shared by concurrent requests (a lookup in another request cleared or replaced it)


class _AmbiguousView:
    """`LAST_AMBIGUOUS` kept as a name; it reads and writes the calling thread's own record."""
    def get(self, k, default=None):
        return _AMBIG.d.get(k, default)

    def clear(self):
        _AMBIG.d = {}

    def __getitem__(self, k):
        return _AMBIG.d[k]

    def __setitem__(self, k, v):
        _AMBIG.d[k] = v


LAST_AMBIGUOUS = _AmbiguousView()  # set by get_ticket when a bare id matched several folder tickets (MN56)


def ambiguous_hint() -> str:
    """The sentence for a lookup that returned None because the bare id was ambiguous, else ''."""
    if not LAST_AMBIGUOUS.get("choices"):
        return ""
    return f"ticket id {LAST_AMBIGUOUS['id']!r} names several tickets: use one of " + ", ".join(LAST_AMBIGUOUS["choices"])


def ticket_folder(t: "Ticket", tickets_root: Optional[Path] = None) -> str:
    """The project folder a ticket file lives in (its first path part under the tickets root), else
    its frontmatter project. A renamed folder's tickets keep the old name in their frontmatter, so a
    `project/ID` reference built from `t.project` named another ticket or none (AG86)."""
    root = tickets_root or TICKETS_ROOT
    try:
        parts = Path(t.path).relative_to(root).parts
    except (TypeError, ValueError):
        parts = ()
    return parts[0] if len(parts) > 1 else (getattr(t, "project", "") or "")


def ticket_ref(t: "Ticket", tickets_root: Optional[Path] = None) -> str:
    """`folder/ID` for a ticket in a project folder, the bare id for one at the root."""
    folder = ticket_folder(t, tickets_root)
    try:
        at_root = Path(t.path).parent == Path(tickets_root or TICKETS_ROOT)
    except TypeError:
        at_root = False
    return t.id if at_root or not folder else f"{folder}/{t.id}"


def get_ticket(ticket_id: str, tickets_root: Optional[Path] = None) -> Optional[Ticket]:
    """Return a Ticket by ID (e.g. 'ATL-007'), or None if not found.

    Does a recursive glob under TICKETS_ROOT so project subdirectories are transparent
    to the caller.
    """
    LAST_AMBIGUOUS.clear()
    root = tickets_root or TICKETS_ROOT
    if "/" in ticket_id:
        # `project/ID` names one ticket when two projects minted the same id (MN29)
        proj, _, ticket_id = ticket_id.partition("/")
        if not proj or not _is_safe_project(proj):
            return None  # `C:` escaped the tickets root on Windows (AG38): the one project rule everywhere
        root = root / proj
    if not root.exists():
        return None
    _want = ticket_id.upper()  # ids are minted upper case: `show alp-001` said "not found" where the dashboard found it (AG86)
    direct = root / f"{ticket_id}.md"
    if "/" not in ticket_id and direct.is_file():
        t = _parse_ticket(direct)
        if t and str(t.id).upper() == _want:
            return t  # a bare id names the ROOT-level ticket first when a folder holds the same id (AG49)
    _found: list = []
    for f in _walk_live_ticket_files(root):
        # The file name must agree with the frontmatter id: a spoofed `id:` in some other
        # file must not become the ticket a status change rewrites (round-B 2026-11-09, N-O).
        if f.stem.upper() != ticket_id.upper() and not f.stem.upper().startswith(ticket_id.upper() + "-"):
            continue
        t = _parse_ticket(f)
        if t and str(t.id).upper() == _want:
            _found.append(t)
    if len(_found) > 1 and "/" not in ticket_id:
        # a bare id that names a ticket in several project folders would act on whichever the walk met first
        # (MN56: a status change rewrote an arbitrary one); the caller gets the qualified ids to choose from
        LAST_AMBIGUOUS["id"] = ticket_id
        # a renamed folder's tickets keep their frontmatter project: the hint named one folder twice (MN73)
        LAST_AMBIGUOUS["choices"] = sorted(f"{ticket_folder(t, root) or '?'}/{t.id}" for t in _found)
        return None
    return _found[0] if _found else None


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
    if requires and not re.fullmatch(r'[A-Za-z0-9]{1,16}-\d+', requires):  # K8S-001 is an id the store mints (MN79)  # `$` let a trailing newline through (AG44)
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
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:  # LF like every other corpus file: Windows wrote CRLF (AG70)
            f.write(ticket.to_markdown())
    except BaseException:
        # Never leave an empty claimed slot posing as a ticket.
        try:
            dest.unlink()
        except OSError:
            pass
        raise
    ticket.path = dest
    _remember_own_create(project, root, prefix, n, dest.name)  # our own file changed the folder stamp (AG84)
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
        raise FileNotFoundError(ambiguous_hint() or f"Ticket '{ticket_id}' not found")  # the qualified ids, not "not found" (AG67)
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
        raise FileNotFoundError(ambiguous_hint() or f"Ticket '{ticket_id}' not found")  # the qualified ids, not "not found" (AG67)

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
        activity.record("tickets", "ticket_status", ticket_id=t.id,
                        status=status, project=t.project,
                        ref=f"tickets/{ticket_folder(t, tickets_root)}/{t.id}.md")  # the file's own folder (AG86)
    except Exception:
        pass
    return t
