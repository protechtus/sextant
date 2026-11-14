"""The curation report, importable by a spawned worker process without the dashboard module.

The report is CPU-bound Python; run in a thread of the dashboard it held the GIL and every other
request waited (AG42, sweep 15: search took 23 to 60 s while a report ran). A worker process
inherits the environment, so ATLAS_MEMORY_ROOT and ATLAS_STATE_DIR name the same corpus.
"""

from __future__ import annotations


def report_dry_run() -> tuple[str, object]:
    from atlas_plus.curation.weekly_report import generate_report

    text, path = generate_report(dry_run=True)
    return text, (str(path) if path is not None else None)
