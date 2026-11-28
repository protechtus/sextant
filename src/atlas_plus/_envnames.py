"""Both spellings of every setting: `SEXTANT_<NAME>` is honoured wherever `ATLAS_<NAME>` is read (SEX-043, AG132).

The code reads `ATLAS_*` at a few hundred places, many at import time; there is no single read helper. Every process
imports this package first, so ONE adoption step at package import makes the new spelling work everywhere, and
because it writes into `os.environ`, every child process the product starts inherits the value.

Rule (docs/CONFIGURATION.md states it for users):
- `SEXTANT_<NAME>` set and `ATLAS_<NAME>` not set: `ATLAS_<NAME>` takes the value (empty values copied as they are).
- both set to the same value: nothing happens.
- both set and different: `ATLAS_<NAME>` wins (it is the spelling `sextant setup` writes into client settings and
  hook lines, so an upgrade never re-points an existing registration); one stderr line per process names both.
- `SEXTANT_PROOF_*` is the benchmark harness's own namespace and is never adopted.
An environment holding only `ATLAS_*` names is left exactly as it was, and nothing is printed.
"""

from __future__ import annotations

import os
import sys

# `MutableMapping` is only named in annotations (postponed): no `typing` import on the hook path.

NEW_PREFIX = "SEXTANT_"
OLD_PREFIX = "ATLAS_"
_NOT_TWINS = ("SEXTANT_PROOF_",)
_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")  # no `re` import: every hook pays for this module

_warned: set[str] = set()


def twin_of(new_name: str) -> str | None:
    """The `ATLAS_*` name a `SEXTANT_*` name stands for, or None when it is not an adoptable name."""
    if not new_name.startswith(NEW_PREFIX) or new_name.startswith(_NOT_TWINS):
        return None
    rest = new_name[len(NEW_PREFIX):]
    if not rest or rest[0] == "_" or not _NAME_CHARS.issuperset(rest):
        return None
    return OLD_PREFIX + rest


def _warn(new_name: str, old_name: str) -> None:
    if new_name in _warned:
        return
    _warned.add(new_name)
    stream = sys.stderr
    if stream is None:  # pythonw: a lost warning is fine, prose on stdout (an MCP channel) is not
        return
    try:
        print(f"warning: {new_name} and {old_name} are both set and differ; using {old_name}, the older "
              f"spelling that client settings written by setup carry. Unset one of them (docs/CONFIGURATION.md).",
              file=stream)
    except Exception:  # noqa: BLE001 - a broken stderr must not stop the import
        pass


def adopt_one(old_name: str, environ: MutableMapping[str, str] | None = None) -> None:
    """Apply the rule to one name, for a `SEXTANT_*` value set after import (tests, embedding callers)."""
    env = os.environ if environ is None else environ
    if not old_name.startswith(OLD_PREFIX):
        return
    new_name = NEW_PREFIX + old_name[len(OLD_PREFIX):]
    if twin_of(new_name) != old_name or new_name not in env:
        return
    if old_name not in env:
        env[old_name] = env[new_name]
    elif env[old_name] != env[new_name]:
        _warn(new_name, old_name)


def adopt(environ: MutableMapping[str, str] | None = None) -> None:
    """Apply the rule to every `SEXTANT_*` name in the environment."""
    env = os.environ if environ is None else environ
    for name in [k for k in env if k.startswith(NEW_PREFIX)]:
        old = twin_of(name)
        if old is not None:
            adopt_one(old, env)
