"""The agent arm: the model searches Sextant itself, as it would over MCP.

The single-search arms hand the model whatever one search for the question as typed brought
back. Here the model gets two tools, `memory_search` and `memory_read`, and decides what to
look for, how often, and which notes to open. That is how Sextant is used in practice.

It runs as rounds so that the Batch API can carry it: round 1 sends every question; the tool
calls that come back are executed locally against each question's own corpus; round 2 sends
the conversations that are still open with the tool results appended; and so on. The last
round is sent with tool use switched off, so every question ends with an answer.

    python agent.py run hybrid-v1 sonnet [--per-type N | --every N] [--suffix .pilot]
                                         [--max-rounds 5] [--mode auto|batch|sync]

When it finishes it writes an ordinary answer job, `ans.agent-<mode>-<tag>.<model>.rN`, whose
token counts are the sum over all rounds, so the judge and the report treat it like any arm.
"""

from __future__ import annotations

import concurrent.futures as futures
import json
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arms  # noqa: E402
import common  # noqa: E402
import corpus  # noqa: E402
import gate  # noqa: E402
import retrieve  # noqa: E402

SYSTEM = (
    "You are an assistant with a memory of your past conversations with this user. The memory "
    "is a set of notes, one per past conversation. Use memory_search to find notes and "
    "memory_read to read a note in full; a search result shows only a short snippet, so read "
    "the notes that look relevant before you answer. A question may depend on several "
    "conversations: search more than once, with different words, when one search may not have "
    "found everything. When you have what you need, answer the user's question. If the memory "
    "does not contain the information, say so."
)

# Second wording, written after reading the first run's misses on the development half: when
# Sonnet opened every evidence note it was right 131 times out of 133; 15 of its 19 misses were
# answers given from snippets with an evidence note left unopened.
SYSTEM_2 = SYSTEM + (
    " A snippet is a few words around one match and is never the whole story: do not answer "
    "from snippets. Before you answer, open every note that could bear on the question. When "
    "the question counts, totals, compares, or asks what came first or last, more than one "
    "conversation is usually involved: open every candidate note, and search again with other "
    "words to check that you have not missed one."
)
# Third variant: the wording alone moved Sonnet from 1.44 to 1.65 notes opened per question
# and the score by about a point (development half). So the tool does the opening: a search
# returns its best notes in full, and a snippet for the rest.
EXPAND = 3
EXPAND_CHARS = 24_000
SYSTEM_3 = SYSTEM + (
    " A search returns its three best notes in full and a snippet for the others; open any "
    "other note that could bear on the question before you answer. When the question counts, "
    "totals, compares, or asks what came first or last, more than one conversation is usually "
    "involved: search again with other words to check that you have not missed one."
)
PROMPTS = {"1": SYSTEM, "2": SYSTEM_2, "3": SYSTEM_3}

FINAL = ("You have used all your searches. Answer the question now from what you have found. "
         "If the memory does not contain the information, say so.")

TOOLS = [
    {"name": "memory_search",
     "description": ("Search the memory notes. Returns the best matching notes, each with its id, the date of "
                     "the conversation and a short snippet. Works on the words in the notes: use the "
                     "distinctive words you expect the conversation to contain."),
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "words to look for"},
         "limit": {"type": "integer", "description": "how many notes to return, 1 to 10 (default 5)"}},
         "required": ["query"]}},
    {"name": "memory_read",
     "description": "Read one memory note in full.",
     "input_schema": {"type": "object", "properties": {
         "note": {"type": "string", "description": "a note id from a search result, for example s017"}},
         "required": ["note"]}},
]


def _dates(entry: dict) -> dict[int, str]:
    return {i: entry["haystack_dates"][i] for i in range(len(entry["haystack_dates"]))}


def _tools(expand: int) -> list[dict]:
    if not expand:
        return TOOLS
    search = dict(TOOLS[0], description=(
        f"Search the memory notes. Returns the best matching notes with their id and the date of the "
        f"conversation: the top {expand} in full, the others as a short snippet. Works on the words in "
        "the notes: use the distinctive words you expect the conversation to contain."))
    return [search, TOOLS[1]]


def _run_tools(qid: str, mode: str, calls: list[dict], expand: int = 0) -> list[dict]:
    """Execute one question's tool calls; returns tool_result blocks in the order asked."""
    entry = common.load_question(common.split(), qid)
    dates = _dates(entry)
    searches = [c for c in calls if c["name"] == "memory_search"]
    found: dict[str, dict] = {}
    if searches:
        queries = [str(c["input"].get("query", ""))[:500] for c in searches]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump(queries, fh)
        try:
            out = retrieve.run_worker(common.split(), mode, qid, ["--queries", fh.name])
        finally:
            Path(fh.name).unlink(missing_ok=True)
        found = {c["id"]: s for c, s in zip(searches, out["searches"])}
    blocks = []
    for call in calls:
        if call["name"] == "memory_search":
            try:
                limit = max(1, min(10, int(call["input"].get("limit", 5))))
            except (TypeError, ValueError):
                limit = 5
            result = found[call["id"]]
            hits = result["hits"][:limit]
            if not hits:
                text = "No notes matched."
            else:
                lines = [f"{len(hits)} notes (confidence: {result['confidence']}):"]
                lines += [f"- s{h['position']:03d} | {dates.get(h['position'], '?')} | score {h['score']:.2f} | {h['snippet']}"
                          for h in hits]
                for h in hits[:expand]:
                    body = (corpus.corpus_root(common.split(), qid) / "memory" / "sessions"
                            / f"s{h['position']:03d}.md").read_text(encoding="utf-8")
                    if len(body) > EXPAND_CHARS:
                        body = body[:EXPAND_CHARS] + f"\n[cut here; memory_read s{h['position']:03d} for the rest]"
                    lines.append(f"\n===== s{h['position']:03d} in full =====\n{body}")
                text = "\n".join(lines)
        elif call["name"] == "memory_read":
            note = str(call["input"].get("note", "")).strip().lower().removesuffix(".md")
            path = corpus.corpus_root(common.split(), qid) / "memory" / "sessions" / f"{note}.md"
            if re.fullmatch(r"s\d{3}", note) and path.exists():
                text = path.read_text(encoding="utf-8")
            else:
                text = f"There is no note {note!r}. Use an id from a search result, such as s017."
        else:
            text = f"Unknown tool {call['name']!r}."
        blocks.append({"type": "tool_result", "tool_use_id": call["id"], "content": text})
    return blocks


def _wait(job: str, poll: int) -> None:
    while True:
        outcome = gate.collect(job)
        if outcome["state"] == "collected":
            return
        time.sleep(poll)


def run(mode_tag: str, model: str, rows: list[dict], rep: int = 1, suffix: str = "",
        max_rounds: int = 5, route: str = "auto", poll: int = 60, finish_empty: bool = False,
        prompt: str = "1") -> str:
    mode = mode_tag.split("-")[0]
    kind = "agent" if prompt == "1" else f"agent{prompt}"   # the arm name carries the wording
    system = PROMPTS[prompt]
    expand = EXPAND if prompt == "3" else 0
    if common.split() != "s":
        mode_tag = f"{mode_tag}-{common.split()}"   # keep the larger haystack's jobs and arm apart
    base = f"agt{'' if prompt == '1' else prompt}.{mode_tag}.{model}.r{rep}{suffix}"
    folder = common.work_dir() / "agent" / base
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    else:
        state = {"applied": 0, "convos": {}}
        for row in rows:
            entry = common.load_question(common.split(), row["question_id"])
            state["convos"][row["question_id"]] = {
                "messages": [{"role": "user", "content":
                              f"Current Date: {entry['question_date']}\nQuestion: {entry['question']}"}],
                "done": False, "answer": "", "state": "open", "rounds": 0, "searches": 0, "reads": 0,
                "input_tokens": 0, "output_tokens": 0}
    convos = state["convos"]
    if finish_empty:
        # re-ask, once, the conversations that ended on an empty turn
        reopened = [c for c in convos.values() if c["state"].startswith("empty")]
        for c in reopened:
            c.update(done=False, state="open")
        max_rounds = state["applied"] + 1 if reopened else state["applied"]

    for rnd in range(state["applied"] + 1, max_rounds + 1):
        active = [q for q, c in convos.items() if not c["done"]]
        if not active:
            break
        job = f"{base}.round{rnd}"
        last = rnd == max_rounds
        if not (gate.job_dir(job) / "requests.jsonl").exists():
            requests = []
            for qid in active:
                params = {"model": arms.MODELS[model], "max_tokens": 4096,
                          "output_config": {"effort": "medium"}, "system": system, "tools": _tools(expand),
                          "messages": convos[qid]["messages"]}
                if last:
                    # tools off, and say so: with tools silently off, a model that wanted one more
                    # search returned an empty turn (12 of 500 per model in the first run)
                    params["tool_choice"] = {"type": "none"}
                    tail = params["messages"][-1]
                    if isinstance(tail["content"], list):
                        params["messages"] = params["messages"][:-1] + [
                            {"role": "user", "content": tail["content"] + [{"type": "text", "text": FINAL}]}]
                requests.append({"custom_id": qid, "params": params})
            gate.write_job(job, requests, {"kind": "agent-round", "round": rnd, "model": arms.MODELS[model],
                                           "expect_out": 500})
        if not (gate.job_dir(job) / "state.json").exists():
            est = gate.dispatch(job, route)
            print(f"round {rnd}: {len(active)} open, route {est['route']}, estimate {est['est_usd']:.2f} USD", flush=True)
        _wait(job, poll)
        results = gate.read_results(job)

        pending: dict[str, list[dict]] = {}
        for qid in active:
            convo = convos[qid]
            row = results.get(qid)
            if not row or row["type"] != "succeeded":
                convo.update(done=True, state="error")
                continue
            usage = row["usage"]
            convo["rounds"] = rnd
            convo["input_tokens"] += (usage["input_tokens"] + usage["cache_read_input_tokens"]
                                      + usage["cache_creation_input_tokens"])
            convo["output_tokens"] += usage["output_tokens"]
            if row["stop_reason"] == "tool_use" and row["tool_uses"] and not last:
                convo["messages"].append({"role": "assistant", "content": row["content"]})
                pending[qid] = row["tool_uses"]
                convo["searches"] += sum(t["name"] == "memory_search" for t in row["tool_uses"])
                convo["reads"] += sum(t["name"] == "memory_read" for t in row["tool_uses"])
            else:
                convo.update(done=True, answer=row["text"],
                             state="answered" if row["text"] else f"empty:{row['stop_reason']}")
        with futures.ThreadPoolExecutor(max_workers=3) as pool:
            jobs = {pool.submit(_run_tools, qid, mode, calls, expand): qid for qid, calls in pending.items()}
            for future in futures.as_completed(jobs):
                convos[jobs[future]]["messages"].append({"role": "user", "content": future.result()})
        state["applied"] = rnd
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        print(f"round {rnd} applied: {sum(c['done'] for c in convos.values())}/{len(convos)} finished", flush=True)

    # an ordinary answer job, so the judge and the report need no special case
    name = arms.answer_job_name(f"{kind}-{mode_tag}", model, rep, suffix)
    gate.write_job(name, [{"custom_id": q, "params": {"model": arms.MODELS[model]}} for q in convos],
                   {"kind": "answer", "arm": f"{kind}-{mode_tag}", "model": arms.MODELS[model], "rep": rep,
                    "max_rounds": max_rounds, "expect_out": 0})
    rows_out = []
    for qid, c in sorted(convos.items()):
        rows_out.append({"custom_id": qid, "type": "succeeded" if c["state"] != "error" else "errored",
                         "text": c["answer"], "stop_reason": c["state"], "model": arms.MODELS[model],
                         "model_requested": arms.MODELS[model], "tool_uses": [],
                         "rounds": c["rounds"], "searches": c["searches"], "reads": c["reads"],
                         "usage": {"input_tokens": c["input_tokens"], "output_tokens": c["output_tokens"],
                                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}})
    gate._write_results(name, rows_out)
    (gate.job_dir(name) / "state.json").write_text(json.dumps(
        {"route": "agent-rounds", "est_usd": 0, "batches": [], "collected": gate._now(),
         "note": "spend is settled under the agt.* round jobs"}, indent=1), encoding="utf-8")
    print(f"wrote {name}: {sum(1 for r in rows_out if r['text'])}/{len(rows_out)} answered, "
          f"mean rounds {sum(r['rounds'] for r in rows_out) / len(rows_out):.2f}, "
          f"mean searches {sum(r['searches'] for r in rows_out) / len(rows_out):.2f}, "
          f"mean reads {sum(r['reads'] for r in rows_out) / len(rows_out):.2f}", flush=True)
    return name


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "run":
        raise SystemExit(__doc__)

    def opt(flag: str, default: str | None = None) -> str | None:
        return argv[argv.index(flag) + 1] if flag in argv else default

    per_type, every = opt("--per-type"), opt("--every")
    rows = arms.select(per_type=int(per_type) if per_type else None, every=int(every) if every else None,
                       offset=int(opt("--offset", "0")))
    try:
        run(argv[1], argv[2], rows, rep=int(opt("--rep", "1")),
            suffix=opt("--suffix", "") or "", max_rounds=int(opt("--max-rounds", "5")),
            route=opt("--mode", "auto") or "auto", poll=int(opt("--poll", "60")),
            finish_empty="--finish-empty" in argv, prompt=opt("--prompt", "1") or "1")
    except gate.GateError as exc:
        print(f"REFUSED: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
