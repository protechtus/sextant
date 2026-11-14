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
        os.replace(tmp, path)
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


_MARKER_RE_TMPL = r"<!-- session:{sid}(?: turns:(\d+))? -->"


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
            return f, int(m.group(1) or 0)
    return None


def remove_session_entry(text: str, marker_prefix: str) -> str:
    """Drop the `## ...` entry that holds ``marker_prefix`` (from its heading to the next heading).
    The prefix must end the id (`<!-- session:abc` never matches `abc-def`: AG44)."""
    import re as _re
    m = _re.search(_re.escape(marker_prefix) + r"(?= )", text)
    i = m.start() if m else -1
    if i == -1:
        return text
    start = text.rfind("\n## ", 0, i)
    start = 0 if start == -1 else start + 1
    if start == 1 and not text.startswith("## "):
        start = 0
    end = text.find("\n## ", i)
    end = len(text) if end == -1 else end + 1
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
            _atomic_write(f, new if new.strip() else "")


def remove_updates_entry(text: str, marker_prefix: str) -> str:
    """Drop the updates.txt entry (a blank-line-separated block) that holds ``marker_prefix``."""
    import re as _re
    m = _re.search(_re.escape(marker_prefix) + r"(?= )", text)
    if not m:
        return text
    i = m.start()
    start = text.rfind("\n\n", 0, i)
    start = 0 if start == -1 else start + 2
    end = text.find("\n\n", i)
    end = len(text) if end == -1 else end + 2
    return text[:start] + text[end:]


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

    # version-before-edit
    _version_file(session_file)
    _version_file(UPDATES_TXT)

    # write session log (prepend if file exists, else create)
    MEMORY_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    if session_file.exists():
        existing = session_file.read_text(encoding="utf-8")
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
