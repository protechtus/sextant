"""Claims under test (persona-audit retrospective D2): prose that can drift, doesn't.

Every doc defect the three-persona audit found was a TRUE statement that
stopped being true (tool counts three ways, dead links, stale commands).
These tests make the load-bearing claims fail loudly when reality moves.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

def _public_readme() -> Path:
    """README-PUBLIC.md in the private repo; the public tree ships it as
    root README.md (the old hard path made this SHIPPED test fail in the
    very tree it guards -- Codex audit 2026-09-21)."""
    candidate = REPO / "docs" / "README-PUBLIC.md"
    return candidate if candidate.exists() else REPO / "README.md"

# The public-facing docs whose links/claims matter (subset that ships).
SHIPPED_DOCS = [
    "docs/README-PUBLIC.md",
    "docs/GETTING-STARTED.md",
    "docs/USAGE.md",
    "docs/CONFIGURATION.md",
    "docs/SECURITY-TEST-MATRIX.md",
    "docs/HOWTO-sync.md",
    "docs/HOWTO-self-hosting.md",
    "docs/HOWTO-docker.md",
    "docs/HOWTO-litellm.md",
    # HOWTO-backup quarantined 2026-09-22 (operator rclone runbook, P1-F)
    "docs/ARCHITECTURE.md",
    "docs/ARCHITECTURE-PUBLIC.md",
    "docs/DECAY-AS-BEHAVIOR.md",
    "CONTRIBUTING.md",
    "SUPPORT.md",
    "CLA.md",
]

# Overclaims the 2026-10-07 adversarial verification pass REFUTED against the
# code (competitive-sweep section 13). Each phrase was rewritten to bounded
# wording on 2026-10-08; this gate keeps the refuted phrasing from creeping
# back into any shipped doc. If one of these becomes true (the right-hand
# column of the section-13 ledger ships), delete its row here in the same
# commit that ships it.
REFUTED_PHRASES = [
    ("with a verifier step", "verify is an optional post-run check (ATLAS_VERIFY_TICKETS, default off), not a step in the path"),
    ("before its result is accepted", "no pre-commit gate exists; verify failure reopens the ticket after the fact"),
    ("reverification loops", "the loop is one scheduled pass that reports; plural/unbounded wording overclaims"),
    ("queued for quarantine", "OUTDATED is a list in the run report; nothing is moved or flagged"),
    ("resets-or-quarantines on its own", "clock reset is gated behind ATLAS_REVERIFY_RESET; quarantine is report-only"),
    ("escalates to your phone", "NEEDS_HUMAN is a report list; no routed notification exists"),
]

_LINK_RE = re.compile(r"\]\(([^)#\s]+)(?:#[^)\s]*)?\)")


class TestLinkIntegrity:
    @pytest.mark.parametrize("doc", SHIPPED_DOCS)
    def test_relative_links_resolve(self, doc):
        path = REPO / doc
        if not path.exists():
            pytest.skip(f"{doc} not in this tree")
        broken = []
        for target in _LINK_RE.findall(path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            # README-PUBLIC becomes the repo-root README.md in the public
            # tree, so its relative links resolve from the ROOT there.
            base = REPO if doc == "docs/README-PUBLIC.md" else path.parent
            if not (base / target).exists():
                broken.append(target)
        assert not broken, f"{doc}: dead relative link(s): {broken}"

    def test_shipped_root_readme_links_resolve(self):
        """In the public tree the README ships at the root; check it there."""
        if not (REPO / "docs").is_dir():
            pytest.skip("sdist install: docs/ not shipped, link targets absent by design")
        readme = _public_readme()
        broken = [
            t for t in _LINK_RE.findall(readme.read_text(encoding="utf-8"))
            if not t.startswith(("http://", "https://", "mailto:"))
            and not (REPO / t).exists()
        ]
        assert not broken, f"{readme.name}: dead relative link(s): {broken}"


class TestBoundedClaims:
    """Shipped prose stays inside what the code does (claims-rooted-in-fact)."""

    @pytest.mark.parametrize("doc", SHIPPED_DOCS)
    def test_no_refuted_phrasing_in_shipped_docs(self, doc):
        path = REPO / doc
        if not path.exists():
            pytest.skip(f"{doc} not in this tree")
        text = path.read_text(encoding="utf-8").lower()
        hits = [
            f"{phrase!r} ({why})"
            for phrase, why in REFUTED_PHRASES
            if phrase.lower() in text
        ]
        assert not hits, f"{doc} reasserts refuted claim(s): {hits}"


class TestToolCountClaim:
    def test_readme_tool_count_matches_registered(self):
        """'30 MCP tools' must equal what the server actually registers."""
        import asyncio

        from atlas_plus.mcp.memory_server.server import mcp

        readme_path = _public_readme()
        if not readme_path.exists():
            pytest.skip("sdist install: README not alongside tests")
        registered = len(asyncio.new_event_loop().run_until_complete(mcp.list_tools()))
        readme = readme_path.read_text(encoding="utf-8")
        claims = {int(m) for m in re.findall(r"(\d+) (?:MCP )?tools", readme)}
        assert claims == {registered}, (
            f"README claims {sorted(claims)} tools; server registers {registered}. "
            "Update every claim site (README-PUBLIC, USAGE, GETTING-STARTED)."
        )

    def test_getting_started_count_matches(self):
        import asyncio

        from atlas_plus.mcp.memory_server.server import mcp

        gs = REPO / "docs" / "GETTING-STARTED.md"
        if not gs.exists():
            pytest.skip("sdist install: docs/ not shipped")
        registered = len(asyncio.new_event_loop().run_until_complete(mcp.list_tools()))
        text = gs.read_text(encoding="utf-8")
        for m in re.findall(r"(\d+) memory tools", text):
            assert int(m) == registered, (
                f"GETTING-STARTED claims {m} tools; server registers {registered}"
            )


class TestAliasTableClaim:
    def test_readme_alias_table_matches_builder(self):
        """Every sextant-* row in the README table must exist in the builder's
        alias list (and vice versa for user-facing aliases)."""
        builder = REPO / "scripts" / "build_public_repo.py"
        if not builder.exists():
            pytest.skip("builder is private-only")
        import importlib.util

        spec = importlib.util.spec_from_file_location("bpr", builder)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        built_aliases = {s for _, s in mod._SEXTANT_ALIASES}

        readme = _public_readme().read_text(encoding="utf-8")
        table_aliases = set(re.findall(r"^\| `(sextant[a-z-]*)` \|", readme, re.M))
        missing_from_table = built_aliases - table_aliases - {"sextant-embed-index", "sextant-import"}
        assert not (table_aliases - built_aliases), (
            f"README table lists aliases the builder does not create: "
            f"{table_aliases - built_aliases}"
        )
        # Aliases absent from the table are allowed (table is 'user-facing'),
        # but flag drift when the table falls far behind:
        assert len(missing_from_table) <= 2, (
            f"README alias table is missing builder aliases: {missing_from_table}"
        )


class TestConfigurationReference:
    """docs/CONFIGURATION.md must catalog every env var the source reads
    (loop run 7, 2026-09-23). The gate scans the same read-patterns the
    inventory script used, so a new `environ.get("ATLAS_X")` cannot ship
    undocumented.

    2026-09-27: the trading module is PRIVATE (mirror-excluded), so its env
    vars are documented privately instead -- putting
    them in CONFIGURATION.md would leak the module's existence into the
    public build (CONFIGURATION.md is `!`-re-included there). The gate
    accepts both docs; in the public tree the trading source and its doc
    are both absent, so the invariant holds unchanged there."""

    _READ_RE = re.compile(
        r"(?:environ\.get|getenv|environ\[|_env_path)\s*\(?\s*['\"]([A-Z][A-Z0-9_]+)['\"]"
    )

    def test_every_env_read_is_documented(self):
        doc_path = REPO / "docs" / "CONFIGURATION.md"
        if not doc_path.exists():
            pytest.skip("sdist install: docs/ not shipped")
        doc = doc_path.read_text(encoding="utf-8")
        trading_doc = REPO / "docs" / "TRADING-OPERATIONS.md"
        if trading_doc.exists():
            doc += trading_doc.read_text(encoding="utf-8")
        src_root = REPO / "src" / "atlas_plus"
        # Private modules (excluded from the public build) document their env vars
        # privately; the public CONFIGURATION.md must not name them (2026-11-09).
        gate = REPO / "scripts" / "release_gate.py"
        if gate.exists():
            import importlib.util
            spec = importlib.util.spec_from_file_location("_release_gate", gate)
            rg = importlib.util.module_from_spec(spec); spec.loader.exec_module(rg)
            rules = rg.load_exclude_rules()
            def _private(p):
                return rg.is_excluded(p.relative_to(REPO).as_posix(), rules)
        else:
            def _private(p):
                return False
        undocumented = set()
        for py in src_root.rglob("*.py"):
            if _private(py):
                continue
            for name in self._READ_RE.findall(py.read_text(encoding="utf-8", errors="replace")):
                if name in doc:
                    continue
                undocumented.add(name)
        assert not undocumented, (
            f"env vars read in src but missing from docs/CONFIGURATION.md: "
            f"{sorted(undocumented)}"
        )

    def test_model_role_family_documented(self):
        doc_path = REPO / "docs" / "CONFIGURATION.md"
        if not doc_path.exists():
            pytest.skip("sdist install: docs/ not shipped")
        doc = doc_path.read_text(encoding="utf-8")
        # The dynamic f-string family the literal scan cannot see.
        assert "ATLAS_MODEL_<ROLE>_PROVIDER" in doc
        assert "ATLAS_FAST_MODEL" in doc


class TestNoRotProneCounts:
    """Polish (quality loop run 8): precise 'N tests / N passed' claims rot
    the moment the suite grows -- ARCHITECTURE.md shipped '3189+ passed'
    while the suite stood at 4,206. Shipped docs must not carry them;
    CHANGELOG is exempt (its numbers are dated history)."""

    # One optional intervening word: "3,900+ automated tests" slipped past
    # the original pattern (everyman pass, quality loop run 12).
    _COUNT_RE = re.compile(r"\b\d[\d,]{2,}\+?\s+(?:\w+\s+)?(?:tests\b|passed\b)")

    def test_shipped_docs_carry_no_test_count_claims(self):
        offenders = []
        for doc in SHIPPED_DOCS:
            path = REPO / doc
            if not path.exists():
                continue
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if self._COUNT_RE.search(line):
                    offenders.append(f"{doc}:{i}: {line.strip()[:70]}")
        assert not offenders, (
            "rot-prone test-count claim(s) in shipped docs (state 'the full "
            f"suite' or point at CI instead): {offenders}"
        )


class TestNoPrivateDocPointers:
    """User-facing strings must not reference the maintainer's PRIVATE
    runbooks (quality loop run 29: sextant-backup verify said 'See
    .memory/runbooks/backup-crypt-migration.md' -- a path no public user
    has; the public HOWTO-backup is deliberately quarantined). Generic
    mentions of the runbooks DIRECTORY (the user's own corpus feature)
    stay legal; referencing a specific private .md file does not."""

    def test_no_string_references_specific_private_runbook(self):
        import ast
        src_root = REPO / "src" / "atlas_plus"
        offenders = []
        for py in src_root.rglob("*.py"):
            try:
                tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if re.search(r"\.memory[/\\]runbooks[/\\][\w-]+\.md", node.value):
                        offenders.append(f"{py.relative_to(src_root)}:{node.lineno}")
        assert not offenders, (
            "string constant references a private runbook file (point users "
            f"at shipped docs or upstream docs instead): {offenders}"
        )


class TestSecurityMatrixHonest:
    """Every test file the security matrix cites must exist -- a renamed or
    deleted test would silently orphan a public security claim."""

    def test_cited_test_files_exist(self):
        doc = REPO / "docs" / "SECURITY-TEST-MATRIX.md"
        if not doc.exists():
            pytest.skip("matrix not in this tree")
        cited = set(re.findall(r"tests/test_[a-z_]+\.py", doc.read_text(encoding="utf-8")))
        assert cited, "matrix cites no test files?"
        missing = [c for c in cited if not (REPO / c).exists()]
        assert not missing, f"matrix cites nonexistent test files: {missing}"


class TestArchitectureTablesClaim:
    """The ARCHITECTURE.md inventories drifted silently for 5 months (25 vs 30
    tools, 17 vs 30 executor kinds -- P08 contributor persona, 2026-10-16).
    Hold both tables to the registries so they fail loudly next time."""

    def _arch_text(self) -> str:
        p = REPO / "docs" / "ARCHITECTURE.md"
        if not p.exists():
            pytest.skip("sdist install: docs/ not shipped")
        return p.read_text(encoding="utf-8")

    def test_tool_inventory_matches_registered(self):
        import asyncio

        from atlas_plus.mcp.memory_server.server import mcp

        section = self._arch_text().split("## MCP Tool Inventory")[1].split("\n## ")[0]
        doc_tools = set(re.findall(r"^\| `(\w+)` \|", section, re.M))
        registered = {
            t.name
            for t in asyncio.new_event_loop().run_until_complete(mcp.list_tools())
        }
        assert doc_tools == registered, (
            f"ARCHITECTURE tool table drifted. missing={sorted(registered - doc_tools)} "
            f"stale={sorted(doc_tools - registered)}"
        )

    def test_executor_kinds_match_registry(self):
        from atlas_plus.brain.executors import _EXECUTORS

        kinds = set(_EXECUTORS)
        try:  # operator machines register private extras; the doc covers the public set
            from atlas_plus.brain.executors_local import LOCAL_EXECUTORS

            kinds -= set(LOCAL_EXECUTORS)
        except ImportError:
            pass
        section = self._arch_text().split("### Executor Kinds")[1].split("\n## ")[0]
        doc_kinds: set[str] = set()
        for line in section.splitlines():
            m = re.match(r"\|([^|]+)\|", line)
            if m:
                doc_kinds.update(re.findall(r"`([a-z_]+)`", m.group(1)))
        assert doc_kinds == kinds, (
            f"ARCHITECTURE executor table drifted. missing={sorted(kinds - doc_kinds)} "
            f"stale={sorted(doc_kinds - kinds)}"
        )
