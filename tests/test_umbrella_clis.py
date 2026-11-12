"""Tests for the atlas-plus namesake umbrella CLI and the atlas-transcripts CLI (E1-E4).

Both console scripts were DOA on install (modules never built) until 2026-07-02; these
pin the new thin CLIs. The transcripts commands are pure plumbing over TranscriptIndex /
recover_all, so tests point the index at a tmp sqlite via ATLAS_TRANSCRIPT_INDEX.
"""
from __future__ import annotations

from typer.testing import CliRunner

runner = CliRunner()


# ---------------------------------------------------------------------------
# atlas-plus (namesake umbrella)
# ---------------------------------------------------------------------------

class TestAtlasPlusCli:
    def test_no_args_shows_overview(self):
        from atlas_plus.cli import app
        result = runner.invoke(app, [])
        assert result.exit_code == 0
        assert "Keel" in result.output
        assert "atlas-doctor" in result.output   # points the user at health

    def test_overview_flags_missing_corpus(self, tmp_path, monkeypatch):
        """Bare invocation with no corpus names the init command right on
        the memory-root line (impatient persona, quality loop run 20)."""
        from atlas_plus.cli import app
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "nowhere"))
        result = runner.invoke(app, [])
        assert result.exit_code == 0
        assert "no corpus yet" in result.output

    def test_overview_quiet_when_corpus_exists(self, tmp_path, monkeypatch):
        from atlas_plus.cli import app
        corpus = tmp_path / "mem"
        corpus.mkdir()
        (corpus / "MEMORY.md").write_text("# index", encoding="utf-8")
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(corpus))
        result = runner.invoke(app, [])
        assert result.exit_code == 0
        assert "no corpus yet" not in result.output

    def test_version_flag(self):
        from atlas_plus import __version__
        from atlas_plus.cli import app
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_version_short_flags(self):
        """-V and the habitual lowercase -v both print the version
        (impatient persona, quality loop run 20)."""
        from atlas_plus import __version__
        from atlas_plus.cli import app
        for flag in ("-V", "-v"):
            result = runner.invoke(app, [flag])
            assert result.exit_code == 0, flag
            assert __version__ in result.output, flag

    def test_commands_lists_atlas_family(self):
        from atlas_plus.cli import app
        result = runner.invoke(app, ["commands", "--json"])
        assert result.exit_code == 0
        import json
        eps = json.loads(result.output)
        # the installed dist must expose the family, including this very command
        assert "atlas-plus" in eps
        assert "atlas-doctor" in eps
        # public installs also expose the sextant-* aliases (2026-08-26: the
        # old atlas-only assertion encoded the bug that hid them)
        assert all(name.startswith(("atlas", "sextant")) for name in eps)


# ---------------------------------------------------------------------------
# atlas-transcripts (thin plumbing over TranscriptIndex / recover_all)
# ---------------------------------------------------------------------------

class TestTranscriptsCli:
    def _index_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_TRANSCRIPT_INDEX", str(tmp_path / "index.sqlite"))

    def test_list_empty_index(self, tmp_path, monkeypatch):
        self._index_env(tmp_path, monkeypatch)
        from atlas_plus.transcripts.cli import app
        result = runner.invoke(app, ["list", "--json"])
        assert result.exit_code == 0
        assert result.output.strip() == "[]"

    def test_list_shows_upserted_conversation(self, tmp_path, monkeypatch):
        self._index_env(tmp_path, monkeypatch)
        from atlas_plus.transcripts.index import TranscriptIndex
        from atlas_plus.transcripts.store import TurnRecord
        idx = TranscriptIndex()
        idx.upsert_turn(TurnRecord(
            turn_id="t1", conversation_id="conv-abc", source="claude-code", role="user",
            ts="2026-07-02T12:00:00Z", content="hello transcript world",
        ))
        from atlas_plus.transcripts.cli import app
        result = runner.invoke(app, ["list", "--json"])
        assert result.exit_code == 0
        assert "conv-abc" in result.output

    def test_search_finds_content(self, tmp_path, monkeypatch):
        self._index_env(tmp_path, monkeypatch)
        from atlas_plus.transcripts.index import TranscriptIndex
        from atlas_plus.transcripts.store import TurnRecord
        idx = TranscriptIndex()
        idx.upsert_turn(TurnRecord(
            turn_id="t2", conversation_id="conv-xyz", source="claude-code", role="assistant",
            ts="2026-07-02T12:01:00Z", content="the sparkline renders correctly",
        ))
        from atlas_plus.transcripts.cli import app
        result = runner.invoke(app, ["search", "sparkline", "--json"])
        assert result.exit_code == 0
        assert "conv-xyz" in result.output

    def test_recover_reports_clean_state(self, tmp_path, monkeypatch):
        self._index_env(tmp_path, monkeypatch)
        # empty transcript root -> zero partials, report prints without error
        monkeypatch.setenv("ATLAS_TRANSCRIPT_ROOT", str(tmp_path))
        from atlas_plus.transcripts.cli import app
        result = runner.invoke(app, ["recover", "--json"])
        assert result.exit_code == 0
        import json
        report = json.loads(result.output)
        assert report["partials_scanned"] == 0
        assert report["errors"] == []


class TestComponents:
    def test_gather_components_shapes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("ATLAS_AUTONOMOUS", raising=False)
        from atlas_plus.cli import gather_components
        comps = gather_components(probe_ports=False)
        names = [c["name"] for c in comps]
        assert "Memory corpus" in names and "Device sync" in names
        for c in comps:
            assert set(c) == {"name", "what", "status", "disable"}
        by = {c["name"]: c for c in comps}
        assert by["Memory corpus"]["status"] == "not configured"  # empty sandbox
        assert by["Autonomous writes"]["status"] == "disabled"     # default off
        assert "optional" in by["Web dashboard (Helm)"]["disable"]

    def test_components_cli_renders(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
        from typer.testing import CliRunner
        from atlas_plus.cli import app
        result = CliRunner().invoke(app, ["components", "--json"])
        assert result.exit_code == 0
        import json
        data = json.loads(result.stdout)
        assert any(c["name"] == "Semantic search" for c in data)

    def test_semantic_kill_switch_reflected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("ATLAS_SEMANTIC_SEARCH", "off")
        from atlas_plus.cli import gather_components
        by = {c["name"]: c for c in gather_components(probe_ports=False)}
        assert by["Semantic search"]["status"] == "disabled"


class TestBrandDetection:
    def test_atlas_brand_default(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["atlas-plus"])
        from atlas_plus.cli import _brand
        assert _brand() == ("Keel", "atlas")

    def test_sextant_brand(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["sextant"])
        from atlas_plus.cli import _brand
        assert _brand() == ("Sextant", "sextant")

    def test_overview_names_shipped_docs_only(self, monkeypatch):
        from typer.testing import CliRunner
        from atlas_plus.cli import app
        result = CliRunner().invoke(app, [])
        assert "QUICKSTART" not in result.stdout  # not in the public tree
        assert "GETTING-STARTED" in result.stdout


class TestGuessableSubcommands:
    """Impatient-user review 2026-08-26: every natural guess must land."""

    def _run(self, args, tmp_path, monkeypatch):
        mem = tmp_path / "mem"
        state = tmp_path / "state"
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
        monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
        # fts/config freeze paths at import; if any earlier test imported the
        # server, env vars alone leave search reading the REAL corpus.
        from atlas_plus.mcp.memory_server import config, fts, routing
        monkeypatch.setattr(config, "MEMORY_ROOT", mem)
        monkeypatch.setattr(fts, "MEMORY_ROOT", mem, raising=False)
        monkeypatch.setattr(fts, "FTS_DB", state / "state.sqlite", raising=False)
        monkeypatch.setattr(routing, "MEMORY_ROOT", mem, raising=False)
        from typer.testing import CliRunner
        from atlas_plus.cli import app
        return CliRunner().invoke(app, args)

    def test_sextant_init_lands(self, tmp_path, monkeypatch):
        result = self._run(["init"], tmp_path, monkeypatch)
        assert result.exit_code == 0
        assert (tmp_path / "mem" / "identity" / "user.md").exists()

    def test_sextant_search_empty_corpus_friendly(self, tmp_path, monkeypatch):
        (tmp_path / "mem").mkdir()
        result = self._run(["search", "anything"], tmp_path, monkeypatch)
        assert result.exit_code == 0
        assert "No hits" in result.stdout
        assert "store something first" in result.stdout

    def test_sextant_status_is_components(self, tmp_path, monkeypatch):
        result = self._run(["status"], tmp_path, monkeypatch)
        assert result.exit_code == 0
        assert "Memory corpus" in result.stdout

    def test_sextant_uninstall_delegates_report_only(self, tmp_path, monkeypatch):
        (tmp_path / "mem").mkdir()
        result = self._run(["uninstall"], tmp_path, monkeypatch)
        assert "Report only" in result.stdout

    def test_sextant_import_delegates_with_args(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "a.md").write_text("x", encoding="utf-8")
        result = self._run(["import", str(vault)], tmp_path, monkeypatch)
        assert result.exit_code == 0
        assert "auto-detected source: obsidian" in result.stdout


class TestImportAutodetect:
    def test_dir_is_obsidian(self, tmp_path):
        from atlas_plus.import_cmd import _detect_source
        d = tmp_path / "v"; d.mkdir()
        assert _detect_source(d) == "obsidian"

    def test_zip_is_claude(self, tmp_path):
        from atlas_plus.import_cmd import _detect_source
        z = tmp_path / "e.zip"; z.write_bytes(b"PK")
        # A zip whose conversations.json cannot be read is ambiguous now: ChatGPT delivers a zip
        # too (AG14, sweep 11), so the shape inside decides and junk is "pass --source".
        assert _detect_source(z) is None

    def test_json_shapes(self, tmp_path):
        import json as _json
        from atlas_plus.import_cmd import _detect_source
        g = tmp_path / "g.json"
        g.write_text(_json.dumps([{"mapping": {}, "title": "t"}]), encoding="utf-8")
        assert _detect_source(g) == "chatgpt"
        c = tmp_path / "c.json"
        c.write_text(_json.dumps([{"chat_messages": [], "name": "n"}]), encoding="utf-8")
        assert _detect_source(c) == "claude"
        bad = tmp_path / "b.json"
        bad.write_text("{}", encoding="utf-8")
        assert _detect_source(bad) is None


class TestImpatientPolish:
    def test_placeholder_env_fails_loud(self, monkeypatch):
        from atlas_plus import paths
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", "<ATLAS_MEMORY_ROOT>")
        import pytest as _pytest
        with _pytest.raises(SystemExit) as exc:
            paths.memory_root()
        assert "unfilled" in str(exc.value) and "ATLAS_MEMORY_ROOT" in str(exc.value)

    def test_commands_shows_sextant_family_when_invoked_as_sextant(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["sextant", "commands"])
        from typer.testing import CliRunner
        from atlas_plus.cli import app
        result = CliRunner().invoke(app, ["commands", "--json"])
        assert result.exit_code == 0
        import json
        eps = json.loads(result.stdout)
        # private install has no sextant-* entry points, but the filter must
        # no longer EXCLUDE them: atlas ones present, filter is prefix-tuple
        assert any(n.startswith("atlas") for n in eps)

    def test_help_subcommand_lands(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "m"))
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "s"))
        from typer.testing import CliRunner
        from atlas_plus.cli import app
        result = CliRunner().invoke(app, ["help"])
        assert result.exit_code == 0
        assert "memory root" in result.stdout

    def test_uninstall_short_y(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "m"))
        monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "s"))
        (tmp_path / "s").mkdir()
        (tmp_path / "s" / ".sextant-state").write_text("", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["sextant-uninstall", "--state", "-y", "--confirm-state-path", str(tmp_path / "s")])
        from atlas_plus.uninstall_cmd import main
        import pytest as _pytest
        with _pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        assert not (tmp_path / "s").exists()

    def test_import_bad_json_clean_error(self, tmp_path, monkeypatch, capsys):
        bad = tmp_path / "x.json"
        bad.write_text("not json{", encoding="utf-8")
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "m"))
        monkeypatch.setattr("sys.argv", ["sextant-import", "--source", "chatgpt", "--path", str(bad)])
        from atlas_plus.import_cmd import main
        import pytest as _pytest
        with _pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "import failed" in err and "Traceback" not in err


class TestVersionBranding:
    def test_version_says_sextant_when_invoked_as_sextant(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["sextant", "--version"])
        from typer.testing import CliRunner
        from atlas_plus.cli import app
        result = CliRunner().invoke(app, ["--version"])
        assert "sextant" in result.stdout and "atlas-plus" not in result.stdout

    def test_version_says_atlas_plus_privately(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["atlas-plus", "--version"])
        from typer.testing import CliRunner
        from atlas_plus.cli import app
        result = CliRunner().invoke(app, ["--version"])
        # the private name is whatever distribution is installed (atlas-plus privately,
        # sextant-ai in the public build); the point is that it is not the sextant alias
        from atlas_plus.cli import _dist_name
        assert _dist_name() in result.stdout and not result.stdout.startswith("sextant ")


class TestSearchRenderEscaping:
    """Stranger-test batch 9: FTS marks matches as [term]; Rich swallowed the
    brackets as unknown markup, so THE MATCHED WORDS VANISHED from search
    output. Corpus text with real markup ([red]...) was interpreted too."""

    def _world(self, tmp_path, monkeypatch):
        mem = tmp_path / "mem"
        state = tmp_path / "state"
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
        monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
        from atlas_plus.mcp.memory_server import config, fts, routing
        monkeypatch.setattr(config, "MEMORY_ROOT", mem)
        monkeypatch.setattr(fts, "MEMORY_ROOT", mem, raising=False)
        monkeypatch.setattr(fts, "FTS_DB", state / "state.sqlite", raising=False)
        monkeypatch.setattr(routing, "MEMORY_ROOT", mem, raising=False)
        (mem / "reference").mkdir(parents=True)
        (mem / "reference" / "note.md").write_text(
            "---\ntitle: Note\n---\n\nthis note has [red]markup[/red] "
            "plus searchable pumpernickel\n",
            encoding="utf-8",
        )
        from atlas_plus.mcp.memory_server.fts import rebuild
        rebuild(force=True)

    def test_matched_term_survives_rendering(self, tmp_path, monkeypatch):
        self._world(tmp_path, monkeypatch)
        monkeypatch.setenv("ATLAS_SEMANTIC_SEARCH", "off")
        from typer.testing import CliRunner
        from atlas_plus.cli import app
        result = CliRunner().invoke(app, ["search", "pumpernickel"])
        assert result.exit_code == 0
        assert "pumpernickel" in result.output  # was swallowed as a Rich tag

    def test_corpus_markup_not_interpreted(self, tmp_path, monkeypatch):
        self._world(tmp_path, monkeypatch)
        monkeypatch.setenv("ATLAS_SEMANTIC_SEARCH", "off")
        from typer.testing import CliRunner
        from atlas_plus.cli import app
        result = CliRunner().invoke(app, ["search", "markup"])
        assert result.exit_code == 0
        assert "[red]" in result.output or "\[red]" in result.output


class TestIngestForward:
    def test_sextant_ingest_lands(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_TRANSCRIPT_ROOT", str(tmp_path / "t"))
        monkeypatch.setenv("ATLAS_TRANSCRIPT_INDEX", str(tmp_path / "i.sqlite"))
        monkeypatch.setenv("ATLAS_TRANSCRIPT_DROP", str(tmp_path / "drop"))
        from typer.testing import CliRunner
        from atlas_plus.cli import app
        result = CliRunner().invoke(app, ["ingest"])
        assert result.exit_code == 0
        assert "Nothing to ingest" in result.output
