"""Generic transcript drop-directory ingestion (ROADMAP item 7).

Capture beyond Claude Code without writing a per-client adapter: any tool
that can write a file can feed Sextant. Drop transcript files into

    <state-dir>/transcripts/drop/          (override: ATLAS_TRANSCRIPT_DROP)

and run ``sextant-transcripts ingest`` (or point it at any file/dir).

Accepted shapes, per file:

- ``.jsonl``  -- one JSON object per line: ``{"role": ..., "content": ...}``
  (optional ``ts``, ``model``). The common export shape of chat tooling.
- ``.json``   -- either a list of message objects or ``{"messages": [...]}``
  (covers ChatGPT-style and OpenAI-API-style exports of a single thread).
- ``.md`` / ``.txt`` -- ingested as a single user-authored document turn
  (a meeting note, a pasted conversation, anything worth indexing).

conversation_id = the file's stem; source = "drop". Deliberate:
dropping ``foo.json`` today and ``foo.md`` next week CONTINUES the
same conversation -- the stem is the identity, not the format. Ingested files move to
``drop/_ingested/`` with a timestamp suffix (archive, never delete), so a
re-run never double-ingests. Malformed lines are skipped and counted, not
fatal.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from atlas_plus import paths as atlas_paths
from atlas_plus.transcripts.index import TranscriptIndex
from atlas_plus.transcripts.store import TranscriptStore, TurnRecord

log = logging.getLogger("atlas.transcripts.drop")

_ROLES = {"user", "assistant", "system", "tool"}


def drop_dir() -> Path:
    env = os.environ.get("ATLAS_TRANSCRIPT_DROP")
    if env:
        return Path(env)
    return atlas_paths.state_dir() / "transcripts" / "drop"


def _records_from_message(msg: dict, conv_id: str, position: int = 0) -> TurnRecord | None:
    role = str(msg.get("role", "")).strip().lower()
    content = msg.get("content", "")
    if role not in _ROLES or content in ("", None):
        return None
    # Deterministic turn id (expert review 2026-09-27): a crash between
    # store-append and archive-rename leaves the file in place, and a
    # re-run must UPSERT the same rows in the index rather than mint fresh
    # UUIDs and duplicate every turn. Same conversation + position +
    # content -> same id, forever.
    import hashlib
    digest = hashlib.sha256(
        f"{conv_id}:{position}:{role}:{content}".encode("utf-8", "replace")
    ).hexdigest()[:32]
    kwargs: dict = {
        "turn_id": f"drop-{digest}",
        "conversation_id": conv_id,
        "source": "drop",
        "role": role,
        "content": content,
    }
    if msg.get("ts"):
        kwargs["ts"] = str(msg["ts"])
    if msg.get("model"):
        kwargs["model"] = str(msg["model"])
    return TurnRecord(**kwargs)


def _parse_file(path: Path) -> tuple[list[TurnRecord], int]:
    """Return (records, skipped_count) for one dropped file."""
    conv_id = path.stem
    records: list[TurnRecord] = []
    skipped = 0
    suffix = path.suffix.lower()

    if suffix == ".jsonl":
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            rec = (_records_from_message(msg, conv_id, position=len(records))
                   if isinstance(msg, dict) else None)
            if rec is None:
                skipped += 1
            else:
                records.append(rec)
    elif suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            return [], 1
        messages = data.get("messages") if isinstance(data, dict) else data
        if not isinstance(messages, list):
            return [], 1
        for msg in messages:
            rec = (_records_from_message(msg, conv_id, position=len(records))
                   if isinstance(msg, dict) else None)
            if rec is None:
                skipped += 1
            else:
                records.append(rec)
    elif suffix in (".md", ".txt"):
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            import hashlib as _h
            digest = _h.sha256(f"{conv_id}:0:user:{text}".encode("utf-8", "replace")).hexdigest()[:32]
            records.append(TurnRecord(
                turn_id=f"drop-{digest}",
                conversation_id=conv_id, source="drop", role="user", content=text,
            ))
        else:
            skipped += 1
    else:
        skipped += 1  # unsupported extension

    return records, skipped


def ingest(target: Path | None = None) -> dict:
    """Ingest one file or every file in a directory (default: the drop dir).

    Returns {files, turns, skipped, archived_to}.
    """
    root = target or drop_dir()
    if root.is_file():
        files = [root]
        archive_base = root.parent / "_ingested"
    else:
        root.mkdir(parents=True, exist_ok=True)
        archive_base = root / "_ingested"
        files = sorted(
            p for p in root.iterdir()
            if p.is_file() and p.suffix.lower() in (".jsonl", ".json", ".md", ".txt")
        )

    store = TranscriptStore()
    index = TranscriptIndex()
    total_turns = total_skipped = 0
    ingested_files = 0
    for path in files:
        records, skipped = _parse_file(path)
        total_skipped += skipped
        if not records:
            continue
        conv_ids: set[str] = set()
        for rec in records:
            appended = store.append(rec)
            index.upsert_turn(appended)
            conv_ids.add(rec.conversation_id)
        # A dropped file is a finished conversation: promote its .partial to canonical
        # instead of leaving it for the next crash-recovery walk (persona rerun 2026-11-10).
        for cid in conv_ids:
            try:
                store.close(cid)
            except Exception:  # noqa: BLE001
                pass
        total_turns += len(records)
        ingested_files += 1
        # Archive, never delete: the original moves aside with a timestamp.
        # Collision-proof (deep-pass probe, 2026-09-27): re-dropping the same
        # stem within one second raised FileExistsError AFTER indexing but
        # BEFORE archiving, leaving the file in place to double-ingest on the
        # next run. Counter suffix disambiguates instead of crashing.
        archive_base.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target_archive = archive_base / f"{path.stem}.{stamp}{path.suffix}"
        counter = 1
        while target_archive.exists():
            target_archive = archive_base / f"{path.stem}.{stamp}-{counter}{path.suffix}"
            counter += 1
        path.rename(target_archive)

    return {
        "files": ingested_files,
        "turns": total_turns,
        "skipped": total_skipped,
        "archived_to": str(archive_base) if ingested_files else "",
    }
