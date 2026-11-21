"""Brain learning-loop review, 2026-11-20: the four mechanical fixes, tested through the fixed paths.

A. session_patterns parses the heading shape _history.md has used since mid-2026 ("## date HH:MM -- title").
B. Brain python child processes get UTF-8 stdio (the nightly curation report died on a right arrow for a week).
C. The reflexion loop stops after 3 consecutive LLM errors instead of running its clock out.
D. The SessionStart hook primes a "## Brain" section from the state dir (failed jobs, pending facts,
   the weekly supervisor's recommendations, the newest dream).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------- A

class TestHistoryHeadingShapes:
    def _projects(self, tmp_path: Path) -> Path:
        projects = tmp_path / "projects"
        (projects / "gatekeeper").mkdir(parents=True)
        (projects / "tidepool").mkdir(parents=True)
        return projects

    def test_modern_heading_with_time_and_double_dash_is_parsed(self, tmp_path):
        from atlas_plus.session_patterns import _parse_session_history
        history = tmp_path / "_history.md"
        history.write_text(
            "## 2026-11-20 11:30 -- follow-up: gatekeeper pushed (85 commits)\n\nbody\n\n"
            "## 2026-11-19 09:00 -- tidepool cockpit v2 live\n\nbody\n",
            encoding="utf-8",
        )
        with patch.dict("os.environ", {"ATLAS_PROJECTS_ROOT": str(self._projects(tmp_path))}):
            result = _parse_session_history(history)
        assert result == [("2026-11-20", "gatekeeper"), ("2026-11-19", "tidepool")]

    def test_legacy_single_dash_heading_still_parsed(self, tmp_path):
        from atlas_plus.session_patterns import _parse_session_history
        history = tmp_path / "_history.md"
        history.write_text("## 2026-05-11 - gatekeeper Windows Minifilter\n\nbody\n", encoding="utf-8")
        with patch.dict("os.environ", {"ATLAS_PROJECTS_ROOT": str(self._projects(tmp_path))}):
            result = _parse_session_history(history)
        assert result == [("2026-05-11", "gatekeeper")]

    def test_cooccurrence_is_built_from_modern_headings(self, tmp_path, monkeypatch):
        import atlas_plus.session_patterns as mod
        mem = tmp_path / "mem"
        (mem / "sessions").mkdir(parents=True)
        # one project per entry (the title parser keeps the first match); two entries per day co-occur
        (mem / "sessions" / "_history.md").write_text(
            "## 2026-11-20 11:30 -- gatekeeper pushed\n\n"
            "## 2026-11-20 10:00 -- tidepool cockpit live\n\n"
            "## 2026-11-19 12:00 -- tidepool market watch\n\n"
            "## 2026-11-19 09:00 -- gatekeeper minifilter\n\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "MEMORY_ROOT", mem)
        with patch.dict("os.environ", {"ATLAS_PROJECTS_ROOT": str(self._projects(tmp_path))}):
            cooccurrence, project_count = mod.build_cooccurrence(since_days=3650)
        assert project_count == {"gatekeeper": 2, "tidepool": 2}
        assert cooccurrence["gatekeeper"]["tidepool"] == 2


# ---------------------------------------------------------------- B

class TestChildProcessesGetUtf8Stdio:
    def test_utf8_env_sets_both_switches_and_keeps_the_rest(self, monkeypatch):
        from atlas_plus.brain.executors import _utf8_env
        monkeypatch.setenv("ATLAS_TEST_MARKER", "kept")
        env = _utf8_env()
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["PYTHONUTF8"] == "1"
        assert env["ATLAS_TEST_MARKER"] == "kept"

    def test_reporting_subprocess_survives_a_right_arrow(self, tmp_path):
        """The real failure shape: a child prints U+2192 to a non-UTF-8 console and dies with
        UnicodeEncodeError after doing all its work. Through the fixed helper the child succeeds."""
        from atlas_plus.brain import executors
        from atlas_plus.brain.tasks import Task
        task = Task(task_id="t-arrow", session_id="s", instruction="x", status="running",
                    created_at="2026-11-20T00:00:00", kind="curation_report")
        ctx = executors.ExecutorContext(task=task, artifact_root=tmp_path, db_path=tmp_path / "db.sqlite",
                                        worker_id="w")
        code = "import sys; sys.stdout.reconfigure(encoding=sys.stdout.encoding); print('a \\u2192 b')"
        with patch.dict("os.environ", {"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}):
            result = executors._run_reporting_subprocess(ctx, [sys.executable, "-c", code], "arrow.md", "Arrow", timeout=60)
        assert result.status == "done", result.text
        assert "a → b" in Path(result.artifact_path).read_text(encoding="utf-8")

    def test_every_python_child_call_passes_the_env(self):
        """Tree check on the executor module: each subprocess.run of a python child carries env=_utf8_env()."""
        import inspect
        from atlas_plus.brain import executors
        src = inspect.getsource(executors)
        calls = [i for i in range(len(src)) if src.startswith("completed = subprocess.run(", i)]
        assert calls, "no subprocess.run sites found"
        missing = []
        for i in calls:
            window = src[i:i + 500]
            before = src[max(0, i - 1500):i]
            if "--print" in before[-600:] or "CLAUDE_CLI" in before[-600:]:
                continue  # claude CLI invocations are not python children
            if "env=" not in window:
                missing.append(src[:i].count("\n") + 1)
        assert not missing, f"subprocess.run without env at lines {missing}"


# ---------------------------------------------------------------- C

def _recent_facts(p, n: int) -> None:
    from atlas_plus.temporal_graph.models import TemporalFact, _now_iso
    now = _now_iso()
    for i in range(n):
        p.add_fact(TemporalFact(
            fact_id=f"f-{i:03d}", subject_id="e-test", predicate="has_rule", object_id=f"o-{i}",
            fact_text=f"fact {i}", valid_from="2026-01-01T00:00:00+00:00", observed_at=now, ingested_at=now,
            confidence=0.9, authority="memory",
        ))


class TestReflexionStopsWhenTheModelIsDown:
    def test_three_consecutive_llm_errors_abort_the_run(self, tmp_path, monkeypatch):
        import atlas_plus.curation.reflexion_loop as mod
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        monkeypatch.setattr(mod, "STATE_DIR", tmp_path / ".atlas")
        (tmp_path / ".atlas").mkdir()
        p = FileTemporalProvider(graph_root=tmp_path / "graph")
        _recent_facts(p, mod._BATCH_SIZE * 6)
        calls = {"n": 0}

        def _down(prompt):
            calls["n"] += 1
            raise RuntimeError("Ollama not reachable")

        with patch("atlas_plus.curation.reflexion_loop._call_llm", side_effect=_down):
            report = mod.run_reflexion_loop(lookback_days=30, _provider=p)
        assert calls["n"] == mod._MAX_CONSECUTIVE_LLM_ERRORS
        assert report["aborted"] and "consecutive" in report["aborted"]
        assert report["errors"] == report["facts_reviewed"]

    def test_a_success_resets_the_counter(self, tmp_path, monkeypatch):
        import atlas_plus.curation.reflexion_loop as mod
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        monkeypatch.setattr(mod, "STATE_DIR", tmp_path / ".atlas")
        (tmp_path / ".atlas").mkdir()
        p = FileTemporalProvider(graph_root=tmp_path / "graph")
        _recent_facts(p, mod._BATCH_SIZE * 5)
        seq = iter([RuntimeError("down"), RuntimeError("down"), "[]", RuntimeError("down"), RuntimeError("down")])

        def _flaky(prompt):
            item = next(seq)
            if isinstance(item, Exception):
                raise item
            return item

        with patch("atlas_plus.curation.reflexion_loop._call_llm", side_effect=_flaky):
            report = mod.run_reflexion_loop(lookback_days=30, _provider=p)
        assert report["aborted"] is None
        assert report["errors"] == mod._BATCH_SIZE * 4


# ---------------------------------------------------------------- D

class TestSessionStartPrimesBrainNotes:
    def _state(self, tmp_path: Path) -> Path:
        state = tmp_path / ".atlas"
        (state / "reports").mkdir(parents=True)
        con = sqlite3.connect(state / "state.sqlite")
        con.executescript(
            "create table brain_tasks(task_id text, kind text, status text, created_at text);"
            "create table pending_facts(id integer, status text);"
        )
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT03:15:00")
        con.executemany("insert into brain_tasks values (?,?,?,?)", [
            ("t1", "curation_report", "failed", today),
            ("t2", "curation_report", "failed", today),
            ("t3", "reflexion_loop", "failed", today),
            ("t4", "dream", "done", today),
            ("t5", "verify", "failed", "2026-01-01T00:00:00"),  # too old
        ])
        con.executemany("insert into pending_facts values (?,?)", [(1, "pending"), (2, "pending"), (3, "approved")])
        con.commit(); con.close()
        (state / "reports" / "supervisor-2026-11-15.md").write_text(
            "# Weekly Claude Supervisor Report\n\n## 1. Patterns\n\n- x\n\n## 3. Recommendations\n\n"
            "1. **Encoding fix:** pass PYTHONIOENCODING=utf-8 to the curation subprocesses.\n"
            "2. **Reflexion fail-fast:** stop after 2 or 3 consecutive provider errors.\n"
            "3. Third thing.\n4. Fourth thing never shown.\n\n## 4. Other\n\nignored\n",
            encoding="utf-8",
        )
        (state / "_dream-2026-11-20.md").write_text("dream", encoding="utf-8")
        return state

    def test_brain_section_lists_failures_facts_recommendations_and_dream(self, tmp_path, monkeypatch):
        import atlas_plus.hooks.session_start as hook
        monkeypatch.setattr(hook, "STATE_DIR", self._state(tmp_path))
        monkeypatch.setattr(hook, "MEMORY_ROOT", tmp_path / "mem")
        text = hook._brain_notes()
        assert "curation_report x2" in text and "reflexion_loop x1" in text
        assert "verify" not in text  # outside the 7-day window
        assert "Pending facts awaiting review: 2" in text
        assert "supervisor 2026-11-15 recommends" in text
        assert "Encoding fix" in text and "Reflexion fail-fast" in text and "Third thing" in text
        assert "Fourth thing" not in text
        assert "Latest dream: _dream-2026-11-20.md" in text

    def test_brain_section_is_silent_without_a_state_dir(self, tmp_path, monkeypatch):
        import atlas_plus.hooks.session_start as hook
        monkeypatch.setattr(hook, "STATE_DIR", tmp_path / "nowhere")
        monkeypatch.setattr(hook, "MEMORY_ROOT", tmp_path / "mem")
        assert hook._brain_notes() == ""
