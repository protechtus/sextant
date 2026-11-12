"""Hook launcher that carries its own variables: ``python -m atlas_plus.hooks.run --env NAME=VALUE ... session-start|stop``.

Claude Code runs a hook command through a shell it chooses (sh on POSIX, Git Bash on Windows,
PowerShell only when Git Bash is absent; never cmd.exe), and a hook process inherits only the
client's environment, not the MCP registration's ``env`` block. A ``set "NAME=value" &&`` prefix
therefore did nothing under Git Bash and the hook primed from the wrong corpus while reporting
success (MN1, pipeline sweep 2026-11-11). ``--env`` flags work in every shell; the values are
placed in the environment BEFORE any atlas_plus module is imported, because several freeze their
roots at import time.
"""
from __future__ import annotations

import os
import sys

TARGETS = {
    "session-start": ("atlas_plus.hooks.session_start", "main"),
    "stop": ("atlas_plus.summarizer.hook_entry", "main"),
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    target = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--env":
            if i + 1 >= len(args) or "=" not in args[i + 1]:
                print("atlas_plus.hooks.run: --env needs NAME=VALUE", file=sys.stderr)
                return 2
            k, v = args[i + 1].split("=", 1)
            os.environ[k] = v
            i += 2
            continue
        if a.startswith("--env="):
            k, v = a[len("--env="):].split("=", 1) if "=" in a[len("--env="):] else (None, None)
            if k is None:
                print("atlas_plus.hooks.run: --env needs NAME=VALUE", file=sys.stderr)
                return 2
            os.environ[k] = v
            i += 1
            continue
        if a in ("-h", "--help"):
            print("Usage: python -m atlas_plus.hooks.run [--env NAME=VALUE ...] session-start|stop\n\n"
                  "Sets the variables, then runs the named Claude Code hook. Written by `sextant setup`.")
            return 0
        if target is None and a in TARGETS:
            target = a
            i += 1
            continue
        print(f"atlas_plus.hooks.run: unknown argument {a!r} (expected session-start or stop)", file=sys.stderr)
        return 2
    if target is None:
        print("atlas_plus.hooks.run: missing target (session-start or stop)", file=sys.stderr)
        return 2
    import importlib
    module_name, func_name = TARGETS[target]
    sys.argv = [sys.argv[0]]
    mod = importlib.import_module(module_name)
    rc = getattr(mod, func_name)()
    return int(rc or 0)


if __name__ == "__main__":
    sys.exit(main())
