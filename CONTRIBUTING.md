# Contributing to Sextant

Thanks for your interest. Sextant is a curated public cut of a personal system
that runs as its author's daily driver, so contributions work a little
differently than in a committee-run project: issues, bug reports, and small
focused PRs are very welcome; large feature PRs are best discussed in an issue
first so effort is not wasted on a direction the engine will not take.

Versions, dev builds and the release procedure are described in [docs/RELEASING.md](docs/RELEASING.md).

## Getting set up

```bash
git clone https://github.com/protechtus/sextant.git && cd sextant
python -m venv .venv
.venv/Scripts/pip install -e ".[dev,telegram,summarizer]"    # Windows
.venv/bin/pip install -e ".[dev,telegram,summarizer]"        # Linux/macOS
python -m pytest tests -q
```

The test suite is the contract: a green run on your machine before and after
your change is the baseline expectation. CI runs the same suite on every push,
plus three more lanes you can reproduce locally before opening a PR:

```bash
pip install -U pip setuptools pip-audit build twine   # the audit lanes expect current pip/setuptools
pip-audit                                           # deps-audit (gating; a new upstream CVE can turn it red through no fault of yours: say so in the PR)
python -m build && twine check --strict dist/*      # public-package
gitleaks detect --source . --no-git                 # optional; CI runs it on every push with .gitleaks.toml
```

There is no enforced linter or formatter and no pre-commit hook: match the surrounding
code, keep lines under about 110 characters, and write a commit message that says what
changed and why in plain sentences. `git commit -s` is welcome but is not the CLA (see below).
A docs-only or message-only change does not need a new test: say "n/a: docs" on that
checklist line.

## Contributor License Agreement

First-time contributors accept the lightweight CLA in [CLA.md](CLA.md) on their
first PR: reply in the PR thread with the sentence "I have read and agree to
CLA.md" (that comment is the record until an automated check exists). You keep
your copyright; the CLA licenses your contribution to the project so its
licensing stays simple and its future flexible. For a corporate agreement,
email hello@protechtus.com.

## Ground rules

- One change per PR, with a test that fails before the change and passes after
  (docs-only changes: none needed).
- Match the surrounding code style; no reformatting-only diffs.
- The markdown memory corpus format is a compatibility surface -- changes to
  frontmatter fields or file layout need an issue first.
- No new required cloud dependencies. Optional integrations belong behind an
  extra in `pyproject.toml` and must degrade cleanly when absent.
- **The two-actor rule:** any change that writes shared state (corpus files,
  sqlite, caches) is designed assuming a second concurrent actor exists --
  another process, worker, device, or user -- because in this architecture
  one eventually does. Shared-file features include a concurrency test.
- **The front door is part of the feature:** error messages say what went
  wrong AND what to do next; natural command/flag guesses should land or
  redirect in one bounce; a fresh install with default env must never be
  broken by your change (`tests/test_stranger_journey.py` enforces the
  core journey and runs in CI on every push).

## Reporting bugs

Open a GitHub issue using the bug report template, with: what you ran, what you expected, what happened, OS
and Python version, and the shortest reproduction you can manage. For anything
security-sensitive, use the process in [SECURITY.md](SECURITY.md) instead of a
public issue. Feature ideas use the feature request template; questions use
the question template. Blank issues are turned off.

## Forks, support, and response times

Forks are welcome and expected; that is what the license is for. The canonical
project and the Sextant name stay here. Response targets, the weekly triage
pass, and what gets closed are spelled out in [SUPPORT.md](SUPPORT.md).
