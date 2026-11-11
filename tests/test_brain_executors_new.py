"""Tests for Brain executors: narrative_weekly, memory_agent, patterns_rebuild, decay_report, verify,
research_digest, pytest, temporal_build, gaps_report, convergence_report, curation_report,
suggest_facts_review, noop, fail, context_pack, pytest_python helper."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas_plus.brain.executors import ExecutionResult, ExecutorContext, run_executor
from atlas_plus.brain.tasks import Task


def _ctx(tmp: Path, kind: str) -> tuple[Task, ExecutorContext]:
    task = MagicMock(spec=Task)
    task.task_id = "test-task-001"
    task.kind = kind
    task.instruction = f"run {kind}"
    ctx = ExecutorContext(
        task=task,
        artifact_root=tmp / "artifacts",
        db_path=tmp / "state.sqlite",
        worker_id="test-worker",
    )
    return task, ctx


class TestNarrativeWeeklyExecutor:
    def test_calls_run_narrative(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "narrative_weekly")
            mock_result = {
                "ok": True, "week": "2026-W21", "sessions": 3, "energy": "converging",
                "open_loops": 2, "direction_signals": 4, "path": "/memory/sessions/2026-W21.md",
            }
            with patch("atlas_plus.session_narrative.run_narrative", return_value=mock_result):
                result = run_executor(task, ctx)
        assert result.status == "done"
        assert "2026-W21" in result.text

    def test_no_sessions_still_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "narrative_weekly")
            mock_result = {"ok": False, "reason": "no sessions found", "week": "2026-W21"}
            with patch("atlas_plus.session_narrative.run_narrative", return_value=mock_result):
                result = run_executor(task, ctx)
        assert result.status == "done"
        assert "no sessions" in result.text

    def test_exception_returns_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "narrative_weekly")
            with patch("atlas_plus.session_narrative.run_narrative", side_effect=RuntimeError("boom")):
                result = run_executor(task, ctx)
        assert result.status == "failed"
        assert "boom" in result.text


class TestMemoryAgentExecutor:
    def test_calls_run_weekly_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "memory_agent")
            mock_report = MagicMock()
            mock_report.stale_projects = [{"project": "gatekeeper"}]
            mock_report.decayed_facts = []
            mock_report.decisions_to_revisit = []
            mock_report.tickets_created = ["ATL-099"]
            with patch("atlas_plus.memory_agent.run_weekly_agent", return_value=mock_report):
                result = run_executor(task, ctx)
        assert result.status == "done"
        assert "stale_projects=1" in result.text
        assert "tickets_created=1" in result.text

    def test_exception_returns_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "memory_agent")
            with patch("atlas_plus.memory_agent.run_weekly_agent", side_effect=RuntimeError("fail")):
                result = run_executor(task, ctx)
        assert result.status == "failed"


class TestPatternsRebuildExecutor:
    def test_calls_save_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "patterns_rebuild")
            mock_model = {
                "built_at": "2026-05-19T03:25:00+00:00",
                "since_days": 120,
                "project_count": {"atlas-memory+": 45, "gatekeeper": 12},
                "cooccurrence": {},
            }
            with patch("atlas_plus.session_patterns.save_patterns", return_value=mock_model):
                result = run_executor(task, ctx)
        assert result.status == "done"
        assert "2 projects" in result.text

    def test_exception_returns_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "patterns_rebuild")
            with patch("atlas_plus.session_patterns.save_patterns", side_effect=RuntimeError("fail")):
                result = run_executor(task, ctx)
        assert result.status == "failed"


# Narrative endpoint tests live in test_web_app.py (uses the pre-configured shared client)


class TestDecayReportExecutor:
    def test_no_decayed_facts_returns_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "decay_report")
            with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]):
                result = run_executor(task, ctx)
        assert result.status == "done"
        assert "0" in result.text or "overdue" in result.text.lower()

    def test_decayed_facts_included_in_artifact(self):
        from atlas_plus.curation.decay import DecayCandidate
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "decay_report")
            fake_candidates = [
                DecayCandidate("feedback/style.md", "Style Guide", "feedback",
                               90, "2025-01-01", 120),
                DecayCandidate("reference/tools.md", "Tools Ref", "reference",
                               180, "2024-06-01", 200),
            ]
            with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=fake_candidates):
                result = run_executor(task, ctx)
            assert result.status == "done"
            assert "2" in result.text
            artifact_text = result.artifact_path.read_text(encoding="utf-8")
            assert "feedback/style.md" in artifact_text
            assert "90d" in artifact_text or "90" in artifact_text

    def test_exception_returns_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "decay_report")
            with patch("atlas_plus.curation.decay.find_decayed_facts",
                       side_effect=RuntimeError("decay scan error")):
                result = run_executor(task, ctx)
        assert result.status == "failed"
        assert "decay scan error" in result.text


# ---------------------------------------------------------------------------
# verify executor
# ---------------------------------------------------------------------------

class TestVerifyExecutor:
    """_verify executor runs full pytest suite and returns done/failed."""

    def test_passing_pytest_returns_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "verify")
            task.session_id = "verify-ticket-ATL-050"

            class Passed:
                returncode = 0
                stdout = "1579 passed in 2.30s"
                stderr = ""

            with patch("atlas_plus.brain.executors.subprocess.run", return_value=Passed()):
                result = run_executor(task, ctx)

        assert result.status == "done"
        assert "verify passed" in result.text.lower() or "passed" in result.text

    def test_failing_pytest_returns_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "verify")
            task.session_id = "verify-ticket-ATL-051"

            class Failed:
                returncode = 1
                stdout = "3 failed, 1576 passed in 3.10s"
                stderr = ""

            with patch("atlas_plus.brain.executors.subprocess.run", return_value=Failed()):
                result = run_executor(task, ctx)

        assert result.status == "failed"
        assert "verify failed" in result.text.lower() or "failed" in result.text

    def test_verify_executor_returns_artifact_path(self):
        """_verify returns an artifact_path (file existence is gatekeeper-dependent)."""
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "verify")
            task.session_id = "verify-ticket-ATL-052"

            class Passed:
                returncode = 0
                stdout = "1579 passed"
                stderr = ""

            with patch("atlas_plus.brain.executors.subprocess.run", return_value=Passed()), \
                 patch("atlas_plus.brain.executors._check_gate", return_value=None):
                result = run_executor(task, ctx)

        assert result.artifact_path is not None
        # Path object points to verify.md inside the task artifact dir
        assert result.artifact_path.name == "verify.md"

    def test_verify_kind_registered_in_executor_map(self):
        from atlas_plus.brain.executors import _EXECUTORS
        assert "verify" in _EXECUTORS


# ---------------------------------------------------------------------------
# research_gap executor
# ---------------------------------------------------------------------------

class TestResearchGapExecutor:
    def _ctx(self, tmp: Path, instruction: str = "Research gap: SIP registration") -> tuple[Task, ExecutorContext]:
        task = MagicMock(spec=Task)
        task.task_id = "test-gap-001"
        task.kind = "research_gap"
        task.instruction = instruction
        task.session_id = "ticket-ATL-099"
        ctx = ExecutorContext(
            task=task,
            artifact_root=tmp / "artifacts",
            db_path=tmp / "state.sqlite",
            worker_id="test-worker",
        )
        return task, ctx

    def test_runs_research_and_returns_done(self, tmp_path):
        from atlas_plus.brain.research import run_research
        fake_path = tmp_path / "sip-report.md"
        fake_path.parent.mkdir(parents=True, exist_ok=True)
        fake_path.write_text("# SIP report\n\nbody", encoding="utf-8")
        _, ctx = self._ctx(tmp_path)
        with patch("atlas_plus.brain.research.run_research",
                   return_value=("report body", fake_path)):
            result = run_executor(ctx.task, ctx)
        assert result.status == "done"
        assert "SIP registration" in result.text or str(fake_path) in result.text

    def test_dedup_hit_returns_done_not_failed(self, tmp_path):
        """A ValueError from run_research (similar report exists) should mark task done."""
        _, ctx = self._ctx(tmp_path)
        with patch("atlas_plus.brain.research.run_research",
                   side_effect=ValueError("Similar report exists: sip-2026-05.md")):
            result = run_executor(ctx.task, ctx)
        assert result.status == "done"
        assert "dedup" in result.text.lower() or "already" in result.text.lower()

    def test_research_failure_returns_failed(self, tmp_path):
        _, ctx = self._ctx(tmp_path)
        with patch("atlas_plus.brain.research.run_research",
                   side_effect=RuntimeError("LLM timeout")):
            result = run_executor(ctx.task, ctx)
        assert result.status == "failed"
        assert "LLM timeout" in result.text or "failed" in result.text.lower()

    def test_empty_instruction_returns_failed(self, tmp_path):
        _, ctx = self._ctx(tmp_path, instruction="")
        result = run_executor(ctx.task, ctx)
        assert result.status == "failed"

    def test_extracts_topic_from_research_gap_prefix(self, tmp_path):
        _, ctx = self._ctx(tmp_path, instruction="Research gap: DTMF in VoIP calls")
        fake_path = tmp_path / "dtmf.md"
        fake_path.write_text("report", encoding="utf-8")
        called_with = []
        def fake_run(topic, session_id, force=False):
            called_with.append(topic)
            return ("body", fake_path)
        with patch("atlas_plus.brain.research.run_research", side_effect=fake_run):
            run_executor(ctx.task, ctx)
        assert called_with == ["DTMF in VoIP calls"]

    def test_research_gap_registered_in_executor_map(self):
        from atlas_plus.brain.executors import _EXECUTORS
        assert "research_gap" in _EXECUTORS

    def test_result_includes_quality_score_when_present(self, tmp_path):
        """Result text should embed quality_score from report frontmatter when available."""
        fake_path = tmp_path / "reports" / "sip-report.md"
        fake_path.parent.mkdir(parents=True, exist_ok=True)
        fake_path.write_text(
            "---\ntitle: SIP Report\nquality_score: 0.78\n---\n\n## Summary\n\nbody",
            encoding="utf-8",
        )
        _, ctx = self._ctx(tmp_path)
        with patch("atlas_plus.brain.research.run_research",
                   return_value=("report body", fake_path)):
            result = run_executor(ctx.task, ctx)
        assert result.status == "done"
        assert "0.78" in result.text

    def test_result_omits_score_when_frontmatter_missing(self, tmp_path):
        """No quality_score key in frontmatter should not crash; score omitted from text."""
        fake_path = tmp_path / "reports" / "sip-report.md"
        fake_path.parent.mkdir(parents=True, exist_ok=True)
        fake_path.write_text("# SIP report\n\nbody", encoding="utf-8")
        _, ctx = self._ctx(tmp_path)
        with patch("atlas_plus.brain.research.run_research",
                   return_value=("report body", fake_path)):
            result = run_executor(ctx.task, ctx)
        assert result.status == "done"
        assert "quality:" not in result.text.lower()


# ---------------------------------------------------------------------------
# dispatcher routing: research_gap tickets get kind=research_gap
# ---------------------------------------------------------------------------

class TestDispatcherResearchGapRouting:
    def _make_ticket(self, title: str, assignee: str = "any") -> MagicMock:
        t = MagicMock()
        t.id = "ATL-099"
        t.title = title
        t.body = "Auto-filed gap ticket."
        t.status = "todo"
        t.assignee = assignee
        t.priority = "normal"
        return t

    def test_research_gap_ticket_gets_research_gap_kind(self, tmp_path):
        from unittest.mock import MagicMock, call
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = self._make_ticket("Research gap: SIP REGISTER flow")
        created_kinds = []

        def fake_create_task(instruction, session_id, agent, priority, kind):
            created_kinds.append(kind)
            t = MagicMock()
            t.task_id = "fake-task-001"
            return t

        with patch("atlas_plus.brain.tasks.create_task", side_effect=fake_create_task), \
             patch("atlas_plus.brain.dispatcher._active_task_exists", return_value=False), \
             patch("atlas_plus.tickets.store.update_ticket_status"):
            dispatch_ticket(ticket)

        assert created_kinds == ["research_gap"]

    def test_non_gap_ticket_gets_agent_kind(self, tmp_path):
        from atlas_plus.brain.dispatcher import dispatch_ticket
        ticket = self._make_ticket("Fix the SIP parser bug")
        created_kinds = []

        def fake_create_task(instruction, session_id, agent, priority, kind):
            created_kinds.append(kind)
            t = MagicMock()
            t.task_id = "fake-task-002"
            return t

        with patch("atlas_plus.brain.tasks.create_task", side_effect=fake_create_task), \
             patch("atlas_plus.brain.dispatcher._active_task_exists", return_value=False), \
             patch("atlas_plus.tickets.store.update_ticket_status"):
            dispatch_ticket(ticket)

        assert created_kinds == ["agent"]


# ---------------------------------------------------------------------------
# _pytest_python helper
# ---------------------------------------------------------------------------

class TestPytestPythonHelper:
    def test_returns_venv311_when_it_exists(self, tmp_path):
        from atlas_plus.brain.executors import _pytest_python
        venv_dir = tmp_path / ".venv311" / "Scripts"
        venv_dir.mkdir(parents=True)
        (venv_dir / "python.exe").write_bytes(b"")
        result = _pytest_python(tmp_path)
        assert str(result).endswith("python.exe")
        assert ".venv311" in str(result)

    def test_falls_back_to_sys_executable_when_missing(self, tmp_path):
        import sys
        from atlas_plus.brain.executors import _pytest_python
        result = _pytest_python(tmp_path)  # no .venv311 in tmp_path
        assert result == sys.executable


# ---------------------------------------------------------------------------
# _noop and _fail executors
# ---------------------------------------------------------------------------

class TestNoopExecutor:
    def test_returns_done_with_instruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "noop")
            # _noop calls brain_context.write_context_artifact; patch it to avoid real I/O
            fake_path = Path(tmp) / "artifacts" / "noop.md"
            with patch("atlas_plus.brain.context.write_context_artifact",
                       return_value=fake_path):
                result = run_executor(task, ctx)
        assert result.status == "done"

    def test_noop_registered(self):
        from atlas_plus.brain.executors import _EXECUTORS
        assert "noop" in _EXECUTORS


class TestFailExecutor:
    def test_raises_runtime_error(self):
        # run_executor does NOT catch exceptions; _fail raises RuntimeError directly.
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "fail")
            with pytest.raises(RuntimeError, match="intentional test failure"):
                run_executor(task, ctx)

    def test_fail_registered(self):
        from atlas_plus.brain.executors import _EXECUTORS
        assert "fail" in _EXECUTORS


# ---------------------------------------------------------------------------
# _context_pack executor
# ---------------------------------------------------------------------------

class TestContextPackExecutor:
    def test_returns_done_with_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "context_pack")
            fake_path = Path(tmp) / "artifacts" / "context.md"
            with patch("atlas_plus.brain.context.write_context_artifact",
                       return_value=fake_path):
                result = run_executor(task, ctx)
        assert result.status == "done"
        assert "Context pack" in result.text or result.artifact_path is not None


# ---------------------------------------------------------------------------
# _research_digest executor
# ---------------------------------------------------------------------------

class TestResearchDigestExecutor:
    def test_returns_blocked_when_no_plan(self, tmp_path):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "research_digest")
            with patch("atlas_plus.brain.context._DEFAULT_RESEARCH_ROOT",
                       tmp_path / "no-research"):
                result = run_executor(task, ctx)
        assert result.status == "blocked"
        assert "No research plan" in result.text

    def test_returns_done_when_plan_exists(self, tmp_path):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "research_digest")
            plan_dir = tmp_path / "research"
            plan_dir.mkdir()
            plan = plan_dir / "RESEARCH-PLAN.md"
            plan.write_text(
                "# Plan\n\n| RES-001 | SIP | queued |\n| RES-002 | RTP | done |\n",
                encoding="utf-8",
            )
            with patch("atlas_plus.brain.context._DEFAULT_RESEARCH_ROOT", plan_dir):
                result = run_executor(task, ctx)
        assert result.status == "done"
        assert result.artifact_path is not None

    def test_research_digest_registered(self):
        from atlas_plus.brain.executors import _EXECUTORS
        assert "research_digest" in _EXECUTORS


# ---------------------------------------------------------------------------
# _pytest executor (subprocess mocked)
# ---------------------------------------------------------------------------

class TestPytestExecutor:
    def _mock_run(self, returncode: int, stdout: str = "", stderr: str = ""):
        import subprocess
        m = MagicMock(spec=subprocess.CompletedProcess)
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = stderr
        return m

    def test_returns_done_when_pytest_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "pytest")
            mock = self._mock_run(0, "42 passed")
            with patch("atlas_plus.brain.executors.subprocess.run", return_value=mock):
                result = run_executor(task, ctx)
        assert result.status == "done"
        assert "pytest passed" in result.text

    def test_returns_failed_when_pytest_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "pytest")
            mock = self._mock_run(1, "", "2 failed")
            with patch("atlas_plus.brain.executors.subprocess.run", return_value=mock):
                result = run_executor(task, ctx)
        assert result.status == "failed"
        assert "pytest failed" in result.text

    def test_artifact_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "pytest")
            mock = self._mock_run(0, "10 passed")
            with patch("atlas_plus.brain.executors.subprocess.run", return_value=mock):
                result = run_executor(task, ctx)
        assert result.artifact_path is not None

    def test_pytest_registered(self):
        from atlas_plus.brain.executors import _EXECUTORS
        assert "pytest" in _EXECUTORS


# ---------------------------------------------------------------------------
# _temporal_build executor (build_temporal_graph mocked)
# ---------------------------------------------------------------------------

class TestTemporalBuildExecutor:
    def test_returns_done_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "temporal_build")
            fake_result = {
                "source": "all",
                "files_indexed": 10,
                "files_skipped": 5,
                "facts_added": 42,
                "index": {"episodes": 3, "entities": 8, "facts_total": 50, "facts_active": 42},
                "warnings": [],
            }
            with patch("atlas_plus.brain.executors.build_temporal_graph",
                       return_value=fake_result):
                result = run_executor(task, ctx)
        assert result.status == "done"
        assert "10" in result.text or "indexed" in result.text

    def test_returns_failed_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "temporal_build")
            with patch("atlas_plus.brain.executors.build_temporal_graph",
                       return_value={"error": "DB locked"}):
                result = run_executor(task, ctx)
        assert result.status == "failed"
        assert "DB locked" in result.text

    def test_temporal_build_registered(self):
        from atlas_plus.brain.executors import _EXECUTORS
        assert "temporal_build" in _EXECUTORS


# ---------------------------------------------------------------------------
# Subprocess-based report executors: gaps_report, convergence_report, curation_report
# ---------------------------------------------------------------------------

class TestSubprocessReportExecutors:
    """gaps_report, convergence_report, and curation_report share the same pattern:
    subprocess.run -> returncode 0 = done, nonzero = failed."""

    def _mock_run(self, returncode: int):
        import subprocess
        m = MagicMock(spec=subprocess.CompletedProcess)
        m.returncode = returncode
        m.stdout = "report output"
        m.stderr = ""
        return m

    def test_gaps_report_done_on_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "gaps_report")
            with patch("atlas_plus.brain.executors.subprocess.run",
                       return_value=self._mock_run(0)):
                result = run_executor(task, ctx)
        assert result.status == "done"

    def test_gaps_report_failed_on_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "gaps_report")
            with patch("atlas_plus.brain.executors.subprocess.run",
                       return_value=self._mock_run(1)):
                result = run_executor(task, ctx)
        assert result.status == "failed"

    def test_convergence_report_done_on_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "convergence_report")
            with patch("atlas_plus.brain.executors.subprocess.run",
                       return_value=self._mock_run(0)):
                result = run_executor(task, ctx)
        assert result.status == "done"

    def test_convergence_report_failed_on_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "convergence_report")
            with patch("atlas_plus.brain.executors.subprocess.run",
                       return_value=self._mock_run(1)):
                result = run_executor(task, ctx)
        assert result.status == "failed"

    def test_curation_report_done_on_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "curation_report")
            with patch("atlas_plus.brain.executors.subprocess.run",
                       return_value=self._mock_run(0)):
                result = run_executor(task, ctx)
        assert result.status == "done"

    def test_curation_report_failed_on_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "curation_report")
            with patch("atlas_plus.brain.executors.subprocess.run",
                       return_value=self._mock_run(1)):
                result = run_executor(task, ctx)
        assert result.status == "failed"

    def test_all_three_registered(self):
        from atlas_plus.brain.executors import _EXECUTORS
        for kind in ("gaps_report", "convergence_report", "curation_report"):
            assert kind in _EXECUTORS


# ---------------------------------------------------------------------------
# _suggest_facts_review executor
# ---------------------------------------------------------------------------

class TestSuggestFactsReviewExecutor:
    def test_returns_done_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "suggest_facts_review")
            with patch("atlas_plus.brain.executors.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="3 facts queued", stderr="")
                result = run_executor(task, ctx)
        assert result.status == "done"

    def test_returns_failed_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            task, ctx = _ctx(Path(tmp), "suggest_facts_review")
            with patch("atlas_plus.brain.executors.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
                result = run_executor(task, ctx)
        assert result.status == "failed"

    def test_suggest_facts_registered(self):
        from atlas_plus.brain.executors import _EXECUTORS
        assert "suggest_facts_review" in _EXECUTORS
