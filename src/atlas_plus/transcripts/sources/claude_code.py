r"""Claude Code JSONL tailer.

Watches `~\.claude\projects\<workspace-slug>\*.jsonl` files. Each JSONL line is
one event written by the Claude Code client. We map the relevant lines into
canonical TurnRecords and append via the TranscriptStore.

Schema notes (observed from real files):

- type=user      → message.role=user, message.content=str
- type=assistant → message.role=assistant, message.content=list[block]
                    blocks: {type:"text", text} | {type:"tool_use", name, input}
- type=file-history-snapshot → ignored (internal)
- type=summary, type=meta → ignored (we only capture conversational turns)

Cursor persistence: per-file byte offset in `index.cursors`, keyed
(source='claude-code', path_or_id=absolute_path). Restart resumes cleanly.

This module is runnable as `python -m atlas_plus.transcripts.sources.claude_code`.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Iterator

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from atlas_plus.transcripts.index import TranscriptIndex
from atlas_plus.transcripts.store import TranscriptStore, TurnRecord

SOURCE = "claude-code"
DEFAULT_ROOT = str(Path.home() / ".claude" / "projects")

log = logging.getLogger("atlas.claude-code-tailer")


def _coerce_role(line_obj: dict[str, Any]) -> str | None:
    t = line_obj.get("type")
    if t == "user":
        return "user"
    if t == "assistant":
        return "assistant"
    return None


def _coerce_content(line_obj: dict[str, Any]) -> Any:
    msg = line_obj.get("message") or {}
    content = msg.get("content", "")
    return content


def _coerce_tool_calls(line_obj: dict[str, Any]) -> list[dict[str, Any]]:
    msg = line_obj.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            out.append({"name": block.get("name"), "id": block.get("id"), "input": block.get("input")})
    return out


def _coerce_tool_results(line_obj: dict[str, Any]) -> list[dict[str, Any]]:
    msg = line_obj.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    out: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            out.append(block)
    return out


def _coerce_tokens(line_obj: dict[str, Any]) -> tuple[int, int]:
    msg = line_obj.get("message") or {}
    usage = msg.get("usage") or {}
    return int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))


def _line_to_record(line_obj: dict[str, Any]) -> TurnRecord | None:
    role = _coerce_role(line_obj)
    if role is None:
        return None
    msg = line_obj.get("message") or {}
    tin, tout = _coerce_tokens(line_obj)
    return TurnRecord(
        turn_id=line_obj.get("uuid") or "",
        conversation_id=line_obj.get("sessionId") or "",
        source=SOURCE,
        role=role,
        ts=line_obj.get("timestamp") or "",
        content=_coerce_content(line_obj),
        tool_calls=_coerce_tool_calls(line_obj),
        tool_results=_coerce_tool_results(line_obj),
        model=msg.get("model"),
        tokens_in=tin,
        tokens_out=tout,
        parent_turn_id=line_obj.get("parentUuid"),
    )


class ClaudeCodeTailer:
    """Watches Claude Code JSONL session files and ingests new turns into the TranscriptStore and index."""

    def __init__(
        self,
        root: str | Path | None = None,
        store: TranscriptStore | None = None,
        index: TranscriptIndex | None = None,
        on_record: callable | None = None,
    ) -> None:
        self.root = Path(root or os.environ.get("ATLAS_CLAUDE_CODE_ROOT", DEFAULT_ROOT))
        self.store = store or TranscriptStore()
        self.index = index or TranscriptIndex()
        self.on_record = on_record  # optional bus pusher: f(record) -> None

    # --- file-level scanning ---

    def initial_sweep(self) -> int:
        """Process every existing .jsonl from its persisted cursor."""
        count = 0
        for path in self._all_jsonl_files():
            count += self._process_file(path)
        return count

    def _all_jsonl_files(self) -> Iterator[Path]:
        if not self.root.exists():
            return
        for path in self.root.rglob("*.jsonl"):
            yield path

    def _process_file(self, path: Path) -> int:
        cursor = self.index.get_cursor(SOURCE, str(path))
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return 0
        if cursor >= size:
            return 0
        appended = 0
        with path.open("rb") as f:
            f.seek(cursor)
            for raw in f:
                cursor += len(raw)
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception as exc:
                    log.warning("malformed line in %s at offset %d: %s", path, cursor, exc)
                    continue
                rec = _line_to_record(obj)
                if rec is None or not rec.conversation_id:
                    continue
                self.store.append(rec)
                self.index.upsert_turn(rec)
                if self.on_record is not None:
                    try:
                        self.on_record(rec)
                    except Exception as exc:
                        log.warning("on_record failed: %s", exc)
                appended += 1
        self.index.set_cursor(SOURCE, str(path), cursor)
        return appended

    # --- watchdog wiring ---

    def watch(self) -> None:
        """Start watchdog observer on the Claude Code root dir, blocking until Ctrl+C."""
        handler = _Handler(self)
        observer = Observer()
        observer.schedule(handler, str(self.root), recursive=True)
        observer.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            observer.stop()
            observer.join()


class _Handler(FileSystemEventHandler):
    def __init__(self, tailer: ClaudeCodeTailer) -> None:
        self.tailer = tailer

    def on_modified(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        """Process file change: ingest new JSONL turns from the modified session file."""
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix != ".jsonl":
            return
        try:
            self.tailer._process_file(path)
        except Exception as exc:
            log.exception("process_file failed: %s", exc)

    def on_created(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        """New file created -- delegate to on_modified to ingest initial turns."""
        self.on_modified(event)


def main() -> int:
    """Entry point: start the Claude Code JSONL transcript tailer (watchdog mode)."""
    logging.basicConfig(
        level=os.environ.get("ATLAS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    tailer = ClaudeCodeTailer()
    if not tailer.root.exists():
        log.error("Claude Code root does not exist: %s", tailer.root)
        return 1
    log.info("initial sweep of %s", tailer.root)
    appended = tailer.initial_sweep()
    log.info("appended %d turn(s) on initial sweep", appended)

    def _shutdown(_sig, _frm):
        log.info("shutdown signal")
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except Exception:
            pass

    log.info("watching for new turns...")
    tailer.watch()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
