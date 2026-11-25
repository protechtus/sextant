"""Read many files ahead on threads, handing the texts back in the order asked.

Opening a file that is not in the system's cache costs milliseconds (on Windows it is scanned on open:
7.4 ms a note measured, 150 s for 20,000 notes one by one); the wait is outside the interpreter lock, so
eight readers bring it to about 1.2 ms a note. A corpus already in the cache reads no slower this way.
Below READ_AHEAD_MIN files the pool is not worth starting. Used by the curation report
(``curation/_corpus_pass``) and the search index build (``mcp/memory_server/fts``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence, Union

READ_THREADS = 8
READ_AHEAD_MIN = 64
READ_WINDOW = 512  # files handed to the pool at a time: bounds the texts held at once


def read_or_error(path: Path, encoding: str = "utf-8") -> Union[str, OSError]:
    """``path.read_text(encoding=encoding, errors="replace")``, or the OSError it raised."""
    try:
        return path.read_text(encoding=encoding, errors="replace")
    except OSError as exc:
        return exc


def read_texts(paths: Sequence[Path], encoding: str = "utf-8", threads: Optional[int] = None,
               read: Optional[Callable[[Path], Union[str, OSError]]] = None,
               ) -> Iterator[tuple[Path, Union[str, OSError]]]:
    """``(path, text)`` for every path, in the order given; where reading raises OSError, the error comes
    back in place of the text (nothing is raised). ``read`` replaces the plain reader (it must return the
    error, not raise it). The files are read ahead on ``threads`` threads (default READ_THREADS)."""
    reader = read or (lambda p: read_or_error(p, encoding))
    paths = list(paths)
    n_threads = READ_THREADS if threads is None else threads
    if n_threads <= 1 or len(paths) < READ_AHEAD_MIN:
        for p in paths:
            yield p, reader(p)
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=n_threads, thread_name_prefix="atlas-read") as pool:
        for start in range(0, len(paths), READ_WINDOW):
            window = paths[start:start + READ_WINDOW]
            yield from zip(window, pool.map(reader, window))
