# Contributing to Sextant

Thanks for your interest. Sextant is a curated public cut of a personal system
that runs as its author's daily driver, so contributions work a little
differently than in a committee-run project: issues, bug reports, and small
focused PRs are very welcome; large feature PRs are best discussed in an issue
first so effort is not wasted on a direction the engine will not take.

## Getting set up

```bash
git clone https://github.com/protechtus/sextant.git && cd sextant
python -m venv .venv
.venv/Scripts/pip install -e ".[dev,telegram,summarizer]"    # Windows
.venv/bin/pip install -e ".[dev,telegram,summarizer]"        # Linux/macOS
python -m pytest tests -q
```

The test suite is the contract: a green run on your machine before and after
your change is the baseline expectation. CI runs the same suite on every push.

## Contributor License Agreement

First-time contributors will be asked to accept the lightweight CLA in
[CLA.md](CLA.md) on their first PR (via the cla-assistant check once it is
enabled on this repository; until then, a maintainer will ask in the PR
thread). You keep your copyright; the CLA licenses your contribution to
the project so its licensing stays simple and its future flexible.

## Ground rules

- One change per PR, with a test that fails before the change and passes after.
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
