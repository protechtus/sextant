"""P2-T3: Map-reduce chunker for long transcripts.

Splits a list of TurnRecords into token-budget chunks while preserving
turn boundaries. Long transcripts (>50 turns) signal that a map-reduce
pass is needed; callers check needs_reduce().

Token estimation: 1 token ~= 4 chars (heuristic -- good enough for chunking).
"""

from __future__ import annotations

from dataclasses import dataclass

from atlas_plus.transcripts.store import TurnRecord

CHARS_PER_TOKEN = 4


def _turn_chars(turn: TurnRecord) -> int:
    content = turn.content
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(block.get("text", "") if isinstance(block, dict) else str(block))
            for block in content
        )
    return len(str(content))


@dataclass
class Chunk:
    """A single token-budget slice of a conversation, ready for a map-pass summarization call."""

    turns: list[TurnRecord]
    start_ts: str
    end_ts: str
    index: int
    total: int  # filled in after all chunks are known

    @property
    def text(self) -> str:
        """Render all turns as a flat [ROLE] content string, joining text blocks from list content."""
        lines: list[str] = []
        for t in self.turns:
            role = t.role.upper()
            content = t.content
            if isinstance(content, list):
                parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                content = " ".join(parts)
            lines.append(f"[{role}] {content}")
        return "\n\n".join(lines)


def chunk_turns(turns: list[TurnRecord], token_budget: int = 6000) -> list[Chunk]:
    """Split turns into chunks that fit within token_budget each."""
    if not turns:
        return []

    char_budget = token_budget * CHARS_PER_TOKEN
    chunks: list[Chunk] = []
    current: list[TurnRecord] = []
    current_chars = 0

    for turn in turns:
        tc = _turn_chars(turn)
        if current and current_chars + tc > char_budget:
            chunks.append(_make_chunk(current, len(chunks)))
            current = []
            current_chars = 0
        current.append(turn)
        current_chars += tc

    if current:
        chunks.append(_make_chunk(current, len(chunks)))

    for c in chunks:
        c.total = len(chunks)

    return chunks


def _make_chunk(turns: list[TurnRecord], index: int) -> Chunk:
    return Chunk(
        turns=turns,
        start_ts=turns[0].ts or "",
        end_ts=turns[-1].ts or "",
        index=index,
        total=0,
    )


def needs_reduce(chunks: list[Chunk]) -> bool:
    """Return True when there is more than one chunk, signaling that a reduce pass is required."""
    return len(chunks) > 1
