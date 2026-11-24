"""What to type next, said one way everywhere (LES-054: a first-timer is never left at an error
with no direction). Every failure exit names the next command, spelled as THIS install runs it,
or the page that explains it. Plain ASCII: cp1252 and cp932 consoles print it as is."""
from __future__ import annotations

import argparse
import os
import sys


def how_to_run(name: str) -> str:
    """The command as THIS user has to type it. Bare `sextant` only works when the venv is
    activated or its Scripts folder is on PATH; a first-timer who ran `.\\.venv\\Scripts\\sextant.exe`
    and was told to type `sextant init` got "not recognized" (operator's by-hand install,
    2026-11-21). So: the bare name when the shell would find this install's script, else the
    script's path, relative to the current folder when it is inside it."""
    import shutil
    from pathlib import Path

    script = Path(sys.executable).parent / (name + (".exe" if os.name == "nt" else ""))
    try:
        found = shutil.which(name)
        if not script.exists() or (found and Path(found).resolve() == script.resolve()):
            return name
        try:
            rel = script.resolve().relative_to(Path.cwd().resolve())
            shown = f".{os.sep}{rel}"
        except ValueError:
            shown = str(script)
    except OSError:
        return name
    if " " in shown:
        return f'& "{shown}"' if os.name == "nt" else f'"{shown}"'
    return shown


def umbrella() -> str:
    """`sextant` for the public family, `atlas-plus` when invoked through an atlas-* name."""
    from pathlib import Path

    invoked = Path(sys.argv[0]).stem.lower() if sys.argv and sys.argv[0] else ""
    if invoked.startswith("sextant"):
        return "sextant"
    if invoked.startswith("atlas") or not (invoked == "__main__" and Path(sys.argv[0]).parent.name == "atlas_plus"):
        return "atlas-plus" if invoked.startswith("atlas") else "sextant"
    from importlib.metadata import PackageNotFoundError, distribution
    try:  # `python -m atlas_plus ...`: the installed distribution decides
        distribution("sextant-ai")
        return "sextant"
    except PackageNotFoundError:
        return "atlas-plus"


def command(sub: str = "") -> str:
    """`sextant <sub>` as this install must type it, backticked."""
    u = umbrella()
    return f"`{how_to_run(u)}{' ' + sub if sub else ''}`"


def corpus_elsewhere() -> str:
    """One sentence for a "no corpus yet" message when the corpus location comes from the defaults: a
    corpus made yesterday in a terminal that had ATLAS_WORKSPACE_ROOT set is invisible to a new terminal
    without it, and following "run init" there starts a second, empty corpus (MN88 and MN90, SEX-048).
    Empty when a location variable is set: then the path shown is the one the user chose."""
    if any(os.environ.get(v) for v in ("ATLAS_WORKSPACE_ROOT", "ATLAS_MEMORY_ROOT")):
        return ""
    return (" If you already made a corpus somewhere else, this terminal does not know where: set "
            "ATLAS_WORKSPACE_ROOT here as you did then (and put it in your shell profile); init would "
            "start a second, empty corpus.")


PYPI_UPGRADE = "pip install -U sextant-ai"


def _direct_url() -> dict:
    """This install's direct_url.json (PEP 610) as a dict; {} for a PyPI install or unreadable metadata."""
    import json
    from importlib.metadata import PackageNotFoundError, distribution
    raw = None
    for dist in ("sextant-ai", "atlas-plus"):  # the public name, then the name a source checkout installs under
        try:
            raw = distribution(dist).read_text("direct_url.json")
            break
        except (PackageNotFoundError, OSError):
            continue
    try:
        info = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return info if isinstance(info, dict) else {}


def install_kind() -> str:
    """How THIS copy was installed: "git" (pip install git+...), "editable" (a clone, -e), or
    "pypi" (anything else, the default). Read from the distribution's direct_url.json (PEP 610)."""
    info = _direct_url()
    if isinstance(info.get("dir_info"), dict) and info["dir_info"].get("editable"):
        return "editable"
    if "vcs_info" in info:
        return "git"
    return "pypi"


def install_source() -> str:
    """The address a git install came from, as pip recorded it ('' when unknown). The upgrade line
    names THAT address: a copy installed from a fork upgrades from the fork."""
    url = _direct_url().get("url")
    return url if isinstance(url, str) else ""


def upgrade_line(kind: str | None = None) -> str:
    """The one upgrade command that fits this install; the PyPI line when unknown."""
    kind = kind or install_kind()
    if kind == "git":
        src = install_source() or "<the address you installed from>"
        return (f"`pip install --force-reinstall --no-deps git+{src}` "
                "(this copy came from git; every build is 0.1.0, so -U is a no-op)")
    if kind == "editable":
        return "`git pull` in your clone, then `pip install -e .` again (this copy is an editable clone)"
    return f"`{PYPI_UPGRADE}` (a source install updates differently: docs/GETTING-STARTED.md)"


def help_hint(prog: str) -> str:
    return f"Run `{prog} --help` to see what it takes."


def say(message: str, next_step: str, code: int = 1) -> int:
    """Print a failure and its next step to stderr; return the exit code for `return say(...)`."""
    print(f"{message}\nNext: {next_step}", file=sys.stderr)
    return code


_KEYGEN_CODE = '"import secrets; print(secrets.token_urlsafe(32))"'


def _windows() -> bool:
    return os.name == "nt"


def keygen_command() -> str:
    """The key one-liner THIS machine runs: Ubuntu has no `python` outside a venv, so `$(python -c ...)`
    set an EMPTY key (MN88, SEX-048). POSIX: the running interpreter, shell-quoted; Windows: as before."""
    if _windows():
        return f"python -c {_KEYGEN_CODE}"
    import shlex
    return f"{shlex.quote(sys.executable or 'python3')} -c {_KEYGEN_CODE}"


def key_missing(var: str) -> str:
    """`<var> is not set`, or `is set but empty` (what a keygen that printed nothing leaves)."""
    if var in os.environ:
        return (f"{var} is set but empty (the command that made it printed nothing; "
                "run the one below on its own first)")
    return f"{var} is not set"


def is_write_refused(exc: BaseException) -> bool:
    """A permission or read-only-filesystem refusal (not a full disk, not a bad name)."""
    import errno
    return isinstance(exc, PermissionError) or (
        isinstance(exc, OSError) and exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS))


def write_refused(exc: OSError, folder: object) -> str:
    """A refused write as the folder, the reason and the next step, not a raw errno (SEX-048)."""
    from pathlib import Path
    name = getattr(exc, "filename", None)
    where = Path(name).parent if name else Path(str(folder))
    why = exc.strerror or exc.__class__.__name__
    return (f"cannot write in {where} ({why}): the folder is read-only or belongs to another account. "
            f"Next: check that folder's permissions (it must be writable by you), then run {command('doctor')}.")


class ArgumentParser(argparse.ArgumentParser):
    """argparse whose usage errors end with the pointer to --help instead of a bare
    `error: unrecognized arguments`. Subparsers inherit it (argparse uses type(self))."""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n{help_hint(self.prog)}\n")
