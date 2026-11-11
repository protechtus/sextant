"""Guardrails for the public-mirror builder's pyproject identity patches.

The builder itself (scripts/build_public_repo.py) is excluded from the public
tree, so every test here skips when the script is absent -- the public suite
must stay green without it.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER = REPO_ROOT / "scripts" / "build_public_repo.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"

pytestmark = pytest.mark.skipif(
    not BUILDER.exists(),
    reason="builder is private-only; not shipped in the public tree",
)


@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location("build_public_repo", BUILDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def patched_pyproject(builder) -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    for pattern, repl in builder.PYPROJECT_PATCHES:
        text = pattern.sub(repl, text)
    return text


class TestEmptyBuildTarget:
    """Regression pins for the 2026-10-09 incident: a cwd inside the
    target broke rmtree AFTER .git was gone, and `git -C target` fell
    through to the parent (private) repo."""

    def _populate(self, target: Path) -> None:
        (target / "docs").mkdir(parents=True)
        (target / "docs" / "a.md").write_text("x", encoding="utf-8")
        (target / ".git" / "objects").mkdir(parents=True)
        ro = target / ".git" / "objects" / "pack"
        ro.write_text("y", encoding="utf-8")
        ro.chmod(0o444)  # .git objects are read-only, the historic rmtree pain
        (target / "README.md").write_text("z", encoding="utf-8")

    def test_empties_everything_but_keeps_the_root(self, builder, tmp_path):
        target = tmp_path / "mirror"
        self._populate(target)
        builder._empty_build_target(target)
        assert target.exists(), "the root directory itself must survive"
        assert list(target.iterdir()) == []

    def test_works_with_cwd_inside_target(self, builder, tmp_path):
        """THE incident shape: some process's cwd is inside the target.
        Windows refuses to rmdir a cwd; emptying must still succeed
        because the root is never removed."""
        import os as _os
        target = tmp_path / "mirror"
        self._populate(target)
        old_cwd = _os.getcwd()
        _os.chdir(target)
        try:
            builder._empty_build_target(target)
            assert target.exists()
            assert list(target.iterdir()) == []
        finally:
            _os.chdir(old_cwd)

    def test_git_dir_is_deleted_last(self, builder, tmp_path, monkeypatch):
        target = tmp_path / "mirror"
        self._populate(target)
        (target / "zzz-after-git-alphabetically").mkdir()
        order: list[str] = []
        real = builder._rmtree_force

        def recording(p):
            order.append(Path(p).name)
            real(p)

        monkeypatch.setattr(builder, "_rmtree_force", recording)
        builder._empty_build_target(target)
        assert order[-1] == ".git", f"dir removal order was {order}; .git must be last"

    def test_refuses_to_continue_if_git_survives(self, builder, tmp_path, monkeypatch):
        """Fail-closed: a stale .git means stale history; the build must
        stop before copying rather than commit on top of it."""
        target = tmp_path / "mirror"
        self._populate(target)

        def skip_git(p):
            if Path(p).name == ".git":
                return  # simulate a locked .git that would not delete
            builder.shutil.rmtree(p, ignore_errors=True)

        monkeypatch.setattr(builder, "_rmtree_force", skip_git)
        with pytest.raises(RuntimeError, match="stale history"):
            builder._empty_build_target(target)


class TestShippedDocsDeployReferences:
    """A shipped doc that points the reader at deploy/<file> must ship that
    file (2026-10-11: HOWTO-ntfy-control told strangers to run
    deploy/atlas-ctl.ps1 and deploy/run-ntfy-listener.ps1, neither of which
    ships -- code-block references are invisible to the markdown link
    checker, so this class needs its own gate)."""

    DEPLOY_REF = re.compile(r"deploy[/\\]([A-Za-z0-9_][A-Za-z0-9_.-]*\.[A-Za-z0-9]+)")

    # References that explicitly tell the reader the file is NOT shipped
    # (ARCHITECTURE.md describes the maintainer's Windows installer and says
    # so in the same sentence). Add here only with that disclaimer present.
    ALLOWED_UNSHIPPED = {"install-atlas-stack.ps1"}

    def test_no_broken_deploy_references_in_shipped_docs(self, builder) -> None:
        shipped = {
            p.relative_to(builder.REPO).as_posix() for p in builder.shippable_set()
        }
        shipped_deploy_names = {
            Path(rel).name for rel in shipped if rel.startswith("deploy/")
        }
        offenders = []
        for rel in sorted(shipped):
            if not rel.endswith(".md"):
                continue
            text = (builder.REPO / rel).read_text(encoding="utf-8", errors="replace")
            for name in sorted(set(self.DEPLOY_REF.findall(text))):
                if name not in shipped_deploy_names and name not in self.ALLOWED_UNSHIPPED:
                    offenders.append(f"{rel} -> deploy/{name}")
        assert not offenders, (
            "shipped docs reference deploy files that do not ship: "
            f"{offenders}"
        )


def test_every_patch_matches_exactly_once(builder) -> None:
    """A pyproject rename/removal must not silently disable a patch."""
    text = PYPROJECT.read_text(encoding="utf-8")
    for pattern, _ in builder.PYPROJECT_PATCHES:
        assert len(pattern.findall(text)) == 1, (
            f"builder patch no longer matches pyproject exactly once: "
            f"{pattern.pattern!r}"
        )


def test_alias_sources_are_real_entry_points(builder) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    for atlas_name, _ in builder._SEXTANT_ALIASES:
        assert re.search(rf'^{re.escape(atlas_name)} = "', text, re.MULTILINE), (
            f"alias source {atlas_name!r} is not an entry point in pyproject"
        )


def test_patched_output_contains_all_sextant_aliases(builder, patched_pyproject) -> None:
    for atlas_name, sextant_name in builder._SEXTANT_ALIASES:
        atlas_line = re.search(
            rf'^{re.escape(atlas_name)} = "(?P<entry>[^"]+)"$',
            patched_pyproject,
            re.MULTILINE,
        )
        sextant_line = re.search(
            rf'^{re.escape(sextant_name)} = "(?P<entry>[^"]+)"$',
            patched_pyproject,
            re.MULTILINE,
        )
        assert atlas_line, f"{atlas_name} missing after patching (back-compat broken)"
        assert sextant_line, f"{sextant_name} alias missing after patching"
        assert atlas_line["entry"] == sextant_line["entry"], (
            f"{sextant_name} points at a different entry than {atlas_name}"
        )


def test_public_identity_is_sextant_ai(patched_pyproject) -> None:
    assert '\nname = "sextant-ai"\n' in patched_pyproject
    assert 'name = "atlas-plus"' not in patched_pyproject
