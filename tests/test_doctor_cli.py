"""Tests for atlas_plus.doctor.cli -- collect(), render_text(), and individual checks."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# check_disk
# ---------------------------------------------------------------------------

class TestCheckDisk:
    def test_returns_dict_with_expected_keys(self):
        from atlas_plus.doctor.cli import check_disk
        result = check_disk()
        assert isinstance(result, dict)
        if result:  # may be empty on unusual systems
            assert "free_gb" in result
            assert "total_gb" in result
            assert "pct_used" in result

    def test_returns_empty_dict_on_error(self):
        from atlas_plus.doctor.cli import check_disk
        import shutil as _shutil
        with patch.object(_shutil, "disk_usage", side_effect=OSError("no disk")):
            result = check_disk()
        assert result == {}


# ---------------------------------------------------------------------------
# check_autonomous
# ---------------------------------------------------------------------------

class TestCheckAutonomous:
    def test_returns_env_var_values(self):
        from atlas_plus.doctor.cli import check_autonomous
        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "off", "ATLAS_SESSION_AUTONOMOUS": "on"}):
            result = check_autonomous()
        assert result["ATLAS_AUTONOMOUS"] == "off"
        assert result["ATLAS_SESSION_AUTONOMOUS"] == "on"

    def test_missing_vars_show_not_set(self):
        from atlas_plus.doctor.cli import check_autonomous
        env = {k: v for k, v in os.environ.items()
               if k not in ("ATLAS_AUTONOMOUS", "ATLAS_SESSION_AUTONOMOUS")}
        with patch.dict(os.environ, env, clear=True):
            result = check_autonomous()
        assert "(not set)" in result["ATLAS_AUTONOMOUS"]


# ---------------------------------------------------------------------------
# check_fsdiff_count
# ---------------------------------------------------------------------------

class TestCheckFsdiffCount:
    def test_returns_zero_when_dir_absent(self):
        from atlas_plus.doctor.cli import check_fsdiff_count, STATE_DIR
        with patch("atlas_plus.doctor.cli.STATE_DIR", Path("/nonexistent/path")):
            count = check_fsdiff_count()
        assert count == 0

    def test_counts_fsdiff_files(self):
        from atlas_plus.doctor.cli import check_fsdiff_count
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fsdiff_dir = tmp_path / "fsdiffs"
            fsdiff_dir.mkdir()
            (fsdiff_dir / "a.fsdiff.json").write_text("{}")
            (fsdiff_dir / "b.fsdiff.json").write_text("{}")
            (fsdiff_dir / "notthis.json").write_text("{}")
            with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
                count = check_fsdiff_count()
        assert count == 2


# ---------------------------------------------------------------------------
# check_backup
# ---------------------------------------------------------------------------

class TestCheckBackup:
    def test_never_when_no_log(self):
        from atlas_plus.doctor.cli import check_backup
        with patch("atlas_plus.doctor.cli.STATE_DIR", Path("/nonexistent/path")):
            result = check_backup()
        assert result["last_run"] == "never"

    def test_reads_last_line(self):
        from atlas_plus.doctor.cli import check_backup
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "backup.log"
            log.write_text("first line\nsecond line success\n")
            with patch("atlas_plus.doctor.cli.STATE_DIR", Path(tmp)):
                result = check_backup()
        assert "second line" in result["last_run"]
        assert result["result"] == "ok"


# ---------------------------------------------------------------------------
# check_worker_status
# ---------------------------------------------------------------------------

class TestCheckWorkerStatus:
    def _write_db(self, db_path):
        """Create a minimal brain_tasks table in a temp SQLite DB."""
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE brain_tasks("
            "task_id TEXT, session_id TEXT, instruction TEXT, kind TEXT, priority INT,"
            "agent TEXT, status TEXT, created_at TEXT, started_at TEXT, finished_at TEXT,"
            "result TEXT, error TEXT, max_attempts INT, attempts INT, artifact_path TEXT,"
            "heartbeat_at TEXT, lease_owner TEXT, lease_expires_at TEXT)"
        )
        conn.commit()
        conn.close()

    def test_unknown_when_no_db(self):
        from atlas_plus.doctor.cli import check_worker_status
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "atlas_plus.doctor.cli.STATE_DIR", __import__("pathlib").Path("/nonexistent")
        ):
            result = check_worker_status()
        assert result["assessment"] == "unknown"

    def test_idle_empty_queue(self, tmp_path):
        from atlas_plus.doctor.cli import check_worker_status
        db = tmp_path / "state.sqlite"
        self._write_db(db)
        import unittest.mock as mock
        with mock.patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            result = check_worker_status()
        assert result["assessment"] == "idle"
        assert result["pending"] == 0
        assert result["running"] == 0

    def test_warning_when_pending_and_no_recent_activity(self, tmp_path):
        from atlas_plus.doctor.cli import check_worker_status
        import sqlite3
        db = tmp_path / "state.sqlite"
        self._write_db(db)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO brain_tasks(task_id, status, created_at) VALUES('t1', 'pending', '2026-01-01T00:00:00')"
        )
        conn.commit()
        conn.close()
        import unittest.mock as mock
        with mock.patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            result = check_worker_status()
        assert result["assessment"] == "warning"
        assert result["pending"] == 1

    def test_ok_when_task_finished_recently(self, tmp_path):
        from atlas_plus.doctor.cli import check_worker_status
        import sqlite3
        from datetime import datetime, timezone
        db = tmp_path / "state.sqlite"
        self._write_db(db)
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO brain_tasks(task_id, status, finished_at) VALUES('t2', 'done', ?)",
            (now_iso,),
        )
        conn.commit()
        conn.close()
        import unittest.mock as mock
        with mock.patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            result = check_worker_status()
        assert result["assessment"] == "ok"
        assert result["last_finished_at"] != "never"


# ---------------------------------------------------------------------------
# check_convergence
# ---------------------------------------------------------------------------

class TestCheckConvergence:
    def _write_with_contradictions(self, path, title, contradictions):
        import yaml
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = {"title": title, "contradictions": contradictions}
        path.write_text(f"---\n{yaml.dump(fm)}---\n\nbody\n", encoding="utf-8")

    def test_returns_zero_when_no_contradictions(self, tmp_path):
        from unittest.mock import patch
        from atlas_plus.doctor.cli import check_convergence
        (tmp_path / "ref.md").write_text("---\ntitle: clean\n---\nbody\n", encoding="utf-8")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            result = check_convergence()
        assert result["flagged"] == 0

    def test_counts_flagged_files(self, tmp_path):
        from unittest.mock import patch
        from atlas_plus.doctor.cli import check_convergence
        self._write_with_contradictions(
            tmp_path / "ref.md", "SIP", ["voip.md"]
        )
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            result = check_convergence()
        assert result["flagged"] == 1
        assert "ref.md" in result["paths"][0]

    def test_skips_archive_directory(self, tmp_path):
        from unittest.mock import patch
        from atlas_plus.doctor.cli import check_convergence
        self._write_with_contradictions(
            tmp_path / "archive" / "old.md", "Old", ["other.md"]
        )
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            result = check_convergence()
        assert result["flagged"] == 0


# ---------------------------------------------------------------------------
# check_decisions
# ---------------------------------------------------------------------------

class TestCheckDecisions:
    def _write_decision(self, path, title, made, last_reviewed=""):
        import yaml
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = {"title": title, "type": "decision", "made": made}
        if last_reviewed:
            fm["last_reviewed"] = last_reviewed
        path.write_text(f"---\n{yaml.dump(fm)}---\nbody\n", encoding="utf-8")

    def test_returns_zeros_when_no_decisions_dir(self, tmp_path):
        from unittest.mock import patch
        from atlas_plus.doctor.cli import check_decisions
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            result = check_decisions()
        assert result["total"] == 0
        assert result["stale"] == 0

    def test_counts_total_decisions(self, tmp_path):
        from unittest.mock import patch
        from atlas_plus.doctor.cli import check_decisions
        self._write_decision(tmp_path / "decisions" / "d1.md", "Decision 1", "2026-05-01", "2026-05-15")
        self._write_decision(tmp_path / "decisions" / "d2.md", "Decision 2", "2026-05-02", "2026-05-15")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            result = check_decisions()
        assert result["total"] == 2

    def test_flags_stale_decisions(self, tmp_path):
        from unittest.mock import patch
        from atlas_plus.doctor.cli import check_decisions
        self._write_decision(tmp_path / "decisions" / "old.md", "Stale Decision", "2026-01-01")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            result = check_decisions()
        assert result["stale"] >= 1
        assert any("Stale Decision" in t for t in result.get("stale_titles", []))


# ---------------------------------------------------------------------------
# collect() -- keys present
# ---------------------------------------------------------------------------

class TestCollect:
    @pytest.fixture(autouse=True)
    def _isolate_live_checks(self, tmp_path, monkeypatch):
        """Make collect()'s structural tests fast and deterministic.

        collect() aggregates ~30 checks, including live HTTP probes (Brain/MCP/
        Ollama via _http_get) and filesystem scans of .memory / .atlas. Calling
        the real aggregator three times made these tests slow and
        environment-dependent -- they behaved differently (and dragged) when the
        services were up. Stub the single HTTP primitive and point the scans at
        an empty tmp dir so the tests assert collect()'s STRUCTURE only, with no
        live calls.
        """
        monkeypatch.setattr("atlas_plus.doctor.cli._http_get", lambda *a, **k: (200, {}))
        monkeypatch.setattr("atlas_plus.doctor.cli._powershell_json", lambda *a, **k: None)
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path))
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path))
        monkeypatch.setattr("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path)
        monkeypatch.setattr("atlas_plus.doctor.cli.STATE_DIR", tmp_path)

    def test_all_keys_present(self):
        from atlas_plus.doctor.cli import collect
        data = collect()
        for key in ("ts", "services", "brain_diagnostic", "autonomous",
                    "disk", "temporal", "tickets", "memory_gaps",
                    "backup", "nightly", "fsdiff_count", "worker", "convergence", "decisions",
                    "rederivable", "memory_trends", "research_quality", "memory_health"):
            assert key in data, f"missing key: {key}"
        # conditional keys: present exactly when their probe is relevant on this machine
        from atlas_plus.doctor import cli as doctor_cli
        assert ("gatekeeper" in data) == (doctor_cli.check_gatekeeper is not None)
        assert ("tunnel" in data) == doctor_cli._tunnel_check_relevant()

    def test_tunnel_key_present_when_relevant(self, monkeypatch):
        from atlas_plus.doctor import cli as doctor_cli
        monkeypatch.setattr(doctor_cli, "_tunnel_check_relevant", lambda: True)
        assert "tunnel" in doctor_cli.collect()

    def test_tunnel_key_absent_when_irrelevant(self, monkeypatch):
        from atlas_plus.doctor import cli as doctor_cli
        monkeypatch.setattr(doctor_cli, "_tunnel_check_relevant", lambda: False)
        assert "tunnel" not in doctor_cli.collect()

    def test_services_list_has_brain_mcp_ollama(self):
        from atlas_plus.doctor.cli import collect
        data = collect()
        names = {s["name"] for s in data["services"]}
        assert {"Brain", "MCP", "Web", "Sync", "Ollama"} == names

    def test_each_service_has_status(self):
        from atlas_plus.doctor.cli import collect
        data = collect()
        for svc in data["services"]:
            assert svc["status"] in ("up", "down", "degraded")


# ---------------------------------------------------------------------------
# render_text() -- smoke test
# ---------------------------------------------------------------------------

class TestRenderText:
    def _minimal_data(self) -> dict:
        return {
            "ts": "2026-05-19T00:00:00+00:00",
            "services": [
                {"name": "Brain",  "status": "up",      "detail": ""},
                {"name": "MCP",    "status": "down",     "detail": "no connection"},
                {"name": "Ollama", "status": "degraded", "detail": "HTTP 503"},
            ],
            "brain_diagnostic": {"model": "qwen3:8b", "autonomous": True, "credit_exhausted_agents": []},
            "autonomous": {"ATLAS_AUTONOMOUS": "on", "ATLAS_SESSION_AUTONOMOUS": "off"},
            "disk": {"drive": "C:\\", "free_gb": 50.0, "total_gb": 200.0, "pct_used": 75.0},
            "temporal": {"entities": 10, "facts_active": 30},
            "tickets": {"total": 5, "by_status": {"todo": 2, "done": 3}, "by_assignee": {"human": 3, "claude-code": 2}},
            "memory_gaps": {"unique_miss_queries": 0, "top": []},
            "backup": {"last_run": "2026-05-19 backup success", "result": "ok"},
            "nightly": {"temporal-build": {"status": "done", "last": "2026-05-19T03:05"}},
            "fsdiff_count": 3,
        }

    def test_render_returns_string(self):
        from atlas_plus.doctor.cli import render_text
        out = render_text(self._minimal_data())
        assert isinstance(out, str)
        assert len(out) > 100

    def test_render_contains_service_names(self):
        from atlas_plus.doctor.cli import render_text
        out = render_text(self._minimal_data())
        assert "Brain" in out
        assert "MCP" in out
        assert "Ollama" in out

    def test_render_shows_credit_exhausted(self):
        from atlas_plus.doctor.cli import render_text
        data = self._minimal_data()
        data["brain_diagnostic"]["credit_exhausted_agents"] = ["claude-code"]
        out = render_text(data)
        assert "Credit exhausted" in out
        assert "claude-code" in out

    def test_render_no_credit_exhausted_shows_nothing_extra(self):
        from atlas_plus.doctor.cli import render_text
        out = render_text(self._minimal_data())
        assert "Credit exhausted" not in out

    def test_render_empty_data(self):
        from atlas_plus.doctor.cli import render_text
        out = render_text({"ts": "2026-05-19", "services": []})
        assert isinstance(out, str)

    def test_render_shows_disk_free(self):
        from atlas_plus.doctor.cli import render_text
        out = render_text(self._minimal_data())
        assert "50" in out  # free_gb

    def test_render_shows_nightly_jobs(self):
        from atlas_plus.doctor.cli import render_text
        out = render_text(self._minimal_data())
        assert "temporal-build" in out

    def test_render_shows_temporal_staleness(self):
        from atlas_plus.doctor.cli import render_text
        data = self._minimal_data()
        data["temporal"] = {"entities": 5, "facts_active": 10, "stale": True, "age_days": 10, "last_built": "2026-05-10"}
        out = render_text(data)
        assert "STALE" in out or "stale" in out.lower()

    def test_render_shows_temporal_fresh_date(self):
        from atlas_plus.doctor.cli import render_text
        data = self._minimal_data()
        data["temporal"] = {"entities": 5, "facts_active": 10, "stale": False, "age_days": 1, "last_built": "2026-05-20"}
        out = render_text(data)
        assert "2026-05-20" in out or "5" in out

    def test_render_shows_memory_health_avg(self):
        from atlas_plus.doctor.cli import render_text
        data = self._minimal_data()
        # memory_health is rendered inside the memory_quality block
        data["memory_quality"] = {
            "total_files": 20, "confirmed_pct": 60.0, "tagged_pct": 50.0,
            "avg_age_days": 30.0, "stale_confirmed": 0, "anti_flagged": 0,
            "confidence_dist": {"high": 10, "medium": 8, "low": 2, "none": 0},
            "untagged_files": 0,
        }
        data["memory_health"] = {
            "total_files": 20, "score_0": 2, "score_25": 3, "score_50": 5,
            "score_75": 6, "score_100": 4, "avg_score": 0.62,
        }
        out = render_text(data)
        assert "0.62" in out or "Health avg" in out

    def test_render_shows_trends_sparkline(self):
        from atlas_plus.doctor.cli import render_text
        data = self._minimal_data()
        data["memory_quality"] = {
            "total_files": 10, "confirmed_pct": 70.0, "tagged_pct": 80.0,
            "avg_age_days": 20.0, "stale_confirmed": 0, "anti_flagged": 0,
            "confidence_dist": {"high": 8, "medium": 2, "low": 0, "none": 0},
            "untagged_files": 0,
        }
        data["memory_trends"] = [
            {"ts": "2026-05-15", "total_files": 100, "confirmed_pct": 60.0},
            {"ts": "2026-05-16", "total_files": 102, "confirmed_pct": 61.0},
        ]
        out = render_text(data)
        # Should show the 7d trend sparkline
        assert "7d" in out or "trend" in out.lower()


# ---------------------------------------------------------------------------
# check_brain_diagnostic -- error path
# ---------------------------------------------------------------------------

class TestCheckBrainDiagnostic:
    def test_unreachable_brain_returns_error_key(self):
        from atlas_plus.doctor.cli import check_brain_diagnostic
        with patch("atlas_plus.doctor.cli._http_get", return_value=(0, {"error": "connection refused"})):
            result = check_brain_diagnostic()
        assert "_error" in result

    def test_401_returns_error_key(self):
        from atlas_plus.doctor.cli import check_brain_diagnostic
        with patch("atlas_plus.doctor.cli._http_get", return_value=(401, {})):
            result = check_brain_diagnostic()
        assert "_error" in result
        assert "401" in result["_error"]

    def test_200_returns_body(self):
        from atlas_plus.doctor.cli import check_brain_diagnostic
        body = {"model": "qwen3:8b", "autonomous": True, "credit_exhausted_agents": []}
        with patch("atlas_plus.doctor.cli._http_get", return_value=(200, body)):
            result = check_brain_diagnostic()
        assert result["model"] == "qwen3:8b"


# ---------------------------------------------------------------------------
# check_rederivable
# ---------------------------------------------------------------------------

class TestCheckRederivable:
    def test_returns_zero_when_no_candidates(self):
        from atlas_plus.doctor.cli import check_rederivable
        from unittest.mock import patch

        with patch("atlas_plus.curation.rederive.find_rederivable", return_value=[]):
            result = check_rederivable()

        assert result["candidates"] == 0
        assert result.get("top") == [] or "top" in result

    def test_returns_candidate_count(self):
        from atlas_plus.doctor.cli import check_rederivable
        from atlas_plus.curation.rederive import RederiveCandidate
        from unittest.mock import patch

        fake = [
            RederiveCandidate("ref/tool.md", "Tool Reference", "reference",
                              "AGENTS.md", 0.55, ["atlas"]),
            RederiveCandidate("ref/sip.md", "SIP Reference", "reference",
                              "README.md", 0.42, ["sip"]),
        ]
        with patch("atlas_plus.curation.rederive.find_rederivable", return_value=fake):
            result = check_rederivable()

        assert result["candidates"] == 2
        assert len(result["top"]) == 2
        assert result["top"][0]["similarity"] == 0.55

    def test_exception_returns_zero_with_error(self):
        from atlas_plus.doctor.cli import check_rederivable
        from unittest.mock import patch

        with patch("atlas_plus.curation.rederive.find_rederivable",
                   side_effect=RuntimeError("workspace not found")):
            result = check_rederivable()

        assert result["candidates"] == 0
        assert "error" in result


class TestCheckPendingFacts:
    def test_returns_zero_when_empty(self):
        from atlas_plus.doctor.cli import check_pending_facts
        from unittest.mock import patch

        with patch("atlas_plus.brain.task_facts.pending_count", return_value=0):
            result = check_pending_facts()

        assert result["count"] == 0
        assert result["oldest"] is None

    def test_returns_count_and_oldest(self, tmp_path, monkeypatch):
        from atlas_plus.doctor import cli as doc
        from atlas_plus.doctor.cli import check_pending_facts
        from unittest.mock import patch, MagicMock
        (tmp_path / "state.sqlite").write_bytes(b"")
        monkeypatch.setattr(doc, "STATE_DIR", tmp_path)

        mock_fact = MagicMock()
        mock_fact.created_at = "2026-05-10T12:00:00"

        with patch("atlas_plus.brain.task_facts.pending_count", return_value=3), \
             patch("atlas_plus.brain.task_facts.list_pending_facts", return_value=[mock_fact]):
            result = check_pending_facts()

        assert result["count"] == 3
        assert result["oldest"] == "2026-05-10T12:00:00"

    def test_exception_returns_zero(self):
        from atlas_plus.doctor.cli import check_pending_facts
        from unittest.mock import patch

        with patch("atlas_plus.brain.task_facts.pending_count",
                   side_effect=RuntimeError("db error")):
            result = check_pending_facts()

        assert result["count"] == 0


class TestCheckSLABreaches:
    def test_returns_zero_when_disabled(self, monkeypatch):
        from atlas_plus.doctor.cli import check_sla_breaches
        monkeypatch.setenv("ATLAS_SLA_MINUTES", "0")
        result = check_sla_breaches()
        assert result["count"] == 0

    def test_returns_breached_task_ids(self, monkeypatch, tmp_path):
        from atlas_plus.doctor import cli as doc
        from atlas_plus.doctor.cli import check_sla_breaches
        from unittest.mock import MagicMock, patch
        from datetime import datetime, timedelta, timezone
        (tmp_path / "state.sqlite").write_bytes(b"")
        monkeypatch.setattr(doc, "STATE_DIR", tmp_path)

        monkeypatch.setenv("ATLAS_SLA_MINUTES", "15")
        mock_task = MagicMock()
        mock_task.task_id = "sla-breached-001"
        mock_task.started_at = (
            datetime.now(timezone.utc) - timedelta(minutes=20)
        ).strftime("%Y-%m-%dT%H:%M:%S")

        with patch("atlas_plus.brain.tasks.list_tasks", return_value=[mock_task]):
            result = check_sla_breaches()

        assert result["count"] == 1
        assert any("sla-breached" in tid for tid in result["task_ids"])

    def test_exception_returns_zero(self, monkeypatch):
        from atlas_plus.doctor.cli import check_sla_breaches
        from unittest.mock import patch

        monkeypatch.setenv("ATLAS_SLA_MINUTES", "15")
        with patch("atlas_plus.brain.tasks.list_tasks", side_effect=RuntimeError("db err")):
            result = check_sla_breaches()

        assert result["count"] == 0


# ---------------------------------------------------------------------------
# check_worker_detail
# ---------------------------------------------------------------------------

class TestCheckWorkerDetail:
    def test_returns_expected_keys(self, tmp_path):
        from atlas_plus.doctor.cli import check_worker_detail
        from atlas_plus.brain import tasks as ts

        db = tmp_path / "state.sqlite"
        ts.create_task("a job", db_path=db)

        with patch("atlas_plus.brain.tasks._DB_PATH", db):
            result = check_worker_detail()

        assert "active_workers" in result
        assert "pending_by_agent" in result
        assert "zombie_count" in result
        assert "queue_depth" in result

    def test_returns_empty_on_missing_db(self, tmp_path):
        from atlas_plus.doctor.cli import check_worker_detail
        from atlas_plus.brain.tasks import worker_detail as _real_wd
        from unittest.mock import patch

        missing = tmp_path / "missing.sqlite"
        # Pass the missing path explicitly via a wrapper. (db_path now defaults
        # to None and resolves _DB_PATH at call time, so patching the module
        # attribute would also work; the wrapper is kept for directness.)
        with patch("atlas_plus.brain.tasks.worker_detail", lambda: _real_wd(db_path=missing)):
            result = check_worker_detail()

        assert result["active_workers"] == []
        assert result["queue_depth"] == 0

    def test_exception_gracefully_returns_empty(self):
        from atlas_plus.doctor.cli import check_worker_detail
        from unittest.mock import patch

        import tempfile
        from pathlib import Path as _P
        state = _P(tempfile.mkdtemp())
        (state / "state.sqlite").write_bytes(b"")  # the check only runs when the task DB exists
        with patch("atlas_plus.doctor.cli.STATE_DIR", state),              patch("atlas_plus.brain.tasks.worker_detail", side_effect=RuntimeError("boom")):
            result = check_worker_detail()

        assert result["active_workers"] == []
        assert result.get("error") is not None


# ---------------------------------------------------------------------------
# check_memory_quality
# ---------------------------------------------------------------------------

class TestCheckMemoryQuality:
    def _write_md(self, path, frontmatter: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{frontmatter}\n---\n\nContent.\n", encoding="utf-8")

    def test_empty_memory_returns_zero(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_quality
        from unittest.mock import patch
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path / "empty"):
            result = check_memory_quality()
        assert result["total_files"] == 0

    def test_counts_total_files(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_quality
        from unittest.mock import patch
        mem = tmp_path / "memory"
        self._write_md(mem / "feedback" / "a.md", "type: feedback")
        self._write_md(mem / "identity" / "b.md", "type: identity")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem):
            result = check_memory_quality()
        assert result["total_files"] == 2

    def test_confirmed_pct_calculated(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_quality
        from unittest.mock import patch
        from datetime import date
        mem = tmp_path / "memory"
        today = date.today().isoformat()
        self._write_md(mem / "feedback" / "a.md", f"type: feedback\nlast_confirmed: {today}")
        self._write_md(mem / "identity" / "b.md", "type: identity")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem):
            result = check_memory_quality()
        assert result["confirmed_pct"] == 50.0

    def test_tagged_pct_calculated(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_quality
        from unittest.mock import patch
        mem = tmp_path / "memory"
        self._write_md(mem / "feedback" / "tagged.md", "type: feedback\ntags:\n  - python")
        self._write_md(mem / "reference" / "untagged.md", "type: reference")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem):
            result = check_memory_quality()
        assert result["tagged_pct"] == 50.0

    def test_confidence_distribution(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_quality
        from unittest.mock import patch
        mem = tmp_path / "memory"
        self._write_md(mem / "feedback" / "h.md", "type: feedback\nconfidence: high")
        self._write_md(mem / "feedback" / "l.md", "type: feedback\nconfidence: low")
        self._write_md(mem / "reference" / "n.md", "type: reference")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem):
            result = check_memory_quality()
        dist = result["confidence_dist"]
        assert dist["high"] == 1
        assert dist["low"] == 1
        assert dist["none"] == 1

    def test_skips_archive_dir(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_quality
        from unittest.mock import patch
        mem = tmp_path / "memory"
        self._write_md(mem / "feedback" / "real.md", "type: feedback")
        self._write_md(mem / "archive" / "old.md", "type: reference")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem):
            result = check_memory_quality()
        assert result["total_files"] == 1  # archive not counted

    def test_untagged_files_count(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_quality
        from unittest.mock import patch
        mem = tmp_path / "memory"
        self._write_md(mem / "feedback" / "tagged.md", "type: feedback\ntags: [feedback]")
        self._write_md(mem / "feedback" / "untagged.md", "type: feedback")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem):
            result = check_memory_quality()
        assert "untagged_files" in result
        assert result["untagged_files"] == 1


# ---------------------------------------------------------------------------
# Memory trends (record_trends_snapshot / read_trends / _sparkline)
# ---------------------------------------------------------------------------

class TestMemoryTrends:
    """Tests for daily memory file-count trend tracking."""

    def _mk_memory(self, base: Path) -> Path:
        """Create a minimal .memory/ tree under base for scan tests."""
        mem = base / ".memory"
        for sub in ("identity", "feedback", "reference"):
            (mem / sub).mkdir(parents=True, exist_ok=True)
        (mem / "identity" / "alex.md").write_text(
            "---\nid: alex\ntitle: Alex\ntype: identity\nlast_confirmed: 2026-05-01\ntags: [identity]\n---\nAlex is a support engineer.\n",
            encoding="utf-8",
        )
        (mem / "feedback" / "editor.md").write_text(
            "---\nid: editor\ntitle: Editor\ntype: feedback\n---\nVS Code.\n",
            encoding="utf-8",
        )
        return mem

    def test_record_creates_trends_file(self, tmp_path):
        from atlas_plus.doctor.cli import record_trends_snapshot
        from unittest.mock import patch
        mem = self._mk_memory(tmp_path)
        trends_file = tmp_path / "trends.jsonl"
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem), \
             patch("atlas_plus.doctor.cli.TRENDS_FILE", trends_file), \
             patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            snap = record_trends_snapshot()
        assert trends_file.exists()
        assert snap["total_files"] == 2
        assert "ts" in snap
        assert "confirmed_pct" in snap
        assert "tagged_pct" in snap

    def test_record_appends_and_prunes(self, tmp_path):
        """Snapshot file is pruned to _TRENDS_MAX_LINES after many writes."""
        import json as _json
        from atlas_plus.doctor.cli import record_trends_snapshot, _TRENDS_MAX_LINES
        from unittest.mock import patch
        mem = self._mk_memory(tmp_path)
        trends_file = tmp_path / "trends.jsonl"
        # Pre-populate with max_lines entries
        pre = [_json.dumps({"ts": f"2026-01-{i:02d}T00:00:00Z", "total_files": i,
                             "confirmed_pct": 0.0, "tagged_pct": 0.0})
               for i in range(1, _TRENDS_MAX_LINES + 1)]
        trends_file.write_text("\n".join(pre) + "\n", encoding="utf-8")
        # One more write should push the oldest out
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem), \
             patch("atlas_plus.doctor.cli.TRENDS_FILE", trends_file), \
             patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            record_trends_snapshot()
        lines = [l for l in trends_file.read_text().splitlines() if l.strip()]
        assert len(lines) == _TRENDS_MAX_LINES

    def test_read_trends_returns_empty_when_no_file(self, tmp_path):
        from atlas_plus.doctor.cli import read_trends
        from unittest.mock import patch
        trends_file = tmp_path / "nonexistent.jsonl"
        with patch("atlas_plus.doctor.cli.TRENDS_FILE", trends_file):
            result = read_trends()
        assert result == []

    def test_read_trends_returns_last_n_entries(self, tmp_path):
        import json as _json
        from atlas_plus.doctor.cli import read_trends
        from unittest.mock import patch
        trends_file = tmp_path / "trends.jsonl"
        entries = [_json.dumps({"ts": f"2026-05-{i:02d}T04:05:00Z",
                                "total_files": 100 + i,
                                "confirmed_pct": float(i * 5),
                                "tagged_pct": float(i * 3)})
                   for i in range(1, 15)]
        trends_file.write_text("\n".join(entries) + "\n", encoding="utf-8")
        with patch("atlas_plus.doctor.cli.TRENDS_FILE", trends_file):
            result = read_trends(days=7)
        assert len(result) == 7
        assert result[0]["total_files"] == 108  # 14th entry back 7 = entry 8
        assert result[-1]["total_files"] == 114

    def test_sparkline_single_value(self):
        from atlas_plus.doctor.cli import _sparkline
        spark = _sparkline([42.0])
        assert len(spark) == 1

    def test_sparkline_flat_series(self):
        from atlas_plus.doctor.cli import _sparkline
        spark = _sparkline([10.0, 10.0, 10.0])
        assert len(spark) == 3
        # All equal -- should all be the same midpoint block character
        assert len(set(spark)) == 1

    def test_sparkline_rising_series(self):
        from atlas_plus.doctor.cli import _sparkline
        spark = _sparkline([1.0, 5.0, 10.0, 15.0])
        assert len(spark) == 4
        # Last character should be a higher block than first
        blocks = " ▁▂▃▄▅▆▇█"
        assert blocks.index(spark[-1]) >= blocks.index(spark[0])

    def test_render_text_includes_trend_sparkline(self, tmp_path):
        """render_text() shows 7d trend when memory_trends has >=2 entries."""
        from atlas_plus.doctor.cli import render_text
        data = {
            "memory_quality": {
                "total_files": 120, "confirmed_pct": 45.0, "tagged_pct": 30.0,
                "avg_age_days": 20.0, "stale_confirmed": 0, "anti_flagged": 0,
                "confidence_dist": {"high": 50, "medium": 40, "low": 30, "none": 0},
            },
            "memory_trends": [
                {"ts": "2026-05-13T04:05:00Z", "total_files": 100, "confirmed_pct": 40.0, "tagged_pct": 28.0},
                {"ts": "2026-05-14T04:05:00Z", "total_files": 105, "confirmed_pct": 41.0, "tagged_pct": 29.0},
                {"ts": "2026-05-15T04:05:00Z", "total_files": 110, "confirmed_pct": 42.0, "tagged_pct": 29.5},
                {"ts": "2026-05-16T04:05:00Z", "total_files": 112, "confirmed_pct": 43.0, "tagged_pct": 30.0},
                {"ts": "2026-05-17T04:05:00Z", "total_files": 115, "confirmed_pct": 44.0, "tagged_pct": 30.0},
                {"ts": "2026-05-18T04:05:00Z", "total_files": 118, "confirmed_pct": 44.5, "tagged_pct": 30.0},
                {"ts": "2026-05-19T04:05:00Z", "total_files": 120, "confirmed_pct": 45.0, "tagged_pct": 30.0},
            ],
            "services": [], "brain_diagnostic": {}, "routing": {}, "worker": {},
            "worker_detail": {}, "autonomous": {}, "disk": {}, "temporal": {},
            "tickets": {}, "nightly": {}, "backup": {}, "db_backup": {},
            "mcp_stats": {}, "memory_gaps": {}, "decisions": {}, "convergence": {},
            "rederivable": {}, "pending_facts": {}, "sla_breaches": {}, "fsdiff_count": 0,
            "ts": "2026-05-20T04:06:00Z",
        }
        output = render_text(data)
        assert "7d trend" in output
        assert "+20" in output  # +20 files over the period


# ---------------------------------------------------------------------------
# check_research_quality
# ---------------------------------------------------------------------------

class TestCheckResearchQuality:
    def _write_report(self, d: Path, name: str, score: float) -> Path:
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        p.write_text(
            f"---\ntitle: {name}\nquality_score: {score}\n---\n\n# Report\n",
            encoding="utf-8",
        )
        return p

    def test_returns_empty_when_no_reports_dir(self, tmp_path):
        from atlas_plus.doctor.cli import check_research_quality
        from unittest.mock import patch
        with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path / "nonexistent"):
            result = check_research_quality()
        assert result == {}

    def test_returns_zero_scored_when_no_quality_score(self, tmp_path):
        from atlas_plus.doctor.cli import check_research_quality
        from unittest.mock import patch
        rdir = tmp_path / "reports"
        (rdir / "r.md").parent.mkdir(parents=True, exist_ok=True)
        (rdir / "r.md").write_text("---\ntitle: T\n---\nBody.\n", encoding="utf-8")
        with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            result = check_research_quality()
        assert result["scored_reports"] == 0
        assert result["total_reports"] == 1

    def test_computes_avg_score(self, tmp_path):
        from atlas_plus.doctor.cli import check_research_quality
        from unittest.mock import patch
        rdir = tmp_path / "reports"
        self._write_report(rdir, "a.md", 0.8)
        self._write_report(rdir, "b.md", 0.4)
        with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            result = check_research_quality()
        assert result["scored_reports"] == 2
        assert abs(result["avg_score"] - 0.6) < 0.01

    def test_counts_low_quality(self, tmp_path):
        from atlas_plus.doctor.cli import check_research_quality
        from unittest.mock import patch
        rdir = tmp_path / "reports"
        self._write_report(rdir, "good.md", 0.8)
        self._write_report(rdir, "low.md", 0.3)
        with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            result = check_research_quality()
        assert result["low_quality_count"] == 1
        assert result["min_score_file"] == "low.md"

    def test_render_text_shows_research_quality_panel(self):
        from atlas_plus.doctor.cli import render_text
        data = {
            "research_quality": {
                "total_reports": 10, "scored_reports": 8,
                "avg_score": 0.72, "low_quality_count": 2,
                "min_score_file": "old-report.md",
            },
            "services": [], "brain_diagnostic": {}, "routing": {}, "worker": {},
            "worker_detail": {}, "autonomous": {}, "disk": {}, "temporal": {},
            "tickets": {}, "nightly": {}, "backup": {}, "db_backup": {},
            "mcp_stats": {}, "memory_gaps": {}, "decisions": {}, "convergence": {},
            "rederivable": {}, "pending_facts": {}, "sla_breaches": {}, "fsdiff_count": 0,
            "memory_quality": {}, "memory_trends": [], "ts": "2026-05-20T00:00:00Z",
        }
        output = render_text(data)
        assert "Research Quality" in output
        assert "0.72" in output
        assert "old-report.md" in output


# ---------------------------------------------------------------------------
# check_memory_health_overview
# ---------------------------------------------------------------------------

class TestCheckMemoryHealthOverview:
    def _write_md(self, path, fm: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        from datetime import date
        today = date.today().isoformat()
        path.write_text(f"---\n{fm}\n---\n\nContent.\n", encoding="utf-8")

    def test_returns_expected_keys(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_health_overview
        from unittest.mock import patch
        mem = tmp_path / "memory"
        self._write_md(mem / "id" / "alex.md", "title: Alex")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem):
            result = check_memory_health_overview()
        assert "total_files" in result
        assert "avg_score" in result
        assert "score_0" in result
        assert "score_100" in result

    def test_completely_unadorned_file_counts_score_0(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_health_overview
        from unittest.mock import patch
        mem = tmp_path / "memory"
        self._write_md(mem / "feedback" / "bare.md", "title: Bare")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem):
            result = check_memory_health_overview()
        assert result["score_0"] >= 1


# ---------------------------------------------------------------------------
# check_dispatcher
# ---------------------------------------------------------------------------

class TestCheckDispatcher:
    def test_returns_enabled_false_when_env_off(self, monkeypatch):
        from atlas_plus.doctor.cli import check_dispatcher
        monkeypatch.delenv("ATLAS_TICKET_DISPATCH", raising=False)
        result = check_dispatcher()
        assert "enabled" in result
        assert result["enabled"] is False

    def test_returns_enabled_true_when_env_on(self, monkeypatch):
        from atlas_plus.doctor.cli import check_dispatcher
        monkeypatch.setenv("ATLAS_TICKET_DISPATCH", "on")
        monkeypatch.setenv("ATLAS_AUTONOMOUS", "on")
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "atlas_plus.tickets.store.list_tickets", return_value=[]
        ):
            result = check_dispatcher()
        assert result["enabled"] is True

    def test_returns_in_flight_count(self, monkeypatch):
        from atlas_plus.doctor.cli import check_dispatcher
        from unittest.mock import MagicMock, patch
        t1 = MagicMock()
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[t1, t1]):
            result = check_dispatcher()
        assert result["in_flight"] == 2

    def test_returns_last_run_at_from_stats(self, monkeypatch):
        from atlas_plus.doctor.cli import check_dispatcher
        from unittest.mock import patch
        import atlas_plus.brain.dispatcher as disp_mod
        with patch.object(disp_mod, "_DISPATCH_STATS", {
            "last_run_at": "2026-05-20T04:00:00+00:00",
            "last_dispatched_count": 3,
            "total_dispatched": 7,
        }):
            with patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
                result = check_dispatcher()
        assert result["last_run_at"] == "2026-05-20T04:00:00+00:00"
        assert result["total_dispatched"] == 7

    def test_render_text_includes_dispatcher_panel(self):
        from atlas_plus.doctor.cli import render_text
        data = {"dispatcher": {
            "enabled": True,
            "last_run_at": None,
            "last_dispatched_count": 0,
            "total_dispatched": 0,
            "in_flight": 2,
        }}
        out = render_text(data)
        assert "dispatcher" in out.lower() or "Ticket" in out
        assert "2" in out  # in_flight count appears


# ---------------------------------------------------------------------------
# check_orphans
# ---------------------------------------------------------------------------

class TestCheckOrphans:
    def _write_md(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_returns_expected_keys(self, tmp_path):
        from atlas_plus.doctor.cli import check_orphans
        from unittest.mock import patch
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            result = check_orphans()
        assert "candidates" in result
        assert isinstance(result["candidates"], int)

    def test_returns_zero_when_no_old_files(self, tmp_path):
        from atlas_plus.doctor.cli import check_orphans
        from unittest.mock import patch
        from datetime import date
        today = date.today().isoformat()
        self._write_md(
            tmp_path / "feedback" / "fresh.md",
            f"---\ntitle: Fresh\nupdated: {today}\ntags: [python]\nconfidence: high\nlast_confirmed: {today}\n---\nContent.\n",
        )
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            result = check_orphans()
        assert result["candidates"] == 0

    def test_render_text_includes_orphan_panel_when_candidates_exist(self):
        from atlas_plus.doctor.cli import render_text
        data = {"orphans": {
            "candidates": 3,
            "top": [
                {"path": "observations/old.md", "age_days": 400, "score": 0.0, "inbound_refs": 0},
            ],
        }}
        out = render_text(data)
        assert "orphan" in out.lower() or "Orphan" in out
        assert "observations/old.md" in out or "3" in out


class TestCheckExhaustedTasks:
    def test_returns_zero_when_no_exhausted_tasks(self, tmp_path):
        from atlas_plus.doctor.cli import check_exhausted_tasks
        from unittest.mock import patch
        from atlas_plus.brain import tasks as ts
        ts.create_task("fresh task", db_path=tmp_path / "s.sqlite")
        with patch("atlas_plus.brain.tasks._DB_PATH", tmp_path / "s.sqlite"):
            result = check_exhausted_tasks()
        assert result["count"] == 0
        assert result["tasks"] == []

    def test_detects_failed_exhausted_task(self, tmp_path):
        from atlas_plus.doctor.cli import check_exhausted_tasks
        from unittest.mock import patch
        from atlas_plus.brain import tasks as ts
        db = tmp_path / "s.sqlite"
        task = ts.create_task("fix bug", agent="brain", max_attempts=1, db_path=db)
        ts.mark_running(task.task_id, db_path=db)
        ts.mark_failed(task.task_id, "executor error", db_path=db)
        # Force attempts=1 so the task reads as exhausted (attempts >= max_attempts)
        import sqlite3
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE brain_tasks SET attempts=1 WHERE task_id=?", (task.task_id,))
        conn.commit()
        conn.close()
        # Patch the function to use our test db (default arg bound at definition time)
        real_fn = ts.list_exhausted_tasks
        with patch(
            "atlas_plus.brain.tasks.list_exhausted_tasks",
            lambda limit=10, db_path=db: real_fn(limit=limit, db_path=db),
        ):
            result = check_exhausted_tasks()
        assert result["count"] == 1
        assert result["tasks"][0]["agent"] == "brain"

    def test_render_text_shows_exhausted_panel(self):
        from atlas_plus.doctor.cli import render_text
        data = {"exhausted_tasks": {
            "count": 2,
            "tasks": [
                {"task_id": "task-abc123", "agent": "brain", "instruction": "do X",
                 "status": "failed", "finished_at": "2026-05-20T01:00:00"},
            ],
        }}
        out = render_text(data)
        assert "Exhausted" in out or "exhausted" in out.lower()
        assert "task-abc123" in out

    def test_render_text_hides_panel_when_count_zero(self):
        from atlas_plus.doctor.cli import render_text
        data = {"exhausted_tasks": {"count": 0, "tasks": []}}
        out = render_text(data)
        assert "Exhausted Tasks" not in out


# ---------------------------------------------------------------------------
# check_untagged_by_scope
# ---------------------------------------------------------------------------

class TestCheckUntaggedByScope:
    def _write_md(self, path, tags=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        if tags is None:
            path.write_text("# No frontmatter\nbody\n", encoding="utf-8")
        elif not tags:
            path.write_text("---\ntitle: test\ntags: []\n---\nbody\n", encoding="utf-8")
        else:
            path.write_text(f"---\ntitle: test\ntags: {tags}\n---\nbody\n", encoding="utf-8")

    def test_counts_by_scope(self, tmp_path, monkeypatch):
        from atlas_plus.doctor.cli import check_untagged_by_scope
        mem = tmp_path / ".memory"
        self._write_md(mem / "feedback" / "a.md")            # no fm = untagged
        self._write_md(mem / "feedback" / "b.md", tags=[])   # empty tags = untagged
        self._write_md(mem / "feedback" / "c.md", tags=["style"])  # tagged = skip
        self._write_md(mem / "projects" / "x.md")            # no fm = untagged
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
        counts = check_untagged_by_scope()
        assert counts.get("feedback") == 2
        assert counts.get("projects") == 1

    def test_returns_empty_when_no_memory_dir(self, tmp_path, monkeypatch):
        from atlas_plus.doctor.cli import check_untagged_by_scope
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "no-memory"))
        counts = check_untagged_by_scope()
        assert counts == {}

    def test_tagged_files_not_counted(self, tmp_path, monkeypatch):
        from atlas_plus.doctor.cli import check_untagged_by_scope
        mem = tmp_path / ".memory"
        self._write_md(mem / "reference" / "a.md", tags=["examplecorp"])
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
        counts = check_untagged_by_scope()
        assert counts.get("reference", 0) == 0

    def test_hidden_dirs_skipped(self, tmp_path, monkeypatch):
        from atlas_plus.doctor.cli import check_untagged_by_scope
        mem = tmp_path / ".memory"
        self._write_md(mem / ".git" / "config.md")
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
        counts = check_untagged_by_scope()
        assert ".git" not in counts

    def test_render_text_shows_top_scopes_when_untagged_high(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        from atlas_plus.doctor.cli import render_text, check_untagged_by_scope
        data = {
            "memory_quality": {
                "total_files": 100, "confirmed_pct": 50, "tagged_pct": 30,
                "untagged_files": 25, "avg_age_days": 10, "stale_confirmed": 0,
                "anti_flagged": 0, "confidence_dist": {}
            }
        }
        with patch("atlas_plus.doctor.cli.check_untagged_by_scope",
                   return_value={"feedback": 15, "projects": 8, "reference": 2}):
            out = render_text(data)
        assert "feedback" in out
        assert "Top untagged" in out or "untagged scopes" in out.lower()


# ---------------------------------------------------------------------------
# check_services
# ---------------------------------------------------------------------------

class TestCheckServices:
    def test_returns_three_services_in_order(self):
        from atlas_plus.doctor.cli import check_services
        with patch("atlas_plus.doctor.cli._http_get", return_value=(200, {})):
            result = check_services()
        assert len(result) == 5
        assert result[0]["name"] == "Brain"
        assert result[1]["name"] == "MCP"
        assert [r["name"] for r in result[2:]] == ["Web", "Sync", "Ollama"]

    def test_returns_up_on_200(self):
        from atlas_plus.doctor.cli import check_services
        with patch("atlas_plus.doctor.cli._http_get", return_value=(200, {})):
            result = check_services()
        assert all(s["status"] == "up" for s in result)

    def test_returns_down_on_connection_error(self):
        from atlas_plus.doctor.cli import check_services
        with patch("atlas_plus.doctor.cli._http_get", return_value=(0, {"error": "refused"})):
            result = check_services()
        assert all(s["status"] == "down" for s in result)
        assert all("refused" in s["detail"] for s in result)

    def test_returns_degraded_on_non_200(self):
        from atlas_plus.doctor.cli import check_services
        with patch("atlas_plus.doctor.cli._http_get", return_value=(503, {})):
            result = check_services()
        assert all(s["status"] == "degraded" for s in result)
        assert all("503" in s["detail"] for s in result)

    def test_brain_degraded_body_surfaces_as_degraded(self):
        """The Brain answers HTTP 200 while alive but reports a dead model
        backend in its body (run 51); doctor must not call that 'up'."""
        from atlas_plus.doctor.cli import check_services
        degraded = {"status": "degraded", "backend": "unreachable"}
        with patch("atlas_plus.doctor.cli._http_get", return_value=(200, degraded)):
            result = check_services()
        brain = next(s for s in result if s["name"] == "Brain")
        assert brain["status"] == "degraded"
        assert "backend unreachable" in brain["detail"]
        # Only the Brain interprets that body; the others stay plain 200=up.
        assert next(s for s in result if s["name"] == "MCP")["status"] == "up"

    def test_ollama_shows_model_count(self):
        from atlas_plus.doctor.cli import check_services
        ollama_body = {"models": [{"name": "qwen3:8b"}, {"name": "qwen3:1.7b"}]}
        with patch("atlas_plus.doctor.cli._http_get", return_value=(200, ollama_body)):
            result = check_services()
        ollama = next(s for s in result if s["name"] == "Ollama")
        assert "2 model" in ollama["detail"]


# ---------------------------------------------------------------------------
# check_nightly
# ---------------------------------------------------------------------------

class TestCheckNightly:
    def _write_db(self, db_path: "Path") -> None:
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE brain_tasks("
            "session_id TEXT, status TEXT, finished_at TEXT)"
        )
        conn.commit()
        conn.close()

    def test_empty_when_no_db(self, tmp_path):
        from atlas_plus.doctor.cli import check_nightly
        with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path / "nonexistent"):
            result = check_nightly()
        assert result == {}

    def test_returns_scheduler_jobs(self, tmp_path):
        from atlas_plus.doctor.cli import check_nightly
        import sqlite3
        db = tmp_path / "state.sqlite"
        self._write_db(db)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO brain_tasks VALUES('scheduler-temporal-build','done','2026-05-21T03:05:00')"
        )
        conn.execute(
            "INSERT INTO brain_tasks VALUES('scheduler-curation','done','2026-05-21T03:15:00')"
        )
        conn.commit()
        conn.close()
        with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            result = check_nightly()
        assert "temporal-build" in result
        assert result["temporal-build"]["status"] == "done"
        assert "curation" in result

    def test_returns_latest_run_per_job(self, tmp_path):
        """When multiple rows exist for the same job, only the latest is returned."""
        from atlas_plus.doctor.cli import check_nightly
        import sqlite3
        db = tmp_path / "state.sqlite"
        self._write_db(db)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO brain_tasks VALUES('scheduler-temporal-build','done','2026-05-20T03:05:00')"
        )
        conn.execute(
            "INSERT INTO brain_tasks VALUES('scheduler-temporal-build','done','2026-05-21T03:05:00')"
        )
        conn.commit()
        conn.close()
        with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            result = check_nightly()
        # Should have exactly one entry for temporal-build (last run wins)
        assert "temporal-build" in result
        assert result["temporal-build"]["last"] == "2026-05-21T03:05:00"


# ---------------------------------------------------------------------------
# check_temporal
# ---------------------------------------------------------------------------

class TestCheckTemporal:
    @pytest.fixture(autouse=True)
    def _graph_dir_exists(self, tmp_path, monkeypatch):
        # The check returns "not built" (and creates nothing) when the graph dir is absent (sweep 2026-11-11).
        from atlas_plus.temporal_graph import file_provider as fp
        monkeypatch.setattr(fp, "_DEFAULT_ROOT", tmp_path)

    def test_returns_entity_and_fact_counts(self, tmp_path):
        from atlas_plus.doctor.cli import check_temporal
        from unittest.mock import MagicMock

        mock_provider = MagicMock()
        mock_provider.stats.return_value = {"entities": 42, "facts_active": 120}
        mock_provider._manifest_file = tmp_path / "manifest.json"

        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   return_value=mock_provider):
            result = check_temporal()
        assert result["entities"] == 42
        assert result["facts_active"] == 120

    def test_returns_empty_on_error(self):
        from atlas_plus.doctor.cli import check_temporal
        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   side_effect=RuntimeError("graph not found")):
            result = check_temporal()
        assert result == {}

    def test_stale_when_no_manifest(self, tmp_path):
        from atlas_plus.doctor.cli import check_temporal
        from unittest.mock import MagicMock

        mock_provider = MagicMock()
        mock_provider.stats.return_value = {"entities": 5, "facts_active": 10}
        mock_provider._manifest_file = tmp_path / "nonexistent_manifest.json"

        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   return_value=mock_provider):
            result = check_temporal()
        assert result.get("stale") is True
        assert result.get("last_built") is None

    def test_fresh_when_recently_built(self, tmp_path):
        from atlas_plus.doctor.cli import check_temporal
        from unittest.mock import MagicMock
        import json
        from datetime import datetime, timezone

        recent_ts = datetime.now(timezone.utc).isoformat()
        manifest_data = {"last_built": recent_ts, "files": {}}
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest_data))

        mock_provider = MagicMock()
        mock_provider.stats.return_value = {"entities": 5, "facts_active": 10}
        mock_provider._manifest_file = manifest_path

        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   return_value=mock_provider):
            result = check_temporal()
        assert result.get("stale") is False
        assert result.get("last_built") is not None
        assert result.get("age_days") == 0

    def test_stale_when_old_build(self, tmp_path):
        from atlas_plus.doctor.cli import check_temporal
        from unittest.mock import MagicMock
        import json
        from datetime import datetime, timezone, timedelta

        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        manifest_data = {"last_built": old_ts, "files": {}}
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest_data))

        mock_provider = MagicMock()
        mock_provider.stats.return_value = {"entities": 5, "facts_active": 10}
        mock_provider._manifest_file = manifest_path

        with patch("atlas_plus.temporal_graph.file_provider.FileTemporalProvider",
                   return_value=mock_provider):
            result = check_temporal()
        assert result.get("stale") is True
        assert result.get("age_days") >= 10


# ---------------------------------------------------------------------------
# check_research_gaps
# ---------------------------------------------------------------------------

class TestCheckResearchGaps:
    def _make_ticket(self, title: str, status: str = "todo", priority: str = "normal"):
        from unittest.mock import MagicMock
        t = MagicMock()
        t.id = "ATL-001"
        t.title = title
        t.status = status
        t.priority = priority
        return t

    def test_returns_zero_when_no_gap_tickets(self):
        from atlas_plus.doctor.cli import check_research_gaps
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            result = check_research_gaps()
        assert result["count"] == 0
        assert result["top"] == []

    def test_counts_research_gap_tickets(self):
        from atlas_plus.doctor.cli import check_research_gaps
        tickets = [
            self._make_ticket("Research gap: SIP registration"),
            self._make_ticket("Research gap: DTMF handling"),
            self._make_ticket("Fix login bug"),  # not a gap
        ]
        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets):
            result = check_research_gaps()
        assert result["count"] == 2
        assert len(result["top"]) == 2

    def test_filters_to_todo_status(self):
        from atlas_plus.doctor.cli import check_research_gaps
        # list_tickets already filters to status=todo -- check we pass status arg
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[]) as mock_list:
            check_research_gaps()
        mock_list.assert_called_once_with(status="todo")

    def test_returns_error_key_on_exception(self):
        from atlas_plus.doctor.cli import check_research_gaps
        with patch("atlas_plus.tickets.store.list_tickets",
                   side_effect=RuntimeError("tickets dir missing")):
            result = check_research_gaps()
        assert result["count"] == 0
        assert "error" in result

    def test_render_text_shows_gap_panel_when_present(self):
        from atlas_plus.doctor.cli import render_text
        data = {
            "research_gaps": {
                "count": 2,
                "top": [
                    {"id": "ATL-042", "title": "Research gap: SIP REGISTER flow", "priority": "normal"},
                    {"id": "ATL-043", "title": "Research gap: DTMF in VoIP", "priority": "normal"},
                ],
            },
        }
        out = render_text(data)
        assert "Research Gaps" in out
        assert "ATL-042" in out
        assert "2 unresolved" in out


# ---------------------------------------------------------------------------
# check_routing
# ---------------------------------------------------------------------------

class TestCheckRouting:
    def test_returns_empty_on_brain_unreachable(self):
        from atlas_plus.doctor.cli import check_routing
        with patch("atlas_plus.doctor.cli._http_get", return_value=(503, {})):
            result = check_routing()
        assert result == {}

    def test_returns_routing_stats_on_success(self):
        from atlas_plus.doctor.cli import check_routing
        fake = {"smart_routing": False, "local_only": False, "cache_size": 0}
        with patch("atlas_plus.doctor.cli._http_get", return_value=(200, fake)):
            result = check_routing()
        assert result["smart_routing"] is False


# ---------------------------------------------------------------------------
# check_tickets
# ---------------------------------------------------------------------------

class TestCheckTickets:
    def test_returns_ticket_counts(self):
        from atlas_plus.doctor.cli import check_tickets
        from atlas_plus.tickets.store import Ticket
        from pathlib import Path
        tickets = [
            Ticket(id="ATL-001", title="A", project="p", status="todo", path=Path("a.md")),
            Ticket(id="ATL-002", title="B", project="p", status="done", path=Path("b.md")),
        ]
        with patch("atlas_plus.tickets.store.list_tickets", return_value=tickets):
            result = check_tickets()
        assert result["total"] == 2
        assert "by_status" in result
        assert result["by_status"]["todo"] == 1

    def test_returns_empty_on_exception(self):
        from atlas_plus.doctor.cli import check_tickets
        with patch("atlas_plus.tickets.store.list_tickets",
                   side_effect=Exception("db error")):
            result = check_tickets()
        assert result == {}

    def test_flags_high_reopen_tickets(self):
        from atlas_plus.doctor.cli import check_tickets
        from atlas_plus.tickets.store import Ticket
        from pathlib import Path
        t = Ticket(id="ATL-003", title="Problematic", project="p",
                   status="todo", path=Path("c.md"))
        t.reopen_count = 5
        with patch("atlas_plus.tickets.store.list_tickets", return_value=[t]):
            result = check_tickets()
        assert len(result["high_reopen"]) == 1
        assert result["high_reopen"][0]["id"] == "ATL-003"


# ---------------------------------------------------------------------------
# check_db_backup
# ---------------------------------------------------------------------------

class TestCheckDbBackup:
    def test_returns_never_when_no_snapshots(self):
        from atlas_plus.doctor.cli import check_db_backup
        with patch("atlas_plus.db_backup.core.last_backup_info", return_value=None), \
             patch("atlas_plus.db_backup.core.list_backups", return_value=[]):
            result = check_db_backup()
        assert result.get("last_ts") == "never" or "never" in str(result)

    def test_returns_snapshot_info(self):
        from atlas_plus.doctor.cli import check_db_backup
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        info = {
            "ts_str": now.strftime("%Y%m%d_%H%M%S"),
            "size_kb": 256,
            "modified": now.isoformat(),
            "path": "/tmp/state-backup.sqlite",
        }
        with patch("atlas_plus.db_backup.core.last_backup_info", return_value=info), \
             patch("atlas_plus.db_backup.core.list_backups", return_value=[info]):
            result = check_db_backup()
        assert "last_ts" in result or "size_kb" in result

    def test_returns_empty_on_exception(self):
        from atlas_plus.doctor.cli import check_db_backup
        with patch("atlas_plus.db_backup.core.last_backup_info",
                   side_effect=Exception("import error")):
            result = check_db_backup()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# check_mcp_stats
# ---------------------------------------------------------------------------

class TestCheckMcpStats:
    def test_returns_empty_when_no_log(self):
        from atlas_plus.doctor.cli import check_mcp_stats
        with patch("atlas_plus.mcp.memory_server.tools.search_log.load_log",
                   return_value=[]):
            result = check_mcp_stats()
        assert result == {}

    def test_returns_search_metrics(self):
        from atlas_plus.doctor.cli import check_mcp_stats
        records = [{"miss": True}, {"miss": False}, {"miss": True}]
        with patch("atlas_plus.mcp.memory_server.tools.search_log.load_log",
                   return_value=records):
            result = check_mcp_stats()
        assert result["total_searches_7d"] == 3
        assert result["miss_count_7d"] == 2
        assert result["miss_rate_pct"] > 0


# ---------------------------------------------------------------------------
# check_memory_gaps
# ---------------------------------------------------------------------------

class TestCheckMemoryGaps:
    def test_returns_empty_when_no_gaps(self):
        from atlas_plus.doctor.cli import check_memory_gaps
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   return_value=[]):
            result = check_memory_gaps()
        assert result.get("unique_miss_queries") == 0

    def test_returns_gap_queries(self):
        from atlas_plus.doctor.cli import check_memory_gaps
        gaps = [{"query": "SIP REGISTER", "miss_count": 5, "total": 6},
                {"query": "RTP media", "miss_count": 3, "total": 4}]
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   return_value=gaps):
            result = check_memory_gaps()
        assert result["unique_miss_queries"] == 2
        assert len(result["top"]) > 0


# ---------------------------------------------------------------------------
# check_ref_graph
# ---------------------------------------------------------------------------

class TestCheckRefGraph:
    def test_returns_hub_info(self):
        from atlas_plus.doctor.cli import check_ref_graph
        fake_graph = {"identity/alex.md": {"identity/role.md", "projects/atlas.md"},
                      "feedback/testing.md": {"projects/atlas.md"}}
        with patch("atlas_plus.curation.ref_graph.build_reference_graph",
                   return_value=fake_graph), \
             patch("atlas_plus.curation.ref_graph.find_hub_files",
                   return_value=[("identity/alex.md", 2)]):
            result = check_ref_graph()
        assert "hub_count" in result or "top" in result

    def test_returns_empty_on_exception(self):
        from atlas_plus.doctor.cli import check_ref_graph
        with patch("atlas_plus.curation.ref_graph.build_reference_graph",
                   side_effect=Exception("ref graph error")):
            result = check_ref_graph()
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# check_memory_health_overview
# ---------------------------------------------------------------------------

class TestCheckMemoryHealthOverview:
    def _make_results(self, scores):
        return [{"path": f"f{i}.md", "score": s} for i, s in enumerate(scores)]

    def test_returns_expected_keys(self):
        from atlas_plus.doctor.cli import check_memory_health_overview
        results = self._make_results([1.0, 0.75, 0.50, 0.25, 0.0])
        with patch("atlas_plus.curation.health.scan_memory_health", return_value=results):
            out = check_memory_health_overview()
        assert "total_files" in out
        assert "avg_score" in out
        assert out["total_files"] == 5

    def test_score_buckets_correct(self):
        from atlas_plus.doctor.cli import check_memory_health_overview
        results = self._make_results([1.0, 1.0, 0.75, 0.50, 0.25, 0.0])
        with patch("atlas_plus.curation.health.scan_memory_health", return_value=results):
            out = check_memory_health_overview()
        assert out["score_100"] == 2
        assert out["score_75"]  == 1
        assert out["score_50"]  == 1
        assert out["score_25"]  == 1
        assert out["score_0"]   == 1

    def test_avg_score_computed(self):
        from atlas_plus.doctor.cli import check_memory_health_overview
        results = self._make_results([1.0, 0.0])  # avg = 0.5
        with patch("atlas_plus.curation.health.scan_memory_health", return_value=results):
            out = check_memory_health_overview()
        assert abs(out["avg_score"] - 0.5) < 0.01

    def test_empty_returns_zero_totals(self):
        from atlas_plus.doctor.cli import check_memory_health_overview
        with patch("atlas_plus.curation.health.scan_memory_health", return_value=[]):
            out = check_memory_health_overview()
        assert out["total_files"] == 0
        assert out["avg_score"] == 0.0

    def test_returns_error_key_on_exception(self):
        from atlas_plus.doctor.cli import check_memory_health_overview
        with patch("atlas_plus.curation.health.scan_memory_health",
                   side_effect=RuntimeError("health scan failed")):
            out = check_memory_health_overview()
        assert "error" in out
        assert out["total_files"] == 0


# ---------------------------------------------------------------------------
# check_research_quality
# ---------------------------------------------------------------------------

class TestCheckResearchQuality:
    def _write_report(self, path, title, quality_score=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        if quality_score is not None:
            text = f"---\ntitle: {title}\nquality_score: {quality_score}\n---\n\nbody\n"
        else:
            text = f"---\ntitle: {title}\n---\n\nbody\n"
        path.write_text(text, encoding="utf-8")

    def test_returns_empty_when_no_reports_dir(self, tmp_path):
        from atlas_plus.doctor.cli import check_research_quality
        with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            result = check_research_quality()
        assert result == {}

    def test_counts_total_reports(self, tmp_path):
        from atlas_plus.doctor.cli import check_research_quality
        reports = tmp_path / "reports"
        self._write_report(reports / "r1.md", "Report 1", 0.8)
        self._write_report(reports / "r2.md", "Report 2")
        with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            result = check_research_quality()
        assert result["total_reports"] == 2
        assert result["scored_reports"] == 1

    def test_avg_score_correct(self, tmp_path):
        from atlas_plus.doctor.cli import check_research_quality
        reports = tmp_path / "reports"
        self._write_report(reports / "r1.md", "High", 0.9)
        self._write_report(reports / "r2.md", "Low",  0.3)
        with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            result = check_research_quality()
        assert abs(result["avg_score"] - 0.6) < 0.01

    def test_low_quality_count(self, tmp_path):
        from atlas_plus.doctor.cli import check_research_quality
        reports = tmp_path / "reports"
        self._write_report(reports / "r1.md", "Low", 0.2)
        self._write_report(reports / "r2.md", "OK",  0.8)
        with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            result = check_research_quality()
        assert result["low_quality_count"] == 1

    def test_min_score_file_identified(self, tmp_path):
        from atlas_plus.doctor.cli import check_research_quality
        reports = tmp_path / "reports"
        self._write_report(reports / "good.md", "Good",  0.9)
        self._write_report(reports / "worst.md", "Worst", 0.1)
        with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            result = check_research_quality()
        assert result["min_score_file"] == "worst.md"


# ---------------------------------------------------------------------------
# check_exhausted_tasks
# ---------------------------------------------------------------------------

class TestCheckExhaustedTasks:
    @pytest.fixture(autouse=True)
    def _state_db_exists(self, tmp_path, monkeypatch):
        # The check returns early (and creates nothing) when state.sqlite is absent (sweep 2026-11-11).
        from atlas_plus.doctor import cli as doc
        (tmp_path / "state.sqlite").write_bytes(b"")
        monkeypatch.setattr(doc, "STATE_DIR", tmp_path)

    def test_returns_zero_when_no_exhausted_tasks(self):
        from atlas_plus.doctor.cli import check_exhausted_tasks
        with patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=[]):
            result = check_exhausted_tasks()
        assert result["count"] == 0
        assert result["tasks"] == []

    def test_returns_task_list(self):
        from atlas_plus.doctor.cli import check_exhausted_tasks
        task = MagicMock()
        task.task_id = "ex-001-abc"
        task.agent = "brain"
        task.instruction = "Do something"
        task.status = "failed"
        task.finished_at = "2026-05-21T00:00:00"
        with patch("atlas_plus.brain.tasks.list_exhausted_tasks", return_value=[task]):
            result = check_exhausted_tasks()
        assert result["count"] == 1
        assert result["tasks"][0]["agent"] == "brain"

    def test_returns_error_key_on_exception(self):
        from atlas_plus.doctor.cli import check_exhausted_tasks
        with patch("atlas_plus.brain.tasks.list_exhausted_tasks",
                   side_effect=RuntimeError("db error")):
            result = check_exhausted_tasks()
        assert "error" in result
        assert result["count"] == 0


# ---------------------------------------------------------------------------
# check_untagged_by_scope
# ---------------------------------------------------------------------------

class TestCheckUntaggedByScope:
    def test_returns_empty_when_memory_dir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "nonexistent"))
        from atlas_plus.doctor.cli import check_untagged_by_scope
        result = check_untagged_by_scope()
        assert result == {}

    def test_counts_untagged_files(self, tmp_path, monkeypatch):
        # Create a scope dir with one tagged and one untagged file
        scope = tmp_path / "feedback"
        scope.mkdir()
        (scope / "tagged.md").write_text("---\ntitle: T\ntags: [testing]\n---\nbody\n", encoding="utf-8")
        (scope / "untagged.md").write_text("---\ntitle: U\n---\nbody\n", encoding="utf-8")
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path))
        from atlas_plus.doctor.cli import check_untagged_by_scope
        result = check_untagged_by_scope()
        assert result.get("feedback", 0) == 1

    def test_skips_archive_directory(self, tmp_path, monkeypatch):
        archive = tmp_path / "archive"
        archive.mkdir()
        (archive / "old.md").write_text("---\ntitle: Old\n---\nbody\n", encoding="utf-8")
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path))
        from atlas_plus.doctor.cli import check_untagged_by_scope
        result = check_untagged_by_scope()
        assert "archive" not in result


# ---------------------------------------------------------------------------
# read_trends
# ---------------------------------------------------------------------------

class TestReadTrends:
    def test_returns_empty_when_no_file(self, tmp_path):
        from atlas_plus.doctor.cli import read_trends
        with patch("atlas_plus.doctor.cli.TRENDS_FILE", tmp_path / "nonexistent.jsonl"):
            result = read_trends()
        assert result == []

    def test_returns_last_n_days(self, tmp_path):
        import json as _json
        trends_file = tmp_path / "memory-trends.jsonl"
        records = [{"ts": f"2026-05-{i:02d}", "total_files": i * 10} for i in range(1, 6)]
        trends_file.write_text("\n".join(_json.dumps(r) for r in records), encoding="utf-8")
        from atlas_plus.doctor.cli import read_trends
        with patch("atlas_plus.doctor.cli.TRENDS_FILE", trends_file):
            result = read_trends(days=3)
        assert len(result) == 3
        assert result[-1]["ts"] == "2026-05-05"

    def test_ignores_malformed_lines(self, tmp_path):
        trends_file = tmp_path / "memory-trends.jsonl"
        trends_file.write_text('{"ts":"good"}\nnot-json\n{"ts":"also-good"}\n', encoding="utf-8")
        from atlas_plus.doctor.cli import read_trends
        with patch("atlas_plus.doctor.cli.TRENDS_FILE", trends_file):
            result = read_trends()
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _sparkline
# ---------------------------------------------------------------------------

class TestSparkline:
    def test_empty_returns_empty_string(self):
        from atlas_plus.doctor.cli import _sparkline
        assert _sparkline([]) == ""

    def test_single_value_returns_midpoint_block(self):
        from atlas_plus.doctor.cli import _sparkline
        result = _sparkline([5.0])
        assert len(result) == 1
        assert result != " "  # not a space

    def test_all_same_values_returns_midpoint_blocks(self):
        from atlas_plus.doctor.cli import _sparkline
        result = _sparkline([3.0, 3.0, 3.0])
        assert len(result) == 3
        # All same -> all midpoint -> all the same character
        assert len(set(result)) == 1

    def test_output_length_matches_input(self):
        from atlas_plus.doctor.cli import _sparkline
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = _sparkline(values)
        assert len(result) == 5


# ---------------------------------------------------------------------------
# record_trends_snapshot
# ---------------------------------------------------------------------------

class TestRecordTrendsSnapshot:
    def _write_md(self, path, confirmed=False, tagged=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        fm_lines = ["---", "title: Test"]
        if confirmed:
            fm_lines.append("last_confirmed: 2026-01-01")
        if tagged:
            fm_lines.append("tags: [testing]")
        fm_lines.append("---")
        path.write_text("\n".join(fm_lines) + "\n\nbody\n", encoding="utf-8")

    def test_returns_snapshot_with_expected_keys(self, tmp_path):
        from atlas_plus.doctor.cli import record_trends_snapshot
        mem = tmp_path / "memory"
        mem.mkdir()
        self._write_md(mem / "ref.md", confirmed=True, tagged=True)
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem), \
             patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path), \
             patch("atlas_plus.doctor.cli.TRENDS_FILE", tmp_path / "trends.jsonl"):
            snap = record_trends_snapshot()
        assert "ts" in snap
        assert "total_files" in snap
        assert "confirmed_pct" in snap
        assert "tagged_pct" in snap
        assert snap["total_files"] == 1

    def test_appends_to_existing_file(self, tmp_path):
        import json as _json
        from atlas_plus.doctor.cli import record_trends_snapshot
        mem = tmp_path / "memory"
        mem.mkdir()
        trends = tmp_path / "trends.jsonl"
        trends.write_text('{"ts":"old","total_files":0,"confirmed_pct":0.0,"tagged_pct":0.0}\n',
                          encoding="utf-8")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem), \
             patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path), \
             patch("atlas_plus.doctor.cli.TRENDS_FILE", trends):
            record_trends_snapshot()
        lines = trends.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2  # old + new

    def test_skips_archive_directory(self, tmp_path):
        from atlas_plus.doctor.cli import record_trends_snapshot
        mem = tmp_path / "memory"
        (mem / "archive").mkdir(parents=True)
        self._write_md(mem / "archive" / "old.md")
        self._write_md(mem / "ref.md")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem), \
             patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path), \
             patch("atlas_plus.doctor.cli.TRENDS_FILE", tmp_path / "t.jsonl"):
            snap = record_trends_snapshot()
        # Only ref.md should be counted, archive/old.md skipped
        assert snap["total_files"] == 1


# ---------------------------------------------------------------------------
# check_memory_quality
# ---------------------------------------------------------------------------

class TestCheckMemoryQuality:
    def _write_fm(self, path, **fields):
        """Write a .md file with YAML frontmatter."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fm_lines = ["---"]
        for k, v in fields.items():
            fm_lines.append(f"{k}: {v}")
        fm_lines.append("---")
        path.write_text("\n".join(fm_lines) + "\n\nbody\n", encoding="utf-8")

    def test_returns_zero_when_memory_root_missing(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_quality
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path / "nonexistent"):
            result = check_memory_quality()
        assert result["total_files"] == 0
        assert result["confirmed_pct"] == 0.0

    def test_counts_total_files(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_quality
        mem = tmp_path
        self._write_fm(mem / "ref.md", title="R")
        self._write_fm(mem / "ref2.md", title="R2")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem):
            result = check_memory_quality()
        assert result["total_files"] == 2

    def test_confirmed_pct_computed(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_quality
        mem = tmp_path
        self._write_fm(mem / "confirmed.md", title="C", last_confirmed="2026-05-01")
        self._write_fm(mem / "unconfirmed.md", title="U")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem):
            result = check_memory_quality()
        assert result["confirmed_pct"] == 50.0

    def test_tagged_pct_computed(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_quality
        mem = tmp_path
        self._write_fm(mem / "tagged.md", title="T", tags="[testing]")
        self._write_fm(mem / "untagged.md", title="U")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem):
            result = check_memory_quality()
        assert result["tagged_pct"] == 50.0

    def test_skips_archive_directory(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_quality
        mem = tmp_path
        self._write_fm(mem / "ref.md", title="Ref")
        (mem / "archive").mkdir()
        self._write_fm(mem / "archive" / "old.md", title="Old")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", mem):
            result = check_memory_quality()
        assert result["total_files"] == 1  # archive/old.md skipped


# ---------------------------------------------------------------------------
# check_tui_activity
# ---------------------------------------------------------------------------

class TestCheckTuiActivity:
    def _write_session(self, sessions_dir: Path, date_str: str, lines: list[str]) -> None:
        f = sessions_dir / f"{date_str}.md"
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_empty_when_no_sessions_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "nonexistent"))
        from atlas_plus.doctor.cli import check_tui_activity
        result = check_tui_activity()
        assert result == {}

    def test_detects_sip_dashboard_session(self, tmp_path, monkeypatch):
        mem = tmp_path / ".memory"
        sessions = mem / "sessions"
        sessions.mkdir(parents=True)
        import datetime
        today = datetime.date.today().isoformat()
        self._write_session(sessions, today, [
            "- 09:00 demo-tui: session started",
            "- 09:45 demo-tui: session ended",
        ])
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
        from atlas_plus.doctor import cli as doctor_cli
        monkeypatch.setattr(doctor_cli, "_TUI_APPS", ["demo-tui", "clock-tui"])
        result = doctor_cli.check_tui_activity()
        assert result["apps_active_today"] == 1
        assert result["total_events_today"] == 2
        apps = {a["app"]: a for a in result["apps"]}
        assert "demo-tui" in apps
        assert apps["demo-tui"]["events_today"] == 2

    def test_ignores_non_tui_app_lines(self, tmp_path, monkeypatch):
        mem = tmp_path / ".memory"
        sessions = mem / "sessions"
        sessions.mkdir(parents=True)
        import datetime
        today = datetime.date.today().isoformat()
        self._write_session(sessions, today, [
            "- 10:00 some-other-app: something happened",
            "- 10:01 clock-tui: session started",
        ])
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
        from atlas_plus.doctor import cli as doctor_cli
        monkeypatch.setattr(doctor_cli, "_TUI_APPS", ["demo-tui", "clock-tui"])
        result = doctor_cli.check_tui_activity()
        assert result["apps_active_today"] == 1
        apps = {a["app"]: a for a in result["apps"]}
        assert "clock-tui" in apps
        assert "some-other-app" not in apps

    def test_render_text_includes_tui_panel(self, tmp_path, monkeypatch):
        mem = tmp_path / ".memory"
        sessions = mem / "sessions"
        sessions.mkdir(parents=True)
        import datetime
        today = datetime.date.today().isoformat()
        self._write_session(sessions, today, [
            "- 14:30 demo-tui: session started",
        ])
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
        from atlas_plus.doctor import cli as doctor_cli
        monkeypatch.setattr(doctor_cli, "_TUI_APPS", ["demo-tui"])
        data = {"tui_activity": doctor_cli.check_tui_activity(), "ts": "2026-05-24T00:00:00Z"}
        output = doctor_cli.render_text(data)
        assert "TUI Activity" in output
        assert "demo-tui" in output


# ---------------------------------------------------------------------------
# scan_memory_files (shared single-pass scan)
# ---------------------------------------------------------------------------

class TestScanMemoryFiles:
    def _write(self, path: Path, body: str = "x") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_empty_when_root_missing(self, tmp_path):
        from atlas_plus.doctor.cli import scan_memory_files
        assert scan_memory_files(tmp_path / "nope") == []

    def test_reads_and_excludes_heavy_dirs(self, tmp_path):
        from atlas_plus.doctor.cli import scan_memory_files
        self._write(tmp_path / "feedback" / "a.md", "hello")
        self._write(tmp_path / "versions" / "old.md", "v")
        self._write(tmp_path / "archive" / "x.md", "arch")
        self._write(tmp_path / "_sync" / "s.md", "s")
        self._write(tmp_path / "_atlas" / "z.md", "z")
        recs = scan_memory_files(tmp_path)
        rels = {r.rel for r in recs}
        assert "feedback/a.md" in rels
        assert not any(
            r.rel.startswith(("versions/", "archive/", "_sync/", "_atlas/")) for r in recs
        )
        a = next(r for r in recs if r.rel == "feedback/a.md")
        assert a.text == "hello"
        assert a.name == "a.md"
        assert a.parts == ("feedback", "a.md")

    def test_default_root_uses_module_global(self, tmp_path, monkeypatch):
        from atlas_plus.doctor import cli
        self._write(tmp_path / "ref" / "r.md", "---\ntitle: t\n---\n")
        monkeypatch.setattr(cli, "MEMORY_ROOT", tmp_path)
        recs = cli.scan_memory_files()
        assert any(r.rel == "ref/r.md" for r in recs)


class TestDedupEquivalence:
    """A shared scan must yield results identical to the self-scan path (zero drift)."""

    def _build(self, mem: Path) -> None:
        import yaml
        from datetime import date
        (mem / "feedback").mkdir(parents=True, exist_ok=True)
        (mem / "projects").mkdir(parents=True, exist_ok=True)
        (mem / "feedback" / "c.md").write_text(
            "---\n" + yaml.dump({"title": "C", "contradictions": ["x.md"]}) + "---\nbody\n",
            encoding="utf-8",
        )
        today = date.today().isoformat()
        (mem / "projects" / "p.md").write_text(
            f"---\ntitle: P\nlast_confirmed: {today}\ntags: [a]\nconfidence: high\nupdated: {today}\n---\nbody\n",
            encoding="utf-8",
        )
        (mem / "projects" / "bare.md").write_text("no frontmatter\n", encoding="utf-8")

    def test_convergence_scan_equals_self_scan(self, tmp_path, monkeypatch):
        from atlas_plus.doctor import cli
        self._build(tmp_path)
        monkeypatch.setattr(cli, "MEMORY_ROOT", tmp_path)
        scan = cli.scan_memory_files(tmp_path)
        assert cli.check_convergence(scan) == cli.check_convergence()
        assert cli.check_convergence(scan)["flagged"] == 1

    def test_memory_quality_scan_equals_self_scan(self, tmp_path, monkeypatch):
        from atlas_plus.doctor import cli
        self._build(tmp_path)
        monkeypatch.setattr(cli, "MEMORY_ROOT", tmp_path)
        scan = cli.scan_memory_files(tmp_path)
        assert cli.check_memory_quality(scan) == cli.check_memory_quality()
        assert cli.check_memory_quality(scan)["total_files"] == 3


# ---------------------------------------------------------------------------
# check_tunnel
# ---------------------------------------------------------------------------

class TestCheckTunnel:
    def test_running_when_service_running(self):
        from atlas_plus.doctor.cli import check_tunnel
        probe = {"service": "Cloudflared", "service_state": "Running",
                 "proc_count": 1, "origin_listening": True, "origin_loopback_only": True}
        with patch("atlas_plus.doctor.cli._powershell_json", return_value=probe):
            r = check_tunnel()
        assert r["running"] is True
        assert r["service"] == "Cloudflared"
        assert r["origin_loopback_only"] is True

    def test_running_via_process_only(self):
        from atlas_plus.doctor.cli import check_tunnel
        probe = {"service": None, "service_state": None,
                 "proc_count": 2, "origin_listening": True, "origin_loopback_only": True}
        with patch("atlas_plus.doctor.cli._powershell_json", return_value=probe):
            r = check_tunnel()
        assert r["running"] is True

    def test_down_when_no_service_or_proc(self):
        from atlas_plus.doctor.cli import check_tunnel
        probe = {"service": None, "service_state": None,
                 "proc_count": 0, "origin_listening": False, "origin_loopback_only": True}
        with patch("atlas_plus.doctor.cli._powershell_json", return_value=probe):
            r = check_tunnel()
        assert r["running"] is False

    def test_error_when_probe_returns_none(self):
        from atlas_plus.doctor.cli import check_tunnel
        with patch("atlas_plus.doctor.cli._powershell_json", return_value=None):
            r = check_tunnel()
        assert "error" in r

    def test_exposed_origin_recorded(self):
        from atlas_plus.doctor.cli import check_tunnel
        probe = {"service": "Cloudflared", "service_state": "Running",
                 "proc_count": 1, "origin_listening": True, "origin_loopback_only": False}
        with patch("atlas_plus.doctor.cli._powershell_json", return_value=probe):
            r = check_tunnel()
        assert r["origin_listening"] is True
        assert r["origin_loopback_only"] is False



# ---------------------------------------------------------------------------
# render_text -- tunnel + gatekeeper panels
# ---------------------------------------------------------------------------

class TestRenderTunnelGatekeeper:
    def test_render_tunnel_up(self):
        from atlas_plus.doctor.cli import render_text
        data = {"ts": "2026-06-21", "services": [],
                "tunnel": {"running": True, "service": "Cloudflared", "service_state": "Running",
                           "proc_count": 1, "origin_listening": True, "origin_loopback_only": True}}
        out = render_text(data)
        assert "Tunnel" in out
        assert "up" in out

    def test_render_tunnel_exposed_warns(self):
        from atlas_plus.doctor.cli import render_text
        data = {"ts": "2026-06-21", "services": [],
                "tunnel": {"running": True, "service": "Cloudflared", "service_state": "Running",
                           "proc_count": 1, "origin_listening": True, "origin_loopback_only": False}}
        out = render_text(data)
        assert "NOT loopback-only" in out

    def test_render_tunnel_hidden_on_error(self):
        from atlas_plus.doctor.cli import render_text
        data = {"ts": "2026-06-21", "services": [], "tunnel": {"error": "no data"}}
        out = render_text(data)
        assert "Tunnel (remote MCP ingress)" not in out

    def test_render_gatekeeper_not_deployed(self):
        from atlas_plus.doctor.cli import render_text
        data = {"ts": "2026-06-21", "services": [],
                "gatekeeper": {"installed": False, "active": False}}
        out = render_text(data)
        assert "Gatekeeper" in out
        assert "not deployed" in out

    def test_render_gatekeeper_active(self):
        from atlas_plus.doctor.cli import render_text
        data = {"ts": "2026-06-21", "services": [],
                "gatekeeper": {"installed": True, "active": True,
                               "filter_state": "Running", "usermode_state": "Running"}}
        out = render_text(data)
        assert "Gatekeeper" in out
        assert "active" in out

    def test_render_gatekeeper_hidden_on_error(self):
        from atlas_plus.doctor.cli import render_text
        data = {"ts": "2026-06-21", "services": [], "gatekeeper": {"error": "no data"}}
        out = render_text(data)
        assert "Gatekeeper" not in out


# ---------------------------------------------------------------------------
# check_memory_corpus (X2: first-run / empty-corpus visibility)
# ---------------------------------------------------------------------------

class TestCheckMemoryCorpus:
    def test_missing_root_hints_at_init(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_corpus
        missing = tmp_path / "nope"
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", missing):
            result = check_memory_corpus()
        assert result["exists"] is False
        assert result["md_files"] == 0
        assert "atlas-init" in result["hint"]

    def test_empty_root_hints_at_init(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_corpus
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            result = check_memory_corpus()
        assert result["exists"] is True
        assert result["md_files"] == 0
        assert "atlas-init" in result["hint"]

    def test_corpus_without_identity_hints(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_corpus
        (tmp_path / "reference").mkdir()
        (tmp_path / "reference" / "thing.md").write_text("x", encoding="utf-8")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            result = check_memory_corpus()
        assert result["md_files"] == 1
        assert result["identity_present"] is False
        assert "identity" in result["hint"]

    def test_healthy_corpus_no_hint(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_corpus
        (tmp_path / "identity").mkdir()
        (tmp_path / "identity" / "user.md").write_text("# me", encoding="utf-8")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            result = check_memory_corpus()
        assert result["identity_present"] is True
        assert "hint" not in result

    def test_respects_shared_scan(self, tmp_path):
        from atlas_plus.doctor.cli import check_memory_corpus, scan_memory_files
        (tmp_path / "identity").mkdir()
        (tmp_path / "identity" / "user.md").write_text("# me", encoding="utf-8")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            scan = scan_memory_files(tmp_path)
            result = check_memory_corpus(scan)
        assert result["md_files"] == 1


class TestRenderMemoryCorpus:
    def test_render_empty_corpus_shows_hint(self):
        from atlas_plus.doctor.cli import render_text
        data = {"ts": "2026-08-02", "services": [],
                "memory_corpus": {"root": "/tmp/mem", "exists": True, "md_files": 0,
                                   "identity_present": False,
                                   "hint": "memory root is empty -- run atlas-init"}}
        out = render_text(data)
        assert "Memory corpus" in out
        assert "empty" in out
        assert "atlas-init" in out

    def test_render_healthy_corpus(self):
        from atlas_plus.doctor.cli import render_text
        data = {"ts": "2026-08-02", "services": [],
                "memory_corpus": {"root": "/tmp/mem", "exists": True, "md_files": 42,
                                   "identity_present": True}}
        out = render_text(data)
        assert "Memory corpus" in out
        assert "42 file(s)" in out

    def test_render_hidden_on_error(self):
        from atlas_plus.doctor.cli import render_text
        data = {"ts": "2026-08-02", "services": [], "memory_corpus": {"_error": "boom"}}
        out = render_text(data)
        assert "Memory corpus" not in out


class TestRenderTemporalNeverBuilt:
    def test_never_built_says_not_built_yet(self):
        from atlas_plus.doctor.cli import render_text
        data = {"ts": "2026-08-02", "services": [],
                "temporal": {"entities": 0, "facts_active": 0, "last_built": None, "stale": True}}
        out = render_text(data)
        assert "not built yet" in out
        assert "STALE" not in out

    def test_stale_with_age_still_says_stale(self):
        from atlas_plus.doctor.cli import render_text
        data = {"ts": "2026-08-02", "services": [],
                "temporal": {"entities": 10, "facts_active": 5, "last_built": "2026-07-01",
                              "stale": True, "age_days": 32}}
        out = render_text(data)
        assert "STALE (32d old)" in out


# ---------------------------------------------------------------------------
# check_sync (Sextant Sync visibility)
# ---------------------------------------------------------------------------

class TestCheckSync:
    def test_empty_when_sync_unused(self, tmp_path, monkeypatch):
        from atlas_plus.doctor.cli import check_sync
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            assert check_sync() == {}

    def test_reports_principals_and_pending(self, tmp_path, monkeypatch):
        from atlas_plus.doctor.cli import check_sync
        from atlas_plus.sync import devices
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
        devices.pair("laptop")
        conflict = tmp_path / "_atlas" / "sync-conflicts" / "2026-08-02" / "x.md"
        conflict.parent.mkdir(parents=True)
        conflict.write_text("parked", encoding="utf-8")
        with patch("atlas_plus.doctor.cli.MEMORY_ROOT", tmp_path):
            result = check_sync()
        assert result["principals"][0]["name"] == "laptop"
        assert result["conflicts"] == 1
        assert result["inbox"] == 0


class TestRenderSync:
    def test_hidden_when_unused(self):
        from atlas_plus.doctor.cli import render_text
        out = render_text({"ts": "2026-08-02", "services": [], "sync": {}})
        assert "Sync" not in out

    def test_renders_principals_and_conflicts(self):
        from atlas_plus.doctor.cli import render_text
        data = {"ts": "2026-08-02", "services": [], "sync": {
            "principals": [{"name": "laptop", "kind": "device", "scope": "rw",
                             "revoked": False, "last_checkin": "2026-08-02T20:00:00+00:00"}],
            "conflicts": 2, "inbox": 1,
        }}
        out = render_text(data)
        assert "Sync" in out and "laptop" in out
        assert "UNRESOLVED CONFLICTS: 2" in out
        assert "inbox proposals awaiting review: 1" in out


class TestSchedulerFreshness:
    def _seed(self, tmp_path, stamps):
        import sqlite3
        db = tmp_path / "state.sqlite"
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "CREATE TABLE scheduler_runs (job_id TEXT PRIMARY KEY, last_run_at TEXT,"
                " outcome TEXT, error TEXT, run_count INTEGER)"
            )
            for job, ts in stamps.items():
                conn.execute(
                    "INSERT INTO scheduler_runs VALUES (?, ?, 'ok', '', 1)", (job, ts)
                )
        return db

    def test_fresh_family_not_stale(self, tmp_path):
        from datetime import datetime, timedelta, timezone
        from atlas_plus.doctor.cli import check_scheduler_freshness
        recent = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        self._seed(tmp_path, {"dream": recent})
        with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            result = check_scheduler_freshness()
        assert result["stale"] is False

    def test_old_family_flagged_stale(self, tmp_path):
        from datetime import datetime, timedelta, timezone
        from atlas_plus.doctor.cli import check_scheduler_freshness
        old = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
        self._seed(tmp_path, {"dream": old, "sla-check": datetime.now(timezone.utc).isoformat()})
        with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path):
            result = check_scheduler_freshness()
        assert result["stale"] is True  # sla-check is not a nightly job; dream is 40h old

    def test_missing_db_empty(self, tmp_path):
        from atlas_plus.doctor.cli import check_scheduler_freshness
        with patch("atlas_plus.doctor.cli.STATE_DIR", tmp_path / "nope"):
            assert check_scheduler_freshness() == {}

    def test_render_stale_line(self):
        from atlas_plus.doctor.cli import render_text
        data = {"ts": "x", "services": [],
                "nightly": {"dream": {"status": "done", "last": "2026-08-04"}},
                "scheduler_freshness": {"stale": True, "age_hours": 33.2, "newest": "2026-08-04T04:30:00"}}
        out = render_text(data)
        assert "STALE: nightly family last ran 33.2h ago" in out


class TestEmbedIndexLine:
    def test_render_semantic_index_line(self):
        from atlas_plus.doctor.cli import render_text
        data = {"ts": "x", "services": [], "memory_corpus": {
            "root": "/m", "exists": True, "md_files": 100, "identity_present": True,
            "embed_index": {"indexed": 98, "corpus_files": 100, "model": "nomic-embed-text"},
        }}
        out = render_text(data)
        assert "semantic index" in out and "98/100" in out

    def test_no_line_when_unindexed(self):
        from atlas_plus.doctor.cli import render_text
        data = {"ts": "x", "services": [], "memory_corpus": {
            "root": "/m", "exists": True, "md_files": 100, "identity_present": True,
        }}
        out = render_text(data)
        assert "semantic index" not in out
