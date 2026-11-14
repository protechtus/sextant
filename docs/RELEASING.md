# Versions, builds and releases

How a Sextant version number is chosen, where it is written, what a public build between
releases is called, and who does what at release time.

## One number, written once

The version lives in `pyproject.toml` as plain `X.Y.Z`. Two places repeat it, the fallback
literal in `src/atlas_plus/__init__.py` (what `sextant --version` prints from a checkout that
has no installed metadata) and the `## [X.Y.Z]` headings in `CHANGELOG.md`, and
`tests/test_version_drift.py` fails the suite when they disagree. Nobody edits one by hand:

```
python scripts/bump_version.py --check                 # report drift (what the test runs)
python scripts/bump_version.py --part patch|minor|major
python scripts/bump_version.py --set X.Y.Z
python scripts/bump_version.py --release               # first release of the version pyproject already names
```

A bump rewrites all three places and moves the `[Unreleased]` changelog section under a new
`## [X.Y.Z] - YYYY-MM-DD` heading, leaving `[Unreleased]` empty for the next cycle. `--release`
only closes the section, for the first release of a version that `pyproject.toml` already
names. A bump that goes backwards, or a release with an empty `[Unreleased]` section, is refused.

## Public builds between releases

Every build pushed to this repository between tags is a distinct PEP 440 dev release of the
version it precedes, `X.Y.Z.devN`, where N counts public commits since the last `v*` tag. The
build also carries `src/atlas_plus/_build.py` with the source revision, so

```
$ sextant --version
sextant 0.1.0.dev15 (source d63cb35)
```

names the exact build in a bug report. Dev releases sort below the release they precede, so
`pip install -U git+https://github.com/protechtus/sextant` moves from build to build and the
release itself replaces them all. (Until the maintainer turns the stamp on, every build says
plain `0.1.0`, `-U` is a no-op, and `pip install --force-reinstall --no-deps git+...` is the
way to move; see GETTING-STARTED "Updating".)

In a stamped build `tests/test_version_drift.py` compares the base version (`X.Y.Z`) with the
other two places, so the suite passes on the public tree too.

## Who releases, and how

This repository is a mirror: the maintainer publishes it from a private source tree with a
gated build, and every commit here names the source revision it came from. Releases are the
maintainer's: a contributor's pull request is reviewed, re-imported into the source tree and
published back here; contributors never need to tag or run the publisher.

The maintainer's release is, in order:

1. `python scripts/bump_version.py --part ...` (or `--release`), commit.
2. Publish the mirror. The release build carries the plain version, no dev suffix.
3. Tag the mirror: `git tag -a vX.Y.Z -m "Sextant X.Y.Z"` and push the tag.
4. Create the GitHub release for the tag. `.github/workflows/publish.yml` refuses a tag that
   differs from `pyproject.toml`, runs the full suite on that tree, and uploads to PyPI through
   the trusted publisher (no secrets in the repository).

The first PyPI release is `0.1.0`; the `0.2` to `0.17` headings further down the changelog are
internal development milestones, not published versions (the changelog's note explains).
