"""Smoke tests for the Transcript Manager core.

Covers:
    - per-conversation hash chain (hash_prev/hash) reproducibility
    - fsync-and-rename atomicity on close()
    - crash recovery: torn-tail truncation + archive without delete
    - cursor persistence in TranscriptIndex
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from atlas_plus.transcripts.store import TranscriptStore, TurnRecord, ZERO_HASH
from atlas_plus.transcripts.index import TranscriptIndex
from atlas_plus.transcripts.recovery import recover_all


@pytest.fixture
def store_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "transcripts"
    monkeypatch.setenv("ATLAS_TRANSCRIPT_ROOT", str(root))
    monkeypatch.setenv("ATLAS_TRANSCRIPT_INDEX", str(root / "index.sqlite"))
    return root


def _record(conv: str, role: str, content: str, source: str = "test") -> TurnRecord:
    return TurnRecord(
        conversation_id=conv, source=source, role=role, content=content, ts="2026-04-28T00:00:00Z"
    )


def test_hash_chain_is_reproducible(store_root):
    store = TranscriptStore(root=store_root)
    rec1 = store.append(_record("c1", "user", "hi"))
    rec2 = store.append(_record("c1", "assistant", "hello"))
    assert rec1.hash_prev == ZERO_HASH
    assert rec1.hash != ZERO_HASH
    assert rec2.hash_prev == rec1.hash
    assert rec2.hash == rec2.compute_hash()


def test_close_atomically_promotes_partial(store_root):
    store = TranscriptStore(root=store_root)
    store.append(_record("c-close", "user", "x"))
    promoted = store.close("c-close")
    assert promoted is not None
    assert promoted.suffix == ".ndjson"
    assert promoted.exists()
    sibling_partial = promoted.with_suffix(".ndjson.partial")
    assert not sibling_partial.exists()


def test_torn_tail_recovery_archives_and_truncates(store_root):
    store = TranscriptStore(root=store_root)
    store.append(_record("c-torn", "user", "first"))
    store.append(_record("c-torn", "assistant", "second"))

    # Locate the .partial and append a torn (non-JSON) byte string.
    partials = list(store.list_partials())
    assert len(partials) == 1
    partial = partials[0]
    pre_size = partial.stat().st_size
    with partial.open("ab") as f:
        f.write(b'{"this is": "a corrupted partial line\n')

    # Recovery should detect the bad line, archive it, truncate the partial.
    report = recover_all(store=TranscriptStore(root=store_root), index=TranscriptIndex())
    assert report.partials_scanned == 1
    assert report.truncated_partials == 1
    assert report.indexed_turns >= 2
    assert partial.stat().st_size == pre_size

    archive = partial.parent / "archive"
    assert archive.exists()
    archived = list(archive.glob(f"{partial.stem}.torn-*.ndjson"))
    assert archived, "torn tail must be preserved as an archive copy"


def test_chain_break_stops_recovery_at_first_bad_record(store_root, tmp_path):
    store = TranscriptStore(root=store_root)
    rec1 = store.append(_record("c-break", "user", "a"))
    rec2 = store.append(_record("c-break", "assistant", "b"))

    # Tamper with the second line by rewriting its content but keeping its hash —
    # this should fail compute_hash() == hash and the recovery should stop after rec1.
    partial = next(iter(store.list_partials()))
    lines = partial.read_bytes().splitlines(keepends=True)
    obj = json.loads(lines[1])
    obj["content"] = "tampered"
    lines[1] = (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    partial.write_bytes(b"".join(lines))

    report = recover_all(store=TranscriptStore(root=store_root), index=TranscriptIndex())
    assert report.indexed_turns == 1
    assert report.torn_lines >= 1


def test_cursor_persistence(store_root):
    index = TranscriptIndex()
    assert index.get_cursor("claude-code", "/some/path.jsonl") == 0
    index.set_cursor("claude-code", "/some/path.jsonl", 12345)
    assert index.get_cursor("claude-code", "/some/path.jsonl") == 12345

    # Re-open to confirm persistence across instances.
    index2 = TranscriptIndex()
    assert index2.get_cursor("claude-code", "/some/path.jsonl") == 12345


def test_iter_conversation_after_close(store_root):
    store = TranscriptStore(root=store_root)
    for n in range(3):
        store.append(_record("c-iter", "user", f"msg-{n}"))
    store.close("c-iter")
    fresh = TranscriptStore(root=store_root)
    seen = list(fresh.iter_conversation("c-iter"))
    assert [r.content for r in seen] == ["msg-0", "msg-1", "msg-2"]


# ---------------------------------------------------------------------------
# Extended coverage
# ---------------------------------------------------------------------------

def test_append_multiple_conversations_independent(store_root):
    """Messages from different conversation IDs stay in their own files."""
    store = TranscriptStore(root=store_root)
    store.append(_record("conv-A", "user", "hello from A"))
    store.append(_record("conv-B", "user", "hello from B"))
    store.close("conv-A")
    store.close("conv-B")

    fresh = TranscriptStore(root=store_root)
    msgs_a = list(fresh.iter_conversation("conv-A"))
    msgs_b = list(fresh.iter_conversation("conv-B"))
    assert len(msgs_a) == 1
    assert len(msgs_b) == 1
    assert msgs_a[0].content == "hello from A"
    assert msgs_b[0].content == "hello from B"


def test_list_partials_shows_open_conversations(store_root):
    """list_partials yields paths for conversations that have not been closed."""
    store = TranscriptStore(root=store_root)
    store.append(_record("open-conv", "user", "still open"))
    # list_partials() yields Path objects; check that the open conv appears
    partials = list(store.list_partials())
    assert any("open-conv" in str(p) for p in partials)


def test_closed_conv_not_in_partials(store_root):
    """Closed conversations are promoted from partial to final and no longer partial."""
    store = TranscriptStore(root=store_root)
    store.append(_record("closing-conv", "user", "about to close"))
    store.close("closing-conv")
    partials = list(store.list_partials())
    assert not any("closing-conv" in str(p) for p in partials)


def test_iter_empty_conversation_returns_no_records(store_root):
    """iter_conversation on a non-existent conversation yields nothing."""
    store = TranscriptStore(root=store_root)
    records = list(store.iter_conversation("nonexistent-conv"))
    assert records == []


def test_hash_is_content_derived(store_root):
    """TurnRecord.compute_hash() is deterministic given identical record state."""
    from atlas_plus.transcripts.store import ZERO_HASH
    # Build a single record and compute the hash twice -- same object, same hash
    r = _record("hash-test", "user", "deterministic content")
    r.hash_prev = ZERO_HASH
    h1 = r.compute_hash()
    h2 = r.compute_hash()
    assert h1 == h2


def test_different_content_produces_different_hash(store_root):
    """Different content produces different hashes (hash function is not trivially broken)."""
    from atlas_plus.transcripts.store import ZERO_HASH
    # Use the same fixed timestamp so only content differs
    r1 = _record("h1", "user", "content one")
    r1.hash_prev = ZERO_HASH
    r2 = _record("h2", "user", "content two")
    r2.hash_prev = ZERO_HASH
    # Force same timestamp to isolate content difference
    shared_ts = "2026-05-20T00:00:00Z"
    r1.ts = shared_ts
    r2.ts = shared_ts
    assert r1.compute_hash() != r2.compute_hash()
