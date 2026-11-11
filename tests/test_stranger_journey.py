"""The stranger test, institutionalized (persona-audit retrospective D1).

Walks the ENTIRE first-ten-minutes journey a fresh user takes, against a
fully isolated corpus/state, with default environment (no ATLAS_AUTONOMOUS,
no routing.yaml, no Ollama assumed). CI runs this on a genuinely fresh
machine on every push, on all three OSes -- which makes every push a
stranger install.

History says this is the highest-yield test in the repo: the journey it
encodes was broken TWICE at launch-readiness (interactive writes gated,
zero routing scopes) without any of 4,000 tests noticing, because every
other test opts into operator conditions. This one never does.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def stranger_machine(tmp_path, monkeypatch):
    """A machine that has never seen Sextant: empty corpus root, no env opt-ins."""
    for var in ("ATLAS_AUTONOMOUS", "ATLAS_SESSION_AUTONOMOUS",
                "ATLAS_INTERACTIVE_WRITES", "ATLAS_SEMANTIC_SEARCH",
                "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    mem = tmp_path / "home" / ".memory"
    state = tmp_path / "home" / ".atlas"
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "home"))
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
    from atlas_plus.mcp.memory_server import config, fts, routing, versioning

    monkeypatch.setattr(config, "MEMORY_ROOT", mem)
    monkeypatch.setattr(routing, "MEMORY_ROOT", mem, raising=False)
    monkeypatch.setattr(versioning, "MEMORY_ROOT", mem, raising=False)
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem, raising=False)
    monkeypatch.setattr(fts, "FTS_DB", state / "state.sqlite", raising=False)
    return tmp_path / "home"


class TestStrangerJourney:
    """Each step in the order GETTING-STARTED prescribes. Order-dependent by
    design within each test; tests themselves stay independent."""

    def test_step1_init_scaffolds(self, stranger_machine):
        from atlas_plus.init_cmd import run_init

        result = run_init(dry_run=False)
        assert (stranger_machine / ".memory" / "identity" / "user.md").exists()
        assert result["created"]

    def test_step2_components_answers_whats_here(self, stranger_machine):
        from atlas_plus.cli import gather_components

        comps = {c["name"]: c for c in gather_components(probe_ports=False)}
        assert comps["Memory corpus"]["status"] == "not configured"  # pre-init
        assert "cannot be disabled" in comps["Memory corpus"]["disable"]

    def test_step3_first_remember_works_on_defaults(self, stranger_machine):
        """THE product moment. Broken twice before this test existed."""
        from atlas_plus.init_cmd import run_init
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember

        run_init(dry_run=False)
        result = memory_remember(
            scope="reference:staging",
            fact="my staging server is at 10.0.0.5",
            source="human-direct",
            interactive=True,
        )
        assert result["ok"] is True, result.get("error")
        stored = (stranger_machine / ".memory" / "reference" / "staging.md")
        assert "10.0.0.5" in stored.read_text(encoding="utf-8")

    def test_step4_search_recalls_it(self, stranger_machine):
        from atlas_plus.init_cmd import run_init
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember
        from atlas_plus.mcp.memory_server.tools.search import memory_search_hybrid

        run_init(dry_run=False)
        memory_remember(scope="reference:staging", fact="staging server at 10.0.0.5",
                        source="human-direct", interactive=True)
        results, mode = memory_search_hybrid("staging server")
        assert results and results[0]["path"] == "reference/staging.md"
        assert mode in ("fts-only", "hybrid")  # no Ollama assumed

    def test_step5_search_admits_ignorance(self, stranger_machine):
        from atlas_plus.init_cmd import run_init
        from atlas_plus.mcp.memory_server.tools.search import memory_search_hybrid

        run_init(dry_run=False)
        results, _ = memory_search_hybrid("production database password")
        assert results == []  # honestly unsure, never confidently wrong

    def test_step6_background_writes_stay_gated(self, stranger_machine):
        """The flip side of step 3: consent separation must HOLD on defaults."""
        from atlas_plus.init_cmd import run_init
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember

        run_init(dry_run=False)
        with pytest.raises(RuntimeError):
            memory_remember(scope="reference:x", fact="background",
                            interactive=False)

    def test_step7_uninstall_reports_truthfully(self, stranger_machine):
        from atlas_plus.init_cmd import run_init
        from atlas_plus import uninstall_cmd

        run_init(dry_run=False)
        info = uninstall_cmd.report()
        assert info["corpus"]["exists"] and info["corpus"]["files"] > 0
        assert info["corpus"]["protected"] is False

    def test_step8_reset_leaves_working_skeleton(self, stranger_machine):
        from atlas_plus.init_cmd import run_init
        from atlas_plus import reset_cmd

        run_init(dry_run=False)
        corpus = stranger_machine / ".memory"
        (corpus / "reference").mkdir(exist_ok=True)
        (corpus / "reference" / "junk.md").write_text("debug leftovers", encoding="utf-8")
        outcome = reset_cmd.reset_corpus(yes=True, confirm_path=str(corpus))
        assert outcome.startswith("reset")
        assert (corpus / "identity" / "user.md").exists()
        assert not (corpus / "reference" / "junk.md").exists()

    def test_full_journey_end_to_end(self, stranger_machine):
        """The whole arc in one breath: init -> remember -> recall -> teardown."""
        from atlas_plus.init_cmd import run_init
        from atlas_plus import uninstall_cmd
        from atlas_plus.mcp.memory_server.tools.remember import memory_remember
        from atlas_plus.mcp.memory_server.tools.search import memory_search_hybrid

        run_init(dry_run=False)
        assert memory_remember(scope="preference:editor", fact="tabs never spaces",
                               source="human-direct", interactive=True)["ok"]
        results, _ = memory_search_hybrid("editor tabs")
        assert results
        assert uninstall_cmd.remove_state(yes=True, confirm_path=str(stranger_machine / ".atlas")) in ("removed", "absent")
        outcome = uninstall_cmd.remove_corpus(
            yes=True, confirm_path=str(stranger_machine / ".memory"))
        assert outcome == "removed"
        assert not (stranger_machine / ".memory").exists()
