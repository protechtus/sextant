#!/usr/bin/env python
"""Bump the package version in ONE place and keep everything that repeats it in step.

Usage:
    python scripts/bump_version.py --part patch|minor|major
    python scripts/bump_version.py --set 0.2.0
    python scripts/bump_version.py --check          # drift check only (what tests/test_version_drift.py runs)

What a bump does (version-before-edit copies first, under versions/):
  1. pyproject.toml           version = "X.Y.Z"
  2. src/atlas_plus/__init__.py  the fallback literal `_installed_version` returns when no
                                 distribution metadata is found (an editable checkout)
  3. CHANGELOG.md             the `## [Unreleased]` body moves under a new `## [X.Y.Z] - YYYY-MM-DD`
                                 heading; `[Unreleased]` stays, empty, for the next cycle
  4. prints the release steps: commit, publish the mirror, tag `vX.Y.Z` ON THE MIRROR, GitHub
     release (publish.yml refuses a tag that differs from pyproject).

Between tags the public builder stamps `X.Y.Z.devN` (N = public commits since the last tag) when
`SEXTANT_PUBLISH_DEV_VERSION=on` (scripts/build_public_repo.py); the private tree never carries a
dev version. Every public build today said 0.1.0 and `pip install -U` was a no-op (MN22, 2026-11-13).
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = REPO / "pyproject.toml"
INIT = REPO / "src" / "atlas_plus" / "__init__.py"
CHANGELOG = REPO / "CHANGELOG.md"
import os

# the workspace keeps version copies two levels up (<workspace>/versions, a mirrored path tree);
# a plain checkout keeps them beside the repo
_WS = REPO.parent.parent
VERSIONS = Path(os.environ.get("SEXTANT_VERSIONS_DIR") or ((_WS / "versions") if (_WS / "versions").is_dir() else (REPO / "versions")))

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def pyproject_version(text: str | None = None) -> str:
    text = PYPROJECT.read_text(encoding="utf-8") if text is None else text
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("pyproject.toml has no version line")
    return m.group(1)


def init_fallback_version(text: str | None = None) -> str:
    text = INIT.read_text(encoding="utf-8") if text is None else text
    m = re.search(r'^    return "(\d+\.\d+\.\d+)"\n', text, re.M)
    if not m:
        raise SystemExit("src/atlas_plus/__init__.py has no fallback version literal")
    return m.group(1)


def changelog_release_headings(text: str | None = None) -> list[str]:
    text = CHANGELOG.read_text(encoding="utf-8") if text is None else text
    return re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, re.M)


def drift(text_pyproject: str | None = None, text_init: str | None = None, text_changelog: str | None = None) -> list[str]:
    """Every place that repeats the version must agree. Returns the problems (empty = clean)."""
    problems: list[str] = []
    v = pyproject_version(text_pyproject)
    if not _SEMVER.match(v):
        problems.append(f"pyproject version {v!r} is not X.Y.Z (the private tree never carries a dev or local suffix)")
    fb = init_fallback_version(text_init)
    if fb != v:
        problems.append(f"__init__ fallback {fb!r} != pyproject {v!r} (run scripts/bump_version.py, never edit one by hand)")
    cl = CHANGELOG.read_text(encoding="utf-8") if text_changelog is None else text_changelog
    if "## [Unreleased]" not in cl:
        problems.append("CHANGELOG.md has no `## [Unreleased]` heading")
    heads = changelog_release_headings(cl)
    for h in heads:
        if tuple(map(int, h.split("."))) > tuple(map(int, v.split("."))):
            problems.append(f"CHANGELOG release heading [{h}] is newer than pyproject {v}")
    return problems


def _version_copy(path: Path) -> None:
    try:
        rel = path.resolve().relative_to(_WS.resolve())
    except ValueError:
        rel = Path(path.name)
    dest_dir = VERSIONS / rel.parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while (dest_dir / f"{path.name}_{date.today().isoformat()}_v{n}").exists():
        n += 1
    shutil.copy2(path, dest_dir / f"{path.name}_{date.today().isoformat()}_v{n}")


def close_unreleased(version: str, dry_run: bool = False) -> None:
    """Move the `[Unreleased]` body under `## [version] - today` without changing the version."""
    cl = CHANGELOG.read_text(encoding="utf-8")
    m = re.search(r"^## \[Unreleased\]\n", cl, re.M)
    if not m:
        raise SystemExit("CHANGELOG.md has no `## [Unreleased]` heading")
    nxt = re.compile(r"^## \[", re.M).search(cl, m.end())
    body = cl[m.end():nxt.start() if nxt else len(cl)]
    if not body.strip():
        raise SystemExit("CHANGELOG `[Unreleased]` is empty: nothing to release")
    heading = f"## [{version}] - {date.today().isoformat()}\n"
    if dry_run:
        print(f"would close [Unreleased] ({len(body.splitlines())} lines) under {heading.strip()}")
        return
    _version_copy(CHANGELOG)
    CHANGELOG.write_text(cl[:m.end()] + "\n" + heading + body.lstrip("\n") + cl[nxt.start() if nxt else len(cl):], encoding="utf-8", newline="\n")
    print(f"closed [Unreleased] under [{version}]; tag v{version} on the mirror after publishing")


def bump(new: str, dry_run: bool = False) -> None:
    if not _SEMVER.match(new):
        raise SystemExit(f"{new!r} is not X.Y.Z")
    old = pyproject_version()
    if tuple(map(int, new.split("."))) <= tuple(map(int, old.split("."))):
        raise SystemExit(f"{new} is not newer than {old}")
    py = PYPROJECT.read_text(encoding="utf-8")
    ini = INIT.read_text(encoding="utf-8")
    cl = CHANGELOG.read_text(encoding="utf-8")
    py2 = re.sub(r'^version\s*=\s*"[^"]+"', f'version = "{new}"', py, count=1, flags=re.M)
    ini2 = re.sub(r'^    return "\d+\.\d+\.\d+"\n', f'    return "{new}"\n', ini, count=1, flags=re.M)
    m = re.search(r"^## \[Unreleased\]\n", cl, re.M)
    if not m:
        raise SystemExit("CHANGELOG.md has no `## [Unreleased]` heading")
    nxt = re.compile(r"^## \[", re.M).search(cl, m.end())
    body = cl[m.end():nxt.start() if nxt else len(cl)]
    if not body.strip():
        raise SystemExit("CHANGELOG `[Unreleased]` is empty: nothing to release (write the entry first)")
    heading = f"## [{new}] - {date.today().isoformat()}\n"
    cl2 = cl[:m.end()] + "\n" + heading + body.lstrip("\n") + cl[nxt.start() if nxt else len(cl):]
    if dry_run:
        print(f"would bump {old} -> {new}; CHANGELOG [Unreleased] ({len(body.splitlines())} lines) -> {heading.strip()}")
        return
    for p in (PYPROJECT, INIT, CHANGELOG):
        _version_copy(p)
    PYPROJECT.write_text(py2, encoding="utf-8", newline="\n")
    INIT.write_text(ini2, encoding="utf-8", newline="\n")
    CHANGELOG.write_text(cl2, encoding="utf-8", newline="\n")
    print(f"bumped {old} -> {new}")
    print("next:\n"
          f"  git add pyproject.toml src/atlas_plus/__init__.py CHANGELOG.md && git commit -m 'Release {new}'\n"
          "  python scripts/publish_sextant.py --push                        # the mirror build carries the plain version\n"
          f"  git -C <mirror checkout> tag -a v{new} -m 'Sextant {new}' && git -C <mirror checkout> push origin v{new}\n"
          f"  gh release create v{new} --generate-notes                      # publish.yml builds and uploads to PyPI (trusted publisher)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--part", choices=("patch", "minor", "major"))
    g.add_argument("--set", dest="set_version")
    g.add_argument("--check", action="store_true")
    g.add_argument("--release", action="store_true", help="close the [Unreleased] section under the CURRENT version (the first release of a version that pyproject already names)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.check or (not a.part and not a.set_version and not a.release):
        problems = drift()
        print("version drift: " + ("clean" if not problems else "\n  " + "\n  ".join(problems)))
        print(f"  pyproject {pyproject_version()}; CHANGELOG releases: {', '.join(changelog_release_headings()) or 'none yet'}")
        return 0 if not problems else 1
    if a.release:
        cur = pyproject_version()
        if cur in changelog_release_headings():
            raise SystemExit(f"CHANGELOG already has a [{cur}] release heading: bump with --part or --set instead")
        close_unreleased(cur, dry_run=a.dry_run)
        return 0
    if a.set_version:
        new = a.set_version
    else:
        major, minor, patch = map(int, pyproject_version().split("."))
        new = {"patch": f"{major}.{minor}.{patch + 1}", "minor": f"{major}.{minor + 1}.0", "major": f"{major + 1}.0.0"}[a.part]
    bump(new, dry_run=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
