"""AG111 (sweep 32, ticket SEX-044): what was left of the PyPI stranger pass.

- `sextant upgrade` named no upgrade line (finding 2b): it now redirects like `sextant update`.
- The untouched init identity template came back as a search hit (finding 6f): its instructions are
  not a memory, so the keyword index and the embeddings skip that body until the user edits it.
- The README named repository-only files and clone-only commands to a pip user (findings 3, 4, 6d).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _readme() -> str:
    p = REPO / "docs" / "README-PUBLIC.md"   # the private tree keeps the public README here (G10)
    if not p.exists():
        p = REPO / "README.md"
    return p.read_text(encoding="utf-8")


def _run_cli(monkeypatch, argv: list[str]) -> int:
    from atlas_plus import cli

    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys.modules["__main__"], "__spec__", None, raising=False)
    with pytest.raises(SystemExit) as exc:
        rv = cli.main()
        raise SystemExit(rv)
    code = exc.value.code
    return 0 if code is None else (code if isinstance(code, int) else 1)


def test_upgrade_names_the_upgrade_line_like_update(monkeypatch, capsys):
    from atlas_plus import _next_step

    rc = _run_cli(monkeypatch, ["sextant", "upgrade"])
    out, err = capsys.readouterr()
    flat = " ".join(err.split())
    assert rc == 2 and out.strip() == ""
    assert "No such command 'upgrade'" in flat
    assert "To upgrade the package:" in flat
    assert " ".join(_next_step.upgrade_line().split()) in flat


def test_upgrade_with_arguments_still_redirects(monkeypatch, capsys):
    rc = _run_cli(monkeypatch, ["sextant", "upgrade", "--pre"])
    err = " ".join(capsys.readouterr().err.split())
    assert rc == 2 and "To upgrade the package:" in err and "--pre" in err


@pytest.fixture
def fresh_corpus(tmp_path, monkeypatch):
    mem, state = tmp_path / "ws" / ".memory", tmp_path / "ws" / ".atlas"
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(state))
    monkeypatch.setenv("ATLAS_EMBED_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ATLAS_OLLAMA_URL", "http://127.0.0.1:9")
    from atlas_plus import init_cmd
    from atlas_plus.mcp.memory_server import fts

    init_cmd.run_init()
    assert (mem / "identity" / "user.md").is_file()
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "FTS_DB", state / "ag111-fts.sqlite")
    fts.mark_dirty()
    return mem


def test_init_template_instructions_are_not_a_search_hit(fresh_corpus):
    from atlas_plus.mcp.memory_server import fts

    fts.rebuild(force=True)
    assert fts.search("collaborator") == []
    assert fts.search("Introduce yourself") == []


def test_an_edited_identity_note_is_searchable_again(fresh_corpus):
    from atlas_plus.mcp.memory_server import fts

    user = fresh_corpus / "identity" / "user.md"
    user.write_text(user.read_text(encoding="utf-8") + "\nI prefer a collaborator who writes tests first.\n",
                    encoding="utf-8")
    fts.rebuild(force=True)
    hits = fts.search("collaborator")
    assert [h["path"] for h in hits] == ["identity/user.md"]


def test_a_fact_appended_under_the_template_is_found_without_the_instructions(fresh_corpus):
    from atlas_plus.mcp.memory_server import fts

    user = fresh_corpus / "identity" / "user.md"
    user.write_text(user.read_text(encoding="utf-8") + "\nGoes by Sam; on call for the payments team.\n",
                    encoding="utf-8")   # what `remember --scope identity` does: append under the template
    fts.rebuild(force=True)
    assert [h["path"] for h in fts.search("payments")] == ["identity/user.md"]
    assert fts.search("collaborator") == []


def test_template_with_crlf_and_bom_is_still_the_template(tmp_path):
    from atlas_plus.init_cmd import _IDENTITY_TEMPLATE
    from atlas_plus.mcp.memory_server import embeddings, fts

    p = tmp_path / "user.md"
    p.write_bytes(b"\xef\xbb\xbf" + _IDENTITY_TEMPLATE.replace("\n", "\r\n").encode("utf-8"))
    raw = p.read_text(encoding="utf-8")
    assert fts.is_untouched_scaffold(raw)
    assert fts._parse_md(raw)[1] == ""
    assert embeddings._body_of(p) == ""   # the sibling path: the semantic index skips it too
    p.write_text(_IDENTITY_TEMPLATE + "\nCurrent focus: the billing rewrite.\n", encoding="utf-8")
    assert not fts.is_untouched_scaffold(p.read_text(encoding="utf-8"))
    assert "billing rewrite" in embeddings._body_of(p)


def test_readme_marks_repository_files_and_clone_only_steps_for_a_pip_user():
    text = _readme()
    gh = r"https://github\.com/[\w.-]+/[\w.-]+/blob/main/"   # any owner/repo: the README is the one source
    # every deploy/ file the README names is a link that opens on GitHub (or says it lives in the repository)
    for name in ("deploy/session_start_hook.json", "deploy/stop_hook.json"):
        assert re.search(r"\[`" + re.escape(name) + r"`\]\(" + gh + re.escape(name) + r"\)", text), name
    assert re.search(r"\[`deploy/`\]\(" + gh + r"deploy\) in the repository", text)
    for name in ("docs/ARCHITECTURE.md", "docs/adr"):
        assert re.search(r"\]\(" + gh + re.escape(name) + r"\)", text), name
    assert "GETTING-STARTED section 4 explains" not in text
    # constraints: a pip user gets a URL that pip's -c accepts
    assert re.search(r"-c https://raw\.githubusercontent\.com/[\w.-]+/[\w.-]+/main/constraints\.txt", text)
    # the hand-typed claude mcp add lines and the load-bearing paragraph say they are for a clone
    for m in re.finditer(r"^#.*\$PWD bakes the path in", text, re.M):
        assert m.group(0).startswith("# Clone installs only."), m.group(0)
    assert "Clone installs only: the clone path is load-bearing" in text
    # how the autonomous switch reaches the hook process, with the link
    assert "ATLAS_SESSION_AUTONOMOUS=on" in text
    assert re.search(gh + r"docs/GETTING-STARTED\.md#switching-the-stop-hook-on-and-testing-it-by-hand", text)


def test_readme_anchors_exist_in_getting_started():
    gs = (REPO / "docs" / "GETTING-STARTED.md").read_text(encoding="utf-8")
    headings = re.findall(r"^#+ (.+)$", gs, re.M)
    slugs = {re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-") for h in headings}
    for anchor in re.findall(r"docs/GETTING-STARTED\.md#([a-z0-9-]+)", _readme()):
        assert anchor in slugs, anchor
