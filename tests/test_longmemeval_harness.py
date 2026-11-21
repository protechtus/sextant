"""The LongMemEval harness end to end against a stand-in API client: build, gate, dispatch on
both routes, collect, judge, report. No network, no key, no spend."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmarks" / "longmemeval"
pytestmark = pytest.mark.skipif(not BENCH.exists(), reason="the benchmark harness is not in this tree")


def _entry(qid: str, qtype: str, question: str, answer: str, filler: int) -> dict:
    sessions = [[{"role": "user", "content": f"My favourite tea is {answer}.", "has_answer": True},
                 {"role": "assistant", "content": "Noted."}]]
    sessions += [[{"role": "user", "content": "filler talk " * 40, "has_answer": False}] for _ in range(filler)]
    ids = ["answer_1"] + [f"noise_{i}" for i in range(filler)]
    return {"question_id": qid, "question_type": qtype, "question": question, "answer": answer,
            "question_date": "2023/05/02 (Tue) 10:00",
            "haystack_dates": [f"2023/04/{10 + i:02d} (Mon) 09:00" for i in range(len(sessions))],
            "haystack_session_ids": ids, "haystack_sessions": sessions, "answer_session_ids": ["answer_1"]}


class FakeClient:
    """Answers correctly only when the evidence is in the prompt; judges by string match."""

    def __init__(self):
        self.batches = {}
        self.sync_calls = 0
        self.messages = SimpleNamespace(create=self._create, batches=SimpleNamespace(
            create=self._batch_create, retrieve=self._batch_retrieve, results=self._batch_results))

    def _message(self, params: dict):
        prompt = params["messages"][0]["content"]
        if "Is the model response correct?" in prompt or "unanswerable" in prompt:
            answer, response = prompt.split("Correct Answer: ")[-1].split("\n\nModel Response: ") \
                if "Correct Answer: " in prompt else ("", "")
            text = "yes" if answer and answer.strip() in response else "no"
        else:
            text = prompt.split("My favourite tea is ")[1].split(".")[0] if "My favourite tea is " in prompt else "I do not know."
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", model=params["model"],
            usage=SimpleNamespace(input_tokens=len(prompt) // 4, output_tokens=20,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0))

    def _create(self, **params):
        self.sync_calls += 1
        assert "output_config" in params and "temperature" not in params
        return self._message(params)

    def _batch_create(self, requests):
        batch_id = f"msgbatch_{len(self.batches) + 1}"
        self.batches[batch_id] = requests
        return SimpleNamespace(id=batch_id)

    def _batch_retrieve(self, batch_id):
        n = len(self.batches[batch_id])
        return SimpleNamespace(processing_status="ended", request_counts=SimpleNamespace(
            processing=0, succeeded=n, errored=0, canceled=0, expired=0))

    def _batch_results(self, batch_id):
        for request in self.batches[batch_id]:
            yield SimpleNamespace(custom_id=request["custom_id"], result=SimpleNamespace(
                type="succeeded", message=self._message(request["params"])))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("SEXTANT_PROOF_WORK", str(tmp_path))
    monkeypatch.setenv("SEXTANT_PROOF_TAG", "t")
    monkeypatch.delenv("SEXTANT_PROOF_BUDGET", raising=False)
    monkeypatch.delenv("SEXTANT_PROOF_SYNC_LIMIT", raising=False)
    monkeypatch.syspath_prepend(str(BENCH))
    for name in ("common", "gate", "arms", "report", "retrieve", "corpus"):
        sys.modules.pop(name, None)
    import arms
    import common
    import gate
    import report

    full = [_entry("q1", "single-session-user", "What is my favourite tea?", "oolong", 3),
            _entry("q2", "knowledge-update", "Which tea do I like best?", "sencha", 3),
            _entry("q3_abs", "multi-session", "What is my favourite coffee?", "not mentioned", 3)]
    data = tmp_path / "data"
    data.mkdir()
    (data / common.DATASET_FILES["s"]).write_text(json.dumps(full), encoding="utf-8")
    oracle = [dict(e, haystack_sessions=e["haystack_sessions"][:1], haystack_dates=e["haystack_dates"][:1],
                   haystack_session_ids=e["haystack_session_ids"][:1]) for e in full]
    (data / common.DATASET_FILES["oracle"]).write_text(json.dumps(oracle), encoding="utf-8")
    for split in ("s", "oracle"):   # the large `m` file is optional and not part of this fixture
        common.split_dataset(split, data / common.DATASET_FILES[split])
    retrieved = tmp_path / "retrieval" / "s" / "fts-t"
    retrieved.mkdir(parents=True)
    for e, hits in (("q1", [0, 2]), ("q2", [3]), ("q3_abs", [])):
        (retrieved / f"{e}.json").write_text(json.dumps({"hits": [{"position": p} for p in hits]}), encoding="utf-8")
    fake = FakeClient()
    monkeypatch.setattr(gate, "client", lambda: fake)
    return SimpleNamespace(arms=arms, common=common, gate=gate, report=report, fake=fake, work=tmp_path)


def test_labels_never_reach_a_prompt(harness):
    entry = harness.common.load_question("s", "q1")
    assert "has_answer" not in json.dumps(entry["haystack_sessions"])
    prompt = harness.arms.prompt_for("full", "q1")
    assert "answer_1" not in prompt and "noise_" not in prompt and "has_answer" not in prompt


def test_arms_show_different_history(harness):
    a = harness.arms
    assert "History Chats" not in a.prompt_for("none", "q1")
    assert a.prompt_for("full", "q1").count("### Session") == 4
    assert a.prompt_for("oracle", "q1").count("### Session") == 1
    assert a.prompt_for("sextant-fts-t-k5", "q1").count("### Session") == 2
    assert a.prompt_for("sextant-fts-t-k1", "q1").count("### Session") == 1
    assert a.EMPTY_HISTORY in a.prompt_for("sextant-fts-t-k5", "q3_abs")


def test_small_job_runs_sync_and_settles(harness):
    a, g = harness.arms, harness.gate
    job = a.build_answer_job("oracle", "haiku", 1, a.select())
    out = g.dispatch(job)
    assert out["route"] == "sync" and harness.fake.sync_calls == 3
    assert g.committed()["pending_usd"] == 0 and g.committed()["settled_usd"] > 0
    assert g.read_results(job)["q1"]["text"] == "oolong"


def test_job_over_the_sync_limit_must_use_batch(harness, monkeypatch):
    a, g = harness.arms, harness.gate
    monkeypatch.setenv("SEXTANT_PROOF_SYNC_LIMIT", "0.000001")
    job = a.build_answer_job("full", "sonnet", 1, a.select())
    with pytest.raises(g.GateError, match="must go through the Batch API"):
        g.dispatch(job, "sync")
    assert harness.fake.sync_calls == 0
    out = g.dispatch(job)
    assert out["route"] == "batch" and out["batches"] == ["msgbatch_1"]
    assert g.committed()["pending_usd"] > 0
    settled = g.collect(job)["settled"]
    assert settled["route"] == "batch" and settled["failed"] == 0
    assert g.committed()["pending_usd"] == 0
    with pytest.raises(g.GateError, match="already dispatched"):
        g.dispatch(job)


def test_budget_refuses_a_dispatch_that_would_cross_it(harness, monkeypatch):
    a, g = harness.arms, harness.gate
    monkeypatch.setenv("SEXTANT_PROOF_BUDGET", "0.0000001")
    job = a.build_answer_job("full", "sonnet", 1, a.select())
    with pytest.raises(g.GateError, match="above the"):
        g.dispatch(job)
    assert harness.fake.batches == {} and harness.fake.sync_calls == 0


def test_judge_and_report(harness):
    a, g, r = harness.arms, harness.gate, harness.report
    for arm in ("none", "full", "sextant-fts-t-k5"):
        job = a.build_answer_job(arm, "haiku", 1, a.select())
        g.dispatch(job)
        g.dispatch(a.build_judge_job(job))
    result = r.build()
    by_arm = {s["arm"]: s for s in result["jobs"]}
    assert by_arm["full"]["groups"]["overall"]["correct"] == 2      # q3_abs has no tea line to quote
    assert by_arm["none"]["groups"]["overall"]["correct"] == 0
    assert by_arm["sextant-fts-t-k5"]["groups"]["overall"]["correct"] == 1   # q2's hit missed the evidence
    assert by_arm["full"]["mean_input_tokens"] > by_arm["sextant-fts-t-k5"]["mean_input_tokens"]
    pair = next(p for p in result["paired"] if p["b"].startswith("ans.full"))
    assert (pair["a_only"], pair["b_only"]) == (0, 1)
    assert "| full |" in r.table(result)


def test_verdict_parsing(harness):
    v = harness.arms.verdict
    assert v("Yes") is True and v("no.") is False and v("  YES, it is correct") is True
    assert v("The answer is no") is False and v("it depends: yes and no") is None and v("") is None
