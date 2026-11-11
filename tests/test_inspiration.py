"""Tests for atlas_plus.brain.inspiration -- the Inspiration Engine MVP.

Generation is dependency-injected, so every test is deterministic and never calls an
LLM. The central guarantee under test: captured ideas are always status="unverified".
"""

from __future__ import annotations

import pytest

from atlas_plus.brain import inspiration as insp


# --- normalization and equivalence ----------------------------------------

def test_normalize_answer_strips_case_punct_and_whitespace():
    assert insp.normalize_answer("  Paris, France.  ") == "paris, france"
    assert insp.normalize_answer("PARIS") == "paris"
    assert insp.normalize_answer("") == ""


def test_default_equiv_normalized_equal_and_token_overlap():
    assert insp.default_equiv("Paris", "paris.") is True
    # same tokens, reordered -> Jaccard 1.0
    assert insp.default_equiv("The capital is Paris", "Paris is the capital") is True
    assert insp.default_equiv("Paris", "London") is False
    # MVP lexical limitation: subset overlap below threshold is NOT equivalent
    assert insp.default_equiv("Paris", "Paris France") is False


# --- clustering ------------------------------------------------------------

def test_cluster_identical_answers_one_cluster():
    clusters = insp.cluster_answers(["Paris", "Paris", "Paris"])
    assert len(clusters) == 1
    assert sorted(clusters[0]) == [0, 1, 2]


def test_cluster_all_distinct_n_clusters():
    clusters = insp.cluster_answers(["Paris", "London", "Tokyo"])
    assert len(clusters) == 3


def test_cluster_groups_normalized_variants():
    clusters = insp.cluster_answers(["Paris", "paris.", "London"])
    sizes = sorted((len(c) for c in clusters), reverse=True)
    assert sizes == [2, 1]


# --- entropy ---------------------------------------------------------------

def test_entropy_zero_when_unanimous():
    assert insp.semantic_entropy([3], 3) == 0.0


def test_entropy_max_when_all_distinct():
    assert insp.semantic_entropy([1, 1, 1], 3) == pytest.approx(1.0)


def test_entropy_between_for_mixed():
    e = insp.semantic_entropy([2, 1], 3)
    assert 0.0 < e < 1.0


def test_entropy_single_sample_is_zero():
    assert insp.semantic_entropy([1], 1) == 0.0


# --- score_consistency -----------------------------------------------------

def test_score_grounded_when_unanimous():
    r = insp.score_consistency(["Paris"] * 5)
    assert r.verdict == "grounded"
    assert r.top_fraction == 1.0
    assert r.confidence == 1.0
    assert r.entropy == 0.0


def test_score_speculative_when_scattered():
    r = insp.score_consistency(["a", "b", "c", "d", "e"])
    assert r.verdict == "speculative"
    assert r.top_fraction == pytest.approx(0.2)
    assert r.entropy == pytest.approx(1.0)


def test_score_uncertain_in_between():
    r = insp.score_consistency(["Paris", "Paris", "Paris", "London", "Tokyo"])
    assert r.top_fraction == pytest.approx(0.6)
    assert r.verdict == "uncertain"


def test_score_grounded_threshold_is_inclusive():
    # 4/5 = 0.8 == grounded_threshold -> grounded
    r = insp.score_consistency(["Paris"] * 4 + ["London"])
    assert r.top_fraction == pytest.approx(0.8)
    assert r.verdict == "grounded"


def test_score_empty_is_speculative_zero_confidence():
    r = insp.score_consistency([])
    assert r.n == 0
    assert r.verdict == "speculative"
    assert r.confidence == 0.0


# --- capture_ideas ---------------------------------------------------------

def test_capture_ideas_are_always_unverified():
    r = insp.score_consistency(["Paris"] * 4 + ["London"])
    ideas = insp.capture_ideas(r, ["Paris", "Paris", "Paris", "Paris", "London"], source="t")
    assert ideas  # non-empty
    assert all(i.status == "unverified" for i in ideas)


def test_capture_ideas_lane_and_support_and_order():
    answers = ["Paris", "Paris", "Paris", "Paris", "London"]
    r = insp.score_consistency(answers)
    ideas = insp.capture_ideas(r, answers, source="brain/inspire")
    assert len(ideas) == 2
    # most-supported first
    assert ideas[0].text == "Paris"
    assert ideas[0].support == 4
    assert ideas[0].confidence == pytest.approx(0.8)
    assert ideas[0].lane == "grounded"      # 0.8 >= grounded threshold
    assert ideas[1].text == "London"
    assert ideas[1].support == 1
    assert ideas[1].lane == "speculative"
    assert all(i.source == "brain/inspire" for i in ideas)


# --- markdown + persistence ------------------------------------------------

def test_idea_to_markdown_frontmatter():
    idea = insp.Idea(text="Paris", lane="grounded", confidence=0.8, support=4, source="brain/inspire")
    md = insp.idea_to_markdown(idea)
    assert "status: unverified" in md
    assert "lane: grounded" in md
    assert "confidence: 0.8" in md
    assert "support: 4" in md
    assert "source: brain/inspire" in md
    assert "created:" not in md          # omitted when not supplied
    assert md.rstrip().endswith("Paris")


def test_idea_to_markdown_includes_created_when_given():
    idea = insp.Idea(text="x")
    md = insp.idea_to_markdown(idea, created="2026-05-29T06:40:00+00:00")
    assert "created: 2026-05-29T06:40:00+00:00" in md


def test_persist_ideas_writes_files(tmp_path):
    ideas = [insp.Idea(text="Paris idea"), insp.Idea(text="London idea")]
    paths = insp.persist_ideas(ideas, tmp_path)
    assert len(paths) == 2
    for p in paths:
        assert p.exists()
        assert "status: unverified" in p.read_text(encoding="utf-8")


# --- inspire (end to end) --------------------------------------------------

def _gen_from(seq):
    it = iter(seq)
    return lambda prompt: next(it)


def test_inspire_grounded_run():
    run = insp.inspire(lambda p: "Paris", "capital of France?", n=5)
    assert len(run.samples) == 5
    assert run.result.verdict == "grounded"
    assert run.result.confidence == 1.0
    assert len(run.ideas) == 1
    assert run.ideas[0].text == "Paris"
    assert run.ideas[0].status == "unverified"
    assert run.ideas[0].lane == "grounded"


def test_inspire_speculative_run_keeps_all_ideas_unverified():
    run = insp.inspire(_gen_from(["a", "b", "c", "d", "e"]), "brainstorm names", n=5)
    assert run.result.verdict == "speculative"
    assert len(run.ideas) == 5
    assert all(i.status == "unverified" for i in run.ideas)
    assert all(i.lane == "speculative" for i in run.ideas)


def test_inspire_clamps_n_to_at_least_one():
    run = insp.inspire(lambda p: "x", "q", n=0)
    assert len(run.samples) == 1


# --- Brain integration: run_to_markdown + the kind=inspire executor ----------

def test_run_to_markdown_contains_verdict_and_ideas():
    run = insp.inspire(lambda p: "Aurora", "name it", n=3)
    md = insp.run_to_markdown(run, run_id="task-xyz")
    assert "type: inspiration-run" in md
    assert "status: unverified" in md
    assert "verdict: grounded" in md
    assert "Aurora" in md
    assert "Raw samples" in md


def test_inspire_executor_persists_unverified_ideas(tmp_path, monkeypatch):
    from atlas_plus.brain import executors
    from atlas_plus.brain import tasks as ts
    from atlas_plus.brain import inspiration as insp_mod

    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ATLAS_INSPIRE_SAMPLES", "5")
    class _P:
        def generate(self, p, **k):
            return "Aurora"
    monkeypatch.setattr("atlas_plus.llm.get_provider", lambda role="brain": _P())

    db = tmp_path / "state.sqlite"
    task = ts.create_task("inspire: name for the project", kind="inspire", db_path=db)
    ctx = executors.ExecutorContext(
        task=task, artifact_root=tmp_path / "artifacts", db_path=db, worker_id="w1",
    )

    result = executors._inspire(ctx)

    assert result.status == "done"
    assert "unverified idea" in result.text
    ideas_dir = tmp_path / "brain" / "ideas" / task.task_id
    files = list(ideas_dir.glob("*.md"))
    assert files, "ideas should persist to the quarantine store"
    assert all("status: unverified" in f.read_text(encoding="utf-8") for f in files)
    assert result.artifact_path is not None and result.artifact_path.exists()


def test_inspire_executor_speculative_run_all_unverified(tmp_path, monkeypatch):
    from atlas_plus.brain import executors
    from atlas_plus.brain import tasks as ts
    from atlas_plus.brain import inspiration as insp_mod

    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ATLAS_INSPIRE_SAMPLES", "5")
    seq = iter(["alpha", "bravo", "charlie", "delta", "echo"])

    class _P:
        def generate(self, p, **k):
            return next(seq)
    monkeypatch.setattr("atlas_plus.llm.get_provider", lambda role="brain": _P())

    db = tmp_path / "state.sqlite"
    task = ts.create_task("brainstorm taglines", kind="inspire", db_path=db)
    ctx = executors.ExecutorContext(
        task=task, artifact_root=tmp_path / "artifacts", db_path=db, worker_id="w1",
    )
    result = executors._inspire(ctx)
    assert result.status == "done"
    files = list((tmp_path / "brain" / "ideas" / task.task_id).glob("*.md"))
    assert len(files) == 5
    assert all("status: unverified" in f.read_text(encoding="utf-8") for f in files)


def test_inspire_executor_empty_prompt_fails(tmp_path, monkeypatch):
    from atlas_plus.brain import executors
    from atlas_plus.brain import tasks as ts

    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path))
    db = tmp_path / "state.sqlite"
    task = ts.create_task("inspire:", kind="inspire", db_path=db)
    ctx = executors.ExecutorContext(
        task=task, artifact_root=tmp_path / "artifacts", db_path=db, worker_id="w1",
    )
    result = executors._inspire(ctx)
    assert result.status == "failed"


def test_inspire_executor_registered_kind():
    from atlas_plus.brain import executors
    assert executors._EXECUTORS.get("inspire") is executors._inspire


# --- embedding-based equivalence (increment 6) -----------------------------

def test_cosine_basic():
    assert insp.cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert insp.cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert insp.cosine([], [1.0]) == 0.0


def test_embedding_equiv_clusters_by_cosine_with_injected_embedder():
    vecs = {"cat": [1.0, 0.0, 0.0], "dog": [0.0, 1.0, 0.0]}

    def fake_embed(t):
        key = "cat" if "cat" in t.lower() else ("dog" if "dog" in t.lower() else "other")
        return vecs.get(key, [0.0, 0.0, 1.0])

    eq = insp.make_embedding_equiv(fake_embed, threshold=0.9)
    assert eq("a cat", "the cat sat") is True     # same embedding -> cosine 1.0
    assert eq("a cat", "a dog") is False           # orthogonal -> cosine 0

    answers = ["a cat", "the cat", "a dog", "a dog", "a dog"]
    r = insp.score_consistency(answers, equiv=eq)
    assert r.cluster_sizes == [3, 2]
    assert r.top_fraction == pytest.approx(0.6)
