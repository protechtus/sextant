"""Startup recovery for unfinished transcripts.

On every Keel start, walk every `.ndjson.partial` file and:

1. Validate the per-conversation hash chain. If it breaks at line N, treat
   line N onwards as torn — copy them to <name>.torn.<ts>.ndjson under an
   `archive/` directory so nothing is deleted (archive-not-delete rule), and
   truncate the partial file to the last good offset.
2. Rebuild the SQLite index entries for any turns missing from it.
3. Optionally re-broadcast any turns the bus consumers may have missed.
   The default policy is to broadcast everything past the journaled offset.

Use:
    from atlas_plus.transcripts.recovery import recover_all
    recover_all()
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from atlas_plus.transcripts.index import TranscriptIndex
from atlas_plus.transcripts.store import TranscriptStore, TurnRecord, ZERO_HASH


@dataclass
class RecoveryReport:
    """Summary of a partial NDJSON recovery pass: scanned, recovered, promoted, and torn-line counts."""

    partials_scanned: int = 0
    fully_recovered: int = 0
    truncated_partials: int = 0
    promoted: int = 0
    torn_lines: int = 0
    indexed_turns: int = 0
    rebroadcast_turns: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def recover_all(
    store: TranscriptStore | None = None,
    index: TranscriptIndex | None = None,
    rebroadcast: Callable[[TurnRecord], None] | None = None,
) -> RecoveryReport:
    """Walk every .partial file and reconcile.

    Args:
        store: TranscriptStore (defaults to the env-driven default).
        index: TranscriptIndex (defaults to the env-driven default).
        rebroadcast: optional callback invoked once per turn that was on disk
                     but missing from the index. Sources can use this to push
                     to the kernel's `transcript.turn` topic.

    Returns:
        RecoveryReport summary.
    """
    store = store or TranscriptStore()
    index = index or TranscriptIndex()
    report = RecoveryReport()

    for partial in store.list_partials():
        report.partials_scanned += 1
        try:
            turns, last_good_offset = _validate_chain(partial)
        except Exception as exc:
            report.errors.append(f"{partial}: {exc}")
            continue

        original_size = partial.stat().st_size
        torn_count = 0
        if last_good_offset < original_size:
            torn_count = _archive_torn_tail(partial, last_good_offset)
            report.truncated_partials += 1
            report.torn_lines += torn_count
            # The journal head now points past the truncated end; rewrite it
            # to the last good (hash, offset) so the writer resumes the chain
            # from valid state.
            try:
                conv_id = partial.name[: -len(".ndjson.partial")]
                good_hash = turns[-1].hash if turns else ZERO_HASH
                store.write_journal(
                    store.journal_path_for(conv_id), partial, good_hash, last_good_offset
                )
            except Exception as exc:
                report.errors.append(f"{partial}: journal rewrite failed: {exc}")

        for record in turns:
            index.upsert_turn(record)
            report.indexed_turns += 1
            if rebroadcast is not None:
                try:
                    rebroadcast(record)
                    report.rebroadcast_turns += 1
                except Exception as exc:
                    report.errors.append(f"rebroadcast failed for {record.turn_id}: {exc}")

        # If the conversation had a clean tail (no torn lines) and looks idle
        # (no recent activity, but we cannot tell from disk alone), leave the
        # .partial in place. Sources will pick up from where they left off.
        report.fully_recovered += 1

    return report


def _validate_chain(path: Path) -> tuple[list[TurnRecord], int]:
    """Read line-by-line, recompute hashes, return the longest valid prefix.

    Returns (records, byte_offset_after_last_good_line). If a line fails to
    parse or its hash does not match the chain, scanning stops there and the
    byte offset reported is just before that line.
    """
    records: list[TurnRecord] = []
    last_hash = ZERO_HASH
    last_good_offset = 0
    with path.open("rb") as f:
        offset = 0
        while True:
            line = f.readline()
            if not line:
                break
            try:
                obj = json.loads(line.decode("utf-8"))
                rec = TurnRecord.model_validate(obj)
                if rec.hash_prev != last_hash:
                    break
                if rec.compute_hash() != rec.hash:
                    break
                records.append(rec)
                last_hash = rec.hash
                offset += len(line)
                last_good_offset = offset
            except Exception:
                break
    return records, last_good_offset


def _archive_torn_tail(path: Path, last_good_offset: int) -> int:
    """Move the torn tail of a partial file to a sibling archive (no delete).

    Returns the count of torn lines moved.
    """
    archive_dir = path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    archive_path = archive_dir / f"{path.stem}.torn-{ts}.ndjson"

    # A writer in another process must not append between the archive copy and the
    # truncate (its turn would vanish from both files; round-B 2026-11-09, N-E). The store
    # waits on this sentinel before appending.
    sentinel = path.with_name(path.name + ".recovering")
    try:
        fd = os.open(str(sentinel), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        raise RuntimeError(f"recovery already in progress for {path.name} ({sentinel.name} present)")
    try:
        with path.open("rb") as f:
            f.seek(last_good_offset)
            torn = f.read()

        # Durable archive BEFORE the live file is truncated.
        with archive_path.open("wb") as af:
            af.write(torn)
            af.flush()
            os.fsync(af.fileno())
        torn_lines = torn.count(b"\n")

        # Re-read the tail right before truncating: anything a writer appended after the
        # archive copy (a complete line past the torn bytes) is archived too, never dropped.
        with path.open("rb+") as f:
            f.seek(last_good_offset + len(torn))
            late = f.read()
            if late:
                with archive_path.open("ab") as af:
                    af.write(late)
                    af.flush()
                    os.fsync(af.fileno())
                torn_lines += late.count(b"\n")
            f.truncate(last_good_offset)
            f.flush()
            os.fsync(f.fileno())
    finally:
        try:
            sentinel.unlink()
        except OSError:
            pass

    # Drop a sibling _README.md per the archive-not-delete rule.
    readme = archive_dir / "_README.md"
    if not readme.exists():
        readme.write_text(
            "Torn-tail archive\n=================\n\n"
            "Files in this directory are byte-exact copies of the trailing\n"
            "bytes of a `.ndjson.partial` file that failed hash-chain\n"
            "validation at recovery time. The original `.partial` was\n"
            "truncated to the last valid offset; nothing was deleted.\n",
            encoding="utf-8",
        )

    return torn_lines
