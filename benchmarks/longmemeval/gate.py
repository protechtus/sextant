"""The spend gate: every hosted-model call in this benchmark goes through here.

Three rules, enforced in code rather than by habit:

1. A job whose estimated cost at interactive prices is above the sync limit (default 1 USD)
   is sent through the Message Batches API, which bills at half price. Asking for a
   synchronous run of such a job is refused.
2. A hard budget (default 150 USD) covers settled spend plus the estimates of everything
   still in flight. A dispatch that would cross it is refused.
3. Every dispatch and every settlement is appended to a ledger, with the estimate next to the
   amount computed from the usage blocks the API returned.

A job is a folder under <work>/jobs/<name>/ holding requests.jsonl (one
{"custom_id", "params"} per line) and meta.json. Results land beside it as results.jsonl.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

BATCH_MAX_REQUESTS = 2_000
BATCH_MAX_BYTES = 150 * 1024 * 1024   # the API limit is 256 MB; stay well under it


class GateError(RuntimeError):
    """A dispatch the rules do not allow."""


def sync_limit() -> float:
    return float(os.environ.get("SEXTANT_PROOF_SYNC_LIMIT", "1.00"))


def budget() -> float:
    return float(os.environ.get("SEXTANT_PROOF_BUDGET", "150"))


def jobs_dir() -> Path:
    path = common.work_dir() / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def job_dir(name: str) -> Path:
    return jobs_dir() / name


def ledger_path() -> Path:
    return common.work_dir() / "ledger.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- jobs

def write_job(name: str, requests: list[dict], meta: dict) -> Path:
    folder = job_dir(name)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "requests.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for request in requests:
            fh.write(json.dumps(request, ensure_ascii=False) + "\n")
    meta = dict(meta, name=name, requests=len(requests), built=_now())
    (folder / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return folder


def read_requests(name: str) -> list[dict]:
    with (job_dir(name) / "requests.jsonl").open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def read_meta(name: str) -> dict:
    return json.loads((job_dir(name) / "meta.json").read_text(encoding="utf-8"))


def read_results(name: str) -> dict[str, dict]:
    path = job_dir(name) / "results.jsonl"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    return {row["custom_id"]: row for row in rows}


def _request_text(params: dict) -> str:
    parts = []
    system = params.get("system")
    if isinstance(system, str):
        parts.append(system)
    for message in params.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        else:
            parts.extend(json.dumps(block, ensure_ascii=False) for block in content or [])
    if params.get("tools"):
        parts.append(json.dumps(params["tools"]))
    return "\n".join(parts)


def estimate(name: str) -> dict:
    """Planning figures for a job. Output tokens are taken from the job's `expect_out`."""
    meta = read_meta(name)
    expect_out = int(meta.get("expect_out", 600))
    tokens_in = sync = batch = 0.0
    count = 0
    for request in read_requests(name):
        params = request["params"]
        n_in = common.estimate_tokens(_request_text(params))
        tokens_in += n_in
        sync += common.price(params["model"], n_in, expect_out, batch=False)
        batch += common.price(params["model"], n_in, expect_out, batch=True)
        count += 1
    return {"job": name, "requests": count, "input_tokens": int(tokens_in),
            "output_tokens": count * expect_out, "sync_usd": round(sync, 4), "batch_usd": round(batch, 4)}


# ---------------------------------------------------------------- ledger

def ledger() -> list[dict]:
    path = ledger_path()
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _append_ledger(entry: dict) -> None:
    with ledger_path().open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(dict(entry, ts=_now())) + "\n")


def committed() -> dict:
    """Settled spend plus the estimates of jobs dispatched and not yet settled."""
    settled: dict[str, float] = {}
    pending: dict[str, float] = {}
    for entry in ledger():
        if entry["event"] == "dispatch":
            pending[entry["job"]] = entry["est_usd"]
        elif entry["event"] == "settle":
            pending.pop(entry["job"], None)
            settled[entry["job"]] = settled.get(entry["job"], 0.0) + entry["actual_usd"]
    return {"settled_usd": round(sum(settled.values()), 4), "pending_usd": round(sum(pending.values()), 4),
            "total_usd": round(sum(settled.values()) + sum(pending.values()), 4)}


def route(name: str, mode: str = "auto") -> tuple[str, dict]:
    """Decide how a job may run. Raises GateError when the rules forbid it."""
    if (job_dir(name) / "state.json").exists():
        raise GateError(f"{name}: already dispatched (state.json exists); collect it or build a new job")
    est = estimate(name)
    over = est["sync_usd"] > sync_limit()
    if mode == "sync" and over:
        raise GateError(
            f"{name}: estimated {est['sync_usd']:.2f} USD at interactive prices is above the "
            f"{sync_limit():.2f} USD sync limit; this job must go through the Batch API")
    chosen = "batch" if (mode == "batch" or (mode == "auto" and over)) else "sync"
    cost = est["batch_usd"] if chosen == "batch" else est["sync_usd"]
    spent = committed()
    if spent["total_usd"] + cost > budget():
        raise GateError(
            f"{name}: {cost:.2f} USD would take committed spend to "
            f"{spent['total_usd'] + cost:.2f} USD, above the {budget():.2f} USD budget")
    return chosen, dict(est, route=chosen, est_usd=round(cost, 4))


# ---------------------------------------------------------------- API

def load_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    path = Path(os.environ.get("SEXTANT_PROOF_KEY_FILE", "") or Path.home() / ".sextant-proof" / "anthropic.key")
    if path.exists():
        key = path.read_text(encoding="utf-8-sig").strip()
        if key:
            return key
    raise GateError(f"no API key: set ANTHROPIC_API_KEY for this process or put the key in {path}")


def client():
    import anthropic

    # a key that is not scoped to one workspace must name the workspace on every request
    workspace = os.environ.get("SEXTANT_PROOF_WORKSPACE_ID", "").strip()
    id_file = Path.home() / ".sextant-proof" / "workspace.id"
    if not workspace and id_file.exists():
        workspace = id_file.read_text(encoding="utf-8-sig").strip()
    headers = {"anthropic-workspace-id": workspace} if workspace else None
    return anthropic.Anthropic(api_key=load_key(), max_retries=4, timeout=600.0, default_headers=headers)


def _drop_none(value):
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


def _normalise(custom_id: str, message) -> dict:
    text = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
    usage = message.usage
    raw = message.to_dict().get("content", []) if hasattr(message, "to_dict") else []
    return {
        "custom_id": custom_id, "type": "succeeded", "text": text.strip(),
        "content": _drop_none(raw),   # the turn as returned, for replay in a multi-round job
        "stop_reason": message.stop_reason, "model": message.model,
        "tool_uses": [{"id": b.id, "name": b.name, "input": b.input}
                      for b in message.content if getattr(b, "type", "") == "tool_use"],
        "usage": {
            "input_tokens": int(usage.input_tokens or 0),
            "output_tokens": int(usage.output_tokens or 0),
            "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            "cache_creation_input_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        },
    }


def _actual_cost(rows: list[dict], *, batch: bool) -> float:
    total = 0.0
    for row in rows:
        if row["type"] != "succeeded":
            continue
        usage = row["usage"]
        prompt = usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"]
        total += common.price(row["model_requested"], prompt, usage["output_tokens"], batch=batch)
    return round(total, 4)


def _write_results(name: str, rows: list[dict]) -> None:
    with (job_dir(name) / "results.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _settle(name: str, rows: list[dict], *, batch: bool, est_usd: float) -> dict:
    actual = _actual_cost(rows, batch=batch)
    failed = sum(1 for r in rows if r["type"] != "succeeded")
    entry = {"event": "settle", "job": name, "route": "batch" if batch else "sync",
             "requests": len(rows), "failed": failed, "est_usd": est_usd, "actual_usd": actual,
             "input_tokens": sum(r["usage"]["input_tokens"] for r in rows if r["type"] == "succeeded"),
             "output_tokens": sum(r["usage"]["output_tokens"] for r in rows if r["type"] == "succeeded")}
    _append_ledger(entry)
    return entry


def dispatch(name: str, mode: str = "auto") -> dict:
    import anthropic

    chosen, est = route(name, mode)
    requests = read_requests(name)
    api = client()
    _append_ledger({"event": "dispatch", "job": name, "route": chosen, "requests": len(requests),
                    "est_usd": est["est_usd"]})
    state = {"route": chosen, "est_usd": est["est_usd"], "dispatched": _now(), "batches": []}
    state_path = job_dir(name) / "state.json"

    if chosen == "batch":
        chunk: list[dict] = []
        size = 0
        chunks = []
        for request in requests:
            n = len(json.dumps(request, ensure_ascii=False).encode("utf-8"))
            if chunk and (len(chunk) >= BATCH_MAX_REQUESTS or size + n > BATCH_MAX_BYTES):
                chunks.append(chunk)
                chunk, size = [], 0
            chunk.append(request)
            size += n
        if chunk:
            chunks.append(chunk)
        for chunk in chunks:
            batch = api.messages.batches.create(requests=chunk)
            state["batches"].append({"id": batch.id, "requests": len(chunk)})
            state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")  # after every create: resumable
        return dict(est, batches=[b["id"] for b in state["batches"]])

    # A synchronous job has no state file until it has finished and its rows are on disk. Written
    # before the calls, an interrupted run left a state with no batches, which a later `collect`
    # read as "ended", settling the job at 0 USD with no results (code review 2026-11-23). Now an
    # interrupted run is simply dispatched again; its estimate stays in the ledger as in flight,
    # so the budget errs high, never low.
    rows = []
    for request in requests:
        params = request["params"]
        try:
            message = api.messages.create(**params)
            row = _normalise(request["custom_id"], message)
        except anthropic.BadRequestError as exc:
            row = {"custom_id": request["custom_id"], "type": "errored", "error": f"invalid_request: {exc.message}"}
        except anthropic.RateLimitError as exc:
            row = {"custom_id": request["custom_id"], "type": "errored", "error": f"rate_limit: {exc.message}"}
        except anthropic.APIStatusError as exc:
            row = {"custom_id": request["custom_id"], "type": "errored", "error": f"api_{exc.status_code}: {exc.message}"}
        except anthropic.APIConnectionError as exc:
            row = {"custom_id": request["custom_id"], "type": "errored", "error": f"connection: {exc}"}
        row["model_requested"] = params["model"]
        rows.append(row)
    _write_results(name, rows)
    state["collected"] = _now()
    state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
    return dict(est, settled=_settle(name, rows, batch=False, est_usd=est["est_usd"]))


def status(name: str) -> dict:
    state_path = job_dir(name) / "state.json"
    if not state_path.exists():
        return {"job": name, "state": "built"}
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("collected"):
        return {"job": name, "state": "collected", "route": state["route"]}
    api = client()
    counts = {"processing": 0, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0}
    if not state["batches"]:
        # nothing was ever submitted (a state file from an interrupted run): not a finished job
        return {"job": name, "state": "built", **counts}
    ended = True
    for item in state["batches"]:
        batch = api.messages.batches.retrieve(item["id"])
        ended = ended and batch.processing_status == "ended"
        for key in counts:
            counts[key] += int(getattr(batch.request_counts, key, 0) or 0)
    return {"job": name, "state": "ended" if ended else "in_progress", "route": state["route"], **counts}


def collect(name: str) -> dict:
    """Download a finished batch job's results and settle it in the ledger."""
    state_path = job_dir(name) / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("collected"):
        return {"job": name, "state": "collected"}
    current = status(name)
    if current["state"] != "ended":
        return current
    api = client()
    models = {r["custom_id"]: r["params"]["model"] for r in read_requests(name)}
    rows = []
    for item in state["batches"]:
        for result in api.messages.batches.results(item["id"]):
            kind = result.result.type
            if kind == "succeeded":
                row = _normalise(result.custom_id, result.result.message)
            elif kind == "errored":
                error = result.result.error
                detail = getattr(getattr(error, "error", None), "message", "") or str(error)
                row = {"custom_id": result.custom_id, "type": "errored", "error": detail[:500]}
            else:
                row = {"custom_id": result.custom_id, "type": kind, "error": kind}
            row["model_requested"] = models.get(result.custom_id, "")
            rows.append(row)
    rows.sort(key=lambda r: r["custom_id"])
    _write_results(name, rows)
    state["collected"] = _now()
    state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
    return {"job": name, "state": "collected", "settled": _settle(name, rows, batch=True, est_usd=state["est_usd"])}
