"""P2-T6: Render and write session log + updates.txt with version-before-edit."""

from __future__ import annotations

import os
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

from atlas_plus.autonomous import is_session_autonomous
from atlas_plus import paths as atlas_paths

MEMORY_SESSIONS_DIR = atlas_paths.memory_root() / "sessions"
UPDATES_TXT = atlas_paths.updates_file()
VERSIONS_DIR = atlas_paths.memory_root() / "versions" / "sessions"


def _atomic_write(path: Path, text: str) -> None:
    """tmp-and-replace with fsync: a crash never leaves a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        import time as _time
        for _attempt in range(6):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if _attempt == 5:
                    raise
                _time.sleep(0.05 * (_attempt + 1))  # a scanner holding the file for a moment (Windows sharing violation: MN51)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _version_file(path: Path) -> Path | None:
    """Copy path to versions/<name>_YYYY-MM-DD_vN.ext. Returns version path."""
    if not path.exists():
        return None
    date_str = date.today().isoformat()
    stem = path.stem
    suffix = path.suffix
    # Session logs version into the corpus; the updates file versions BESIDE
    # itself (the state dir by default) -- never into ~/versions.
    dest_dir = VERSIONS_DIR if path.is_relative_to(MEMORY_SESSIONS_DIR) else path.parent / "versions"
    dest_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        dest = dest_dir / f"{stem}_{date_str}_v{n}{suffix}"
        if not dest.exists():
            break
        n += 1
    shutil.copy2(path, dest)
    return dest


_MARKER_RE_TMPL = r"<!-- session:{sid}(?: turns:(\d+))?(?: partial:(\d+))? -->"  # `partial:N` = a chunk was rejected N times: retried at the next Stop while N < 3 (MN49: an unsummarisable chunk was retried for ever)
_PARTIAL_RETRIES = 3


def find_session_entry(sessions_dir: Path, safe_id: str, days: int = 3):
    """(file, turns) of this session's newest entry in the newest ``days`` dated logs, or None
    (MN33: a Stop after local midnight summarised the session a second time)."""
    try:
        dated = sorted(sessions_dir.glob("20[0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"))[-days:]
    except OSError:
        return None
    import re as _re
    pat = _re.compile(_MARKER_RE_TMPL.format(sid=_re.escape(safe_id)))
    for f in reversed(dated):
        try:
            m = pat.search(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if m:
            _partial = int(m.group(2) or 0)
            return f, (0 if 0 < _partial < _PARTIAL_RETRIES else int(m.group(1) or 0))  # a partial entry is retried at the next Stop, at most twice without new turns (MN49, MN51)
    return None


def remove_session_entry(text: str, marker_prefix: str) -> str:
    """Drop the entry whose `## ...` heading is IMMEDIATELY followed by this session's marker line
    (the shape write_outputs writes). A marker quoted anywhere else, in a code block or a hand-written
    section, is left alone (MN37: a user's notes that quoted the marker were deleted). The prefix must
    end the id (`<!-- session:abc` never matches `abc-def`: AG44)."""
    import re as _re
    pat = _re.compile(r"^## [^\n]*\n" + _re.escape(marker_prefix) + r"(?: turns:\d+)?(?: partial:\d+)? -->\n", _re.M)
    m = None
    for cand in pat.finditer(text):
        # a match inside a fenced code block is a user's quotation, not the entry (AG50: the quoted pair was cut)
        if not _inside_fence(text, cand.start()):  # a real fence tracker (MN43, AG60: parity counting let `~~~` inside a backtick block hide the entry)
            m = cand
            break
    if not m:
        return text
    start = m.start()
    end = text.find("\n## ", m.end())
    end = len(text) if end == -1 else end + 1
    # the entry written since MN49 ends with `<!-- end session:ID -->`: removal stops there, so a line the user typed
    # under the entry survives the refresh (it was deleted with the entry: MN49, MN51)
    _sid = marker_prefix.split("session:", 1)[1] if "session:" in marker_prefix else ""
    if _sid:
        _em = _re.compile(r"^<!-- end session:" + _re.escape(_sid) + r" -->[ \t]*\n?", _re.M)
        _m2 = _em.search(text, m.end())  # the end marker wins over a quoted `## ` line inside the body (AG64: the body's heading cut the entry short and left an orphan tail)
        if _m2:
            end = _m2.end()
    return (text[:start] + text[end:]).lstrip("\n")


def _remove_entry_elsewhere(current: Path, marker_prefix: str, days: int = 3) -> None:
    """Drop this session's entry from the other recent dated logs (a session summarised yesterday
    and refreshed after local midnight moves to today's log instead of appearing twice: AG43)."""
    try:
        dated = sorted(MEMORY_SESSIONS_DIR.glob("20[0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"))[-days:]
    except OSError:
        return
    for f in dated:
        if f.resolve() == current.resolve():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        new = remove_session_entry(text, marker_prefix)
        if new != text:
            _version_file(f)
            if new.strip():
                _atomic_write(f, new)
            else:
                try:
                    f.unlink()  # AG58: the entry was the file's only content and moved to the new day; a 0-byte dated log is noise (the version copy keeps it)
                except OSError:
                    _atomic_write(f, "")


def _day_log_unmerged(session_file) -> bool:
    """True when git holds the day log as unmerged (the corpus is a repo and the file is in `ls-files -u`)."""
    from atlas_plus import gitsafe as _gs
    root = Path(session_file).parent.parent
    if not (root / ".git").exists():
        return False
    try:
        rel = Path(session_file).resolve().relative_to(root.resolve()).as_posix()
        out = _gs.run(["-c", "core.quotePath=false", "ls-files", "-u", "-z", "--", rel], root, timeout=5).stdout  # gitsafe (AG61: a raw call ran the repo's core.fsmonitor)
    except Exception:  # noqa: BLE001
        return False
    return bool(out.strip())


def _inside_fence(text: str, pos: int) -> bool:
    """True when `pos` lies inside a CommonMark code fence. A fence opens with three or more backticks or tildes
    indented at most three spaces and closes only on the same character with at least the opening length; a
    tab-indented line is indented code, not a fence (AG60)."""
    import re as _re
    open_ch, open_len = "", 0
    for m in _re.finditer(r"(?m)^ {0,3}(`{3,}|~{3,})", text[:pos]):
        run = m.group(1)
        if not open_ch:
            open_ch, open_len = run[0], len(run)
        elif run[0] == open_ch and len(run) >= open_len:
            open_ch, open_len = "", 0
    return bool(open_ch)


def remove_updates_entry(text: str, marker_prefix: str) -> str:
    """Drop the updates.txt entry whose first line is followed by this session's marker line. The
    entry ends at the next blank line OR the next line that starts a new entry (a date, a heading),
    so hand-written lines appended without a blank line survive (MN37)."""
    import re as _re
    pat = _re.compile(r"^[^\n]+\n" + _re.escape(marker_prefix) + r"(?: turns:\d+)?(?: partial:\d+)? -->\n", _re.M)
    m = pat.search(text)
    if not m:
        return text
    start = m.start()
    end = m.end()
    first = True
    _lines = text[m.end():].splitlines(keepends=True)
    # AG58: the entry's LAST line is `<!-- end session:ID -->` (written since this fix). When it is there the
    # entry ends exactly at it, so an indented line a user typed straight after the last item survives;
    # entries written before have no end line and keep the heuristics below.
    _sid = marker_prefix.split("session:", 1)[-1]
    _end_re = _re.compile(r"^<!-- end session:" + _re.escape(_sid) + r" -->\s*$")
    for _j, _ln in enumerate(_lines):
        if not _ln.strip():
            break
        if _end_re.match(_ln):
            _tail = sum(len(x) for x in _lines[:_j + 1])
            if _j + 1 < len(_lines) and not _lines[_j + 1].strip():
                _tail += len(_lines[_j + 1])  # the blank separator goes with the entry
            return text[:start] + text[m.end() + _tail:]
    for _i, line in enumerate(_lines):
        if not line.strip():
            end += len(line)  # the blank separator goes with the entry
            break
        _next = _lines[_i + 1] if _i + 1 < len(_lines) else ""
        if not first and _re.match(r"^-{5,}\s*$", line) and (_next.startswith("<!-- session:") or _re.match(r"^\d{4}-\d{2}-\d{2} \| ", _next)):
            break  # the next entry's opening rule, with no blank line between (MN43: it was eaten and the entry could never be matched again)
        if not first and _re.match(r"^\d{4}-\d{2}-\d{2} \| ", line):
            break  # a second `date | title` line is another entry (AG50)
        first = False
        # the rendered entry (templates/updates_entry.txt.j2): a dashed rule, `date | title`, a dashed rule,
        # `COMPLETED:`/`FILES:`/`NEXT:` section lines and indented items; anything else is not ours
        if not _re.match(r"^(?:-{5,}\s*$|\d{4}-\d{2}-\d{2} \| |(?:COMPLETED|FILES|NEXT):\s*$|[ \t]+\S)", line):  # the template's three sections only; a hand-written `NOTES:` is not ours (AG49)
            break  # a hand-written line, the next entry's date line, a heading: not ours
        end += len(line)
    return text[:start] + text[end:]


def partial_count(sessions_dir: Path, safe_id: str, days: int = 3) -> int:
    """How many Stops in a row have written this session's entry as partial (0 when the entry is clean or absent)."""
    import re as _re
    try:
        dated = sorted(sessions_dir.glob("20[0-9][0-9]-[0-9][0-9]-[0-9][0-9].md"))[-days:]
    except OSError:
        return 0
    pat = _re.compile(_MARKER_RE_TMPL.format(sid=_re.escape(safe_id)))
    for f in reversed(dated):
        try:
            m = pat.search(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if m:
            return int(m.group(2) or 0)
    return 0


def _write_lock(timeout: float = 60.0, stale: float = 120.0):
    """AG58: one writer at a time across SESSIONS (`<state>/summarizer/write.lock`). Four sessions stopping
    together each read the day's log, prepended and replaced it: one died on a Windows PermissionError with its
    output discarded (no event) and the others could overwrite each other's entry."""
    import contextlib
    import random
    import time as _time

    @contextlib.contextmanager
    def _cm():
        lock = atlas_paths.state_dir() / "summarizer" / "write.lock"
        held = False
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            deadline = _time.time() + timeout
            while True:
                try:
                    _fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(_fd, str(os.getpid()).encode())  # the owner's pid: only the owner releases it (AG61: a waiter's lock was deleted by the previous holder's finally)
                    os.close(_fd)
                    held = True
                    break
                except FileExistsError:
                    try:
                        if _time.time() - lock.stat().st_mtime > stale:
                            lock.unlink()
                            continue
                    except OSError:
                        pass
                    if _time.time() > deadline:
                        # never write unlocked (AG64: a waiter broke a slow holder's lock and the holder's stale copy then erased the
                        # waiter's entry); a lock older than `stale` is broken above, a younger one means a live writer: this Stop's
                        # entry is skipped and retried at the next Stop (the chunk cache keeps every summary)
                        raise RuntimeError(f"another summarizer run holds {lock} (broken once it is {int(stale)} s old); this entry is retried at the next Stop")
                    _time.sleep(0.05 + random.random() * 0.1)
        except OSError:
            pass
        try:
            yield
        finally:
            if held:
                try:
                    if lock.read_text(encoding="ascii", errors="replace").strip() in ("", str(os.getpid())):
                        lock.unlink()
                except OSError:
                    pass
    return _cm()


def write_outputs(
    session_log_text: str,
    updates_text: str,
    date: str,
    dry_run: bool = False,
    session_marker: str = "",
    replace_session: str = "",
) -> dict[str, str]:
    """Write session log and updates.txt. Returns paths written (or `skipped` when the session's
    marker is already in today's log: a re-fired Stop hook must not write twice, MN28)."""
    if not dry_run and not is_session_autonomous():
        raise RuntimeError(
            "Session log write blocked. Set ATLAS_SESSION_AUTONOMOUS=on or ATLAS_AUTONOMOUS=on."
        )

    session_file = MEMORY_SESSIONS_DIR / f"{date}.md"
    result: dict[str, str] = {}
    if session_marker and session_file.exists():
        try:
            if session_marker in session_file.read_text(encoding="utf-8", errors="replace"):
                return {"skipped": f"already written for this session in {session_file.name}"}
        except OSError:
            pass

    if dry_run:
        result["session_log"] = str(session_file)
        result["updates_txt"] = str(UPDATES_TXT)
        return result

    _prev = session_file.read_text(encoding="utf-8", errors="replace") if session_file.exists() else None
    try:
        with _write_lock():
            return _write_locked(session_file, session_log_text, updates_text, replace_session, result)
    except Exception:
        # the day log and updates.txt move together: a failed updates write had left the day-log entry in place, so the
        # next Stop found the session "done" and the updates entry stayed missing (AG64)
        try:
            if _prev is None:
                session_file.unlink(missing_ok=True)
            elif session_file.read_text(encoding="utf-8", errors="replace") != _prev:
                _atomic_write(session_file, _prev)
        except Exception:  # noqa: BLE001
            pass
        raise


def _write_locked(session_file, session_log_text, updates_text, replace_session, result):
    # version-before-edit
    _version_file(session_file)
    _version_file(UPDATES_TXT)

    # write session log (prepend if file exists, else create)
    MEMORY_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    if session_file.exists():
        existing = session_file.read_text(encoding="utf-8")
        from atlas_plus.mcp.memory_server.routing import has_conflict_markers as _hcm
        if _hcm(existing):
            raise RuntimeError(f"{session_file.name} holds git conflict markers (two people summarised the same day): resolve the merge first; this summary was not written")  # the writer wrote above the markers (AG57)
        if _day_log_unmerged(session_file):
            raise RuntimeError(f"{session_file.name} is unmerged in git (resolve it and `git add` it first); this summary was not written")  # markers hand-deleted but not staged (MN53)
        if replace_session:
            # Claude Code fires Stop after every reply, so the session's entry is refreshed in place
            # when it grew (MN33: the first summary froze and every later Stop was skipped)
            existing = remove_session_entry(existing, replace_session)
            _remove_entry_elsewhere(session_file, replace_session)
        _atomic_write(session_file, session_log_text + ("\n\n" + existing if existing.strip() else "\n"))
    else:
        if replace_session:
            _remove_entry_elsewhere(session_file, replace_session)  # a Stop after midnight keeps ONE entry (AG43)
        _atomic_write(session_file, session_log_text)
    result["session_log"] = str(session_file)

    # prepend to updates.txt
    if UPDATES_TXT.exists():
        header_end = UPDATES_TXT.read_text(encoding="utf-8", errors="replace")
        if replace_session:
            header_end = remove_updates_entry(header_end, replace_session)  # the updates entry is refreshed too (AG43: six stale entries after one day)
        # find end of header block (first blank line after the header)
        lines = header_end.splitlines(keepends=True)
        insert_at = 0
        for i, line in enumerate(lines):
            if line.strip().startswith("===="):
                insert_at = i + 1
                break
        # one blank line between entries, every time (MN28: later entries ran together)
        new_content = (
            "".join(lines[: insert_at])
            + "\n"
            + updates_text.rstrip("\n") + "\n\n"
            + "".join(lines[insert_at:]).lstrip("\n")
        )
        _atomic_write(UPDATES_TXT, new_content)
    else:
        _atomic_write(UPDATES_TXT, updates_text)
    result["updates_txt"] = str(UPDATES_TXT)

    return result
