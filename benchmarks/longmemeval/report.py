"""Turn judged answers into the numbers: accuracy with intervals, cost, and paired tests.

Scoring rules:
- An answer the judge calls correct scores 1, anything else 0.
- A question with no usable answer (API error, refusal, empty text) scores 0 and is counted
  under `unanswered`.
- A judge reply that cannot be read as yes or no is counted under `unparsed` and scores 0.
- Abstention questions (ids containing `_abs`) are reported as their own group, as in the
  LongMemEval paper, and are also part of the overall figure.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arms  # noqa: E402
import common  # noqa: E402
import gate  # noqa: E402


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def score_job(answer_job: str, judge: str = "opus") -> dict | None:
    """Per-question outcome for one answer job, or None if it has not been judged yet."""
    answers = gate.read_results(answer_job)
    judged = gate.read_results(f"judge-{judge}.{answer_job}")
    if not answers or not judged:
        return None
    meta = gate.read_meta(answer_job)
    rows = {}
    for request in gate.read_requests(answer_job):
        qid = request["custom_id"]
        answer = answers.get(qid)
        row = {"correct": False, "state": "ok", "input_tokens": 0, "output_tokens": 0}
        if not answer or answer["type"] != "succeeded" or not answer.get("text"):
            row["state"] = "unanswered"
        else:
            usage = answer["usage"]
            row["input_tokens"] = (usage["input_tokens"] + usage["cache_read_input_tokens"]
                                   + usage["cache_creation_input_tokens"])
            row["output_tokens"] = usage["output_tokens"]
            reply = judged.get(qid)
            label = arms.verdict(reply["text"]) if reply and reply["type"] == "succeeded" else None
            if label is None:
                row["state"] = "unparsed"
            else:
                row["correct"] = label
        rows[qid] = row
    return {"job": answer_job, "arm": meta["arm"], "model": meta["model"], "rep": meta["rep"], "rows": rows}


def summarise(scored: dict) -> dict:
    index = {r["question_id"]: r["question_type"] for r in common.load_index("s")}
    rows = scored["rows"]
    groups: dict[str, list[str]] = {"overall": list(rows)}
    for qtype in common.QUESTION_TYPES:
        groups[qtype] = [q for q in rows if index[q] == qtype and not common.is_abstention(q)]
    groups["abstention"] = [q for q in rows if common.is_abstention(q)]
    out = {"job": scored["job"], "arm": scored["arm"], "model": scored["model"], "rep": scored["rep"],
           "groups": {}}
    for name, qids in groups.items():
        if not qids:
            continue
        hits = sum(rows[q]["correct"] for q in qids)
        low, high = wilson(hits, len(qids))
        out["groups"][name] = {"n": len(qids), "correct": hits, "accuracy": round(hits / len(qids), 4),
                               "ci95": [round(low, 4), round(high, 4)]}
    answered = [r for r in rows.values() if r["state"] != "unanswered"]
    n = max(1, len(answered))
    mean_in = sum(r["input_tokens"] for r in answered) / n
    mean_out = sum(r["output_tokens"] for r in answered) / n
    out["unanswered"] = sum(1 for r in rows.values() if r["state"] == "unanswered")
    out["unparsed"] = sum(1 for r in rows.values() if r["state"] == "unparsed")
    out["mean_input_tokens"] = round(mean_in)
    out["mean_output_tokens"] = round(mean_out)
    per_request = agent_request_costs(scored["job"], scored["model"])
    if per_request is None:
        cost = sum(common.price(scored["model"], r["input_tokens"], r["output_tokens"], batch=False) for r in answered)
    else:
        # An agent conversation is several requests. Each is priced on its own prompt: pricing the
        # conversation's summed tokens as one request put every token of a long Haiku conversation
        # in the above-100K tier (code review 2026-11-23).
        cost = sum(per_request.get(q, 0.0) for q, r in rows.items() if r["state"] != "unanswered")
    out["usd_per_question_list"] = round(cost / n, 6)
    return out


def agent_request_costs(answer_job: str, model: str) -> dict[str, float] | None:
    """List price per question for an agent arm, summed over the requests that were made
    (`agt*.roundN` jobs). None for an arm that is one request per question."""
    m = re.match(r"ans\.agent(\d*)-(.+)$", answer_job)
    if not m:
        return None
    base = f"agt{m.group(1)}.{m.group(2)}"
    costs: dict[str, float] = {}
    rounds = sorted(gate.jobs_dir().glob(f"{base}.round*"))
    if not rounds:
        return None
    for folder in rounds:
        for qid, row in gate.read_results(folder.name).items():
            usage = row.get("usage")
            if row.get("type") != "succeeded" or not usage:
                continue
            tokens_in = usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"]
            costs[qid] = costs.get(qid, 0.0) + common.price(model, tokens_in, usage["output_tokens"], batch=False)
    return costs


def paired(a: dict, b: dict) -> dict:
    """a against b on the questions both answered: b_only = b right and a wrong."""
    shared = sorted(set(a["rows"]) & set(b["rows"]))
    a_only = sum(a["rows"][q]["correct"] and not b["rows"][q]["correct"] for q in shared)
    b_only = sum(b["rows"][q]["correct"] and not a["rows"][q]["correct"] for q in shared)
    return {"a": a["job"], "b": b["job"], "n": len(shared), "a_only": a_only, "b_only": b_only,
            "p_mcnemar": float(f"{mcnemar_exact(a_only, b_only):.3g}")}


def build(judge: str = "opus") -> dict:
    scored = []
    for folder in sorted(gate.jobs_dir().glob("ans.*")):
        if folder.name.endswith(".pilot"):
            continue   # pilots only prove the plumbing; they are not results
        result = score_job(folder.name, judge)
        if result:
            scored.append(result)
    summaries = [summarise(s) for s in scored]
    pairs = []
    # Every Sextant arm (one search or any agent variant, any repetition, any question subset)
    # against each baseline for the same model, on the questions both hold. Keyed by job, not by
    # (arm, model, rep): two jobs of one arm on different halves used to overwrite each other,
    # and the agent2-/agent3- arms were not paired at all (code review 2026-11-23).
    baselines = [s for s in scored if s["arm"] in ("full", "none", "oracle") and s["rep"] == 1]
    for s in scored:
        if not (s["arm"].startswith("sextant-") or re.match(r"agent\d*-", s["arm"])):
            continue
        for other in baselines:
            if other["model"] == s["model"] and set(s["rows"]) & set(other["rows"]):
                pairs.append(paired(s, other))
    return {"judge": arms.MODELS[judge], "prices_usd_per_mtok": common.PRICES, "spend": gate.committed(),
            "jobs": summaries, "paired": pairs}


def table(result: dict) -> str:
    lines = ["| arm | model | rep | n | accuracy | 95% CI | abstention | input tok/q | list USD/q | unanswered |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for s in result["jobs"]:
        o = s["groups"]["overall"]
        ab = s["groups"].get("abstention")
        lines.append(
            f"| {s['arm']} | {s['model']} | {s['rep']} | {o['n']} | {o['accuracy']:.1%} | "
            f"{o['ci95'][0]:.1%} to {o['ci95'][1]:.1%} | "
            f"{(format(ab['accuracy'], '.1%') if ab else 'n/a')} | {s['mean_input_tokens']:,} | "
            f"{s['usd_per_question_list']:.5f} | {s['unanswered'] + s['unparsed']} |")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    result = build(argv[0] if argv else "opus")
    out = common.work_dir() / "results.json"
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(table(result))
    for p in result["paired"]:
        print(f"{p['a']} vs {p['b']}: n={p['n']} first-only {p['a_only']} second-only {p['b_only']} p={p['p_mcnemar']}")
    print("spend:", result["spend"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
