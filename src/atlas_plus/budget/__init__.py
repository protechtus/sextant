"""Atlas token-budget preflight.

Before an agent commits to a large task, ask whether there is enough room in the 5h rate-limit
window to finish it -- a task killed mid-flight wastes tokens and leaves corrupt, half-done work.

    from atlas_plus.budget import preflight, preflight_size

    v = preflight(estimated_tokens=900_000)      # the agent's gross estimate
    if not v.ok:
        print(v.recommendation)                  # e.g. "wait ~12 min" or "split the task"

    v = preflight_size("large")                  # when you can't estimate precisely
"""

from __future__ import annotations

from .meter import (DEFAULT_GLOB, WINDOW_HOURS, UsageEvent, human, load_events,
                    max_rolling_window, minutes_until_freed, window_sum)
from .preflight import (DEFAULT_MARGIN, SIZE_PRESETS, BudgetSnapshot, BudgetVerdict,
                        preflight, preflight_size, snapshot)

__all__ = [
    "preflight", "preflight_size", "snapshot", "BudgetVerdict", "BudgetSnapshot",
    "SIZE_PRESETS", "DEFAULT_MARGIN", "WINDOW_HOURS",
    "load_events", "window_sum", "max_rolling_window", "minutes_until_freed",
    "UsageEvent", "human", "DEFAULT_GLOB",
]
