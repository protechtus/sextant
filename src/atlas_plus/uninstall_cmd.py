"""Clean removal: report and (only when explicitly told) delete Sextant data.

The mirror image of atlas-init, with the safety model inverted twice:

    atlas-uninstall                 # REPORT ONLY: what exists, where, how big
    atlas-uninstall --state --yes   # remove the state dir: transcripts, task DB, ledger, sync,
                                    # research reports. NOT a cache. Asks you to type its path.
    atlas-uninstall --corpus --yes  # remove the memory corpus (asks you to
                                    #   type its full path back, first)
    atlas-uninstall --all --yes     # both

(The public distribution also exposes it as `sextant-uninstall`.)

Safety rules, in order:
1. Nothing is deleted without an explicit target flag AND --yes.
2. The corpus (your data) additionally requires typing its exact path at the
   prompt, or --confirm-path <path> for scripted/VM use.
3. A corpus containing a `.sextant-protect` marker file REFUSES deletion
   unconditionally -- no flag overrides it. Drop that file in any corpus you
   never want a script to be able to wipe (e.g. your daily driver).
4. The package itself and its venv are never touched: `pip uninstall
   sextant-ai` / deleting the clone directory is the user's explicit action,
   and this command prints those instructions instead of doing them.

The corpus is plain markdown -- the report reminds you to archive it first.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

from atlas_plus import paths as atlas_paths

PROTECT_MARKER = ".sextant-protect"
STATE_MARKER = ".sextant-state"   # written by sextant-init; a dir without it is never deleted as "state"

# What `reset --state` may clear: regenerable caches and indexes only. Everything else in
# the state dir (transcripts, task DB + backups, intake ledger, sync devices, research
# reports) is the ONLY copy and is never touched by a reset (data-safety audit 2026-11-09).
DERIVED_STATE_ENTRIES = ("temporal_graph", "temporal", "embeddings")
DERIVED_STATE_GLOBS = ("*.faiss", "*.npy", "embed-index*")

# Things only Sextant creates in a state dir. A directory with none of them (and no marker)
# is somebody's folder that ATLAS_STATE_DIR happens to point at: init will not claim it and
# reset will not clear anything in it (round-B re-audit 2026-11-09, rows 1 and 2).
STATE_SIGNATURE = ("state.sqlite", "transcripts", "intake", "weekly-draft.md", "temporal_graph",
                   "embeddings", "sync", "backups")


# What the server itself creates on first contact, before init ever ran: `sextant verify` or an
# MCP client connecting early leaves exactly these. A directory holding nothing else is Sextant's
# own (AG1 and AG2, pipeline sweep 2026-11-11: init refused its own default directory after a
# failed verify and sent the user to a nested state dir that nothing else knew about).
RUNTIME_ARTIFACTS = ("logs", "search.sqlite", "search.sqlite-journal", "search.sqlite-wal",
                     "search.sqlite-shm", "state.sqlite-journal", "state.sqlite-wal", "state.sqlite-shm",
                     "mcp-search.ndjson", "activity.ndjson", STATE_MARKER)


#: Every entry Sextant itself creates in a state dir. A directory is Sextant state only when the
#: marker is present or EVERY entry is one of these: "any one generic name" (a `backups/` folder in
#: ~/Documents) made init adopt a user's folder and uninstall delete it, taxes and all (MN3,
#: pipeline sweep 2026-11-11).
EXTRA_STATE_ENTRIES = ("versions", "cache", "updates.txt", "fsdiff", "temporal", "tmp",
                       "weekly-status", "digests", "dream", "summaries", "sync.sqlite", "sextant-state")

#: What `sextant-init` scaffolds in a corpus; a corpus with none of these is somebody's folder.
CORPUS_SIGNATURE = ("identity", "projects", "reference", "feedback", "sessions", "_atlas", "decisions")


def _known_state_entries() -> frozenset:
    return (frozenset(STATE_SIGNATURE) | frozenset(RUNTIME_ARTIFACTS) | frozenset(EXTRA_STATE_ENTRIES)
            | frozenset(name for name, _ in NON_DERIVED_STATE))


def _known_state_entry(name: str) -> bool:
    known = _known_state_entries()
    if name in known:
        return True
    base = name.split(".corrupt-", 1)[0]
    return base in known or name.endswith((".sqlite-journal", ".sqlite-wal", ".sqlite-shm"))


def looks_like_sextant_state(path: Path) -> bool:
    """Marker present, or every entry is something Sextant creates (pre-marker installs, and a
    verify that ran before init). One recognised name among strangers is NOT enough."""
    if (path / STATE_MARKER).exists():
        return True
    try:
        names = [p.name for p in path.iterdir()]
    except OSError:
        return False
    return bool(names) and all(_known_state_entry(n) for n in names)


def looks_like_sextant_corpus(path: Path) -> bool:
    """At least one of the directories sextant-init scaffolds, or the protection marker."""
    if (path / PROTECT_MARKER).exists():
        return True
    return any((path / name).is_dir() for name in CORPUS_SIGNATURE)

# Non-derived subtrees the report names so nobody mistakes the state dir for a cache.
NON_DERIVED_STATE = (
    ("transcripts", "captured session transcripts (only copy)"),
    ("state.sqlite", "Brain task queue, history and pending facts"),
    ("backups", "SQLite snapshots"),
    ("brain", "Brain working data"),
    ("intake", "intake provenance ledger (never trimmed)"),
    ("sync", "sync device registry"),
    ("reports", "research reports"),
    ("logs", "activity log (only copy)"),
    ("cli-history", "Brain CLI chat history (only copy)"),
    ("security", "security audit reports"),
)


def _dir_stats(path: Path) -> tuple[int, int]:
    """(file_count, total_bytes) for a directory tree; (0, 0) when absent."""
    files = 0
    size = 0
    if path.exists():
        for p in path.rglob("*"):
            if p.is_file():
                files += 1
                try:
                    size += p.stat().st_size
                except OSError:
                    pass
    return files, size


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n / 1:.1f} {unit}"
        n = n / 1024
    return f"{n} B"


class RefusedTarget(RuntimeError):
    """A deletion the guard refuses at the last moment (symbolic link)."""


def _rmtree(path: Path) -> None:
    """rmtree that survives Windows read-only files and never follows a symbolic link.

    Python's rmtree refuses a symlink by calling onerror(os.path.islink, path); the old handler
    then chmod-ed the path, which FOLLOWED the link and left the real corpus mode 0200 and
    unreadable while the caller reported "removed" (MN3, pipeline sweep 2026-11-11).
    """
    if _is_link(path):
        raise RefusedTarget(f"{path} is a symbolic link or junction; point the variable at the real directory")

    def _onerror(func, p, _exc):
        if func is os.path.islink or os.path.islink(p):
            raise RefusedTarget(f"{p} is a symbolic link inside the target; not followed, nothing deleted there")
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            raise

    shutil.rmtree(path, onerror=_onerror)


def _session_log_info() -> dict:
    """The summarizer's session log + its version copies (REPORT ONLY).

    These live outside the corpus and state dirs, so "exactly two things"
    was never true for stop-hook users (SRE persona finding 2026-10-16).
    They may predate Sextant or be shared with the user's own files, so
    this command only reports them and never deletes them.
    """
    try:
        import os as _os
        log = atlas_paths.updates_file()
        # Attribute the log to this install only when its location was configured or sits
        # in the default (home) layout; a legacy autodetected workspace belongs to whoever
        # set it up, not necessarily to this install.
        configured = bool(_os.environ.get("ATLAS_UPDATES_FILE") or _os.environ.get("ATLAS_WORKSPACE_ROOT"))
        in_home = Path.home().resolve() in log.resolve().parents or log.resolve().parent == Path.home().resolve()
        if not (configured or in_home):
            return {"path": str(log), "exists": False, "versions_dir": "", "version_copies": 0}
        versions_dir = log.parent / "versions"
        version_copies = (
            len(list(versions_dir.glob(f"{log.stem}_*"))) if versions_dir.is_dir() else 0
        )
        return {
            "path": str(log),
            "exists": log.exists(),
            "versions_dir": str(versions_dir),
            "version_copies": version_copies,
        }
    except Exception:
        return {"path": "", "exists": False, "versions_dir": "", "version_copies": 0}


def _client_config_references() -> list[str]:
    """Claude Code / Desktop config files that still mention this install (REPORT ONLY).

    A deleted clone with a live registration means a failed MCP launch and a
    dead Stop hook on every session; surface it so the user unhooks first.
    """
    return _scan_client_configs()


def _client_config_candidates() -> list[Path]:
    home = Path.home()
    candidates = [
        home / ".claude.json",
        home / ".claude" / "settings.json",
        Path.cwd() / ".mcp.json",
        Path.cwd() / ".claude" / "settings.json",
        home / ".cursor" / "mcp.json",
        Path.cwd() / ".cursor" / "mcp.json",   # the project form setup names (AG13, sweep 11)
    ]
    # Claude Code honours CLAUDE_CONFIG_DIR; Claude Desktop keeps its own file (AG2, pipeline
    # sweep 2026-11-11: both were invisible to this report).
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if cfg_dir:
        candidates += [Path(cfg_dir) / ".claude.json", Path(cfg_dir) / "settings.json"]
    # Codex CLI and opencode registrations were invisible to this report (AG12, sweep 10).
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    candidates += [Path(codex_home) / "config.toml"] if codex_home else [home / ".codex" / "config.toml"]
    candidates += [Path.cwd() / "opencode.json", Path.cwd() / "opencode.jsonc",
                   home / ".config" / "opencode" / "opencode.json",
                   home / ".config" / "opencode" / "opencode.jsonc"]
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        candidates += [Path(xdg) / "opencode" / "opencode.json", Path(xdg) / "opencode" / "opencode.jsonc"]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            candidates.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
        # The Microsoft Store (MSIX) build keeps its roaming files under the package's
        # LocalCache, not %APPDATA% (AG13, sweep 11).
        localappdata = os.environ.get("LOCALAPPDATA", "").strip()
        if localappdata:
            try:
                for pkg in (Path(localappdata) / "Packages").glob("Claude_*"):
                    candidates.append(pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
            except OSError:
                pass
    else:
        candidates += [home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
                       home / ".config" / "Claude" / "claude_desktop_config.json"]
    return candidates


def _scan_client_configs() -> list[str]:
    hits: list[str] = []
    candidates = _client_config_candidates()
    # Only count a config that points at THIS install (its venv or its source tree), not any
    # Sextant mention: a second install on the same machine must not be told to unhook the
    # first one (agent-runbook rerun 2026-11-09).
    import sys as _sys
    markers = {str(Path(_sys.prefix).resolve()), str(Path(__file__).resolve().parents[1])}

    def _norm(p: str) -> str:
        # JSON stores a Windows path with doubled backslashes: fold the escaped form first, or no
        # registration in a JSON file ever matched on Windows (pipeline sweep 2026-11-11).
        return p.replace("\\\\", "/").replace("\\", "/").lower()

    norm_markers = [_norm(m) for m in markers]
    for cfg in candidates:
        try:
            if cfg.is_file():
                if cfg.stat().st_size > 4_000_000:
                    continue   # no client config is that big; a planted one must not be read whole (MN12)
                text = _norm(cfg.read_text(encoding="utf-8", errors="replace"))
                if any(m in text for m in norm_markers):
                    hits.append(str(cfg))
        except OSError:
            continue
    return hits


_CMD_PATH_RE = re.compile(
    r'(?:"command"\s*:\s*\[?\s*|\bcommand\s*=\s*\[?\s*)'
    r'(?:"((?:[^"\\]|\\.)+)"|\'([^\']+)\')')   # JSON string or array, TOML basic or literal string (MN16)


def _running_hints(corpus: Path) -> list[str]:
    """Signs that a server or writer is still using this install: the report said nothing while
    a live writing server had its corpus deleted from under it (AG15, sweep 11)."""
    import socket
    import time as _time
    hints: list[str] = []
    # Both servers read the corpus; the port names say whether the value is set or the default
    # (MN18: the hint named ATLAS_MCP_PORT while it was unset, and never looked at the dashboard).
    for var, default, what in (("ATLAS_MCP_PORT", 8765, "MCP server"), ("ATLAS_WEB_PORT", 8766, "dashboard")):
        raw = os.environ.get(var)
        try:
            port = int(raw) if raw else default
        except ValueError:
            port = default
        label = f"{var}={port}" if raw else f"the default {what} port {port}; {var} is not set in this shell"
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                hints.append(f"something answers on 127.0.0.1:{port} ({label}): if it is this install's "
                             f"{what}, stop it before removing data")
        except OSError:
            pass
    try:
        locks = corpus / "_atlas" / ".locks"
        if locks.is_dir():
            fresh = [p for p in locks.iterdir() if _time.time() - p.stat().st_mtime < 120]
            if fresh:
                hints.append(f"{len(fresh)} write lock(s) touched in the last two minutes under {locks}: "
                             "a writer may still be running")
    except OSError:
        pass
    return hints


def _stale_client_references() -> list[str]:
    """Client configs whose Sextant command points at a path that no longer exists: after a
    moved or deleted venv the registration matched nothing and went unreported (AG13)."""
    out: list[str] = []
    for cfg in _client_config_candidates():
        try:
            if not cfg.is_file() or cfg.stat().st_size > 4_000_000:
                continue
            text = cfg.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _CMD_PATH_RE.finditer(text):
            raw = (m.group(1) or m.group(2) or "").replace("\\\\", "\\")
            exe = _first_token(raw)
            low = exe.lower()
            if "sextant" not in low and "atlas_plus" not in low and "atlas-memory" not in low:
                continue
            if os.sep in exe or "/" in exe:
                if not Path(exe).exists():
                    out.append(f"{cfg} -> {exe}")
    return out


def _first_token(command: str) -> str:
    """The executable of a command line: a quoted path or the text up to the first space. The
    whole hook line (`'...python.exe' -m atlas_plus.hooks.run ...`) was tested as a path and every
    correct hook was reported as stale (MN13, sweep 11)."""
    cmd = command.strip()
    if cmd[:1] in ("'", '"'):
        end = cmd.find(cmd[0], 1)
        return cmd[1:end] if end > 0 else cmd[1:]
    # Unquoted: the executable ends where the first option starts (a path may carry spaces).
    m = re.search(r"\s-{1,2}[A-Za-z]", cmd)
    return cmd[:m.start()] if m else cmd


def report() -> dict:
    """Inventory everything a Sextant install leaves on a machine."""
    corpus = atlas_paths.memory_root()
    state = atlas_paths.state_dir()
    corpus_files, corpus_size = _dir_stats(corpus)
    state_files, state_size = _dir_stats(state)
    return {
        "corpus": {
            "path": str(corpus),
            "exists": corpus.exists(),
            "files": corpus_files,
            "size": corpus_size,
            "protected": (corpus / PROTECT_MARKER).exists(),
        },
        "state": {
            "path": str(state),
            "exists": state.exists(),
            "files": state_files,
            "size": state_size,
        },
        "session_log": _session_log_info(),
        "client_configs": _client_config_references(),
        "stale_client_configs": _stale_client_references(),
        "running": _running_hints(corpus),
        # A command run before the variables were set (components, search) creates the HOME
        # defaults; with the variables set, this report never mentioned them (MN6, pipeline
        # sweep 2026-11-11).
        "stray_defaults": _stray_defaults(corpus, state),
    }


def _stray_defaults(corpus: Path, state: Path) -> list[dict]:
    out: list[dict] = []
    home = Path.home()
    for label, default, configured in (("corpus", home / ".memory", corpus), ("state", home / ".atlas", state)):
        try:
            if default.exists() and _resolved(default) != _resolved(configured):
                n, size = _dir_stats(default)
                out.append({"kind": label, "path": str(default), "files": n, "size": size})
        except OSError:
            continue
    return out


def _resolved(p: Path) -> Path:
    """Resolve symlinks/junctions, ~ and relative spellings so overlap checks compare real places."""
    try:
        return p.expanduser().resolve()
    except OSError:
        return p.expanduser().absolute()


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _protected_roots() -> list[tuple[str, Path]]:
    import sys as _sys
    roots = [("home directory", Path.home()), ("workspace root", atlas_paths.workspace_root()),
             ("projects root", atlas_paths.projects_root()), ("memory corpus", atlas_paths.memory_root()),
             ("state dir", atlas_paths.state_dir()), ("Python installation", Path(_sys.prefix))]
    return [(label, _resolved(p)) for label, p in roots]


def _contains(container: Path, inner: Path) -> bool:
    """True when ``inner`` is ``container`` or lies under it, decided by file identity for every
    ancestor (a mapped drive or UNC spelling of the same place fools a textual relative_to;
    round-B re-audit 2026-11-09, N-D)."""
    if _is_within(inner, container):
        return True
    for candidate in (inner, *inner.parents):
        if _same_place(candidate, container):
            return True
    return False


def _same_place(a: Path, b: Path) -> bool:
    """True when two paths name the same directory, by identity when both exist (catches UNC,
    8.3 short names, junctions and case differences that string comparison misses)."""
    if a == b:
        return True
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def validate_destructive_target(kind: str, target: Path) -> str | None:
    """Central guard run before ANY recursive deletion (misuse + data-safety audits 2026-11-09).

    `kind` is "state" or "corpus". Returns a refusal string (the CLI prints it and exits 1) or
    None when the target is a safe, specific directory. The guard is POSITIVE for the state
    dir: it must carry the marker sextant-init writes. Both kinds refuse filesystem roots,
    UNC / `\\\\?\\` spellings, anything that IS or CONTAINS the home directory, the workspace
    or projects root, the other data root, or the Python installation, and (for state) any
    overlap with the corpus or a directory carrying the protection marker.
    """
    raw = str(target)
    if raw.startswith(("\\\\", "//")):
        return f"REFUSED: {kind} path {raw} is a UNC or extended-length spelling; use a plain local path"
    t = _resolved(target)
    if str(t).startswith(("\\\\", "//")):
        return f"REFUSED: {kind} path {raw} resolves to a network or extended-length path ({t}); use a plain local path"
    if t.parent == t or t == Path(t.anchor):
        return f"REFUSED: {kind} path {t} is a filesystem root"
    if _is_link(target):
        return (f"REFUSED: {kind} path {raw} is a symbolic link or junction to {t}; point the variable "
                "at the real directory (nothing behind a link is ever deleted)")
    for label, root in _protected_roots():
        if label == f"{'state dir' if kind == 'state' else 'memory corpus'}":
            continue  # the target itself
        if _same_place(t, root):
            return f"REFUSED: {kind} path {t} is the {label}; set a specific directory"
        if _contains(t, root) and not _same_place(t, root):
            return f"REFUSED: {kind} path {t} contains the {label} ({root})"
    if kind == "state":
        corpus = _resolved(atlas_paths.memory_root())
        if _same_place(t, corpus):
            return f"REFUSED: state dir equals the memory corpus ({t}); refusing to delete your notes"
        if _contains(corpus, t):
            return f"REFUSED: state dir {t} is inside the memory corpus {corpus}"
        if (t / PROTECT_MARKER).exists():
            return f"REFUSED: {PROTECT_MARKER} marker present in {t}"
        if t.exists() and not (t / STATE_MARKER).exists():
            return (f"REFUSED: {t} does not carry the {STATE_MARKER} marker that sextant-init writes, "
                    f"so it is not known to be a Sextant state dir; if it is one, run sextant-init with "
                    f"ATLAS_STATE_DIR pointing at it first")
    if kind == "corpus" and t.exists() and not looks_like_sextant_corpus(t):
        return (f"REFUSED: {t} does not look like a Sextant corpus (no identity/, projects/, "
                f"reference/, feedback/, sessions/ or _atlas/ inside it); a mistyped ATLAS_MEMORY_ROOT "
                "must not turn into rm -rf of somebody's folder")
    return None


def _is_link(p: Path) -> bool:
    """Symbolic link, or an NTFS junction (Path.is_symlink is False for junctions)."""
    try:
        if p.is_symlink():
            return True
        isj = getattr(os.path, "isjunction", None)
        return bool(isj and isj(str(p)))
    except OSError:
        return False


def state_inventory(state: Path) -> list[tuple[str, str, int]]:
    """(entry, description, file count) for every non-derived subtree present in the state dir."""
    out: list[tuple[str, str, int]] = []
    for entry, desc in NON_DERIVED_STATE:
        p = state / entry
        if p.is_file():
            out.append((entry, desc, 1))
        elif p.is_dir():
            out.append((entry, desc, sum(1 for q in p.rglob("*") if q.is_file())))
    return out


def confirm_state_removal(yes: bool, confirm_path: str | None, ask=input) -> str | None:
    """The state dir holds irreplaceable data, so removing it takes the same typed confirmation
    as the corpus (audit 2026-11-09). Returns a refusal string or None when authorized."""
    state = atlas_paths.state_dir()
    if not state.exists():
        return None
    refusal = validate_destructive_target("state", state)
    if refusal:
        return refusal
    if not yes:
        return "refused: --yes required"
    expected = str(state)
    if confirm_path is not None:
        provided = confirm_path
    else:
        try:
            provided = ask(f"Type the FULL state-dir path to confirm deletion ({expected}): ")
        except (EOFError, OSError):
            return ("refused: no interactive terminal to confirm the state path "
                    "(use --confirm-state-path <path> in scripts)")
    import os as _os

    def _norm(p: str) -> str:
        return _os.path.normcase(_os.path.normpath(str(p).strip()))

    if _norm(provided) != _norm(expected):
        return "refused: state confirmation path did not match"
    return None


def clear_derived_state(yes: bool) -> str:
    """reset --state: remove ONLY regenerable caches/indexes inside the state dir."""
    state = atlas_paths.state_dir()
    if not state.exists():
        return "absent"
    refusal = validate_destructive_target("state", state)
    if refusal:
        return refusal
    if not (state / STATE_MARKER).exists() or not any((state / n).exists() for n in STATE_SIGNATURE):
        # The marker alone proves only that sextant-init once ran with this env. Clearing
        # fixed names out of a directory that shows no other sign of being Sextant state could
        # hit somebody's own "logs" or "temporal" folder (round-B re-audit 2026-11-09, N2).
        return ("REFUSED: this directory carries the marker but none of Sextant's own state "
                f"({', '.join(STATE_SIGNATURE[:4])}, ...); nothing cleared")
    if not yes:
        return "refused: --yes required"
    cleared: list[str] = []
    for entry in DERIVED_STATE_ENTRIES:
        p = state / entry
        if p.is_dir():
            _rmtree(p); cleared.append(entry + "/")
        elif p.is_file():
            p.unlink(); cleared.append(entry)
    for pattern in DERIVED_STATE_GLOBS:
        for p in state.glob(pattern):
            if p.is_file():
                p.unlink(); cleared.append(p.name)
    return "cleared derived: " + (", ".join(cleared) if cleared else "nothing to clear") + " (transcripts, task DB, ledger, sync, reports kept)"


def remove_state(yes: bool, confirm_path: str | None = None, ask=input,
                 preauthorized: bool = False) -> str:
    """Remove the whole state dir. NOT a cache wipe: see NON_DERIVED_STATE. Requires the typed
    state path (or --confirm-state-path) in addition to --yes."""
    state = atlas_paths.state_dir()
    if not state.exists():
        return "absent"
    if not preauthorized:
        refusal = confirm_state_removal(yes, confirm_path, ask=ask)
        if refusal is not None:
            return refusal
    else:
        refusal = validate_destructive_target("state", state)
        if refusal:
            return refusal
    try:
        _rmtree(state)
    except RefusedTarget as exc:
        return f"refused: {exc}"
    return "removed"


def confirm_corpus_removal(yes: bool, confirm_path: str | None, ask=input) -> str | None:
    """Collect and validate EVERY corpus-removal confirmation WITHOUT deleting.

    Returns a refusal string, or None when removal is fully authorized.
    Split out (Codex artifact audit 2026-09-21) so --all can validate ALL
    confirmations BEFORE mutating anything -- previously state was deleted,
    then the corpus prompt hit EOF in a noninteractive shell and crashed,
    leaving a partial uninstall.
    """
    corpus = atlas_paths.memory_root()
    if not corpus.exists():
        return None  # nothing to authorize
    if (corpus / PROTECT_MARKER).exists():
        return (
            f"REFUSED: {PROTECT_MARKER} marker present -- this corpus is "
            "protected from scripted deletion. Remove the marker by hand "
            "first if you truly mean it."
        )
    if not yes:
        return "refused: --yes required"
    expected = str(corpus)
    if confirm_path is not None:
        provided = confirm_path
    else:
        try:
            provided = ask(f"Type the FULL corpus path to confirm deletion ({expected}): ")
        except (EOFError, OSError):  # closed/captured stdin (scripts, CI)
            return (
                "refused: no interactive terminal to confirm the corpus path "
                "(use --confirm-path <path> in scripts)"
            )
    # Separator- and case-insensitive comparison: the confirmation proves the
    # user knows WHICH corpus, and a Windows user pasting the forward-slash
    # form from docs knows exactly that (impatient-persona finding). Any
    # other difference still refuses.
    import os as _os

    def _norm(p: str) -> str:
        return _os.path.normcase(_os.path.normpath(str(p).strip()))

    if _norm(provided) != _norm(expected):
        return "refused: confirmation path did not match"
    return None


def remove_corpus(yes: bool, confirm_path: str | None, ask=input,
                  preauthorized: bool = False) -> str:
    corpus = atlas_paths.memory_root()
    if not corpus.exists():
        return "absent"
    refusal = validate_destructive_target("corpus", corpus)
    if refusal:
        return refusal
    if not preauthorized:
        refusal = confirm_corpus_removal(yes, confirm_path, ask=ask)
        if refusal is not None:
            return refusal
    try:
        _rmtree(corpus)
    except RefusedTarget as exc:
        return f"refused: {exc}"
    return "removed"


def _prog() -> str:
    """Report the name the user actually typed (sextant-* alias, subcommand, or atlas-*)."""
    import sys
    from pathlib import Path as _P
    return _P(sys.argv[0]).stem if sys.argv and sys.argv[0] else "atlas-uninstall"


def main() -> None:
    try:
        _main()
    except KeyboardInterrupt:
        # a Ctrl-C at a confirmation prompt is an answer, not a crash (sweep 13, MN19)
        print("\naborted: nothing was removed", file=sys.stderr)
        raise SystemExit(130)


def _main() -> None:
    from atlas_plus._console import utf8_stdout_when_piped
    utf8_stdout_when_piped()
    parser = argparse.ArgumentParser(
        prog=_prog(),
        description="Report and (only when explicitly told) remove Sextant data.",
    )
    parser.add_argument("--state", action="store_true",
                        help="remove the state dir (transcripts, task DB, ledger, sync, reports: NOT a cache; asks for its path)")
    parser.add_argument("--corpus", action="store_true", help="remove the memory corpus (YOUR DATA)")
    parser.add_argument("--all", action="store_true", help="remove both")
    parser.add_argument("--dry-run", action="store_true",
                        help="report only, even with --state/--corpus/--all (a safe preview)")
    parser.add_argument("--json", action="store_true", help="print the report as one JSON object")
    parser.add_argument("--yes", "-y", action="store_true", help="required for any deletion")
    parser.add_argument(
        "--confirm-path",
        default=None,
        help="non-interactive corpus confirmation: must name the corpus path (slash style and trailing separators are tolerated, case only where the filesystem ignores it; anything else refuses)",
    )
    parser.add_argument(
        "--confirm-state-path",
        default=None,
        help="non-interactive state-dir confirmation (same rule as --confirm-path, for the state path)",
    )
    args = parser.parse_args()

    info = report()
    if args.json:
        print(json.dumps(info, indent=2, default=str))
        if not args.dry_run and (args.state or args.corpus or args.all):
            print("refused: --json is a report; run the removal without it", file=sys.stderr)
            raise SystemExit(1)
        return
    c, s = info["corpus"], info["state"]
    print("Sextant on this machine:")
    print(f"  corpus: {c['path']}")
    if c["exists"]:
        prot = "  [PROTECTED by .sextant-protect]" if c["protected"] else ""
        print(f"          {c['files']} file(s), {_fmt_size(c['size'])}{prot}")
    else:
        print("          (absent)")
    print(f"  state:  {s['path']}")
    print(f"          {s['files']} file(s), {_fmt_size(s['size'])}" if s["exists"] else "          (absent)")
    if s["exists"]:
        inv = state_inventory(atlas_paths.state_dir())
        if inv:
            print("          holds (not rebuildable): " + "; ".join(f"{e} = {d} ({n} file(s))" for e, d, n in inv))
    log = info["session_log"]
    if log["exists"]:
        extra = f" (+{log['version_copies']} version copies in {log['versions_dir']})" if log["version_copies"] else ""
        print(f"  session log: {log['path']}{extra}")
        print("           written by the stop-hook summarizer; may contain or predate")
        print("           your own notes, so this command never deletes it -- review by hand.")
    for cfg in info["client_configs"]:
        print(f"  client config still references this install: {cfg}")
    for hint in info.get("running", []):
        print(f"  running?  {hint}")
    for stale in info.get("stale_client_configs", []):
        print(f"  client config points at a Sextant command that no longer exists (moved or deleted "
              f"venv?): {stale}")
        print("           re-run `sextant setup` from the live install and replace that entry.")
    for stray in info.get("stray_defaults", []):
        print(f"  also on this machine: the default {stray['kind']} location {stray['path']} "
              f"({stray['files']} file(s), {_fmt_size(stray['size'])}) is not the configured one: either a "
              "command run before the variables were set created it, or another install on this machine "
              "uses the defaults. Check which before removing anything (with ATLAS_" +
              ("MEMORY_ROOT" if stray["kind"] == "corpus" else "STATE_DIR") + " pointing at it).")
    if info["client_configs"]:
        print("           unhook BEFORE deleting the clone, venv or tool install: `claude mcp remove -s user sextant`")
        print("           (with the same CLAUDE_CONFIG_DIR the registration used), `codex mcp remove sextant` for")
        print("           Codex CLI, delete the `sextant` entry from opencode.json, and delete any hook command")
        print("           containing atlas_plus.hooks.run (older installs: atlas_plus.hooks.session_start or")
        print("           atlas_plus.summarizer.hook_entry) from settings.json.")
    print("  package: this command never uninstalls the package or venv --")
    print("           run `pip uninstall sextant-ai` (or `uv tool uninstall sextant-ai` / `pipx uninstall sextant-ai`)")
    print("           and delete the clone or venv yourself (a uv tool or pipx install has neither).")

    do_state = (args.state or args.all) and not args.dry_run
    do_corpus = (args.corpus or args.all) and not args.dry_run
    if args.dry_run and (args.state or args.corpus or args.all):
        print("\n--dry-run: nothing was changed (drop --dry-run to remove).")
        return
    if not do_state and not do_corpus:
        print("\nReport only. Nothing was changed. To remove: --state, --corpus or --all, each with --yes;\n"
              "the corpus also needs --confirm-path <corpus path> and the state dir --confirm-state-path\n"
              "<state path> typed back exactly (see --help). Unhook the client first.")
        return

    if do_corpus and c["exists"]:
        print("\nThe corpus is plain markdown: consider zipping it before deletion.")

    # TWO-PHASE: validate every confirmation BEFORE deleting anything, so a
    # refusal (or closed stdin) can never leave a partial teardown.
    corpus_refusal = confirm_corpus_removal(args.yes, args.confirm_path) if do_corpus else None
    state_refusal = confirm_state_removal(args.yes, args.confirm_state_path) if do_state else None
    if corpus_refusal or state_refusal:
        if do_state:
            print(f"state:  {state_refusal or 'not removed (corpus confirmation failed; nothing was deleted)'}")
        if do_corpus:
            print(f"corpus: {corpus_refusal or 'not removed'}")
        raise SystemExit(1)

    failed = False
    if do_state:
        outcome = remove_state(args.yes, args.confirm_state_path, preauthorized=True)
        print(f"state:  {outcome}")
        failed = failed or outcome.startswith(("refused", "REFUSED"))
    if do_corpus:
        outcome = remove_corpus(args.yes, args.confirm_path, preauthorized=True)
        print(f"corpus: {outcome}")
        failed = failed or outcome.startswith(("refused", "REFUSED"))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
