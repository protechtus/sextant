## What and why

<!-- One or two sentences. What changes, and what problem it solves. -->

## Linked issue

<!-- "Fixes #123". Bug fixes may skip this; features need an issue first (see CONTRIBUTING.md). -->

## Checklist

- [ ] One change per PR, small focused diff, no reformatting-only changes.
- [ ] A test that fails before the change and passes after it.
- [ ] The full suite is green locally (`python -m pytest tests -q`).
- [ ] If this touches shared state (corpus files, sqlite, caches): a concurrency test, per the two-actor rule.
- [ ] No new required cloud dependency; optional integrations sit behind an extra and degrade cleanly.
- [ ] Changes to frontmatter fields or corpus file layout have an agreed issue.
- [ ] User-facing behavior changes are noted in `CHANGELOG.md` and the relevant doc.
- [ ] First-time contributor: I have read `CLA.md` and will accept the CLA check (or confirm in this thread until the check is enabled).
