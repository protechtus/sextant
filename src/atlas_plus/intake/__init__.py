"""Sextant Intake: a watched drop folder that lands files where they belong.

The operator drops a file into one folder from anywhere (a phone, another
machine, a browser export). The next intake run picks it up and:

1. waits until the file is stable (size unchanged across two samples and
   readable; cloud-sync placeholders and native Google Docs shortcuts are
   skipped, never half-copied);
2. hashes it (sha256) and consults the ledger so the same bytes are never
   imported twice;
3. classifies it: YAML frontmatter first (``destination`` > ``project``),
   then extension rules, then an ``unsorted`` fallback;
4. copies it to the destination (never moves; atomic write; an existing
   file with different content is versioned first, never overwritten);
5. re-hashes the destination to prove the copy;
6. appends one ledger line (the machine record of what happened);
7. archives the original under ``<source>/_imported/<date>/`` with a
   provenance sidecar (archive-not-delete; ``--leave`` keeps it in place);
8. regenerates ``<source>/_imported/MANIFEST.md`` so the operator can read
   what happened from the same folder they dropped the file into;
9. runs the rule's post-import actions (ticket, notify).

Nothing here deletes. The ledger is the authority; the manifest is a view
of it. Design: docs/INTAKE-DESIGN-2026-11.md.
"""

from __future__ import annotations

PROTOCOL_VERSION = 1

__all__ = ["PROTOCOL_VERSION"]
