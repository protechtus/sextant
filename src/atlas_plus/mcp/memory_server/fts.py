"""SQLite FTS5 index over .memory/ for memory.search.

Index lives at <ATLAS_STATE_DIR>/search.sqlite (its own file since 2026-11-09), table memory_fts.
Auto-rebuilds when any .memory/ file is newer than the index timestamp.

Re-ranking blends three signals (all normalised 0-1):
  BM25 relevance   -- FTS5 rank, 65% weight
  mtime recency    -- file modification age vs 1-year window, 20% weight
  confirmation age -- last_confirmed frontmatter field vs 1-year window, 15% weight

Scope authority multiplier: 1.2x for feedback/ and identity/ files.
"""

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .config import EXCLUDED_DIRS, FTS_DB, MEMORY_ROOT

log = logging.getLogger("atlas.mcp.fts")

_CREATE_META = """
CREATE TABLE IF NOT EXISTS atlas_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# unicode61 folds case and, with remove_diacritics, accents: 'zurich' finds "Zürich" (AG8,
# sweep 10; the old 'porter ascii' tokenizer did not). A changed tokenizer is detected at
# connect time and the index is rebuilt, since the virtual table bakes the tokenizer in.
_TOKENIZER = "porter unicode61 remove_diacritics 2"
_TOKENIZER_FALLBACK = "porter unicode61 remove_diacritics 1"   # SQLite before 3.27
_CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts
USING fts5(path, frontmatter_json, body, tokenize='{tokenizer}');
"""

_CREATE_FILES = """
CREATE TABLE IF NOT EXISTS memory_files (
    path            TEXT PRIMARY KEY,
    mtime           REAL,
    indexed         REAL,
    last_confirmed  REAL
);
"""

_MIGRATE_LAST_CONFIRMED = """
ALTER TABLE memory_files ADD COLUMN last_confirmed REAL;
"""

# Decay window for last_confirmed scoring: confirmed within 30d = full boost
_CONFIRMED_FULL_BOOST_SECS = 30 * 24 * 3600
# Confirmed older than 1y = no boost (neutral 0.5 used for unconfirmed)
_CONFIRMED_MAX_AGE_SECS = 365 * 24 * 3600
# Unconfirmed files get a neutral score so they are not penalised
_CONFIRMED_NEUTRAL = 0.5


def _parse_confirmed_ts(fm: dict) -> float | None:
    """Return unix timestamp for last_confirmed frontmatter field, or None."""
    raw = fm.get("last_confirmed")
    if not raw:
        return None
    try:
        s = str(raw)[:10]  # accept full ISO or YYYY-MM-DD
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _confirmed_score(last_confirmed_ts: float | None, now_ts: float) -> float:
    """Map last_confirmed timestamp to a 0-1 contribution score.

    None (no last_confirmed) -> _CONFIRMED_NEUTRAL (no penalty, no boost)
    Confirmed within 30d     -> 1.0
    Confirmed between 30d-1y -> linear decay from 1.0 to 0.0
    Confirmed >1y ago        -> 0.0
    """
    if last_confirmed_ts is None:
        return _CONFIRMED_NEUTRAL
    age_secs = now_ts - last_confirmed_ts
    if age_secs <= 0:
        return 1.0
    if age_secs <= _CONFIRMED_FULL_BOOST_SECS:
        return 1.0
    if age_secs >= _CONFIRMED_MAX_AGE_SECS:
        return 0.0
    # Linear decay in [30d, 1y] range
    span = _CONFIRMED_MAX_AGE_SECS - _CONFIRMED_FULL_BOOST_SECS
    return 1.0 - (age_secs - _CONFIRMED_FULL_BOOST_SECS) / span


def fts5_available() -> bool:
    """Whether this interpreter's SQLite carries the FTS5 module keyword search needs
    (AG8, sweep 10: without it every search silently returned nothing)."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        return False


def _connect() -> sqlite3.Connection:
    FTS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(FTS_DB), timeout=30)
    # If setup raises (a trashed file fails at the WAL pragma, which reads
    # the header), CLOSE before re-raising: sqlite3.connect() has already
    # opened the file, the caller never receives the object, and on Windows
    # the leaked handle blocks the quarantine rename with WinError 32
    # (found 2026-10-09 hardening the file-level-corruption path).
    try:
        # busy_timeout FIRST so the journal_mode switch waits on a lock
        # instead of failing instantly with "database is locked".
        conn.execute("PRAGMA busy_timeout=30000")  # embed-index bulk writes vs interactive search
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_CREATE_META)
        conn.execute(_CREATE_FILES)
        row = conn.execute("SELECT value FROM atlas_meta WHERE key='tokenizer'").fetchone()
        if row is None or row[0] not in (_TOKENIZER, _TOKENIZER_FALLBACK):
            # First run on an index built with another tokenizer: drop it and forget every
            # file's mtime so the next update_incremental rebuilds from the corpus.
            conn.execute("DROP TABLE IF EXISTS memory_fts")
            conn.execute("DELETE FROM memory_files")
            chosen = _TOKENIZER
            try:
                conn.execute(_CREATE_FTS.format(tokenizer=_TOKENIZER))
            except sqlite3.OperationalError as exc:
                if "no such module" in str(exc).lower():
                    raise
                chosen = _TOKENIZER_FALLBACK
                conn.execute(_CREATE_FTS.format(tokenizer=_TOKENIZER_FALLBACK))
            conn.execute("INSERT OR REPLACE INTO atlas_meta(key,value) VALUES ('tokenizer',?)", (chosen,))
        else:
            conn.execute(_CREATE_FTS.format(tokenizer=row[0]))
        # Safe migration: add last_confirmed column if it doesn't exist yet
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_files)")}
        if "last_confirmed" not in existing_cols:
            conn.execute(_MIGRATE_LAST_CONFIRMED)
        if "size" not in existing_cols:
            conn.execute("ALTER TABLE memory_files ADD COLUMN size INTEGER")
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def _walk_memory():
    """Yield (relative_path_str, absolute_path) for every .md file in MEMORY_ROOT.

    Skips EXCLUDED_DIRS and the reference/research mirror -- that subtree mirrors the
    standalone research library (~54% of the corpus) and has its own dedicated RAG
    (research_search_tool), so indexing it into the general memory FTS only dilutes
    results and bloats the rebuild. (Single source of truth: curation.scan.)
    """
    from atlas_plus.curation.scan import is_excluded_mirror
    for p in MEMORY_ROOT.rglob("*.md"):
        parts = p.relative_to(MEMORY_ROOT).parts
        if parts and (parts[0] in EXCLUDED_DIRS or is_excluded_mirror(parts)):
            continue
        if p.name == "_README.md":  # sextant-init scaffolding notes, not memories (persona pass 2026-11-10)
            continue
        yield str(p.relative_to(MEMORY_ROOT)).replace("\\", "/"), p


def _walk_memory_stats():
    """Yield (relative_path_str, mtime, size) for every indexed .md file.

    The freshness-sweep fast path (quality loop v2 run 1): ``rglob`` +
    ``Path.stat()`` costs one extra syscall per file and descends into
    excluded subtrees before filtering; ``os.scandir`` entries carry
    ``st_mtime`` for free (Windows fills it from the directory read) and
    excluded directories are PRUNED, never entered. Measured at 2,000
    files: sweep 219.6 ms -> see commit message for the after number.
    Same exclusion semantics as ``_walk_memory`` (parity-tested).
    """
    from atlas_plus.curation.scan import is_excluded_mirror
    # The relative path rides the stack as a prefix: the previous version
    # called os.path.relpath() once per directory ENTRY (including non-.md
    # files), and relpath's normalization dominated the sweep -- 6,756 calls
    # / 141ms of the 244ms walk on a 2,220-file corpus (profiled 2026-10-09).
    # Non-.md files are now skipped before any path work at all.
    stack: list[tuple[str, str]] = [(str(MEMORY_ROOT), "")]
    while stack:
        d, prefix = stack.pop()
        try:
            entries = os.scandir(d)
        except OSError:
            continue
        with entries:
            for entry in entries:
                name = entry.name
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if not is_dir and not name.endswith(".md"):
                    continue
                rel = prefix + name
                parts = rel.split("/")
                if parts[0] in EXCLUDED_DIRS or is_excluded_mirror(parts):
                    continue
                if not is_dir and name == "_README.md":
                    continue
                if is_dir:
                    stack.append((entry.path, rel + "/"))
                else:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    yield rel, st.st_mtime, st.st_size


def _needs_rebuild() -> bool:
    if not FTS_DB.exists():
        return True
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM atlas_meta WHERE key='last_indexed'"
        ).fetchone()
        if not row:
            return True
        last_indexed = float(row[0])
        for _, mtime, _size in _walk_memory_stats():
            if mtime > last_indexed:
                return True
        return False
    finally:
        conn.close()


_rebuild_lock = threading.Lock()


def _parse_md(raw: str) -> tuple[str, str, float | None]:
    """Split a memory file into (frontmatter_json, body, confirmed_ts)."""
    frontmatter_json = "{}"
    body = raw
    confirmed_ts = None
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            fm_str = raw[3:end].strip()
            body = raw[end + 4:].strip()
            try:
                fm = yaml.safe_load(fm_str) or {}
                confirmed_ts = _parse_confirmed_ts(fm)
            except Exception:
                fm = {}
            try:
                frontmatter_json = json.dumps(fm, default=str)
            except Exception:
                pass
    return frontmatter_json, body, confirmed_ts


def update_incremental() -> int:
    """Bring the index up to date by touching ONLY changed files.

    Diffs the filesystem against memory_files by mtime: new and modified
    files are (re)indexed, deleted files are dropped, untouched files are
    never re-read. Returns the number of rows changed (0 = already fresh).

    This is the search-path freshness mechanism (ROADMAP item 4): the old
    path ran a FULL rebuild whenever one file changed, which is the
    documented cliff at tens-of-thousands-of-files corpus size. The full
    rebuild() remains for the CLI / recovery path.
    """
    # Empty/missing index falls back to the full rebuild -- checked OUTSIDE
    # the lock (rebuild() takes the same non-reentrant lock).
    conn = _connect()
    try:
        empty = conn.execute("SELECT COUNT(*) FROM memory_files").fetchone()[0] == 0
    finally:
        conn.close()
    if empty:
        return max(rebuild(force=True), 0)

    with _rebuild_lock:
        conn = _connect()
        try:
            db_state = {
                row[0]: (row[1], row[2])
                for row in conn.execute("SELECT path, mtime, size FROM memory_files")
            }
            now = time.time()
            changed = 0
            seen: set[str] = set()
            for rel, mtime, size in _walk_memory_stats():
                seen.add(rel)
                # (mtime, size) pair: mtime alone misses a same-mtime rewrite
                # on coarse-timestamp mounts (SMB, some Docker volumes) --
                # expert review 2026-09-27. Size catches the common case;
                # a same-size same-mtime rewrite remains theoretically
                # invisible until the next real change.
                if db_state.get(rel) == (mtime, size):
                    continue
                try:
                    raw = (MEMORY_ROOT / rel).read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                frontmatter_json, body, confirmed_ts = _parse_md(raw)
                conn.execute("DELETE FROM memory_fts WHERE path=?", (rel,))
                conn.execute(
                    "INSERT INTO memory_fts(path, frontmatter_json, body) VALUES (?,?,?)",
                    (rel, frontmatter_json, body),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO memory_files(path, mtime, indexed, last_confirmed, size)"
                    " VALUES (?,?,?,?,?)",
                    (rel, mtime, now, confirmed_ts, size),
                )
                changed += 1
            for rel in set(db_state) - seen:
                conn.execute("DELETE FROM memory_fts WHERE path=?", (rel,))
                conn.execute("DELETE FROM memory_files WHERE path=?", (rel,))
                changed += 1
            if changed:
                conn.execute(
                    "INSERT OR REPLACE INTO atlas_meta(key,value) VALUES ('last_indexed',?)",
                    (str(now),),
                )
                conn.commit()
                log.info("FTS incremental update: %d row(s) changed", changed)
            return changed
        finally:
            conn.close()


# Messages sqlite emits for a damaged FTS5 index or file, across versions: 3.42 reports
# trashed shadow rows as "vtable constructor failed: memory_fts" where 3.50 says "malformed"
# (CI matrix 2026-11-09). A LOCKED/busy database matches none of these and is never corrupt.
_CORRUPTION_MARKERS = ("malformed", "not a database", "vtable constructor failed",
                       "database disk image", "fts5: corrupt", "no such table: memory_fts")


def _is_corruption(exc: sqlite3.Error) -> bool:
    """True only for genuine corruption. A LOCKED/busy database is never corrupt."""
    msg = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError) and ("locked" in msg or "busy" in msg):
        return False
    return any(marker in msg for marker in _CORRUPTION_MARKERS)


def _heal_malformed() -> bool:
    """Recover from FTS5 shadow-table corruption (run 39, found live).

    Drop + recreate the FTS tables in place -- this preserves co-tenant
    tables in the same DB file (memory_vectors) -- then rebuild from the
    corpus. If even DROP fails, the FILE is corrupt: quarantine it
    (rename, never delete -- archive-not-delete) and rebuild fresh,
    sacrificing co-tenants (they are derived too; embed-index refills).
    Returns True when a rebuild completed.
    """
    try:
        conn = _connect()
        try:
            conn.execute("DROP TABLE IF EXISTS memory_fts")
            conn.execute("DROP TABLE IF EXISTS memory_files")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            FTS_DB.rename(FTS_DB.with_name(FTS_DB.name + f".corrupt-{stamp}"))
            log.error("FTS DB quarantined as %s.corrupt-%s (file-level corruption)",
                      FTS_DB.name, stamp)
            # Keep the three newest quarantines; they accumulated forever (MN3, 2026-11-11).
            olds = sorted(FTS_DB.parent.glob(FTS_DB.name + ".corrupt-*"))
            for stale in olds[:-3]:
                try:
                    stale.unlink()
                except OSError:
                    pass
        except OSError as exc:
            log.error("could not quarantine corrupt FTS DB: %s", exc)
            return False
    try:
        rebuild(force=True)
        return True
    except sqlite3.DatabaseError as exc:
        log.error("rebuild after corruption failed: %s", exc)
        return False


def rebuild(force: bool = False) -> int:
    """Rebuild the FTS index. Returns count of indexed files."""
    if not force and not _needs_rebuild():
        return -1  # no-op

    with _rebuild_lock:
        # Re-check after acquiring lock -- another thread may have rebuilt while we waited
        if not force and not _needs_rebuild():
            return -1

        conn = _connect()
        try:
            conn.execute("DELETE FROM memory_fts")
            conn.execute("DELETE FROM memory_files")
            count = 0
            now = time.time()
            for rel, abs_path in _walk_memory():
                try:
                    raw = abs_path.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue

                frontmatter_json, body, confirmed_ts = _parse_md(raw)

                conn.execute(
                    "INSERT INTO memory_fts(path, frontmatter_json, body) VALUES (?,?,?)",
                    (rel, frontmatter_json, body),
                )
                st = abs_path.stat()
                conn.execute(
                    "INSERT OR REPLACE INTO memory_files(path, mtime, indexed, last_confirmed, size)"
                    " VALUES (?,?,?,?,?)",
                    (rel, st.st_mtime, now, confirmed_ts, st.st_size),
                )
                count += 1

            conn.execute(
                "INSERT OR REPLACE INTO atlas_meta(key,value) VALUES ('last_indexed',?)",
                (str(now),),
            )
            conn.commit()
            log.info("FTS index rebuilt: %d files indexed into %s", count, FTS_DB)
            return count
        finally:
            conn.close()


def main() -> None:
    """Console entry point for ``atlas-fts-rebuild``.

    The bare ``rebuild()`` is unsuitable as an entry point: console_scripts run
    ``sys.exit(func())``, and rebuild() returns the indexed-file COUNT (a nonzero
    "failure" exit code) -- or -1 (a no-op) when the index is already fresh. This
    wrapper forces a full rebuild by default, prints the count, and exits 0.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog=__import__("atlas_plus.branding", fromlist=["prog"]).prog("atlas-fts-rebuild"),
        description="Rebuild the .memory FTS index (full rebuild by default).",
    )
    parser.add_argument(
        "--if-stale", action="store_true",
        help="only rebuild when the index is out of date (default: force a full rebuild)",
    )
    args = parser.parse_args()

    count = rebuild(force=not args.if_stale)
    if count == -1:
        print("FTS index already fresh -- nothing to do.")
    else:
        print(f"FTS index rebuilt: {count} files indexed into {FTS_DB}")


# Freshness debounce (measured, quality loop run 27): the incremental sweep
# is a full scandir mtime walk and cost 130ms of every 200ms search at ~2,200
# files -- paid again by every query in a burst. Within this window, skip the
# sweep; mark_dirty() (called by every in-process corpus write path) forces
# the next search to sweep, so write-then-search never misses its own write.
# Cross-process writers are covered by the window's expiry (worst case:
# results up to ATLAS_FTS_FRESHNESS_WINDOW seconds stale, default 10).
#
# Expert pass 2026-10-05 hardening: a GENERATION counter instead of zeroing
# the timestamp -- a sweep that finishes after a concurrent mark_dirty()
# could otherwise stamp the window and hide that write for a full window
# (searches run in threads via the web app's to_thread). The sweep captures
# the generation BEFORE sweeping and only counts as covering up to it.
# _last_fresh_sweep starts at -inf (0.0 skipped the first sweep when the
# process started within `window` seconds of boot -- monotonic() is small).
_last_fresh_sweep: float = float("-inf")
_dirty_gen: int = 0
_swept_gen: int = -1


def _freshness_window() -> float:
    raw = os.environ.get("ATLAS_FTS_FRESHNESS_WINDOW", "10")
    try:
        val = float(raw)
    except ValueError:
        return 10.0
    if val != val:  # NaN
        return 10.0
    return min(max(0.0, val), 3600.0)  # cap: 'inf' must not disable freshness


def mark_dirty() -> None:
    """Force the next search to run the freshness sweep (in-process writes)."""
    global _dirty_gen
    _dirty_gen += 1


def search(query: str, scope: str = "", limit: int = 10,
           _healed: bool = False, _fallback_stage: int = 0) -> list[dict]:
    """FTS search. Returns list of {path, snippet, score}."""
    # Freshness on search: incremental -- only changed files are
    # re-read/re-indexed (a stat sweep when nothing changed). The old
    # full-rebuild-on-any-change was the large-corpus cliff (ROADMAP 4).
    # Fail open (expert review 2026-09-27): if another process holds the DB
    # past busy_timeout, serve the possibly-slightly-stale index instead of
    # letting OperationalError escape a SEARCH call.
    global _last_fresh_sweep, _swept_gen
    now = time.monotonic()
    if _swept_gen != _dirty_gen or now - _last_fresh_sweep >= _freshness_window():
        gen_before = _dirty_gen
        try:
            update_incremental()
            _last_fresh_sweep = time.monotonic()
            _swept_gen = gen_before
        except sqlite3.OperationalError as exc:
            log.warning("freshness update skipped (db busy): %s -- serving current index", exc)
        except sqlite3.DatabaseError as exc:
            # Malformed index: let the query below trigger the self-heal.
            log.error("freshness update hit a corrupt index: %s", exc)

    try:
        conn = _connect()
    except sqlite3.DatabaseError as exc:
        # File-level corruption ("file is not a database") raises at CONNECT
        # time -- the WAL pragma reads the header -- so the query-level
        # self-heal below never saw it and search CRASHED on a truly trashed
        # DB file (found 2026-10-09 when the quarantine test's corruption
        # became genuinely file-level). Same contract as below: heal once,
        # retry once, never raise out of a search call.
        if not _is_corruption(exc):
            if "no such module" in str(exc).lower():
                log.error("keyword search unavailable: this Python's SQLite has no FTS5 module (%s); "
                          "install a Python whose sqlite3 was built with FTS5 (python.org, uv and the "
                          "distro packages all have it) -- returning empty results", exc)
                return []
            log.warning("FTS connect failed (not corruption, e.g. locked): %s -- returning empty results", exc)
            return []
        if not _healed and _heal_malformed():
            log.error("FTS DB was corrupt at connect (%s) -- self-healed; retrying query once", exc)
            return search(query, scope=scope, limit=limit, _healed=True)
        log.error("FTS connect failed on a corrupt DB and self-heal did not recover: %s", exc)
        return []
    try:
        if scope:
            # Escape LIKE wildcards in scope so a scope like "project_%foo"
            # doesn't match unexpected paths.
            # Use | as the LIKE escape character (unambiguous, never appears in memory paths).
            # Original code used ESCAPE '\' but Python string processing in triple-quoted
            # strings converts \' to ', making the ESCAPE clause an empty string which
            # SQLite rejects with "ESCAPE expression must be a single character".
            scope_safe = scope.rstrip("/").replace("|", "||").replace("%", "|%").replace("_", "|_")
            # a scope that names a file (write-side grammar) matches that file only
            like_pattern = scope_safe if scope.lower().endswith(".md") else f"{scope_safe}/%"
            rows = conn.execute(
                """
                SELECT path,
                       snippet(memory_fts, 2, '[', ']', '...', 20) AS snippet,
                       rank
                FROM memory_fts
                WHERE memory_fts MATCH ?
                  AND path LIKE ? ESCAPE '|'
                ORDER BY rank
                LIMIT ?
                """,
                (query, like_pattern, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT path,
                       snippet(memory_fts, 2, '[', ']', '...', 20) AS snippet,
                       rank
                FROM memory_fts
                WHERE memory_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()

        # Deduplicate by path: keep the highest-scoring row per file.
        # FTS5 can match the same file multiple times when a query term appears
        # in both the frontmatter and body columns.
        seen: dict[str, dict] = {}
        for r in rows:
            p, snip, sc = r[0], r[1], r[2]
            if p not in seen or sc < seen[p]["score"]:  # FTS5 rank is negative; lower = better
                seen[p] = {"path": p, "snippet": snip, "score": sc}

        results = list(seen.values())
        if not results:
            return results
        # NOTE: a single result MUST still go through the blend below.
        # The old `<= 1` early-return left a lone hit carrying its raw
        # NEGATIVE FTS5 rank, which the server's confidence bands read as
        # top score < 0.2 -> "starved" -- one perfect unique match told
        # the model to ask a clarifying question instead of answering
        # (launch blocker, competitive-sweep raw/18a; fixed 2026-10-07).

        # Re-rank: blend FTS5 BM25, mtime recency, and last_confirmed staleness.
        # FTS5 rank is negative; lower (more negative) = better BM25 hit.
        # All signals normalised to 0-1 before blending.
        #
        # Weights: BM25 65%, mtime recency 20%, confirmation recency 15%.
        # last_confirmed scores: 1.0 if confirmed <30d, 0.5 if unconfirmed (neutral),
        # 0.0 if confirmed >1y ago (stale validation).
        fts_scores = [abs(r["score"]) for r in results]
        max_fts = max(fts_scores) or 1.0

        # Fetch mtime + last_confirmed for all result paths in one query
        path_list = list(seen.keys())
        placeholders = ",".join("?" * len(path_list))
        meta_rows = conn.execute(
            # bandit B608 justification: `placeholders` is a literal "?,?,..." string;
            # every value is bound (bandit triage, quality loop run 7).
            f"SELECT path, mtime, last_confirmed FROM memory_files WHERE path IN ({placeholders})",  # nosec B608
            path_list,
        ).fetchall()
        mtime_map: dict[str, float] = {}
        confirmed_map: dict[str, float | None] = {}
        for row in meta_rows:
            mtime_map[row[0]] = row[1]
            confirmed_map[row[0]] = row[2]
        now_ts = time.time()
        max_age = 365 * 24 * 3600  # 1 year

        for r in results:
            path = r["path"]
            fts_norm = abs(r["score"]) / max_fts  # 0-1, higher = better
            mtime = mtime_map.get(path, now_ts)
            age_secs = now_ts - mtime
            recency = max(0.0, 1.0 - age_secs / max_age)  # 0-1, 1=very recent
            conf_score = _confirmed_score(confirmed_map.get(path), now_ts)
            # Scope authority: feedback/ and identity/ files carry high signal
            authority = 1.2 if path.startswith("feedback/") or path.startswith("identity/") else 1.0
            combined = (fts_norm * 0.65 + recency * 0.20 + conf_score * 0.15) * authority
            r["score"] = combined

        results.sort(key=lambda x: x["score"], reverse=True)
        return results
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        # FTS5 treats operators (AND/OR/NEAR), parens, and stray quotes as
        # SYNTAX -- a human pasting a sentence is not writing FTS5. Retry the
        # exact text as an escaped literal phrase instead of failing (and
        # instead of the old message that blamed a "corrupt" index for a bad
        # query -- impatient-persona finding, quality loop run 4).
        syntax = ("fts5: syntax error" in msg or "unterminated string" in msg or "no such column" in msg)
        if syntax and _fallback_stage == 0:
            # Stage 1: quote each whitespace token and let FTS5 AND them. A dotted or
            # punctuated token ("config.toml AppData") used to drop straight to a literal
            # phrase, which only matches the words adjacent in that order and so returned
            # nothing although every word was on disk (deep install lanes 2026-11-09).
            tokens = [t for t in query.split() if t]
            if len(tokens) > 1:
                tokenized = " ".join('"' + t.replace('"', '""') + '"' for t in tokens)
                log.info("FTS query was not valid FTS5 syntax; retrying with each token quoted")
                conn.close()
                return search(tokenized, scope=scope, limit=limit, _fallback_stage=1)
            _fallback_stage = 1  # single token: go straight to the phrase form
        if syntax and _fallback_stage == 1:
            phrase = '"' + query.replace('"', '""') + '"'
            log.info("FTS query still not valid FTS5 syntax; retrying as one literal phrase")
            conn.close()
            return search(phrase, scope=scope, limit=limit, _fallback_stage=2)
        # Genuinely unexpected: table missing or corrupted DB.
        log.warning("FTS search failed (index missing or corrupt): %s -- returning empty results", exc)
        return []
    except sqlite3.DatabaseError as exc:
        # "database disk image is malformed" -- found LIVE 2026-10-07
        # (quality loop run 39): FTS5 shadow-table corruption passes
        # PRAGMA integrity_check yet throws on every MATCH, and only
        # OperationalError was caught, so the error escaped to every
        # caller. The index is derived and rebuildable BY DESIGN:
        # self-heal (drop + recreate the FTS tables in place, preserving
        # co-tenant tables like memory_vectors; quarantine the file only
        # if even that fails) and retry the query exactly once.
        conn.close()
        if not _is_corruption(exc):
            log.warning("FTS search failed (not corruption): %s -- returning empty results", exc)
            return []
        if not _healed and _heal_malformed():
            log.error("FTS index was corrupt (%s) -- self-healed by rebuild; retrying query once", exc)
            return search(query, scope=scope, limit=limit, _healed=True)
        log.error("FTS search failed on a corrupt index and self-heal did not recover: %s", exc)
        return []
    finally:
        conn.close()


if __name__ == "__main__":
    n = rebuild(force=True)
    print(f"Indexed {n} files into {FTS_DB}")
