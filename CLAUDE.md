# Notes for AI agents working in this repository

Sextant is a self-hosted memory and orchestration layer for AI sessions (Apache-2.0,
stewarded by Protechtus LLC). If a human asked you to **install** it, follow
`docs/AGENT-INSTALL.md` step by step; it has verify commands and the points where you must
ask before acting. If they asked you to **use** it, the tool reference is `docs/USAGE.md`
and the daily workflow is `docs/GETTING-STARTED.md`.

Ground rules for agents in this tree:

- Use the virtual environment's Python explicitly (`.venv/bin/python` or
  `.venv\Scripts\python.exe`); never rely on an activated shell.
- Run the test suite with `<venv-python> -m pytest tests -q` before proposing a change.
  Thousands of tests, a few minutes.
- The memory corpus (`~/.memory` by default) belongs to the human. Never delete or rewrite
  files there except through the memory tools; versioned copies go to `versions/`.
- Services bind to loopback and require tokens by design. Do not loosen that.
- Scopes have a grammar: `project:<name>`, `reference:<topic>`, `preference:<topic>`.
- Security reports go through the repository's private vulnerability reporting, not issues.
  See `SECURITY.md`. Contributions need the CLA described in `CONTRIBUTING.md`.
