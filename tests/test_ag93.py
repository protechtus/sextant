"""AG93 (sweep 24 documentation truth scan): doc facts held against the tree they describe.

Each test reads the shipped documents and checks the thing they name exists in this checkout:
relative links resolve inside the repository, `docs/<NAME>.md` mentions name real files, and
the environment variables CONFIGURATION.md says "you may see in source" are read by the source.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _shipped(paths):
    """In the private repo, docs excluded from the public build link to private files on purpose: check what ships."""
    gate = REPO / "scripts" / "release_gate.py"
    if not gate.exists():
        return [p for p in paths if p.exists()]
    import importlib.util
    spec = importlib.util.spec_from_file_location("_release_gate_ag93", gate)
    rg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rg)
    rules = rg.load_exclude_rules()
    return [p for p in paths if p.exists() and not rg.is_excluded(p.relative_to(REPO).as_posix(), rules)]


USER_DOCS = _shipped([REPO / "README.md", REPO / "SECURITY.md", REPO / "CONTRIBUTING.md", REPO / "SUPPORT.md",
                      REPO / "CHANGELOG.md", *sorted((REPO / "docs").rglob("*.md"))])
_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


def _prose_lines(path: Path):
    fenced = False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            yield n, line


def test_relative_links_resolve_inside_the_repository():
    broken = []
    for doc in USER_DOCS:
        for n, line in _prose_lines(doc):
            for target in _LINK.findall(line):
                if re.match(r"[a-z]+:", target) or target.startswith("#"):
                    continue
                dest = (doc.parent / target.split("#", 1)[0]).resolve()
                if not dest.exists() or REPO not in dest.parents and dest != REPO:
                    broken.append(f"{doc.relative_to(REPO)}:{n} -> {target}")
    assert not broken, broken


def test_named_docs_files_exist():
    missing = []
    for doc in USER_DOCS:
        if doc.name == "CHANGELOG.md":
            continue  # history may name files that were later renamed
        for n, line in _prose_lines(doc):
            for name in re.findall(r"(?<![\w/.])docs/([A-Za-z0-9_-]+\.md)", line):
                if not (REPO / "docs" / name).is_file():
                    missing.append(f"{doc.relative_to(REPO)}:{n} -> docs/{name}")
    assert not missing, missing


def test_source_variables_named_in_configuration_are_read_by_the_source():
    text = (REPO / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    m = re.search(r"Optional-integration variables you may see in source \(([^)]*)\)", text)
    assert m, "the optional-integration paragraph moved"
    names = re.findall(r"`(ATLAS_[A-Z0-9_]+)`", m.group(1))
    assert names
    source = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                       for p in (REPO / "src").rglob("*.py"))
    unread = [n for n in names if f'"{n}"' not in source and f"'{n}'" not in source]
    assert not unread, unread
