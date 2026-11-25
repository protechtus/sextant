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


def script(name: str) -> str:
    """A standalone console script (`sextant-<name>`, or `atlas-<name>` in an atlas-* install) as this
    install must type it, backticked. `sextant fts-rebuild` is not a subcommand: hints named it, or the
    atlas-* alias a public install never shows (SEX-052 item 4)."""
    prefix = "sextant" if umbrella() == "sextant" else "atlas"
    return f"`{how_to_run(prefix + '-' + name)}`"


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
    """How THIS copy was installed: "git" (pip install git+...), "editable" (a clone, -e), "folder"
    (`pip install .` in a clone or an unpacked archive, not editable), or "pypi" (anything else, the
    default). Read from the distribution's direct_url.json (PEP 610)."""
    info = _direct_url()
    if isinstance(info.get("dir_info"), dict):
        return "editable" if info["dir_info"].get("editable") else "folder"
    if "vcs_info" in info:
        return "git"
    return "pypi"


def install_folder() -> str:
    """The folder a "folder" or "editable" install came from, as a path of this system ('' when unknown)."""
    url = install_source()
    if not url.lower().startswith("file:"):
        return ""
    from urllib.parse import urlsplit
    from urllib.request import url2pathname
    try:
        return url2pathname(urlsplit(url).path)
    except (ValueError, OSError):
        return ""


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
    if kind == "folder":
        folder = install_folder()
        where = f"in {folder}" if folder else "in the folder you installed from"
        return (f"`git pull` {where}, then `pip install -U .` there again (this copy was installed from "
                "that folder: PyPI's `-U` line does nothing for it)")
    return f"`{PYPI_UPGRADE}` (a source install updates differently: docs/GETTING-STARTED.md)"


def stored_where(scope: str) -> str:
    """The clause a remember confirmation adds for a scope whose folder differs from its name:
    preference:<topic> lands in the feedback folder (SEX-050 item 11). `sextant remember` and
    sextant-brain's /save print the same clause (AG107)."""
    return (" (preference:<topic> is stored in the feedback folder)"
            if str(scope).strip().lower().startswith("preference:") else "")


def stored_scope(scope: str) -> str:
    """The scope as stored: routing.resolve_target's folding (`Project:Alpha` is `project:alpha`, SEX-048 item 9)."""
    import unicodedata
    if ":" not in str(scope):
        return str(scope).strip()  # a bare route name: shown as typed
    key, _, param = str(scope).partition(":")
    param = unicodedata.normalize("NFC", param.strip()).replace(chr(0x130), "I").lower()
    return f"{key.strip().lower()}:{param.replace(chr(0x307), '').replace(' ', '-')}"


def scope_as_stored(scope: str) -> str:
    shown = stored_scope(scope)  # said only when the folding changed what was typed
    return f" (stored as scope {shown}; scopes are lowercase)" if shown != str(scope).strip() else ""


EMPTY_SEARCH = ("Nothing matched. Try fewer or other words (keyword search needs a word that appears in "
                "a note), drop the scope, or store the fact first.")
"""What to try after an empty search: the MCP search tool and the dashboard's search box say it the
way `sextant search` does (SEX-047, AG107)."""


def user_path(raw: str) -> str:
    """A path argument as the user meant it: a leading `~` is the home folder. PowerShell does not
    expand `~` for a native program, so `--path ~/MyVault` arrived as the literal `~\\MyVault`
    ("path not found", SEX-051 F3). Usable as an argparse `type=`."""
    return os.path.expanduser(raw) if isinstance(raw, str) and raw.startswith("~") else raw


def help_hint(prog: str) -> str:
    return f"Run `{prog} --help` to see what it takes."


def say(message: str, next_step: str, code: int = 1) -> int:
    """Print a failure and its next step to stderr; return the exit code for `return say(...)`."""
    print(f"{message}\nNext: {next_step}", file=sys.stderr)
    return code


KEY_MIN_LEN = 16  # the shortest key the Brain and the dashboard accept; both refusals say it (SEX-051 F5)

_KEYGEN_CODE = '"import secrets; print(secrets.token_urlsafe(32))"'


def _windows() -> bool:
    return os.name == "nt"


def keygen_command(shell: str = "") -> str:
    """The key one-liner for `shell` ("powershell", "cmd", "posix"; default this OS's), naming THIS
    interpreter: Ubuntu has no `python` outside a venv (MN88), and a Windows bare `python` can be the
    Store stub, which prints nothing: an EMPTY key (SEX-051 F4). Every form survives a spaced path."""
    shell = shell or ("powershell" if _windows() else "posix")
    exe = sys.executable or ("py" if _windows() else "python3")
    if shell == "powershell":  # single quotes: literal in PowerShell, a quote inside is doubled
        return "& '" + exe.replace("'", "''") + "' -c " + _KEYGEN_CODE
    if shell == "cmd":  # the command of a `for /f` line: cmd /c strips the outer (caret-escaped) quotes
        return f'^""{exe}" -c {_KEYGEN_CODE}^"'
    import shlex
    return f"{shlex.quote(exe.replace(chr(92), '/') if _windows() else exe)} -c {_KEYGEN_CODE}"


def key_missing(var: str) -> str:
    """`<var> is not set`, or `is set but empty` (what a keygen that printed nothing leaves)."""
    if var in os.environ:
        return (f"{var} is set but empty (the command that made it printed nothing; "
                "run the one below on its own first)")
    return f"{var} is not set"


def key_setup_lines(var: str, rerun: str = "re-run", headline: str | None = None) -> str:
    """How to make and set `var`, one copyable command per line, for printing to stderr OUTSIDE
    the log handler: a log record folds at 80 columns in the middle of a path, so the command
    could not be copied (MN90, SEX-048). `sextant web`, `sextant-mcp` and `sextant-brain` print
    this same block for a missing and a too-short key; the first line keeps the set-but-empty
    wording unless `headline` replaces it."""
    return (f"{headline or key_missing(var)}.\n"
            f"Generate a key and set it, then {rerun}:\n"
            + (f"  PowerShell:  $env:{var} = {keygen_command('powershell')}\n"
               f"               (persist with: setx {var} $env:{var})\n"
               f"  cmd.exe:     for /f \"delims=\" %k in ('{keygen_command('cmd')}') do set {var}=%k\n"
               if _windows() else "")
            + f"  {'Git Bash:  ' if _windows() else 'bash/zsh:  '}  export {var}=$({keygen_command('posix')})")


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
