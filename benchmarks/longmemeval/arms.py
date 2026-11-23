"""Build the request files: what each arm shows the answering model, and what the judge sees.

Arms (every arm with history uses the same reader prompt, taken from the LongMemEval
generation script, so the only thing that differs between arms is which sessions are shown):

    none       the question and its date, no history (floor)
    oracle     only the sessions that hold the evidence (ceiling for any retriever)
    full       the whole history pasted into the prompt (the "just use a big window" baseline)
    sextant-<mode>-<tag>-k<K>
               the top K sessions Sextant's memory_search_tool returned for the question
               as asked, K in the name; <mode> is fts or hybrid, <tag> the Sextant build

The judge prompts are the ones in LongMemEval's evaluate_qa.py, word for word. The judge model
is not: the official script calls GPT-4o, this harness calls a Claude model. Scores are
therefore comparable between the arms in this run and not with numbers published elsewhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import gate  # noqa: E402

MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}

READER = ("I will give you several history chats between you and a user. Please answer the question "
          "based on the relevant chat history.\n\n\nHistory Chats:\n\n{}\n\nCurrent Date: {}\nQuestion: {}\nAnswer:")
NO_HISTORY = "Current Date: {}\nQuestion: {}\nAnswer:"
EMPTY_HISTORY = "(the memory search returned no sessions for this question)"

# LongMemEval src/evaluation/evaluate_qa.py (MIT), verbatim.
JUDGE = {
    "default": "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only.",
    "temporal-reasoning": "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only.",
    "knowledge-update": "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only.",
    "single-session-preference": "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only.",
    "abstention": "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only.",
}


def history_string(sessions: list[tuple[str, list[dict]]]) -> str:
    """Sessions as (date, turns), already in date order; the generation script's `nl` format."""
    out = ""
    for i, (date, turns) in enumerate(sessions):
        body = "".join("\n\n{}: {}".format(t["role"], t["content"].strip()) for t in turns)
        out += "\n### Session {}:\nSession Date: {}\nSession Content:\n{}\n".format(i + 1, date, body)
    return out


def _sessions(entry: dict, positions: list[int] | None = None) -> list[tuple[str, list[dict]]]:
    keep = None if positions is None else set(positions)
    return [(date, turns) for pos, date, turns in common.chronological(entry) if keep is None or pos in keep]


def prompt_for(arm: str, qid: str) -> str:
    if common.split() != "s":
        # These arms read the 50-session files and name their jobs without the split. Run under
        # SEXTANT_PROOF_SPLIT=m they would quietly measure `s` again and overwrite the `s` job of
        # the same name (code review 2026-11-23). The larger haystack is measured by agent.py.
        raise ValueError(f"the single-prompt arms are built for split 's' only; SEXTANT_PROOF_SPLIT is {common.split()!r}")
    entry = common.load_question("s", qid)
    date, question = entry["question_date"], entry["question"]
    if arm == "none":
        return NO_HISTORY.format(date, question)
    if arm == "full":
        return READER.format(history_string(_sessions(entry)), date, question)
    if arm == "oracle":
        return READER.format(history_string(_sessions(common.load_question("oracle", qid))), date, question)
    if arm.startswith("sextant-"):
        _, mode, tag, k = arm.split("-")
        path = common.work_dir() / "retrieval" / "s" / f"{mode}-{tag}" / f"{qid}.json"
        hits = json.loads(path.read_text(encoding="utf-8"))["hits"][: int(k[1:])]
        shown = _sessions(entry, [h["position"] for h in hits])
        return READER.format(history_string(shown) if shown else EMPTY_HISTORY, date, question)
    raise ValueError(f"unknown arm {arm!r}")


def select(types: list[str] | None = None, per_type: int | None = None, qids: list[str] | None = None,
           every: int | None = None, offset: int = 0) -> list[dict]:
    """Questions for a job, in a fixed order. `per_type` takes the first N ids of each type;
    `every` keeps every Nth id within each type (a stratified sample fixed by the id order)."""
    rows = sorted(common.load_index(common.split()), key=lambda r: r["question_id"])
    if qids:
        return [r for r in rows if r["question_id"] in set(qids)]
    if types:
        rows = [r for r in rows if r["question_type"] in types]
    if every:
        position: dict[str, int] = {}
        kept = []
        for r in rows:
            position[r["question_type"]] = position.get(r["question_type"], -1) + 1
            if position[r["question_type"]] % every == offset:
                kept.append(r)
        rows = kept
    if per_type:
        seen: dict[str, int] = {}
        kept = []
        for r in rows:
            seen[r["question_type"]] = seen.get(r["question_type"], 0) + 1
            if seen[r["question_type"]] <= per_type:
                kept.append(r)
        rows = kept
    return rows


def answer_job_name(arm: str, model: str, rep: int, suffix: str = "") -> str:
    return f"ans.{arm}.{model}.r{rep}{suffix}"


def build_answer_job(arm: str, model: str, rep: int, rows: list[dict], suffix: str = "",
                     effort: str = "medium", max_tokens: int = 4096) -> str:
    name = answer_job_name(arm, model, rep, suffix)
    requests = []
    for row in rows:
        qid = row["question_id"]
        requests.append({"custom_id": qid, "params": {
            "model": MODELS[model], "max_tokens": max_tokens,
            "output_config": {"effort": effort},
            "messages": [{"role": "user", "content": prompt_for(arm, qid)}],
        }})
    gate.write_job(name, requests, {"kind": "answer", "arm": arm, "model": MODELS[model], "rep": rep,
                                    "effort": effort, "expect_out": 500})
    return name


def judge_prompt(entry: dict, response: str) -> str:
    if common.is_abstention(entry["question_id"]):
        template = JUDGE["abstention"]
    else:
        template = JUDGE.get(entry["question_type"], JUDGE["default"])
    return template.format(entry["question"], entry["answer"], response)


def build_judge_job(answer_job: str, judge: str = "opus") -> str | None:
    """One judge request per answered question. Unanswered ones (error, refusal, empty text)
    are scored wrong by the report without a judge call."""
    answers = gate.read_results(answer_job)
    name = f"judge-{judge}.{answer_job}"
    requests = []
    for qid, row in sorted(answers.items()):
        if row["type"] != "succeeded" or not row.get("text"):
            continue
        entry = common.load_question("s", qid)
        requests.append({"custom_id": qid, "params": {
            "model": MODELS[judge], "max_tokens": 1024,
            "output_config": {"effort": "low"},
            "messages": [{"role": "user", "content": judge_prompt(entry, row["text"])}],
        }})
    if not requests:
        return None
    gate.write_job(name, requests, {"kind": "judge", "answer_job": answer_job, "model": MODELS[judge],
                                    "expect_out": 150})
    return name


def verdict(text: str) -> bool | None:
    """The official script's rule is `'yes' in response.lower()`. This is the same rule made
    strict: the first word decides; failing that, exactly one of yes/no must appear. Anything
    else is returned as None and listed in the report as unparsed."""
    words = "".join(c if c.isalpha() else " " for c in (text or "").lower()).split()
    if not words:
        return None
    if words[0] in ("yes", "no"):
        return words[0] == "yes"
    has_yes, has_no = "yes" in words, "no" in words
    return has_yes if has_yes != has_no else None
