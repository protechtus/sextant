"""Crash-resilient append-only NDJSON transcript store.

Design (see docs/adr/006-transcript-manager.md):

- One file per conversation: <root>/<source>/<YYYY-MM-DD>/<conv_id>.ndjson(.partial)
- Open .partial with O_APPEND; write + fsync per turn.
- Each line is a TurnRecord serialized as canonical JSON (sorted keys, no
  whitespace) so the chain hash is reproducible.
- Per-conversation hash chain: record["hash"] = sha256(canonical_json(record - hash) + hash_prev).
  No global chain across sources.
- A side journal at <root>/journal/<conv_id>.head records (offset, hash) after
  every successful append; recovery uses it to detect torn writes.
- close(conv_id) atomically renames .partial -> canonical via os.replace.
- The store is process-safe but not multi-process-safe per conversation; pair
  with one-writer-per-conversation discipline (which all sources follow).

The store is intentionally tiny and dependency-free (stdlib + Pydantic). The
SQLite index lives in `index.py`; nothing in this module talks to SQLite.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from pydantic import BaseModel, Field

log = logging.getLogger("atlas.transcripts")

ZERO_HASH = "0" * 64


class TurnRecord(BaseModel):
    """Canonical schema for one captured turn.

    The same shape is written for every source. Fields a source cannot
    populate get sensible defaults rather than being omitted, so downstream
    consumers do not need source-specific code paths.
    """

    turn_id: str = Field(default_factory=lambda: str(uuid4()))
    conversation_id: str
    source: str
    role: str  # user | assistant | system | tool
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    content: Any = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    parent_turn_id: str | None = None
    hash_prev: str = ZERO_HASH
    hash: str = ""

    def compute_hash(self) -> str:
        """Compute a chained SHA-256 hash of all fields (excluding hash) XOR'd with hash_prev."""
        body = self.model_dump(exclude={"hash"})
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        h = hashlib.sha256()
        h.update(canonical.encode("utf-8"))
        h.update(self.hash_prev.encode("utf-8"))
        return h.hexdigest()


@dataclass
class _ConversationHandle:
    """Per-conversation file handle plus chain head for in-process appends."""

    path: Path
    journal_path: Path
    last_hash: str
    last_offset: int
    lock: threading.Lock


class TranscriptStore:
    """Append-only writer keyed by conversation_id.

    Threading model: one lock per conversation. Different conversations are
    written in parallel; turns within one conversation are serialized.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        env_root = os.environ.get("ATLAS_TRANSCRIPT_ROOT")
        from atlas_plus import paths as atlas_paths
        self.root = Path(root or env_root or (atlas_paths.state_dir() / "transcripts"))
        self.journal_root = self.root / "journal"
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal_root.mkdir(parents=True, exist_ok=True)
        self._handles: dict[str, _ConversationHandle] = {}
        self._handles_lock = threading.Lock()

    def _conversation_dir(self, source: str, conv_id: str) -> Path:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        d = self.root / source / date
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _handle_for(self, record: TurnRecord) -> _ConversationHandle:
        with self._handles_lock:
            existing = self._handles.get(record.conversation_id)
            if existing is not None:
                return existing
            cdir = self._conversation_dir(record.source, record.conversation_id)
            partial = cdir / f"{record.conversation_id}.ndjson.partial"
            canonical = cdir / f"{record.conversation_id}.ndjson"
            journal = self.journal_root / f"{record.conversation_id}.head"
            target = partial if not canonical.exists() else canonical
            # The chain is per FILE: a journal written for a different file
            # (e.g. yesterday's .partial, before a UTC date rollover) must not
            # seed the head of a new file, or recovery would see a broken chain
            # at line 1 and archive the whole file.
            last_hash, last_offset, jfile = self._read_journal_full(journal)
            if jfile != str(target):
                last_hash, last_offset = ZERO_HASH, 0
            if last_hash == ZERO_HASH and target.exists():
                # Recover head from existing file (e.g. process restart).
                last_hash, last_offset = self._scan_tail(target)
            elif target.exists():
                # The journal is behind the file (a turn fsynced but the process died
                # before the journal update) or the file has a torn tail. Advance over
                # complete lines; set a torn fragment aside so the next append starts a
                # clean line and recovery does not see two records fused on one line
                # (round-B re-audit 2026-11-09, N7).
                last_hash, last_offset = self._reconcile_tail(target, last_hash, last_offset)
            handle = _ConversationHandle(
                path=partial if not canonical.exists() else canonical,
                journal_path=journal,
                last_hash=last_hash,
                last_offset=last_offset,
                lock=threading.Lock(),
            )
            self._handles[record.conversation_id] = handle
            return handle

    @staticmethod
    def _read_journal_full(journal: Path) -> tuple[str, int, str | None]:
        if not journal.exists():
            return ZERO_HASH, 0, None
        try:
            data = json.loads(journal.read_text(encoding="utf-8"))
            return data.get("hash", ZERO_HASH), int(data.get("offset", 0)), data.get("file")
        except Exception:
            return ZERO_HASH, 0, None

    @staticmethod
    def _read_journal(journal: Path) -> tuple[str, int]:
        if not journal.exists():
            return ZERO_HASH, 0
        try:
            data = json.loads(journal.read_text(encoding="utf-8"))
            return data.get("hash", ZERO_HASH), int(data.get("offset", 0))
        except Exception:
            return ZERO_HASH, 0

    @staticmethod
    def _reconcile_tail(path: Path, last_hash: str, last_offset: int) -> tuple[str, int]:
        """Return the chain head after accounting for bytes past the journaled offset.

        Complete, parseable lines advance the head. A trailing fragment that does not
        parse is moved to ``<file>.torn-<utc>`` (never deleted) and the file is
        truncated back to the last complete line, so the chain stays one record per line.
        """
        try:
            size = path.stat().st_size
        except OSError:
            return last_hash, last_offset
        if size <= last_offset:
            return last_hash, last_offset
        with path.open("rb") as f:
            f.seek(last_offset)
            extra = f.read()
        offset = last_offset
        for line in extra.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                break
            try:
                rec = json.loads(line.decode("utf-8"))
                last_hash = rec.get("hash", last_hash)
                offset += len(line)
            except Exception:
                break
        torn = extra[offset - last_offset:]
        if torn:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            side = path.with_name(path.name + f".torn-{stamp}")
            with side.open("ab") as f:
                f.write(torn)
            with path.open("r+b") as f:
                f.truncate(offset)
                f.flush()
                os.fsync(f.fileno())
            log.warning("transcript %s: %d torn bytes set aside in %s", path.name, len(torn), side.name)
        return last_hash, offset

    @staticmethod
    def _scan_tail(path: Path) -> tuple[str, int]:
        """Read all lines, return hash + byte offset of the last valid line."""
        if not path.exists() or path.stat().st_size == 0:
            return ZERO_HASH, 0
        last_hash = ZERO_HASH
        offset = 0
        with path.open("rb") as f:
            data = f.read()
        # Treat lines defensively. Any line that fails to parse aborts the scan
        # at that offset so recovery treats the rest as torn.
        for line in data.splitlines(keepends=True):
            try:
                rec = json.loads(line.decode("utf-8"))
                last_hash = rec.get("hash", ZERO_HASH)
                offset += len(line)
            except Exception:
                break
        return last_hash, offset

    def append(self, record: TurnRecord) -> TurnRecord:
        """Append a turn; sets hash_prev/hash and returns the persisted record.

        Caller passes a TurnRecord with content + role; the store fills in the
        chain fields. Result is the same instance, mutated in place.
        """
        handle = self._handle_for(record)
        with handle.lock:
            record.hash_prev = handle.last_hash
            record.hash = record.compute_hash()
            line = json.dumps(
                record.model_dump(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ) + "\n"
            data = line.encode("utf-8")
            self._wait_for_recovery(handle.path)
            with handle.path.open("ab") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            handle.last_hash = record.hash
            handle.last_offset += len(data)
            self._write_journal(handle)
        return record

    @staticmethod
    def _wait_for_recovery(path: Path, timeout: float = 5.0) -> None:
        """Hold an append while recovery holds the file's sentinel (round-B 2026-11-09, N-E)."""
        import time as _time
        sentinel = path.with_name(path.name + ".recovering")
        deadline = _time.monotonic() + timeout
        while sentinel.exists():
            if _time.monotonic() > deadline:
                log.warning("transcript %s: recovery sentinel still present after %.0fs; appending anyway",
                            path.name, timeout)
                return
            _time.sleep(0.05)

    def _write_journal(self, handle: _ConversationHandle) -> None:
        self.write_journal(
            handle.journal_path, handle.path, handle.last_hash, handle.last_offset
        )

    @staticmethod
    def write_journal(journal_path: Path, file_path: Path, last_hash: str, offset: int) -> None:
        """Durably write the chain head (hash, offset) for one specific file."""
        payload = json.dumps(
            {"hash": last_hash, "offset": offset, "file": str(file_path)},
            separators=(",", ":"),
        )
        tmp = journal_path.with_suffix(".head.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, journal_path)

    def journal_path_for(self, conversation_id: str) -> Path:
        return self.journal_root / f"{conversation_id}.head"

    def close(self, conversation_id: str) -> Path | None:
        """Atomically promote .partial to canonical for a finished conversation.

        Idempotent: if already canonical or never appended, returns None.
        """
        with self._handles_lock:
            handle = self._handles.pop(conversation_id, None)
        if handle is None:
            return None
        if handle.path.suffix == ".partial":
            canonical = handle.path.with_suffix("")
            os.replace(handle.path, canonical)
            return canonical
        return handle.path

    def iter_conversation(self, conversation_id: str, source: str | None = None) -> Iterator[TurnRecord]:
        """Yield every turn in a conversation, oldest first.

        Walks the on-disk file rather than any in-memory state so it reflects
        post-recovery truth.
        """
        for path in self._find_conversation_files(conversation_id, source=source):
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield TurnRecord.model_validate_json(line)
                    except Exception:
                        return

    def _find_conversation_files(self, conv_id: str, source: str | None) -> list[Path]:
        roots = [self.root / source] if source else [p for p in self.root.iterdir() if p.is_dir() and p.name != "journal"]
        results: list[Path] = []
        for r in roots:
            if not r.exists():
                continue
            for date_dir in sorted(p for p in r.iterdir() if p.is_dir()):
                for ext in (".ndjson", ".ndjson.partial"):
                    candidate = date_dir / f"{conv_id}{ext}"
                    if candidate.exists():
                        results.append(candidate)
        return results

    def list_partials(self) -> Iterator[Path]:
        """Yield every .partial file under the root (used by recovery)."""
        for path in self.root.rglob("*.ndjson.partial"):
            yield path
