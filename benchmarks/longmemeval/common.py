"""Shared pieces of the Sextant LongMemEval harness: paths, dataset access, prices.

Nothing in this module talks to the network. The work directory holds everything bulky or
regenerable (dataset, per-question corpora, request files, raw results) and stays out of the
repository; set SEXTANT_PROOF_WORK to choose it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent

DATASET_REPO = "xiaowu0162/longmemeval-cleaned"
DATASET_FILES = {
    "s": "longmemeval_s_cleaned.json",
    "oracle": "longmemeval_oracle.json",
    "m": "longmemeval_m_cleaned.json",
}
# sha256 of the files this harness was developed against (downloaded 2026-11-20)
DATASET_SHA256 = {
    "s": "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442",
    "oracle": "821a2034d219ab45846873dd14c14f12cfe7776e73527a483f9dac095d38620c",
}

# USD per million tokens (input, output), first-party list prices on 2026-11-20.
# Claude Haiku 5.5 has a second tier for prompts above 100K tokens.
PRICES = {
    "claude-haiku-5-5": {"in": 0.10, "out": 0.50, "in_long": 0.50, "out_long": 2.50, "long_at": 100_000},
    "claude-sonnet-5-5": {"in": 2.00, "out": 10.00},
    "claude-opus-5-5": {"in": 4.00, "out": 20.00},
}
BATCH_DISCOUNT = 0.5

QUESTION_TYPES = (
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
    "temporal-reasoning",
    "knowledge-update",
)


def work_dir() -> Path:
    root = os.environ.get("SEXTANT_PROOF_WORK", "").strip()
    path = Path(root) if root else HERE / ".work"
    path.mkdir(parents=True, exist_ok=True)
    return path


def questions_dir(split: str) -> Path:
    return work_dir() / "questions" / split


def is_abstention(question_id: str) -> bool:
    return "_abs" in str(question_id)


def clean_entry(entry: dict) -> dict:
    """Drop the per-turn `has_answer` label so it can never reach a model or an index."""
    sessions = [
        [{"role": turn["role"], "content": turn["content"]} for turn in session]
        for session in entry["haystack_sessions"]
    ]
    out = dict(entry)
    out["haystack_sessions"] = sessions
    out["answer"] = str(entry["answer"])
    return out


def split() -> str:
    """Which haystack size the paid stages work on: `s` (about 50 sessions a question) or `m`
    (about 500). The questions are the same 500 in both."""
    return os.environ.get("SEXTANT_PROOF_SPLIT", "").strip() or "s"


def _entries(source: Path):
    """The dataset's entries one at a time. The `m` file is 2.7 GB, so it is streamed when
    `ijson` is installed; without it the file is loaded whole."""
    try:
        import ijson
    except ImportError:
        yield from json.loads(source.read_text(encoding="utf-8"))
        return
    with source.open("rb") as fh:
        yield from ijson.items(fh, "item")


def split_dataset(split: str, source: Path) -> int:
    """Write one JSON file per question so workers never parse the whole dataset."""
    target = questions_dir(split)
    target.mkdir(parents=True, exist_ok=True)
    data = _entries(source)
    index = []
    for entry in data:
        entry = clean_entry(entry)
        qid = str(entry["question_id"])
        (target / f"{qid}.json").write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        index.append({"question_id": qid, "question_type": entry["question_type"],
                      "sessions": len(entry["haystack_sessions"])})
    (target / "_index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    return len(index)


def load_index(split: str) -> list[dict]:
    return json.loads((questions_dir(split) / "_index.json").read_text(encoding="utf-8"))


def load_question(split: str, qid: str) -> dict:
    return json.loads((questions_dir(split) / f"{qid}.json").read_text(encoding="utf-8"))


def chronological(entry: dict) -> list[tuple[int, str, list[dict]]]:
    """Sessions as (original position, date string, turns), oldest first.

    Dates look like `2023/04/10 (Mon) 17:50`, which sorts correctly as text.
    """
    rows = [(i, entry["haystack_dates"][i], entry["haystack_sessions"][i])
            for i in range(len(entry["haystack_sessions"]))]
    return sorted(rows, key=lambda r: (r[1], r[0]))


def estimate_tokens(text: str) -> int:
    """Planning estimate only: 2.8 characters per token. The first pilot measured about 2.9
    on this data, so this runs a little high. Billing always uses the usage block the API
    returns."""
    return int(len(text) / 2.8) + 8


def price(model: str, input_tokens: int, output_tokens: int, *, batch: bool) -> float:
    p = PRICES[model]
    long = "long_at" in p and input_tokens > p["long_at"]
    cost = (input_tokens * (p["in_long"] if long else p["in"])
            + output_tokens * (p["out_long"] if long else p["out"])) / 1_000_000
    return cost * (BATCH_DISCOUNT if batch else 1.0)
