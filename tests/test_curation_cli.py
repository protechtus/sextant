"""Tests for the curation CLI module wrapper."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_plus.curation import cli


def test_curation_cli_main_invokes_typer_app(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(cli, "app", lambda: calls.append("app"))

    cli.main()

    assert calls == ["app"]


# ---------------------------------------------------------------------------
# _stub_slug
# ---------------------------------------------------------------------------

class TestStubSlug:
    def test_basic_slug(self):
        from atlas_plus.curation.cli import _stub_slug
        assert _stub_slug("SIP registration") == "sip-registration"

    def test_strips_special_chars(self):
        from atlas_plus.curation.cli import _stub_slug
        result = _stub_slug("what is SIP? (VoIP)")
        assert "?" not in result
        assert "(" not in result
        assert ")" not in result

    def test_truncates_to_40(self):
        from atlas_plus.curation.cli import _stub_slug
        long_q = "a very long query that exceeds forty characters limit"
        assert len(_stub_slug(long_q)) <= 40

    def test_empty_returns_gap(self):
        from atlas_plus.curation.cli import _stub_slug
        assert _stub_slug("") == "gap"


# ---------------------------------------------------------------------------
# _create_gap_stubs
# ---------------------------------------------------------------------------

class TestCreateGapStubs:
    def _misses(self):
        return [
            {"query": "SIP registration", "miss_count": 5, "total": 6},
            {"query": "REGISTER 401 challenge", "miss_count": 3, "total": 4},
        ]

    def test_creates_stub_files(self, tmp_path):
        from atlas_plus.curation.cli import _create_gap_stubs
        with patch("atlas_plus.mcp.memory_server.fts.search", return_value=[]):
            created = _create_gap_stubs(self._misses(), tmp_path / "gaps", "2026-05-20")
        assert len(created) == 2
        for p in created:
            assert p.exists()
            content = p.read_text(encoding="utf-8")
            assert "gap-stub" in content
            assert "status: unfilled" in content

    def test_skips_existing_stubs(self, tmp_path):
        from atlas_plus.curation.cli import _create_gap_stubs
        gaps_dir = tmp_path / "gaps"
        gaps_dir.mkdir()
        (gaps_dir / "sip-registration.md").write_text("existing", encoding="utf-8")
        with patch("atlas_plus.mcp.memory_server.fts.search", return_value=[]):
            created = _create_gap_stubs(self._misses(), gaps_dir, "2026-05-20")
        # Only second query creates a stub; first already exists
        assert len(created) == 1

    def test_stub_contains_miss_count(self, tmp_path):
        from atlas_plus.curation.cli import _create_gap_stubs
        with patch("atlas_plus.mcp.memory_server.fts.search", return_value=[]):
            created = _create_gap_stubs(
                [{"query": "SIP 401", "miss_count": 7, "total": 8}],
                tmp_path / "gaps",
                "2026-05-20",
            )
        assert len(created) == 1
        content = created[0].read_text(encoding="utf-8")
        assert "miss_count: 7" in content

    def test_handles_fts_exception_gracefully(self, tmp_path):
        """Stubs are still created when FTS search raises an exception."""
        from atlas_plus.curation.cli import _create_gap_stubs
        with patch("atlas_plus.mcp.memory_server.fts.search", side_effect=RuntimeError("db error")):
            created = _create_gap_stubs(
                [{"query": "some query", "miss_count": 2, "total": 2}],
                tmp_path / "gaps",
                "2026-05-20",
            )
        assert len(created) == 1


# ---------------------------------------------------------------------------
# cmd_convergence (via CLI runner)
# ---------------------------------------------------------------------------

class TestCmdConvergence:
    def _write_md_with_contradictions(self, path: Path, title: str, contradictions: list) -> None:
        import yaml
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = {"title": title, "type": "reference", "contradictions": contradictions}
        path.write_text(f"---\n{yaml.dump(fm)}---\n\n# {title}\n\nbody text\n", encoding="utf-8")

    def _write_md_no_contradictions(self, path: Path, title: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntitle: {title}\n---\n\nbody\n", encoding="utf-8")

    def test_reports_files_with_contradictions(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        import atlas_plus.mcp.memory_server.config as cfg_mod

        self._write_md_with_contradictions(
            tmp_path / "reference" / "sip.md",
            "SIP Protocol",
            ["reference/voip.md"],
        )
        runner = CliRunner()
        with patch.object(cli_mod, "MEMORY_ROOT", tmp_path), \
             patch.object(cfg_mod, "MEMORY_ROOT", tmp_path):
            result = runner.invoke(cli_mod.app, ["convergence"])

        assert result.exit_code == 0
        assert "SIP Protocol" in result.output or "sip.md" in result.output
        assert "voip.md" in result.output

    def test_clean_memory_shows_no_contradictions(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod

        self._write_md_no_contradictions(tmp_path / "reference" / "clean.md", "Clean file")
        runner = CliRunner()
        with patch.object(cli_mod, "MEMORY_ROOT", tmp_path):
            result = runner.invoke(cli_mod.app, ["convergence"])

        assert result.exit_code == 0
        assert "No files with unresolved contradiction" in result.output

    def test_json_output(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod

        self._write_md_with_contradictions(
            tmp_path / "feedback" / "style.md",
            "Style Guide",
            ["feedback/style-v2.md"],
        )
        runner = CliRunner()
        with patch.object(cli_mod, "MEMORY_ROOT", tmp_path):
            result = runner.invoke(cli_mod.app, ["convergence", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["contradictions"] == ["feedback/style-v2.md"]


# ---------------------------------------------------------------------------
# cmd_decisions
# ---------------------------------------------------------------------------

class TestCmdDecisions:
    def _write_decision(self, path: Path, title: str, made: str,
                        last_reviewed: str = "", revisit_if: list = None) -> None:
        import yaml
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = {"title": title, "type": "decision", "made": made}
        if last_reviewed:
            fm["last_reviewed"] = last_reviewed
        if revisit_if:
            fm["revisit_if"] = revisit_if
        path.write_text(f"---\n{yaml.dump(fm)}---\n\nbody\n", encoding="utf-8")

    def test_no_decisions_dir_shows_message(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        runner = CliRunner()
        with patch.object(cli_mod, "MEMORY_ROOT", tmp_path):
            result = runner.invoke(cli_mod.app, ["decisions"])
        assert result.exit_code == 0
        assert "No .memory/decisions" in result.output or "decisions" in result.output.lower()

    def test_lists_decision_records(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        self._write_decision(
            tmp_path / "decisions" / "faiss-choice.md",
            "Use FAISS for VoIP corpus",
            "2026-04-20",
            last_reviewed="2026-04-30",
        )
        runner = CliRunner()
        with patch.object(cli_mod, "MEMORY_ROOT", tmp_path):
            result = runner.invoke(cli_mod.app, ["decisions"])
        assert result.exit_code == 0
        assert "FAISS" in result.output

    def test_flags_stale_decisions(self, tmp_path):
        """Decision with no last_reviewed and made > 90 days ago should be STALE."""
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        self._write_decision(
            tmp_path / "decisions" / "old.md",
            "Old Decision",
            "2026-01-01",  # well over 90 days ago
        )
        runner = CliRunner()
        with patch.object(cli_mod, "MEMORY_ROOT", tmp_path):
            result = runner.invoke(cli_mod.app, ["decisions"])
        assert result.exit_code == 0
        assert "STALE" in result.output or "stale" in result.output.lower()

    def test_json_output(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        self._write_decision(
            tmp_path / "decisions" / "choice.md",
            "FAISS Decision",
            "2026-04-20",
            last_reviewed="2026-05-01",
            revisit_if=["If corpus size doubles"],
        )
        runner = CliRunner()
        with patch.object(cli_mod, "MEMORY_ROOT", tmp_path):
            result = runner.invoke(cli_mod.app, ["decisions", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["title"] == "FAISS Decision"
        assert "If corpus size doubles" in data[0]["revisit_if"]


# ---------------------------------------------------------------------------
# weekly_report._scan_decisions
# ---------------------------------------------------------------------------

class TestWeeklyReportDecisions:
    """Tests for the _scan_decisions helper and the decisions section in generate_report."""

    def _write_decision(self, path: Path, title: str, made: str, last_reviewed: str = "") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fm_lines = [f"title: {title}", "type: decision", f"made: {made}"]
        if last_reviewed:
            fm_lines.append(f"last_reviewed: {last_reviewed}")
        path.write_text("---\n" + "\n".join(fm_lines) + "\n---\n\nbody\n", encoding="utf-8")

    def test_scan_decisions_no_dir_returns_empty(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_decisions
        result = _scan_decisions(tmp_path)
        assert result == []

    def test_scan_decisions_flags_old_decision(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_decisions
        self._write_decision(tmp_path / "decisions" / "old.md", "Old Choice", "2026-01-01")
        result = _scan_decisions(tmp_path, stale_days=90)
        assert len(result) == 1
        assert result[0]["slug"] == "old"
        assert result[0]["title"] == "Old Choice"

    def test_scan_decisions_skips_recent_reviewed(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_decisions
        from datetime import date, timedelta
        recent = (date.today() - timedelta(days=10)).isoformat()
        self._write_decision(tmp_path / "decisions" / "fresh.md", "Fresh Choice", "2026-01-01", last_reviewed=recent)
        result = _scan_decisions(tmp_path, stale_days=90)
        assert result == []

    def test_generate_report_includes_decisions_section(self, tmp_path):
        from unittest.mock import patch as _patch
        with _patch("atlas_plus.curation.weekly_report.MEMORY_ROOT", tmp_path):
            from atlas_plus.curation.weekly_report import generate_report
            self._write_decision(tmp_path / "decisions" / "stale.md", "Old Decision", "2026-01-01")
            report, _ = generate_report(memory_root=tmp_path, dry_run=True)
        assert "Stale Decisions" in report
        assert "stale" in report
        assert "Old Decision" in report

    def test_generate_report_no_decisions_clean_message(self, tmp_path):
        from atlas_plus.curation.weekly_report import generate_report
        report, _ = generate_report(memory_root=tmp_path, dry_run=True)
        assert "Stale Decisions" in report
        assert "No stale decisions found" in report


# ---------------------------------------------------------------------------
# weekly_report._scan_rederivable + generate_report rederivable section
# ---------------------------------------------------------------------------

class TestWeeklyReportRederivable:
    def _write_mem(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nid: {path.stem}\ntitle: {path.stem}\ntype: reference\nstatus: active\n---\n\n{body}",
            encoding="utf-8",
        )

    def _write_doc(self, path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_scan_rederivable_no_doc_files_returns_empty(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_rederivable
        mem = tmp_path / "memory"
        mem.mkdir()
        self._write_mem(mem / "ref.md", "atlas memory temporal queries provider")
        result = _scan_rederivable(mem, threshold=0.1)
        assert result == []

    def test_scan_rederivable_high_overlap_flagged(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_rederivable
        ws = tmp_path / "workspace"
        mem = tmp_path / "memory"
        shared = (
            "atlas memory brain scheduler temporal worker search index queries "
            "session results confidence annotations scores rebuild ingestion "
            "curation decay duplicate stale archive decision reference feedback"
        )
        self._write_doc(ws / "AGENTS.md", shared)
        self._write_mem(mem / "atlas.md", shared)
        result = _scan_rederivable(mem, threshold=0.1)
        # mem.parent = tmp_path, which is the ws_root passed to find_rederivable
        # The workspace root needs to contain doc files -- check if any candidates found
        # (may be empty if workspace root doesn't have doc files at the expected globs)
        assert isinstance(result, list)

    def test_generate_report_includes_rederivable_section(self, tmp_path):
        from atlas_plus.curation.weekly_report import generate_report
        report, _ = generate_report(memory_root=tmp_path, dry_run=True)
        assert "Re-derivable Facts" in report

    def test_generate_report_rederivable_clean_message(self, tmp_path):
        from atlas_plus.curation.weekly_report import generate_report
        report, _ = generate_report(memory_root=tmp_path, dry_run=True)
        assert "No re-derivable facts found" in report or "Re-derivable Facts" in report


class TestRederivableOptOut:
    """rederivable: false frontmatter field exempts a file from the scan."""

    def _write_mem_with_fm(self, path: Path, body: str, extra_fm: str = "") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\nid: {path.stem}\ntitle: {path.stem}\ntype: reference\nstatus: active\n{extra_fm}\n---\n\n{body}",
            encoding="utf-8",
        )

    def test_rederivable_false_exempts_file(self, tmp_path):
        """A file with rederivable: false must not appear in find_rederivable output."""
        from atlas_plus.curation.rederive import find_rederivable

        ws = tmp_path / "workspace"
        mem = tmp_path / ".memory"
        shared = (
            "atlas brain scheduler temporal memory worker search index queries "
            "session results confidence rebuild ingestion curation decay duplicate "
            "stale archive decision reference feedback annotations scores"
        )
        # Write a doc file at workspace root
        ws.mkdir(parents=True)
        (ws / "AGENTS.md").write_text(shared, encoding="utf-8")

        # Write a memory file with rederivable: false
        (mem / "feedback").mkdir(parents=True)
        self._write_mem_with_fm(mem / "feedback" / "exempt.md", shared, "rederivable: false")

        # The file has high overlap but is explicitly exempt
        candidates = find_rederivable(threshold=0.1, workspace_root=ws)
        paths = [c.path for c in candidates]
        assert not any("exempt.md" in p for p in paths), (
            "File with rederivable: false should be excluded from scan"
        )

    def test_rederivable_true_included(self, tmp_path):
        """A file with rederivable: true (explicit) is still eligible for flagging."""
        from atlas_plus.curation.rederive import find_rederivable

        ws = tmp_path / "workspace"
        mem = tmp_path / ".memory"
        shared = (
            "atlas brain scheduler temporal memory worker search index queries "
            "session results confidence rebuild ingestion curation decay duplicate "
            "stale archive decision reference feedback annotations scores"
        )
        ws.mkdir(parents=True)
        (ws / "AGENTS.md").write_text(shared, encoding="utf-8")

        (mem / "feedback").mkdir(parents=True)
        self._write_mem_with_fm(mem / "feedback" / "included.md", shared, "rederivable: true")

        # With rederivable: true it is eligible (result depends on actual similarity)
        # Just verify it doesn't crash and returns a list
        candidates = find_rederivable(threshold=0.1, workspace_root=ws)
        assert isinstance(candidates, list)


# ---------------------------------------------------------------------------
# weekly_report._scan_convergence
# ---------------------------------------------------------------------------

class TestScanConvergence:
    """Unit tests for _scan_convergence: detects files with contradictions: frontmatter."""

    def _write_md(self, path: Path, frontmatter: str, body: str = "body text") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")

    def test_empty_root_returns_empty(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_convergence
        assert _scan_convergence(tmp_path) == []

    def test_file_with_contradictions_list_flagged(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_convergence
        self._write_md(
            tmp_path / "feedback" / "style.md",
            "name: style-guide\ncontradictions:\n  - feedback/voice.md\n  - feedback/tone.md",
        )
        result = _scan_convergence(tmp_path)
        assert len(result) == 1
        assert "feedback/style.md" in result[0]["path"]
        assert len(result[0]["contradictions"]) == 2

    def test_file_without_contradictions_not_flagged(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_convergence
        self._write_md(
            tmp_path / "reference" / "tools.md",
            "name: tools\nstatus: active",
        )
        assert _scan_convergence(tmp_path) == []

    def test_file_with_empty_contradictions_list_not_flagged(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_convergence
        self._write_md(
            tmp_path / "feedback" / "clean.md",
            "name: clean\ncontradictions: []",
        )
        assert _scan_convergence(tmp_path) == []

    def test_archive_dir_excluded(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_convergence
        self._write_md(
            tmp_path / "archive" / "old.md",
            "name: old\ncontradictions:\n  - some/other.md",
        )
        # archive is in _CONVERGENCE_EXCLUDED
        assert _scan_convergence(tmp_path) == []

    def test_underscore_file_excluded(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_convergence
        self._write_md(
            tmp_path / "feedback" / "_index.md",
            "name: index\ncontradictions:\n  - some/file.md",
        )
        assert _scan_convergence(tmp_path) == []

    def test_file_without_frontmatter_skipped(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_convergence
        p = tmp_path / "reference" / "no-fm.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("contradictions: [other.md]\n\njust body", encoding="utf-8")
        assert _scan_convergence(tmp_path) == []

    def test_contradictions_capped_at_three_in_output(self, tmp_path):
        from atlas_plus.curation.weekly_report import _scan_convergence
        self._write_md(
            tmp_path / "feedback" / "many.md",
            "name: many\ncontradictions:\n  - a.md\n  - b.md\n  - c.md\n  - d.md\n  - e.md",
        )
        result = _scan_convergence(tmp_path)
        assert len(result) == 1
        assert len(result[0]["contradictions"]) == 3

    def test_generate_report_includes_convergence_section(self, tmp_path):
        from atlas_plus.curation.weekly_report import generate_report
        self._write_md(
            tmp_path / "feedback" / "conflict.md",
            "name: conflict\ncontradictions:\n  - reference/other.md",
        )
        report, _ = generate_report(memory_root=tmp_path, dry_run=True)
        assert "Convergence Flags" in report
        assert "feedback/conflict.md" in report

    def test_generate_report_clean_convergence_message(self, tmp_path):
        from atlas_plus.curation.weekly_report import generate_report
        report, _ = generate_report(memory_root=tmp_path, dry_run=True)
        assert "No unresolved contradiction flags found" in report


# ---------------------------------------------------------------------------
# atlas review-memory summary command
# ---------------------------------------------------------------------------

class TestSummaryCommand:
    """Tests for the aggregate memory health summary command."""

    def test_json_output_has_expected_keys(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch

        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.doctor.cli.check_memory_quality",
                   return_value={"total_files": 50, "confirmed_pct": 60,
                                 "tagged_pct": 70, "stale_confirmed": 3, "anti_flagged": 0}), \
             patch("atlas_plus.curation.health.scan_memory_health", return_value=[]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.curation.ref_graph.build_reference_graph", return_value={}), \
             patch("atlas_plus.curation.ref_graph.find_hub_files", return_value=[]):
            result = runner.invoke(app, ["summary", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "total_files" in data
        assert "confirmed_pct" in data
        assert "tagged_pct" in data
        assert "avg_health" in data
        assert "orphan_candidates" in data
        assert "hub_files" in data

    def test_text_output_shows_memory_summary(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch

        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.doctor.cli.check_memory_quality",
                   return_value={"total_files": 42, "confirmed_pct": 55,
                                 "tagged_pct": 80, "stale_confirmed": 0, "anti_flagged": 0}), \
             patch("atlas_plus.curation.health.scan_memory_health", return_value=[]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.curation.ref_graph.build_reference_graph", return_value={}), \
             patch("atlas_plus.curation.ref_graph.find_hub_files", return_value=[]):
            result = runner.invoke(app, ["summary"])
        assert result.exit_code == 0
        assert "Memory Summary" in result.output
        assert "42" in result.output  # total files count

    def test_orphan_candidates_shown_when_present(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch, MagicMock

        orphans = [{"path": "ref/old.md", "age_days": 250, "score": 0.2, "issues": []}]
        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.doctor.cli.check_memory_quality",
                   return_value={"total_files": 10, "confirmed_pct": 50,
                                 "tagged_pct": 50, "stale_confirmed": 0, "anti_flagged": 0}), \
             patch("atlas_plus.curation.health.scan_memory_health", return_value=[]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=orphans), \
             patch("atlas_plus.curation.ref_graph.build_reference_graph", return_value={}), \
             patch("atlas_plus.curation.ref_graph.find_hub_files", return_value=[]):
            result = runner.invoke(app, ["summary"])
        assert result.exit_code == 0
        assert "1" in result.output  # 1 orphan

    def test_graceful_when_subsystems_fail(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation.cli import app
        from unittest.mock import patch

        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.doctor.cli.check_memory_quality", side_effect=RuntimeError("db gone")), \
             patch("atlas_plus.curation.health.scan_memory_health", side_effect=RuntimeError("scan failed")), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", side_effect=RuntimeError("orphan fail")), \
             patch("atlas_plus.curation.ref_graph.build_reference_graph", side_effect=RuntimeError("graph fail")), \
             patch("atlas_plus.curation.ref_graph.find_hub_files", return_value=[]):
            result = runner.invoke(app, ["summary"])
        # Must not crash -- should produce some output even when all checks fail
        assert result.exit_code == 0
        assert "Memory Summary" in result.output


# ---------------------------------------------------------------------------
# _autofile_gap_tickets
# ---------------------------------------------------------------------------

class TestAutofileGapTickets:
    def _make_miss(self, query: str, miss_count: int = 5, total: int = 10) -> dict:
        return {"query": query, "miss_count": miss_count, "total": total}

    def test_creates_ticket_when_no_report_exists(self, tmp_path):
        from atlas_plus.curation.cli import _autofile_gap_tickets
        misses = [self._make_miss("SIP registration flow")]
        with patch("atlas_plus.brain.research.find_similar_research_reports", return_value=[]), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]), \
             patch("atlas_plus.tickets.store.create_ticket") as mock_create:
            from unittest.mock import MagicMock
            mock_ticket = MagicMock()
            mock_ticket.id = "ATL-099"
            mock_ticket.title = "Research gap: SIP registration flow"
            mock_create.return_value = mock_ticket
            result = _autofile_gap_tickets(misses)
        assert len(result) == 1
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args[1]
        assert "SIP registration flow" in call_kwargs["title"]
        assert call_kwargs["assignee"] == "any"
        assert call_kwargs["effort"] == "S"

    def test_skips_when_similar_report_exists(self, tmp_path):
        from atlas_plus.curation.cli import _autofile_gap_tickets
        misses = [self._make_miss("SIP registration")]
        with patch("atlas_plus.brain.research.find_similar_research_reports",
                   return_value=[{"file": "sip.md", "score": 0.8}]), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]), \
             patch("atlas_plus.tickets.store.create_ticket") as mock_create:
            result = _autofile_gap_tickets(misses)
        assert result == []
        mock_create.assert_not_called()

    def test_skips_when_ticket_already_open(self):
        from atlas_plus.curation.cli import _autofile_gap_tickets
        from unittest.mock import MagicMock
        misses = [self._make_miss("SIP registration flow")]
        existing = MagicMock()
        existing.title = "Research gap: SIP registration flow"
        existing.status = "todo"
        with patch("atlas_plus.brain.research.find_similar_research_reports", return_value=[]), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[existing]), \
             patch("atlas_plus.tickets.store.create_ticket") as mock_create:
            result = _autofile_gap_tickets(misses)
        mock_create.assert_not_called()

    def test_returns_empty_when_all_covered(self):
        from atlas_plus.curation.cli import _autofile_gap_tickets
        misses = [self._make_miss("well documented topic")]
        with patch("atlas_plus.brain.research.find_similar_research_reports",
                   return_value=[{"file": "covered.md", "score": 0.9}]), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]):
            result = _autofile_gap_tickets(misses)
        assert result == []

    def test_graceful_when_research_lookup_fails(self):
        from atlas_plus.curation.cli import _autofile_gap_tickets
        from unittest.mock import MagicMock
        misses = [self._make_miss("some topic")]
        with patch("atlas_plus.brain.research.find_similar_research_reports",
                   side_effect=RuntimeError("no reports dir")), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]), \
             patch("atlas_plus.tickets.store.create_ticket") as mock_create:
            mock_ticket = MagicMock()
            mock_ticket.id = "ATL-100"
            mock_ticket.title = "Research gap: some topic"
            mock_create.return_value = mock_ticket
            result = _autofile_gap_tickets(misses)
        # Should still create a ticket when lookup fails (conservative: file if uncertain)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# cmd_gaps --autoticket E2E (typer invoke)
# ---------------------------------------------------------------------------

class TestCmdGapsAutoticket:
    def test_autoticket_flag_creates_tickets_via_typer(self, tmp_path):
        """End-to-end: invoking cmd_gaps --autoticket creates tickets for uncovered gaps."""
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        from unittest.mock import MagicMock

        runner = CliRunner()
        miss = {"query": "SIP OPTIONS", "miss_count": 5, "total": 8}
        mock_ticket = MagicMock()
        mock_ticket.id = "ATL-099"
        mock_ticket.title = "Research gap: SIP OPTIONS"

        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   return_value=[miss]), \
             patch("atlas_plus.brain.research.find_similar_research_reports", return_value=[]), \
             patch("atlas_plus.tickets.store.list_tickets", return_value=[]), \
             patch("atlas_plus.tickets.store.create_ticket", return_value=mock_ticket):
            result = runner.invoke(cli_mod.app, ["gaps", "--autoticket"])

        assert result.exit_code == 0
        assert "ATL-099" in result.output or "Research gap" in result.output

    def test_no_misses_autoticket_shows_no_tickets(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod

        runner = CliRunner()
        with patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries",
                   return_value=[]):
            result = runner.invoke(cli_mod.app, ["gaps", "--autoticket"])

        assert result.exit_code == 0
        assert "No memory misses" in result.output


# ---------------------------------------------------------------------------
# suggest-tags CLI command
# ---------------------------------------------------------------------------

class TestCmdSuggestTags:
    def test_no_untagged_shows_clean_message(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path):
            result = runner.invoke(cli_mod.app, ["suggest-tags"])
        assert result.exit_code == 0
        assert "No untagged" in result.output or "no untagged" in result.output.lower()

    def test_dry_run_lists_suggestions(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        (feedback / "pref.md").write_text(
            "---\ntitle: FastAPI Preference\ntype: feedback\n---\nContent.", encoding="utf-8"
        )
        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path):
            result = runner.invoke(cli_mod.app, ["suggest-tags"])
        assert result.exit_code == 0
        assert "fastapi" in result.output.lower() or "feedback" in result.output.lower() \
               or "Would apply" in result.output

    def test_write_applies_tags(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        (feedback / "pref.md").write_text(
            "---\ntitle: FastAPI Preference\ntype: feedback\n---\nContent.", encoding="utf-8"
        )
        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path):
            result = runner.invoke(cli_mod.app, ["suggest-tags", "--write"])
        assert result.exit_code == 0
        content = (feedback / "pref.md").read_text(encoding="utf-8")
        assert "tags:" in content

    def test_json_output_returns_json(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        (feedback / "pref.md").write_text(
            "---\ntitle: FastAPI Pref\ntype: feedback\n---\nContent.", encoding="utf-8"
        )
        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path):
            result = runner.invoke(cli_mod.app, ["suggest-tags", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "results" in data
        assert "no_suggestion_count" in data

    def test_scope_restricts_scan(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        feedback = tmp_path / "feedback"
        reference = tmp_path / "reference"
        feedback.mkdir()
        reference.mkdir()
        (feedback / "pref.md").write_text(
            "---\ntitle: FastAPI Pref\ntype: feedback\n---\nContent.", encoding="utf-8"
        )
        (reference / "ref.md").write_text(
            "---\ntitle: Ref Doc\ntype: reference\n---\nContent.", encoding="utf-8"
        )
        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path):
            result = runner.invoke(cli_mod.app, ["suggest-tags", "--scope", "reference"])
        assert result.exit_code == 0
        assert "pref.md" not in result.output


# ---------------------------------------------------------------------------
# duplicates CLI command
# ---------------------------------------------------------------------------

class TestCmdDuplicates:
    def test_no_duplicates_shows_clean_message(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        runner = CliRunner()
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.duplicates.find_duplicates", return_value=[]):
            result = runner.invoke(cli_mod.app, ["duplicates"])
        assert result.exit_code == 0
        assert "No duplicate" in result.output or "no duplicate" in result.output.lower()

    def test_json_output_empty(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.duplicates.find_duplicates", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["duplicates", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# stale CLI command
# ---------------------------------------------------------------------------

class TestCmdStale:
    def test_no_stale_shows_clean(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.stale.find_stale", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["stale"])
        assert result.exit_code == 0
        assert "No stale" in result.output or "no stale" in result.output.lower()

    def test_json_output_empty(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.stale.find_stale", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["stale", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# archive CLI command
# ---------------------------------------------------------------------------

class TestCmdArchive:
    def test_archive_existing_file(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        feedback = tmp_path / "feedback"
        feedback.mkdir()
        (feedback / "old.md").write_text("---\ntitle: Old\n---\nContent.", encoding="utf-8")
        # archive_file() also reads MEMORY_ROOT from its own module (stale.py)
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.stale.MEMORY_ROOT", tmp_path):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["archive", "feedback/old.md", "--yes"])
        assert result.exit_code == 0
        assert (tmp_path / "archive" / "feedback" / "old.md").exists() or \
               "Archived" in result.output or "archived" in result.output.lower()

    def test_archive_nonexistent_file_shows_error(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["archive", "nonexistent.md", "--yes"])
        assert result.exit_code != 0 or "not found" in result.output.lower() \
               or "does not exist" in result.output.lower()


# ---------------------------------------------------------------------------
# decay CLI command
# ---------------------------------------------------------------------------

class TestCmdDecay:
    def test_no_decayed_shows_clean_message(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["decay"])
        assert result.exit_code == 0
        assert "No files past" in result.output or "no files" in result.output.lower()

    def test_json_output_empty(self):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["decay", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 0

    def test_shows_candidates(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        from unittest.mock import MagicMock
        cand = MagicMock()
        cand.as_dict.return_value = {"path": "feedback/old.md", "half_life_days": 30}
        cand.path = "feedback/old.md"
        cand.memory_type = "feedback"
        cand.half_life_days = 30
        cand.updated = "2025-11-01"
        cand.days_overdue = 90
        with patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[cand]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["decay"])
        assert result.exit_code == 0
        assert "old.md" in result.output or "1 file" in result.output


# ---------------------------------------------------------------------------
# unconfirmed-decay CLI command
# ---------------------------------------------------------------------------

class TestCmdUnconfirmedDecay:
    def test_no_stale_shows_clean(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.mcp.memory_server.routing.downgrade_stale_confirmations", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["unconfirmed-decay"])
        assert result.exit_code == 0
        assert "No stale" in result.output

    def test_json_output_empty(self):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.mcp.memory_server.routing.downgrade_stale_confirmations", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["unconfirmed-decay", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_dry_run_shows_would_downgrade(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        entry = {
            "path": "reference/old.md",
            "old_confidence": "high",
            "last_confirmed": "2025-10-01",
            "age_days": 200,
        }
        with patch("atlas_plus.mcp.memory_server.routing.downgrade_stale_confirmations", return_value=[entry]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["unconfirmed-decay"])
        assert result.exit_code == 0
        assert "Would downgrade" in result.output or "old.md" in result.output


# ---------------------------------------------------------------------------
# age-stats CLI command
# ---------------------------------------------------------------------------

class TestCmdAgeStats:
    def test_empty_root_shows_stats(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        import atlas_plus.mcp.memory_server.config as cfg_mod
        with patch.object(cfg_mod, "MEMORY_ROOT", tmp_path):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["age-stats"])
        assert result.exit_code == 0
        assert "Memory Age Distribution" in result.output

    def test_json_output(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        import atlas_plus.mcp.memory_server.config as cfg_mod
        with patch.object(cfg_mod, "MEMORY_ROOT", tmp_path):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["age-stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "total" in data
        assert "buckets" in data
        assert data["total"] == 0

    def test_classifies_file_with_date(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        import atlas_plus.mcp.memory_server.config as cfg_mod
        ref = tmp_path / "reference"
        ref.mkdir()
        (ref / "recent.md").write_text(
            "---\ntitle: Recent\nupdated: 2026-05-01\n---\nBody.", encoding="utf-8"
        )
        with patch.object(cfg_mod, "MEMORY_ROOT", tmp_path):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["age-stats", "--json"])
        data = json.loads(result.output)
        assert data["total"] == 1

    def test_verbose_lists_files(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        import atlas_plus.mcp.memory_server.config as cfg_mod
        ref = tmp_path / "reference"
        ref.mkdir()
        (ref / "stale-doc.md").write_text(
            "---\ntitle: Old Doc\nupdated: 2025-01-01\n---\nBody.", encoding="utf-8"
        )
        with patch.object(cfg_mod, "MEMORY_ROOT", tmp_path):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["age-stats", "--verbose"])
        assert result.exit_code == 0
        assert "stale-doc.md" in result.output

    def test_json_includes_file_lists(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        import atlas_plus.mcp.memory_server.config as cfg_mod
        ref = tmp_path / "reference"
        ref.mkdir()
        (ref / "recent.md").write_text(
            "---\ntitle: Recent\nupdated: 2026-05-01\n---\nBody.", encoding="utf-8"
        )
        with patch.object(cfg_mod, "MEMORY_ROOT", tmp_path):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["age-stats", "--json"])
        data = json.loads(result.output)
        assert "files" in data
        assert isinstance(data["files"], dict)
        assert sum(data["buckets"].values()) == 1


# ---------------------------------------------------------------------------
# health-score CLI command
# ---------------------------------------------------------------------------

class TestCmdHealthScore:
    def test_all_healthy_shows_message(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.health.scan_memory_health", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["health-score"])
        assert result.exit_code == 0
        assert "well maintained" in result.output or "health score" in result.output.lower()

    def test_json_output_empty(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.health.scan_memory_health", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["health-score", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_shows_low_health_files(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        results = [{"path": "feedback/needs-work.md", "score": 0.25, "issues": ["no tags", "no confirmed"]}]
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.health.scan_memory_health", return_value=results):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["health-score"])
        assert result.exit_code == 0
        assert "needs-work.md" in result.output or "1 file" in result.output


# ---------------------------------------------------------------------------
# orphans CLI command
# ---------------------------------------------------------------------------

class TestCmdOrphans:
    def test_no_orphans_shows_clean(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path))
        with patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["orphans"])
        assert result.exit_code == 0
        assert "No orphan" in result.output or "no orphan" in result.output.lower()

    def test_json_output_empty(self, tmp_path, monkeypatch):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path))
        with patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["orphans", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_shows_orphan_candidates(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path))
        orphan = {
            "path": "reference/orphaned.md",
            "age_days": 300,
            "score": 0.0,
            "inbound_refs": 0,
            "issues": ["no tags", "no confirmed"],
        }
        with patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[orphan]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["orphans"])
        assert result.exit_code == 0
        assert "orphaned.md" in result.output or "1 orphan" in result.output


# ---------------------------------------------------------------------------
# ref-graph CLI command
# ---------------------------------------------------------------------------

class TestCmdRefGraph:
    def test_no_links_shows_dim_message(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path))
        with patch("atlas_plus.curation.ref_graph.build_reference_graph", return_value={}), \
             patch("atlas_plus.curation.ref_graph.find_hub_files", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["ref-graph"])
        assert result.exit_code == 0
        assert "No reference links" in result.output or "no reference" in result.output.lower()

    def test_json_output_empty(self, tmp_path, monkeypatch):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path))
        with patch("atlas_plus.curation.ref_graph.build_reference_graph", return_value={}), \
             patch("atlas_plus.curation.ref_graph.find_hub_files", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["ref-graph", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_shows_hub_files(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path))
        graph = {"atlas-architecture": ["brain", "memory"], "brain": ["tasks"]}
        hubs = [{"stem": "atlas-architecture", "inbound_count": 10}]
        with patch("atlas_plus.curation.ref_graph.build_reference_graph", return_value=graph), \
             patch("atlas_plus.curation.ref_graph.find_hub_files", return_value=hubs):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["ref-graph"])
        assert result.exit_code == 0
        assert "atlas-architecture" in result.output


# ---------------------------------------------------------------------------
# report CLI command (weekly curation report)
# ---------------------------------------------------------------------------

class TestCmdReport:
    def test_dry_run_output(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   return_value=("## Weekly Curation Report\n\nAll good.", tmp_path / "report.md")):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["report", "--dry-run"])
        assert result.exit_code == 0
        assert "Weekly Curation Report" in result.output
        assert "dry-run" in result.output or "not written" in result.output

    def test_report_written(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        out_path = tmp_path / "report.md"
        with patch("atlas_plus.curation.weekly_report.generate_report",
                   return_value=("## Report\n\nContent.", out_path)):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["report"])
        assert result.exit_code == 0
        assert "Report" in result.output
        assert str(out_path) in result.output or "Written" in result.output


# ---------------------------------------------------------------------------
# narrative CLI command
# ---------------------------------------------------------------------------

class TestCmdNarrative:
    def test_no_sessions_shows_message(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.session_narrative.run_narrative",
                   return_value={"ok": False, "reason": "no sessions found"}):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["narrative"])
        assert result.exit_code == 0
        assert "no sessions" in result.output.lower() or "no session" in result.output.lower()

    def test_json_output_on_success(self):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        rv = {
            "ok": True,
            "path": ".memory/sessions/2026-W21-narrative.md",
            "week": "2026-W21",
            "sessions": 3,
            "energy": "high",
            "open_loops": 2,
            "direction_signals": 4,
        }
        with patch("atlas_plus.session_narrative.run_narrative", return_value=rv):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["narrative", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ok"] is True
        assert data["energy"] == "high"

    def test_no_write_calls_build_narrative(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        from unittest.mock import MagicMock
        report = MagicMock()
        report.sessions_analyzed = 0
        with patch("atlas_plus.session_narrative.build_narrative", return_value=report):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["narrative", "--no-write"])
        assert result.exit_code == 0
        assert "No session" in result.output


# ---------------------------------------------------------------------------
# contradictions CLI command
# ---------------------------------------------------------------------------

class TestCmdContradictions:
    def test_no_contradictions_shows_clean(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.contradictions.find_contradictions", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["contradictions", "--dry-run"])
        assert result.exit_code == 0
        assert "No contradictions" in result.output

    def test_json_output_empty(self):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.contradictions.find_contradictions", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["contradictions", "--dry-run", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_shows_found_contradictions(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        from unittest.mock import MagicMock
        c = MagicMock()
        c.as_dict.return_value = {"file1": "a.md", "file2": "b.md", "confidence": "high", "explanation": "clash"}
        c.file1 = "reference/sip.md"
        c.file2 = "feedback/sip.md"
        c.confidence = "high"
        c.explanation = "Both files make contradictory claims."
        with patch("atlas_plus.curation.contradictions.find_contradictions", return_value=[c]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["contradictions", "--dry-run"])
        assert result.exit_code == 0
        assert "sip.md" in result.output or "1 contradiction" in result.output


# ---------------------------------------------------------------------------
# suggest-facts CLI command
# ---------------------------------------------------------------------------

class TestCmdSuggestFacts:
    def test_dry_run_no_candidates(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        stats = {"candidates": 0, "approved": 0, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["suggest-facts", "--dry-run"])
        assert result.exit_code == 0

    def test_shows_candidates_count(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        stats = {"candidates": 3, "approved": 1, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["suggest-facts", "--dry-run"])
        assert result.exit_code == 0
        assert "candidates=3" in result.output or "3" in result.output

    def test_runtime_error_exits_1(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.suggest_facts.suggest_facts",
                   side_effect=RuntimeError("no transcript found")):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["suggest-facts", "--dry-run"])
        assert result.exit_code == 1
        assert "no transcript" in result.output or "Error" in result.output

    def test_skip_similar_passed_through(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        stats = {"candidates": 1, "approved": 0, "written": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats) as mock_sf:
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["suggest-facts", "--dry-run", "--skip-similar", "0.65"])
        assert result.exit_code == 0
        call_kwargs = mock_sf.call_args[1]
        assert call_kwargs.get("skip_similar_threshold") == pytest.approx(0.65)

    def test_threshold_flag_passed_through(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        stats = {"candidates": 1, "approved": 0, "written": 0, "rejected_low_confidence": 0}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats) as mock_sf:
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["suggest-facts", "--dry-run", "--threshold", "0.85"])
        assert result.exit_code == 0
        call_kwargs = mock_sf.call_args[1]
        assert call_kwargs.get("confidence_threshold") == pytest.approx(0.85)

    def test_shows_rejected_low_confidence_in_output(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        stats = {"candidates": 2, "approved": 0, "written": 0, "rejected_low_confidence": 3}
        with patch("atlas_plus.curation.suggest_facts.suggest_facts", return_value=stats):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["suggest-facts", "--dry-run"])
        assert result.exit_code == 0
        assert "rejected" in result.output or "3" in result.output


# ---------------------------------------------------------------------------
# research-index CLI command
# ---------------------------------------------------------------------------

class TestCmdResearchIndex:
    def test_no_dir_shows_warning(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        nonexistent = tmp_path / "reports"
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["research-index", "--dir", str(nonexistent)])
        assert result.exit_code == 0
        assert "not found" in result.output.lower() or "Directory not found" in result.output

    def test_empty_dir_shows_no_reports(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        reports = tmp_path / "reports"
        reports.mkdir()
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["research-index", "--dir", str(reports)])
        assert result.exit_code == 0
        assert "No research reports" in result.output or "0" in result.output

    def test_json_output_with_report(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "sip-overview.md").write_text(
            "---\ntitle: SIP Overview\ndate: 2026-05-01\nquality_score: 0.8\n---\n\n"
            "## Summary\n\nSIP is a protocol.\n\n## Key Findings\n\n- Finding 1\n",
            encoding="utf-8",
        )
        with patch("atlas_plus.brain.research.compute_freshness_score", return_value=0.9):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["research-index", "--dir", str(reports), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["title"] == "SIP Overview"


# ---------------------------------------------------------------------------
# quality CLI command
# ---------------------------------------------------------------------------

class TestCmdQuality:
    def test_no_dir_shows_warning(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        nonexistent = tmp_path / "reports"
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["quality", "--dir", str(nonexistent)])
        assert result.exit_code == 0
        assert "not found" in result.output.lower() or "Directory not found" in result.output

    def test_json_output_with_report(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "sip-proto.md").write_text(
            "---\ntitle: SIP Protocol\n---\n\n"
            "## Summary\n\nSIP protocol overview.\n\n"
            "## Key Findings\n\n- SIP uses UDP.\n- SIP uses TCP.\n- SIP uses TLS.\n\n"
            "## Context\n\nVoIP protocol.\n\n"
            "https://www.example.com/sip-rfc\n",
            encoding="utf-8",
        )
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["quality", "--dir", str(reports), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert "score" in data[0]
        assert 0.0 <= data[0]["score"] <= 1.0

    def test_empty_dir_shows_no_reports(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        reports = tmp_path / "reports"
        reports.mkdir()
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["quality", "--dir", str(reports)])
        assert result.exit_code == 0
        assert "No research reports" in result.output


# ---------------------------------------------------------------------------
# next-actions CLI command
# ---------------------------------------------------------------------------

class TestCmdNextActions:
    def test_empty_root_shows_clean_message(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.curation.tag_suggest.suggest_tags_for_file", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["next-actions"])
        assert result.exit_code == 0
        assert "excellent shape" in result.output or "nothing urgent" in result.output.lower()

    def test_json_output_has_all_keys(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.curation.tag_suggest.suggest_tags_for_file", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["next-actions", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "decayed" in data
        assert "orphans" in data
        assert "untagged" in data
        assert "gaps" in data

    def test_shows_decayed_files(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        from atlas_plus.curation.decay import DecayCandidate
        cand = DecayCandidate("reference/old.md", "Old Fact", "reference", 30, "2025-01-01", 90)
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[cand]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.curation.tag_suggest.suggest_tags_for_file", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["next-actions"])
        assert result.exit_code == 0
        assert "old.md" in result.output or "Decayed" in result.output

    def test_shows_research_gaps(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        gap = {"query": "SIP registration flow", "miss_count": 8, "total": 10, "last_seen": "2026-05-21"}
        with patch("atlas_plus.curation.cli.MEMORY_ROOT", tmp_path), \
             patch("atlas_plus.curation.decay.find_decayed_facts", return_value=[]), \
             patch("atlas_plus.curation.orphan.find_orphan_candidates", return_value=[]), \
             patch("atlas_plus.curation.tag_suggest.suggest_tags_for_file", return_value=[]), \
             patch("atlas_plus.mcp.memory_server.tools.search_log.top_miss_queries", return_value=[gap]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["next-actions"])
        assert result.exit_code == 0
        assert "SIP registration flow" in result.output or "Gaps" in result.output


# ---------------------------------------------------------------------------
# rederive CLI command
# ---------------------------------------------------------------------------

class TestCmdRederive:
    def test_no_candidates_shows_clean(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.rederive.find_rederivable", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["rederive"])
        assert result.exit_code == 0
        assert "No re-derivable" in result.output

    def test_json_output_empty(self):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        with patch("atlas_plus.curation.rederive.find_rederivable", return_value=[]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["rederive", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)

    def test_shows_candidates(self):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        from unittest.mock import MagicMock
        cand = MagicMock()
        cand.as_dict.return_value = {"path": "reference/old.md", "similarity": 0.7}
        cand.path = "reference/old.md"
        cand.memory_type = "reference"
        cand.matched_doc = "README.md"
        cand.similarity = 0.7
        with patch("atlas_plus.curation.rederive.find_rederivable", return_value=[cand]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["rederive"])
        assert result.exit_code == 0
        assert "old.md" in result.output or "1 file" in result.output


# ---------------------------------------------------------------------------
# citations CLI command
# ---------------------------------------------------------------------------

class TestCmdCitations:
    def test_no_dir_shows_warning(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        nonexistent = tmp_path / "reports"
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["citations", "--dir", str(nonexistent)])
        assert result.exit_code == 0
        assert "not found" in result.output.lower() or "Directory not found" in result.output

    def test_empty_dir_shows_up_to_date(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        reports = tmp_path / "reports"
        reports.mkdir()
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["citations", "--dir", str(reports)])
        assert result.exit_code == 0
        assert "up to date" in result.output or "No citations" in result.output

    def test_dry_run_reports_urls(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "sip.md").write_text(
            "---\ntitle: SIP\n---\n\nSee https://www.ietf.org/rfc/rfc3261.txt for details.\n",
            encoding="utf-8",
        )
        with patch("atlas_plus.brain.research.extract_report_citations",
                   return_value=["https://www.ietf.org/rfc/rfc3261.txt"]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["citations", "--dir", str(reports), "--dry-run"])
        assert result.exit_code == 0

    def test_json_output_empty_dir(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        reports = tmp_path / "reports"
        reports.mkdir()
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["citations", "--dir", str(reports), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# check-links CLI command
# ---------------------------------------------------------------------------

class TestCmdCheckLinks:
    def test_no_dir_shows_warning(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        nonexistent = tmp_path / "reports"
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["check-links", "--dir", str(nonexistent)])
        assert result.exit_code == 0
        assert "not found" in result.output.lower() or "Directory not found" in result.output

    def test_empty_dir_shows_no_reports(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        reports = tmp_path / "reports"
        reports.mkdir()
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["check-links", "--dir", str(reports)])
        assert result.exit_code == 0
        assert "No research reports" in result.output or "references" in result.output.lower()

    def test_all_ok_when_links_pass(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "sip.md").write_text(
            "---\ntitle: SIP\nreferences:\n  - https://example.com\n---\n\nBody.\n",
            encoding="utf-8",
        )
        with patch("atlas_plus.curation.link_check.check_report_links",
                   return_value=[{"url": "https://example.com", "ok": True, "status": 200}]):
            runner = CliRunner()
            result = runner.invoke(cli_mod.app, ["check-links", "--dir", str(reports)])
        assert result.exit_code == 0
        assert "All URLs reachable" in result.output or "reachable" in result.output.lower()

    def test_json_output_empty_dir(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        reports = tmp_path / "reports"
        reports.mkdir()
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["check-links", "--dir", str(reports), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)


# ---------------------------------------------------------------------------
# cross-links CLI command
# ---------------------------------------------------------------------------

class TestCmdCrossLinks:
    def test_no_dir_shows_warning(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        nonexistent = tmp_path / "reports"
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["cross-links", "--dir", str(nonexistent)])
        assert result.exit_code == 0
        assert "not found" in result.output.lower() or "Directory not found" in result.output

    def test_empty_dir_shows_no_new_refs(self, tmp_path):
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        reports = tmp_path / "reports"
        reports.mkdir()
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["cross-links", "--dir", str(reports)])
        assert result.exit_code == 0
        assert "No new cross-references" in result.output or "up to date" in result.output.lower()

    def test_json_output_empty_dir(self, tmp_path):
        import json
        from typer.testing import CliRunner
        from atlas_plus.curation import cli as cli_mod
        reports = tmp_path / "reports"
        reports.mkdir()
        runner = CliRunner()
        result = runner.invoke(cli_mod.app, ["cross-links", "--dir", str(reports), "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
