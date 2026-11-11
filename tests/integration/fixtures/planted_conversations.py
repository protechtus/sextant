"""Atlas Persistence Protocol (APP) -- standard planted-fact conversation definition.

The APP is a 12-turn synthetic conversation with 5 facts planted at known positions.
It is the canonical test script for verifying Atlas memory persistence across LLM providers.

Fact dimensions tested:
  - Survival: does the fact land in .memory/ after session end?
  - Accuracy: does extracted text preserve key information (semantic, not exact match)?
  - Deduplication: do PF-001 and PF-004 (same topic, contradicting detail) collapse?
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from atlas_plus.transcripts.store import TurnRecord


@dataclass
class PlantedFact:
    id: str
    turn_index: int        # which turn (0-based) carries the fact
    scope: str             # routing scope e.g. "preference:editor"
    key_phrase: str        # substring that MUST appear in the extracted fact
    full_content: str      # the full user turn text containing the fact
    confidence: str        # "high" | "medium" | "low" -- expected extraction confidence
    contradicts: Optional[str] = None  # ID of a prior fact this conflicts with


# The canonical Atlas Persistence Protocol fact set
APP_FACTS: list[PlantedFact] = [
    PlantedFact(
        id="PF-001",
        turn_index=2,
        scope="preference:editor",
        key_phrase="dark mode",
        full_content=(
            "Quick note for our records: I always use dark mode in VS Code. "
            "The theme is Dracula. This is non-negotiable for readability."
        ),
        confidence="high",
    ),
    PlantedFact(
        id="PF-002",
        turn_index=4,
        scope="project:atlas-persistence-test",
        key_phrase="PostgreSQL",
        full_content=(
            "The atlas-persistence-test project uses PostgreSQL as its main data store, "
            "not SQLite. This was decided in the last architecture review."
        ),
        confidence="high",
    ),
    PlantedFact(
        id="PF-003",
        turn_index=6,
        scope="preference:testing",
        key_phrase="real database",
        full_content=(
            "Something I keep having to remind myself: integration tests must connect "
            "to a real database. Never mock the database in integration tests. "
            "We got burned last quarter when mocked tests passed but the prod migration failed."
        ),
        confidence="high",
    ),
    PlantedFact(
        id="PF-004",
        turn_index=8,
        scope="preference:editor",
        key_phrase="Solarized Dark",
        full_content=(
            "Actually, I changed my editor theme. I now prefer Solarized Dark over Dracula. "
            "Found it easier on the eyes for long sessions."
        ),
        confidence="medium",
        contradicts="PF-001",  # same scope as PF-001 -- dedup/conflict test
    ),
    PlantedFact(
        id="PF-005",
        turn_index=10,
        scope="reference:tools",
        key_phrase="ripgrep",
        full_content=(
            "For reference: ripgrep is the preferred search tool for all project searches. "
            "It is faster than grep and handles binary files correctly."
        ),
        confidence="medium",
    ),
]


def build_app_turns(
    facts: list[PlantedFact] | None = None,
    base_ts: datetime | None = None,
    conversation_id: str = "app-test-conv-001",
) -> list[TurnRecord]:
    """Build the standard 12-turn APP conversation with facts embedded at known positions.

    Non-planted turns contain neutral filler. Planted turns embed the fact in a
    user turn that also contains surrounding context to make it realistic.
    """
    if facts is None:
        facts = APP_FACTS

    fact_by_turn = {f.turn_index: f for f in facts}
    base = base_ts or datetime(2026, 5, 11, 9, 0, 0, tzinfo=timezone.utc)

    _FILLER_USER = [
        "Can you help me review the latest changes to the project?",
        "What do you think about the current architecture?",
        "Let's check the open tickets and prioritize.",
        "I want to make sure we have good test coverage.",
        "How are things looking overall with the codebase?",
        "Any blockers I should know about?",
    ]
    _FILLER_ASSISTANT = [
        "Sure, I can review the recent changes. Everything looks on track.",
        "The architecture is solid. The main area to watch is the data layer.",
        "Looking at tickets now. Three are high priority, two are medium.",
        "Test coverage is around 85%. The integration layer needs more tests.",
        "Overall the codebase is in good shape. No critical issues.",
        "No blockers currently. The build is green.",
    ]

    turns: list[TurnRecord] = []
    filler_user_idx = 0
    filler_asst_idx = 0

    for i in range(12):
        ts = (base + timedelta(minutes=i)).isoformat()
        role = "user" if i % 2 == 0 else "assistant"

        if i in fact_by_turn and role == "user":
            content = fact_by_turn[i].full_content
        elif role == "user":
            content = _FILLER_USER[filler_user_idx % len(_FILLER_USER)]
            filler_user_idx += 1
        else:
            content = _FILLER_ASSISTANT[filler_asst_idx % len(_FILLER_ASSISTANT)]
            filler_asst_idx += 1

        turns.append(TurnRecord(
            conversation_id=conversation_id,
            source="claude-code",
            role=role,
            content=content,
            ts=ts,
        ))

    return turns


# Deterministic LLM responses for Layer 1 (mocked) tests
# Maps each PlantedFact.id to what the mocked LLM should return
APP_MOCK_EXTRACTIONS: dict[str, dict] = {
    "PF-001": {
        "type": "feedback",
        "scope": "preference:editor",
        "title": "Always use dark mode in VS Code",
        "fact": "Always use dark mode in VS Code. Preferred theme is Dracula.",
        "confidence": 0.95,
    },
    "PF-002": {
        "type": "project",
        "scope": "project:atlas-persistence-test",
        "title": "Project uses PostgreSQL not SQLite",
        "fact": "The atlas-persistence-test project uses PostgreSQL as its main data store, not SQLite.",
        "confidence": 0.95,
    },
    "PF-003": {
        "type": "feedback",
        "scope": "preference:testing",
        "title": "Never mock the database in integration tests",
        "fact": (
            "Integration tests must connect to a real database. "
            "Never mock -- we got burned when mocked tests passed but the prod migration failed."
        ),
        "confidence": 0.95,
    },
    "PF-004": {
        "type": "feedback",
        "scope": "preference:editor",
        "title": "Editor theme changed to Solarized Dark",
        "fact": "Editor theme preference changed to Solarized Dark (previously Dracula). Easier on the eyes for long sessions.",
        "confidence": 0.95,
    },
    "PF-005": {
        "type": "reference",
        "scope": "reference:tools",
        "title": "ripgrep preferred over grep",
        "fact": "ripgrep is the preferred search tool for all project searches. Faster than grep, handles binary files.",
        "confidence": 0.95,
    },
}
