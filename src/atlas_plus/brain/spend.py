"""Token-spend ledger (ROADMAP item 5, spend v2).

Usage is captured at the provider seam: every ``ChatResponse`` carries
``prompt_tokens``/``completion_tokens`` (Ollama ``*_eval_count``,
Anthropic ``usage.input/output_tokens``, OpenAI ``usage.*``), and
``agent.chat`` records one ledger row per turn -- so BOTH task runs and
interactive Brain chat are counted, which per-task columns alone would
have missed.

Cost is an estimate: tokens x a small per-1M price table. Local models
price at zero. Override or extend with ``ATLAS_MODEL_PRICES`` (JSON,
``{"model-prefix": [in_per_1M, out_per_1M]}``); longest matching prefix
wins. Prices drift -- treat the dollar figure as a magnitude, not a bill.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("atlas.brain.spend")

_CREATE = """
CREATE TABLE IF NOT EXISTS spend_log (
    ts                REAL NOT NULL,
    day               TEXT NOT NULL,
    session_id        TEXT,
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0
);
"""
_CREATE_INDEX = "CREATE INDEX IF NOT EXISTS idx_spend_day ON spend_log(day);"

# Per-1M-token (input, output) USD. Cloud defaults only; anything unmatched
# (local Ollama tags) is 0.0. Kept deliberately tiny -- ATLAS_MODEL_PRICES
# extends/overrides without a code change.
_DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-haiku": (1.00, 5.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-opus": (15.00, 75.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
}


def _db_path() -> Path:
    from atlas_plus.brain import tasks as task_store
    return task_store._DB_PATH


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(_CREATE)
    conn.execute(_CREATE_INDEX)
    return conn


def price_table() -> dict[str, tuple[float, float]]:
    table = dict(_DEFAULT_PRICES)
    raw = os.environ.get("ATLAS_MODEL_PRICES", "")
    if raw:
        try:
            for prefix, pair in json.loads(raw).items():
                table[str(prefix)] = (float(pair[0]), float(pair[1]))
        except (ValueError, TypeError, IndexError, KeyError):
            log.warning("ATLAS_MODEL_PRICES is not valid JSON price data; ignored")
    return table


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Longest-prefix price match; unknown models (local) cost 0."""
    model_l = (model or "").lower()
    best: tuple[float, float] | None = None
    best_len = -1
    for prefix, pair in price_table().items():
        if model_l.startswith(prefix.lower()) and len(prefix) > best_len:
            best, best_len = pair, len(prefix)
    if best is None:
        return 0.0
    return prompt_tokens / 1e6 * best[0] + completion_tokens / 1e6 * best[1]


def record_usage(
    session_id: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    db_path: Optional[Path] = None,
) -> None:
    """Append one ledger row. Soft-fail: spend accounting must never break chat."""
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return
    try:
        now = time.time()
        day = datetime.fromtimestamp(now, tz=timezone.utc).date().isoformat()
        with _connect(db_path) as conn:
            conn.execute(
                "INSERT INTO spend_log(ts, day, session_id, model, prompt_tokens, completion_tokens)"
                " VALUES (?,?,?,?,?,?)",
                (now, day, session_id, model, prompt_tokens, completion_tokens),
            )
            # Retention (expert review 2026-09-27): the ledger otherwise
            # grows forever on a box meant to run for years. A year of
            # per-turn rows is plenty for spend trends.
            conn.execute("DELETE FROM spend_log WHERE ts < ?", (now - 365 * 86400,))
    except Exception:  # noqa: BLE001
        log.debug("spend record_usage failed (non-fatal)", exc_info=True)


def summary(days: int = 7, db_path: Optional[Path] = None) -> dict:
    """Aggregate the last ``days`` days: totals, per-day, per-model, est cost."""
    try:
        cutoff = time.time() - days * 86400
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT day, model, SUM(prompt_tokens) AS pt, SUM(completion_tokens) AS ct,"
                " COUNT(*) AS turns FROM spend_log WHERE ts >= ?"
                " GROUP BY day, model ORDER BY day",
                (cutoff,),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return {"days": days, "available": False, "reason": "spend ledger unreadable"}

    by_day: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    total_pt = total_ct = total_turns = 0
    total_cost = 0.0
    for r in rows:
        pt, ct = int(r["pt"] or 0), int(r["ct"] or 0)
        cost = estimate_cost_usd(r["model"] or "", pt, ct)
        d = by_day.setdefault(r["day"], {"prompt_tokens": 0, "completion_tokens": 0, "est_cost_usd": 0.0})
        d["prompt_tokens"] += pt
        d["completion_tokens"] += ct
        d["est_cost_usd"] = round(d["est_cost_usd"] + cost, 4)
        m = by_model.setdefault(r["model"] or "unknown",
                                {"prompt_tokens": 0, "completion_tokens": 0, "turns": 0, "est_cost_usd": 0.0})
        m["prompt_tokens"] += pt
        m["completion_tokens"] += ct
        m["turns"] += int(r["turns"] or 0)
        m["est_cost_usd"] = round(m["est_cost_usd"] + cost, 4)
        total_pt += pt
        total_ct += ct
        total_turns += int(r["turns"] or 0)
        total_cost += cost
    return {
        "days": days,
        "available": True,
        "total_prompt_tokens": total_pt,
        "total_completion_tokens": total_ct,
        "total_turns": total_turns,
        "est_cost_usd": round(total_cost, 4),
        "by_day": by_day,
        "by_model": by_model,
    }
