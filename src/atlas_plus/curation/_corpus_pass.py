"""One directory walk, one read and one frontmatter parse per note for the length of one report.

The curation report is seven sections, and each one walked the corpus, opened every note and parsed
its frontmatter for itself: at 20,000 notes that is 140,000 opens and 80,000 YAML loads for one answer
(SEX-046). Inside ``shared_pass()`` the helpers below remember what they returned, so the second
section to ask for a note gets the text the first one read. Outside it they are the plain calls they
replace: a command that runs one section alone behaves exactly as before and keeps nothing in memory.

The sections keep their own filters and their own way of cutting the frontmatter out of the text; only
the three expensive steps are shared. What is handed out is shared too: callers must not change a
returned list or mapping (none does; they only read).
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence, Union

from atlas_plus import _readahead
from atlas_plus._yaml import load_mapping

# Text kept per pass, in characters. A corpus above it is still read correctly: the notes past the
# limit are simply read again by each section, as before.
MAX_CACHED_CHARS = 256 * 1024 * 1024


class _Pass:
    __slots__ = ("texts", "errors", "lists", "fms", "chars", "max_chars", "reads", "parses", "walks")

    def __init__(self, max_chars: int) -> None:
        self.texts: dict[str, str] = {}
        self.errors: dict[str, OSError] = {}
        self.lists: dict[str, list[Path]] = {}
        self.fms: dict[str, Any] = {}
        self.chars = 0
        self.max_chars = max_chars
        self.reads = 0    # files actually opened
        self.parses = 0   # YAML documents actually loaded
        self.walks = 0    # directory trees actually walked


_ACTIVE: contextvars.ContextVar[Optional[_Pass]] = contextvars.ContextVar("atlas_curation_corpus_pass", default=None)


@contextmanager
def shared_pass(max_chars: int = MAX_CACHED_CHARS) -> Iterator[_Pass]:
    """Share walks, reads and frontmatter parses between every section called inside the block.
    Nested blocks join the outer one. Everything kept is dropped when the outermost block ends."""
    outer = _ACTIVE.get()
    if outer is not None:
        yield outer
        return
    cur = _Pass(max_chars)
    token = _ACTIVE.set(cur)
    try:
        yield cur
    finally:
        _ACTIVE.reset(token)
        cur.texts.clear()
        cur.errors.clear()
        cur.lists.clear()
        cur.fms.clear()


def md_files(root: Path) -> list[Path]:
    """Every ``*.md`` path under ``root``, in the order ``root.rglob`` gives them."""
    cur = _ACTIVE.get()
    if cur is None:
        return list(root.rglob("*.md"))
    key = str(root)
    got = cur.lists.get(key)
    if got is None:
        cur.walks += 1
        got = cur.lists[key] = list(root.rglob("*.md"))
    return got


def read_text(path: Path) -> str:
    """``path.read_text(encoding="utf-8", errors="replace")``; raises the same OSError when the file
    cannot be read (and raises it again, without a second attempt, for the rest of the pass)."""
    cur = _ACTIVE.get()
    if cur is None:
        return path.read_text(encoding="utf-8", errors="replace")
    key = str(path)
    text = cur.texts.get(key)
    if text is not None:
        return text
    err = cur.errors.get(key)
    if err is not None:
        raise err
    cur.reads += 1
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        cur.errors[key] = exc
        raise
    if cur.chars + len(text) <= cur.max_chars:
        cur.texts[key] = text
        cur.chars += len(text)
    return text


# Notes read ahead on threads: the pool lives in the neutral ``atlas_plus._readahead`` (the search index
# build uses it too); this module adds the shared pass's memory. Below READ_AHEAD_MIN notes the pool is not
# worth starting.
READ_THREADS = _readahead.READ_THREADS
READ_AHEAD_MIN = _readahead.READ_AHEAD_MIN
_READ_WINDOW = _readahead.READ_WINDOW  # notes handed to the pool at a time: bounds what is held when nothing is remembered


def read_texts(paths: Sequence[Path]) -> Iterator[tuple[Path, Union[str, OSError]]]:
    """``(path, text)`` for every path, in the order given; where ``read_text(path)`` would raise, the
    ``OSError`` comes back in place of the text. The files are read ahead on ``READ_THREADS`` threads
    (``atlas_plus._readahead.read_texts``). Inside a shared pass a note already read is not read again, and
    what is read here is remembered by the same rules as ``read_text`` (in the order given, so the memory
    limit cuts at the same note)."""
    cur = _ACTIVE.get()
    paths = list(paths)
    if READ_THREADS <= 1 or len(paths) < READ_AHEAD_MIN:
        for p in paths:
            try:
                yield p, read_text(p)
            except OSError as exc:
                yield p, exc
        return
    if cur is None:
        yield from _readahead.read_texts(paths, threads=READ_THREADS)
        return
    for start in range(0, len(paths), _READ_WINDOW):
        window = paths[start:start + _READ_WINDOW]
        todo = [p for p in window if str(p) not in cur.texts and str(p) not in cur.errors]
        fresh = {str(p): got for p, got in _readahead.read_texts(todo, threads=READ_THREADS)}
        for p in window:
            key = str(p)
            if key not in fresh:  # remembered by the pass (or a path given twice): the plain call answers
                try:
                    yield p, read_text(p)
                except OSError as exc:
                    yield p, exc
                continue
            got = fresh.pop(key)
            cur.reads += 1
            if isinstance(got, OSError):
                cur.errors[key] = got
            elif cur.chars + len(got) <= cur.max_chars:
                cur.texts[key] = got
                cur.chars += len(got)
            yield p, got


def prefetch(paths: Sequence[Path]) -> None:
    """Inside a shared pass: read every path not yet read, on threads, so the ``read_text`` calls that
    follow find the text. Outside a pass nothing would be kept, so nothing is read."""
    if _ACTIVE.get() is None:
        return
    for _ in read_texts(paths):
        pass


def load_frontmatter(block: str) -> Any:
    """``fast_safe_load(block)`` for the frontmatter text a section cut out of a note. The result is
    remembered by the text itself (less its leading line breaks), so sections that cut the same block
    share one load. A block that does not parse raises as before and is not remembered."""
    cur = _ACTIVE.get()
    if cur is None:
        return load_mapping(block)
    # Blank lines before the first key mean nothing to YAML, and they are the whole difference between
    # the two cuts the sections make ("\ntitle: x" and "title: x"): one key, one load for both.
    key = block.lstrip("\r\n")
    try:
        return cur.fms[key]
    except KeyError:
        pass
    cur.parses += 1
    value = cur.fms[key] = load_mapping(key)
    return value
