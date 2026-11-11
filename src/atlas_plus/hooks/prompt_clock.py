"""UserPromptSubmit hook: inject a fresh world-clock line every turn.

Refresh-on-event, not on a timer. The model does not watch a clock; it
reads context only at specific moments. This hook fires at exactly one
of those moments -- the instant the user submits a prompt and the model
is about to reason -- and prepends a freshly-computed world-clock line
to the turn's context. So the model's sense of "now" never goes stale
mid-session, without any background job or cache.

It is pure compute (stdlib zoneinfo via the now_state sensor), emits a
single line, and never blocks: any failure exits 0 silently so a hook
problem can never wedge a prompt. See research/atlas-perception-layer.md.

Claude Code injects this script's stdout into the prompt context.
"""

from __future__ import annotations

import sys


def main() -> None:
    try:
        from atlas_plus.mcp.memory_server.tools.now_state import world_clock_line
        line = world_clock_line()
    except Exception:
        return
    if line:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Never block a prompt -- fail silently.
        print(f"# [atlas-prompt-clock] error: {e}", file=sys.stderr)
        sys.exit(0)
