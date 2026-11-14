"""The version is written in one place and repeated in two others; they must agree (2026-11-13: eleven
public builds in one day all said 0.1.0 and nobody could name the build they were on)."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _bump_module():
    spec = importlib.util.spec_from_file_location("bump_version", REPO / "scripts" / "bump_version.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_version_is_repeated_consistently():
    bump = _bump_module()
    assert bump.drift() == [], bump.drift()


def test_private_version_is_plain_semver_and_public_build_may_add_dev_suffix():
    bump = _bump_module()
    v = bump.pyproject_version()
    m = re.fullmatch(r"(\d+\.\d+\.\d+)(?:\.dev\d+)?", v)
    assert m, v
    if m.group(0) != m.group(1):
        assert bump.BUILD_STAMP.exists(), "a dev suffix belongs to a stamped public build only"  # AG53: this assertion ran before the skip and failed the public suite on a stamped tree
    # the public builder's stamp is a valid PEP 440 dev release of the SAME base version
    import pytest
    if not (REPO / "scripts" / "build_public_repo.py").exists():
        pytest.skip("the public builder stays in the private tree; the stamp is checked there")
    build = importlib.util.spec_from_file_location("build_public_repo", REPO / "scripts" / "build_public_repo.py")
    mod = importlib.util.module_from_spec(build)
    build.loader.exec_module(mod)
    assert mod.dev_version(v, 37) == f"{v}.dev37"
    assert mod.dev_version(v, 0) == f"{v}.dev0"
    import packaging.version as pv  # packaging is a build dependency of every environment that runs the suite
    assert pv.Version(mod.dev_version(v, 37)) < pv.Version(v)      # a dev build sorts BELOW the release it precedes
    assert pv.Version(mod.dev_version(v, 38)) > pv.Version(mod.dev_version(v, 37))  # and each build above the last


def test_stamped_public_build_passes_drift_and_check_never_crashes(tmp_path, monkeypatch):
    """MN40: the public suite runs on the stamped tree; the stamp must not fail it."""
    bump = _bump_module()
    py = tmp_path / "pyproject.toml"
    ini = tmp_path / "__init__.py"
    cl = tmp_path / "CHANGELOG.md"
    stamp = tmp_path / "_build.py"
    py.write_text('[project]\nname = "x"\nversion = "0.1.1.dev3"\n', encoding="utf-8")
    ini.write_text('def _installed_version():\n    return "0.1.1"\n', encoding="utf-8")
    cl.write_text("# Changelog\n\n## [Unreleased]\n\n- x\n\n## [0.1.0] - 2026-11-13\n\n- old\n", encoding="utf-8")
    monkeypatch.setattr(bump, "PYPROJECT", py)
    monkeypatch.setattr(bump, "INIT", ini)
    monkeypatch.setattr(bump, "CHANGELOG", cl)
    monkeypatch.setattr(bump, "BUILD_STAMP", stamp)
    assert any("_build.py is absent" in p for p in bump.drift())      # a dev suffix without the builder's stamp is drift
    stamp.write_text('SOURCE = "abc1234"\n', encoding="utf-8")
    assert bump.drift() == []                                            # stamped: the base version is compared
    py.write_text('[project]\nname = "x"\nversion = "0.0.9.dev1"\n', encoding="utf-8")
    problems = bump.drift()                                              # no ValueError (the int('dev1') crash)
    assert any("newer than pyproject" in p for p in problems)


def test_bump_moves_the_unreleased_section(tmp_path, monkeypatch):
    bump = _bump_module()
    py = tmp_path / "pyproject.toml"
    ini = tmp_path / "__init__.py"
    cl = tmp_path / "CHANGELOG.md"
    py.write_text('[project]\nname = "x"\nversion = "0.1.0"\n', encoding="utf-8")
    ini.write_text('def _installed_version():\n    return "0.1.0"\n', encoding="utf-8")
    cl.write_text("# Changelog\n\n## [Unreleased]\n\n- one thing\n- another\n\n## [0.0.9] - 2026-01-01\n\n- old\n", encoding="utf-8")
    monkeypatch.setattr(bump, "PYPROJECT", py)
    monkeypatch.setattr(bump, "INIT", ini)
    monkeypatch.setattr(bump, "CHANGELOG", cl)
    monkeypatch.setattr(bump, "VERSIONS", tmp_path / "versions")
    assert bump.drift() == []
    bump.bump("0.1.1")
    assert 'version = "0.1.1"' in py.read_text(encoding="utf-8")
    assert 'return "0.1.1"' in ini.read_text(encoding="utf-8")
    text = cl.read_text(encoding="utf-8")
    assert re.search(r"## \[Unreleased\]\n\n## \[0\.1\.1\] - \d{4}-\d{2}-\d{2}\n\n- one thing\n- another\n", text), text
    assert bump.drift() == []
    # drift is detected when one copy is edited by hand
    ini.write_text('def _installed_version():\n    return "0.1.0"\n', encoding="utf-8")
    assert any("fallback" in p for p in bump.drift())
    # a version cannot go backwards, and an empty Unreleased section is not a release
    import pytest
    with pytest.raises(SystemExit):
        bump.bump("0.1.0")
    ini.write_text('def _installed_version():\n    return "0.1.1"\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        bump.bump("0.1.2")
