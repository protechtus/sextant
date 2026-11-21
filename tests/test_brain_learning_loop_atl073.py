"""ATL-073 (2026-11-20): Brain watches and measures, Claude synthesizes weekly, sessions read, a desk writes.

E. brain.learning: the deterministic session digest (sessions, history, both updates files, close notes,
   commits, the Brain ledger) and its rendering; reruns overwrite.
F. Proposals: filed, deduplicated, listed, rejected; approval is the only write path (lesson row, ticket,
   memory fact) and runs under the operator's click with an optional edited body.
G. Scheduler: session-digest registered daily; research-batch gone; ticket-dispatch and sla-check only
   when their switch is on.
H. The session_digest executor and the Sonnet supervisor through run_executor: report with fixed
   headings, proposals filed from the fenced JSON block, nothing written to .memory/.
I. Dream reads the digests; the GitHub intake treats bot PRs as normal and investigates them once;
   SessionStart shows proposals and the newest digest; the session-close note lands in the next digest.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from atlas_plus.brain import learning


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                   check=True, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)


# ---------------------------------------------------------------- E

class TestSessionDigest:
    def test_parse_day_entries_reads_both_heading_shapes_and_first_paragraph(self):
        text = (
            "## 2026-11-19 13:40 -- Modern entry\n\nFirst paragraph\nstill first.\n\nSecond paragraph.\n\n"
            "## 2026-11-19 - Legacy entry\n\nLegacy body.\n\n"
            "## 2026-11-18 09:00 -- Other day\n\nIgnored.\n"
        )
        entries = learning.parse_day_entries(text, "2026-11-19")
        assert [e["title"] for e in entries] == ["Modern entry", "Legacy entry"]
        assert entries[0]["time"] == "13:40" and entries[0]["summary"] == "First paragraph still first."
        assert entries[1]["time"] == "" and entries[1]["summary"] == "Legacy body."
        assert learning.parse_day_entries(text, "2026-11-17") == []

    def _fixture(self, tmp_path: Path, day: str):
        ws = tmp_path / "ws"
        mem = ws / ".memory"
        state = ws / ".atlas"
        (mem / "sessions").mkdir(parents=True)
        state.mkdir()
        (mem / "sessions" / f"{day}.md").write_text(
            f"## {day} 10:00 -- Alpha work\n\nDid alpha.\n\n## {day} 12:00 -- Beta work\n\nDid beta.\n", encoding="utf-8")
        (mem / "sessions" / "_history.md").write_text(
            f"## {day} 10:00 -- Alpha work\n\nShort alpha.\n\n## 2026-01-01 -- Old\n\nOld.\n", encoding="utf-8")
        (ws / "updates.txt").write_text(
            f"## {day} 12:00 -- Beta work\n\nDup.\n\n## {day} 14:00 -- Gamma only in updates\n\nGamma body.\n", encoding="utf-8")
        (state / "updates.txt").write_text("", encoding="utf-8")
        repo = ws / "projects" / "demo"
        repo.mkdir(parents=True)
        _git(repo, "init", "-q")
        (repo / "a.txt").write_text("a", encoding="utf-8")
        _git(repo, "add", "a.txt")
        _git(repo, "commit", "-q", "-m", "demo: first commit")
        from atlas_plus.brain import tasks as task_store
        db = state / "state.sqlite"
        t = task_store.create_task("dream it", session_id="s", db_path=db, kind="dream")
        task_store.mark_running(t.task_id, db_path=db)
        task_store.mark_done(t.task_id, "ok", db_path=db)
        t2 = task_store.create_task("curate", session_id="s", db_path=db, kind="curation_report")
        task_store.mark_running(t2.task_id, db_path=db)
        task_store.mark_failed(t2.task_id, "UnicodeEncodeError: charmap", db_path=db)
        learning.add_session_note("Closing note", "Shipped the thing.", project="demo", state_dir=state)
        return ws, mem, state

    def test_digest_counts_sessions_notes_commits_and_brain_jobs(self, tmp_path):
        day = _today()  # notes and task timestamps are "now"; the digest for today sees them
        ws, mem, state = self._fixture(tmp_path, day)
        json_path, md_path, d = learning.write_session_digest(
            day, state_dir=state, memory_root=mem, workspace_root=ws, updates_file=state / "updates.txt")
        c = d["counts"]
        assert c["session_entries"] == 3            # Alpha, Beta (deduped across files), Gamma
        assert c["notes"] == 1 and c["commits"] == 1 and c["repos_touched"] == 1
        assert c["brain_jobs"] == 2 and c["brain_failed"] == 1
        assert [e["title"] for e in d["updates"]] == ["Gamma only in updates"]
        assert d["history"] == []                    # Alpha already came from the day file
        assert d["commits"]["demo"][0].endswith("demo: first commit")
        assert d["brain"]["failed"][0]["kind"] == "curation_report"
        md = md_path.read_text(encoding="utf-8")
        for needle in ("# Session digest", "- 10:00 Alpha work", "Gamma only in updates", "Closing note [demo]",
                       "demo: first commit", "curation_report: done 0, failed 1", "pending proposals"):
            assert needle in md, needle
        assert json.loads(json_path.read_text(encoding="utf-8"))["date"] == day
        # a rerun overwrites the same two files (deterministic)
        again = learning.write_session_digest(day, state_dir=state, memory_root=mem, workspace_root=ws,
                                              updates_file=state / "updates.txt")
        assert again[0] == json_path and sorted(p.name for p in json_path.parent.iterdir()) == [f"{day}.json", f"{day}.md"]

    def test_digest_is_empty_but_valid_without_inputs(self, tmp_path):
        ws = tmp_path / "empty"
        (ws / ".atlas").mkdir(parents=True)
        d = learning.build_session_digest("2026-11-19", state_dir=ws / ".atlas", memory_root=ws / ".memory",
                                          workspace_root=ws, updates_file=ws / "nope.txt")
        assert d["counts"] == {"session_entries": 0, "notes": 0, "repos_touched": 0, "commits": 0,
                               "brain_jobs": 0, "brain_failed": 0, "brain_minutes": 0}
        assert "(no session-log entries for this day)" in learning.render_digest_md(d)

    def test_recent_digests_and_prompt_block_are_bounded(self, tmp_path):
        state = tmp_path / ".atlas"
        out = learning.digests_dir(state)
        out.mkdir(parents=True)
        for i in range(10):
            d = {"date": f"2026-11-{i + 1:02d}", "counts": {"session_entries": 1, "commits": 2, "repos_touched": 1,
                                                          "brain_jobs": 3, "brain_failed": 0},
                 "sessions": [{"title": f"Title {i}", "summary": "x" * 300}], "history": [], "updates": [], "notes": [],
                 "commits": {"repo": ["abc subject one", "def subject two"]}, "brain": {"failed": []}}
            (out / f"{d['date']}.json").write_text(json.dumps(d), encoding="utf-8")
        (out / "garbage.json").write_text("{not json", encoding="utf-8")
        recent = learning.load_recent_digests(7, state_dir=state)
        assert [d["date"] for d in recent] == [f"2026-11-{i:02d}" for i in range(4, 11)]
        block = learning.digest_block(recent, max_chars=600)
        assert block.startswith("2026-11-04: 1 session entries, 2 commits") and len(block) <= 650
        assert block.endswith("(digest block truncated)")
        assert learning.digest_block([]) == ""


# ---------------------------------------------------------------- F

class TestProposalsDesk:
    @pytest.fixture
    def db(self, tmp_path):
        return tmp_path / "state.sqlite"

    def test_file_list_dedupe_count_reject(self, db):
        a = learning.add_proposal("supervisor-2026-11-20", "lesson", "  Name the  consumer ", "Every lane names its reader.", db_path=db)
        assert a["status"] == "pending" and a["title"] == "Name the consumer" and a["deduplicated"] is False
        b = learning.add_proposal("supervisor-2026-11-27", "lesson", "name the consumer", "other body", db_path=db)
        assert b["deduplicated"] is True and b["id"] == a["id"]
        c = learning.add_proposal("reflexion", "ticket", "Review fact f-1", "body", {"assignee": "human"}, db_path=db)
        assert learning.pending_proposal_count(db_path=db) == 2
        assert [p["id"] for p in learning.list_proposals("pending", db_path=db)] == [c["id"], a["id"]]
        assert learning.reject_proposal(c["id"], db_path=db) == {"ok": True, "id": c["id"]}
        assert learning.reject_proposal(c["id"], db_path=db)["ok"] is False  # already decided
        assert [p["id"] for p in learning.list_proposals("rejected", db_path=db)] == [c["id"]]
        assert len(learning.list_proposals("all", db_path=db)) == 2
        with pytest.raises(ValueError):
            learning.add_proposal("x", "tag", "t", "b", db_path=db)
        with pytest.raises(ValueError):
            learning.add_proposal("x", "fact", "", "b", db_path=db)

    def test_approve_lesson_appends_a_proposed_row_with_the_next_id(self, db, tmp_path):
        ledger = tmp_path / "LESSONS.md"
        ledger.write_text(
            "# Lessons ledger\n\n| id | pattern | rule | scope | check | status | sources |\n|---|---|---|---|---|---|---|\n"
            "| LES-005 | Old | Old rule | src | test_x | enforced | A1 |\n", encoding="utf-8")
        p = learning.add_proposal("supervisor-2026-11-20", "lesson", "Lanes name their reader",
                                  "Every scheduled | lane names its consumer.", {"scope": "brain/scheduler.py"}, db_path=db)
        res = learning.approve_proposal(p["id"], edited_body="Every scheduled lane names its consumer in its docstring.",
                                        db_path=db, lessons_path=ledger)
        assert res["ok"] and res["lesson_id"] == "LES-006"
        last = ledger.read_text(encoding="utf-8").rstrip("\n").splitlines()[-1]
        assert last.startswith("| LES-006 | Lanes name their reader | Every scheduled lane names its consumer in its docstring. | brain/scheduler.py | none yet | proposed | supervisor-2026-11-20 |")
        row = learning.list_proposals("approved", db_path=db)[0]
        assert row["body"].endswith("docstring.") and json.loads(row["result"])["lesson_id"] == "LES-006"
        assert learning.approve_proposal(p["id"], db_path=db, lessons_path=ledger) == {"ok": False, "error": "proposal is already approved"}

    def test_approve_lesson_creates_the_table_in_an_empty_ledger(self, db, tmp_path):
        ledger = tmp_path / "LESSONS.md"
        p = learning.add_proposal("s", "lesson", "First", "Rule one.", db_path=db)
        assert learning.approve_proposal(p["id"], db_path=db, lessons_path=ledger)["lesson_id"] == "LES-001"
        assert "| id | pattern | rule |" in ledger.read_text(encoding="utf-8")

    def test_approve_ticket_writes_a_ticket_file(self, db, tmp_path):
        p = learning.add_proposal("reflexion", "ticket", "Reflexion review: f-001", "## Steps\n\n- [ ] check",
                                  {"project": "demo", "assignee": "human", "effort": "S"}, db_path=db)
        res = learning.approve_proposal(p["id"], db_path=db, tickets_root=tmp_path / "tickets")
        assert res["ok"] and res["ticket_id"]
        files = list((tmp_path / "tickets").rglob("*.md"))
        assert len(files) == 1 and "Reflexion review: f-001" in files[0].read_text(encoding="utf-8")

    def test_approve_fact_calls_remember_with_scope_and_body(self, db):
        calls = []
        p = learning.add_proposal("s", "fact", "Operator uses E: for scratch", "Scratch lives on E:.", {"scope": "preference:scratch"}, db_path=db)
        res = learning.approve_proposal(p["id"], db_path=db, remember=lambda **kw: calls.append(kw) or {"file": "pref.md"})
        assert res == {"ok": True, "file": "pref.md", "id": p["id"], "kind": "fact"}
        assert calls == [{"scope": "preference:scratch", "fact": "Scratch lives on E:.", "source": "agent"}]

    def test_approve_failure_keeps_the_row_pending(self, db):
        p = learning.add_proposal("s", "fact", "x y", "body", db_path=db)

        def boom(**kw):
            raise RuntimeError("corpus locked")
        res = learning.approve_proposal(p["id"], db_path=db, remember=boom)
        assert res["ok"] is False and "corpus locked" in res["error"]
        assert learning.pending_proposal_count(db_path=db) == 1
        assert learning.approve_proposal("nope", db_path=db)["ok"] is False

    def test_parse_proposals_takes_the_last_fenced_block_and_drops_junk(self):
        text = (
            "## 1. Patterns\n\nprose\n\n```json\n{\"unrelated\": 1}\n```\n\n## 5. Health\n\nGood.\n\n"
            "```json\n{\"proposals\": [\n"
            "  {\"kind\": \"lesson\", \"title\": \"  Two   spaces  \", \"body\": \"rule\", \"scope\": \"x\"},\n"
            "  {\"kind\": \"tag\", \"title\": \"bad kind\", \"body\": \"b\"},\n"
            "  {\"kind\": \"ticket\", \"title\": \"no body\", \"body\": \"\"},\n"
            "  {\"kind\": \"FACT\", \"title\": \"Upper kind\", \"body\": \"fine\", \"project\": \"ignored-for-fact\"}\n"
            "]}\n```\n"
        )
        items = learning.parse_proposals(text)
        assert [(i["kind"], i["title"]) for i in items] == [("lesson", "Two spaces"), ("fact", "Upper kind")]
        assert items[0]["payload"] == {"scope": "x"} and items[1]["payload"] == {"project": "ignored-for-fact"}
        assert learning.parse_proposals("no block here") == [] and learning.parse_proposals("```json\nnot json\n```") == []

    def test_supervisor_context_lists_digests_ledger_and_history(self, db, tmp_path):
        state = tmp_path / ".atlas"
        out = learning.digests_dir(state)
        out.mkdir(parents=True)
        (out / "2026-11-19.json").write_text(json.dumps({"date": "2026-11-19", "counts": {}, "sessions": [{"title": "Alpha"}],
                                                          "commits": {}, "brain": {}}), encoding="utf-8")
        ledger = tmp_path / "LESSONS.md"
        ledger.write_text("| id | pattern | rule |\n|---|---|---|\n| LES-001 | Timeouts everywhere | r |\n", encoding="utf-8")
        p = learning.add_proposal("s", "lesson", "Rejected idea", "b", db_path=db)
        learning.reject_proposal(p["id"], db_path=db)
        ctx = learning.supervisor_context(state_dir=state, db_path=db, lessons_path=ledger)
        assert "RECENT WORK" in ctx and "Alpha" in ctx
        assert "LES-001: Timeouts everywhere" in ctx
        assert "[rejected] lesson: Rejected idea" in ctx


# ---------------------------------------------------------------- G

def _registered_ids(monkeypatch, env: dict[str, str]) -> set[str]:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from atlas_plus.brain import scheduler
    for var in ("ATLAS_TICKET_DISPATCH", "ATLAS_NTFY_URL", "ATLAS_SLA_MINUTES", "ATLAS_AUTONOMOUS"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    mock = MagicMock(spec=AsyncIOScheduler)
    mock.running = False
    ids: list[str] = []
    mock.add_job = lambda fn, trigger, id, **kw: ids.append(id)
    mock.add_listener = MagicMock()
    mock.start = MagicMock()
    mock.get_jobs = MagicMock(return_value=[])
    with patch("atlas_plus.brain.scheduler.AsyncIOScheduler", return_value=mock), \
         patch("atlas_plus.brain.scheduler._scheduler", None):
        scheduler.start_scheduler()
    return {i[:-8] if i.endswith("-catchup") else i for i in ids}


class TestSchedulerLanes:
    def test_session_digest_in_research_batch_out_switches_off_by_default(self, monkeypatch):
        ids = _registered_ids(monkeypatch, {})
        assert "session-digest" in ids
        assert "research-batch" not in ids
        assert "ticket-dispatch" not in ids and "sla-check" not in ids
        assert "zombie-cleanup" in ids and "dream" in ids and "claude-supervisor" in ids

    def test_switch_gated_lanes_register_when_on(self, monkeypatch):
        ids = _registered_ids(monkeypatch, {"ATLAS_TICKET_DISPATCH": "on", "ATLAS_AUTONOMOUS": "on",
                                            "ATLAS_NTFY_URL": "https://ntfy.example/t", "ATLAS_SLA_MINUTES": "15"})
        assert "ticket-dispatch" in ids and "sla-check" in ids

    def test_sla_check_stays_off_when_disabled_or_unreachable(self, monkeypatch):
        assert "sla-check" not in _registered_ids(monkeypatch, {"ATLAS_NTFY_URL": "https://ntfy.example/t", "ATLAS_SLA_MINUTES": "0"})
        assert "sla-check" not in _registered_ids(monkeypatch, {"ATLAS_SLA_MINUTES": "15"})

    def test_catchup_covers_session_digest_not_research_batch(self):
        from atlas_plus.brain import scheduler
        assert scheduler._CATCHUP_CADENCE_HOURS["session-digest"] == 24.0
        assert "research-batch" not in scheduler._CATCHUP_CADENCE_HOURS

    def test_lane_queues_a_session_digest_worker_task(self, monkeypatch):
        import asyncio
        from atlas_plus.brain import scheduler
        captured = {}

        def fake_create(**kw):
            captured.update(kw)
            return MagicMock(task_id="t1", session_id=kw.get("session_id"))
        monkeypatch.setattr("atlas_plus.brain.tasks.create_task", fake_create)
        asyncio.run(scheduler._session_digest())
        assert captured["kind"] == "session_digest" and captured["priority"] == 3
        assert captured["session_id"] == "scheduler-session-digest"

    def test_brain_server_can_trigger_the_lane_by_hand(self):
        from atlas_plus.brain import server
        assert "session-digest" in server._SCHEDULER_JOB_MAP
        assert "research-batch-manual" in server._SCHEDULER_JOB_MAP


# ---------------------------------------------------------------- H

def _ctx(tmp_path: Path, kind: str, instruction: str = "x"):
    from atlas_plus.brain import executors
    from atlas_plus.brain.tasks import Task
    task = Task(task_id=f"t-{kind}", session_id="s", instruction=instruction, status="running",
                created_at="2026-11-20T00:00:00", kind=kind)
    return task, executors.ExecutorContext(task=task, artifact_root=tmp_path / "art", db_path=tmp_path / "db.sqlite", worker_id="w")


class TestExecutors:
    def test_session_digest_executor_reports_counts_and_writes_the_artifact(self, tmp_path, monkeypatch):
        from atlas_plus.brain import executors
        digest = {"date": "2026-11-19", "counts": {"session_entries": 3, "notes": 1, "commits": 4, "repos_touched": 2,
                                                   "brain_jobs": 6, "brain_failed": 1}}
        md = tmp_path / "2026-11-19.md"
        md.write_text("# Session digest 2026-11-19\n", encoding="utf-8")
        seen = {}

        def fake_write(day):
            seen["day"] = day
            return tmp_path / "2026-11-19.json", md, digest
        monkeypatch.setattr("atlas_plus.brain.learning.write_session_digest", fake_write)
        task, ctx = _ctx(tmp_path, "session_digest", "Write the digest for 2026-11-19 please")
        result = executors.run_executor(task, ctx)
        assert result.status == "done" and seen["day"] == "2026-11-19"
        assert "3 session entries, 1 close notes, 4 commits in 2 repos, 6 Brain jobs (1 failed)" in result.text
        assert Path(result.artifact_path).read_text(encoding="utf-8").startswith("# Session digest")
        task, ctx = _ctx(tmp_path, "session_digest", "no date here")
        executors.run_executor(task, ctx)
        assert seen["day"] is None  # yesterday, local time, decided by the library

    def test_session_digest_executor_fails_the_task_not_the_worker(self, tmp_path, monkeypatch):
        from atlas_plus.brain import executors

        def boom(day):
            raise OSError("disk")
        monkeypatch.setattr("atlas_plus.brain.learning.write_session_digest", boom)
        task, ctx = _ctx(tmp_path, "session_digest")
        result = executors.run_executor(task, ctx)
        assert result.status == "failed" and "OSError: disk" in result.text

    def test_supervisor_writes_the_report_and_files_proposals(self, tmp_path, monkeypatch):
        from atlas_plus.brain import executors
        state = tmp_path / ".atlas"
        state.mkdir()
        monkeypatch.setattr(executors.atlas_paths, "state_dir", lambda: state)  # executors + learning share it
        monkeypatch.setattr(executors.task_store, "list_tasks", lambda status, limit: [])
        monkeypatch.setenv("ATLAS_SUPERVISOR_MODEL", "claude-sonnet-5-5")
        prompts = {}
        report = (
            "## 1. Patterns\n\nCuration crashed nightly.\n\n## 2. Root causes\n\nConsole code page.\n\n"
            "## 3. Recommendations\n\n1. Pass UTF-8 to python children in executors.py.\n2. Stop reflexion after 3 errors.\n\n"
            "## 4. Lessons for the ledger\n\nA lane with no reader is dead weight.\n\n## 5. Health\n\nFair: one lane down a week.\n\n"
            "```json\n{\"proposals\": [{\"kind\": \"lesson\", \"title\": \"Every lane names its reader\", \"body\": \"rule\", \"scope\": \"scheduler\"},"
            " {\"kind\": \"ticket\", \"title\": \"Fix curation encoding\", \"body\": \"pass env\", \"project\": \"atlas-memory+\"}]}\n```\n"
        )

        def fake_model(prompt, model, timeout):
            prompts["prompt"], prompts["model"], prompts["timeout"] = prompt, model, timeout
            return report, f"fake {model}"
        monkeypatch.setattr(executors, "_run_supervisor_model", fake_model)
        task, ctx = _ctx(tmp_path, "claude_supervisor")
        result = executors.run_executor(task, ctx)
        assert result.status == "done" and "2 proposal(s) filed" in result.text
        assert prompts["model"] == "claude-sonnet-5-5" and prompts["timeout"] == 900
        assert "RECENT WORK" in prompts["prompt"] and "LESSONS LEDGER" in prompts["prompt"] and "## 4. Lessons for the ledger" in prompts["prompt"]
        reports = list((state / "reports").glob("supervisor-*.md"))
        assert len(reports) == 1
        text = reports[0].read_text(encoding="utf-8")
        assert "## 3. Recommendations" in text and "Proposals filed: 2 new, 0 already on the desk" in text and "fake claude-sonnet-5-5" in text
        pending = learning.list_proposals("pending", db_path=state / "state.sqlite")
        assert sorted(p["title"] for p in pending) == ["Every lane names its reader", "Fix curation encoding"]
        assert all(p["source"].startswith("supervisor-") for p in pending)
        # a second run the same week dedupes instead of doubling the desk
        result2 = executors.run_executor(task, ctx)
        assert "0 proposal(s) filed" in result2.text
        assert learning.pending_proposal_count(db_path=state / "state.sqlite") == 2
        # the SessionStart hook reads the same report and the same desk
        import atlas_plus.hooks.session_start as hook
        monkeypatch.setattr(hook, "STATE_DIR", state)
        monkeypatch.setattr(hook, "MEMORY_ROOT", tmp_path / "mem")
        notes = hook._brain_notes()
        assert "Proposals awaiting your click: 2" in notes and "lesson: Every lane names its reader" in notes
        assert "Pass UTF-8 to python children" in notes

    def test_supervisor_without_a_report_is_a_failed_task(self, tmp_path, monkeypatch):
        from atlas_plus.brain import executors
        monkeypatch.setattr(executors.atlas_paths, "state_dir", lambda: tmp_path / ".atlas")
        monkeypatch.setattr(executors.task_store, "list_tasks", lambda status, limit: [])
        monkeypatch.setattr(executors, "_run_supervisor_model", lambda p, m, t: ("", "claude --print exited 1: nope"))
        task, ctx = _ctx(tmp_path, "claude_supervisor")
        result = executors.run_executor(task, ctx)
        assert result.status == "failed" and "exited 1" in result.text

    def test_supervisor_model_runner_uses_stdin_single_turn_and_utf8(self, monkeypatch):
        from atlas_plus.brain import executors
        calls = {}

        def fake_run(cmd, **kw):
            calls["cmd"], calls["kw"] = cmd, kw
            return subprocess.CompletedProcess(cmd, 0, stdout="## 1. Patterns\nok\n", stderr="")
        monkeypatch.setattr(executors.subprocess, "run", fake_run)
        monkeypatch.setattr("shutil.which", lambda name: "C:/claude.cmd" if name == "claude" else None)
        text, how = executors._run_supervisor_model("<prompt> with <angles>", "claude-sonnet-5-5", 900)
        assert text.startswith("## 1. Patterns") and how == "claude --print --model claude-sonnet-5-5"
        assert calls["cmd"] == ["C:/claude.cmd", "--print", "--model", "claude-sonnet-5-5", "--max-turns", "1"]
        assert calls["kw"]["input"] == "<prompt> with <angles>"  # stdin: no argv clipping, no bracket stripping
        assert calls["kw"]["timeout"] == 900 and calls["kw"]["env"]["PYTHONUTF8"] == "1"

    def test_supervisor_model_default_is_sonnet(self, monkeypatch):
        from atlas_plus.brain import executors
        monkeypatch.delenv("ATLAS_SUPERVISOR_MODEL", raising=False)
        assert executors._supervisor_model() == "claude-sonnet-5-5"
        monkeypatch.setenv("ATLAS_SUPERVISOR_MODEL", "claude-opus-5-5")
        assert executors._supervisor_model() == "claude-opus-5-5"


# ---------------------------------------------------------------- I

class TestReaders:
    def test_dream_prompt_carries_recent_work_when_digests_exist(self, tmp_path, monkeypatch):
        import atlas_plus.curation.dream_mode as dream
        state = tmp_path / ".atlas"
        out = learning.digests_dir(state)
        out.mkdir(parents=True)
        (out / "2026-11-19.json").write_text(json.dumps({"date": "2026-11-19", "counts": {"session_entries": 2},
                                                          "sessions": [{"title": "Harbor parity sweep", "summary": "s"}],
                                                          "commits": {}, "brain": {}}), encoding="utf-8")
        monkeypatch.setattr(dream, "STATE_DIR", state)
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        mem = tmp_path / "mem"
        (mem / "feedback").mkdir(parents=True)
        (mem / "feedback" / "rule.md").write_text("---\ntitle: Rule\n---\n\nVersion before edit.\n", encoding="utf-8")
        prompts = []
        result = dream.run_dream(memory_root=mem, scopes=["feedback"], _call_llm_fn=lambda p: prompts.append(p) or "reflection")
        assert result["written"], result
        assert "=== RECENT WORK (session digests, oldest first) ===" in prompts[0]
        assert "Harbor parity sweep" in prompts[0] and "Version before edit." in prompts[0]
        assert prompts[0].index("RECENT WORK") < prompts[0].index("=== FEEDBACK ===")

    def test_dream_prompt_is_unchanged_without_digests(self, tmp_path, monkeypatch):
        import atlas_plus.curation.dream_mode as dream
        monkeypatch.setattr(dream, "STATE_DIR", tmp_path / ".atlas")
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "off")
        mem = tmp_path / "mem"
        (mem / "feedback").mkdir(parents=True)
        (mem / "feedback" / "rule.md").write_text("---\ntitle: Rule\n---\n\nBody.\n", encoding="utf-8")
        prompts = []
        dream.run_dream(memory_root=mem, scopes=["feedback"], _call_llm_fn=lambda p: prompts.append(p) or "r")
        assert "RECENT WORK (session digests" not in prompts[0]

    def test_bot_authored_items_are_normal_whatever_the_body_says(self):
        from atlas_plus.github_intake import classify
        body = "Bumps cryptography. Fixes CVE-2026-1234, a security vulnerability allowing an auth bypass."
        assert classify.classify_issue("Bump cryptography", body, ["dependencies"])[0] == "critical"
        sev, reason = classify.classify_issue("Bump cryptography", body, ["dependencies"], author="dependabot[bot]")
        assert sev == "normal" and "bot-authored" in reason
        assert classify.is_bot_author("github-actions[bot]") and not classify.is_bot_author("someone") and not classify.is_bot_author(None)

    def test_bot_pull_request_is_investigated_once(self):
        from atlas_plus.github_intake import poller

        def raw(n, user, updated, pr=True, state="open"):
            return {"number": n, "title": f"Bump x {n}", "body": "security fix CVE-2026-1", "labels": [],
                    "user": {"login": user}, "html_url": "u", "state": state, "created_at": "2026-11-20T09:00:00Z",
                    "updated_at": updated, "comments": 0, **({"pull_request": {"url": "x"}} if pr else {})}

        def runner_for(issues):
            def run(args):
                path = args[1]
                if "/issues?" in path:
                    return 0, json.dumps(issues), ""
                return 1, "", "gh: Not Found (HTTP 404)"
            return run
        first = poller.poll("x/y", cursor_utc=None, seen={},
                            runner=runner_for([raw(3, "dependabot[bot]", "2026-11-20T10:00:00Z"), raw(7, "alice", "2026-11-20T10:00:00Z")]))
        by_key = {i.key: i for i in first.items}
        assert by_key["pr:3"].severity == "normal" and by_key["pr:7"].severity == "critical"
        seen = {i.key: i.version for i in first.items}
        # both PRs rebased / updated
        second = poller.poll("x/y", cursor_utc=None, seen=seen,
                             runner=runner_for([raw(3, "dependabot[bot]", "2026-11-20T12:00:00Z"), raw(7, "alice", "2026-11-20T12:00:00Z")]))
        assert [i.key for i in second.items] == ["pr:7"] and second.items[0].is_update
        # the bot PR closing still comes through (the ledger records it)
        third = poller.poll("x/y", cursor_utc=None, seen=seen,
                            runner=runner_for([raw(3, "dependabot[bot]", "2026-11-20T13:00:00Z", state="closed")]))
        assert [i.key for i in third.items] == ["pr:3"]

    def test_session_start_shows_proposals_and_the_newest_digest(self, tmp_path, monkeypatch):
        import atlas_plus.hooks.session_start as hook
        state = tmp_path / ".atlas"
        state.mkdir()
        db = state / "state.sqlite"
        learning.add_proposal("supervisor-2026-11-20", "lesson", "Name the reader", "r", db_path=db)
        learning.add_proposal("reflexion", "ticket", "Reflexion review: f-9", "b", db_path=db)
        con = sqlite3.connect(db)
        con.executescript("create table brain_tasks(task_id text, kind text, status text, created_at text);"
                          "create table pending_facts(id integer, status text);")
        con.commit(); con.close()
        out = learning.digests_dir(state)
        out.mkdir(parents=True)
        (out / "2026-11-18.json").write_text(json.dumps({"date": "2026-11-18", "counts": {}}), encoding="utf-8")
        (out / "2026-11-19.json").write_text(json.dumps({"date": "2026-11-19", "counts": {"session_entries": 3, "commits": 4,
                                                          "repos_touched": 2, "brain_jobs": 6, "brain_failed": 1}}), encoding="utf-8")
        monkeypatch.setattr(hook, "STATE_DIR", state)
        monkeypatch.setattr(hook, "MEMORY_ROOT", tmp_path / "mem")
        text = hook._brain_notes()
        assert "Proposals awaiting your click: 2 (Helm > Memory > Proposals)" in text
        assert "  - lesson: Name the reader" in text and "  - ticket: Reflexion review: f-9" in text
        assert "Digest 2026-11-19: 3 session entries, 4 commits in 2 repos, 6 Brain jobs (1 failed)" in text

    def test_session_close_note_cli_appends_one_line(self, tmp_path, monkeypatch):
        state = tmp_path / ".atlas"
        monkeypatch.setattr(learning.atlas_paths, "state_dir", lambda: state)
        assert learning.main(["note", "--title", "Brain pass", "--text", "Shipped ATL-073.", "--project", "atlas-memory+"]) == 0
        rows = [json.loads(l) for l in learning.notes_path(state).read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1 and rows[0]["title"] == "Brain pass" and rows[0]["project"] == "atlas-memory+"
        assert rows[0]["date"] == _today()
        assert learning._notes_for(_today(), state)[0]["text"] == "Shipped ATL-073."
        assert learning._notes_for("2001-01-01", state) == []
