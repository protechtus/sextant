"""Guardrails: lessons from the Sextant sweeps, enforced over the WHOLE private tree (ATL-069).

A lesson is not learned until it is a check that runs over every module, the private-only ones
included (github_intake, trading, gatekeeper, watchtower, executors_local, slcene_tip). The public
Sextant is a filtered build of this repo, so shared code cannot drift; these tests exist for the
code the public sweeps never see. Each test is one lesson (LES-G<n>, docs/lessons/LESSONS.md).

Every exception is listed in an allowlist WITH A REASON. A new hit is fixed, or allowlisted with
its reason in the same commit; a bare path is not an entry. The scans use ``ast``, not regex, so
a call that spans lines is seen as one call.
"""
from __future__ import annotations

import ast
import fnmatch
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "atlas_plus"
TESTS = REPO / "tests"

# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------


def _py_files(*roots: Path) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        out.extend(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(out)


def _rel(p: Path) -> str:
    return p.relative_to(REPO).as_posix()


def _parse(p: Path) -> ast.Module:
    return ast.parse(p.read_text(encoding="utf-8", errors="replace"), filename=str(p))


def _dotted(node: ast.AST) -> str:
    """``httpx.get``, ``_ur.urlopen``, ``STATE_DIR.mkdir`` ... or '' for anything else."""
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive
        return ""


def _has_kw(call: ast.Call, name: str) -> bool:
    return any(kw.arg == name for kw in call.keywords) or any(kw.arg is None for kw in call.keywords)


def _line(p: Path, lineno: int) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace").splitlines()[lineno - 1].strip()
    except IndexError:
        return ""


# allowlist entry: (relative path, snippet that must appear on the offending line or "*", reason)
Allow = tuple[str, str, str]


def _allowed(rel: str, line_text: str, allow: list[Allow]) -> bool:
    for path, snippet, reason in allow:
        assert reason.strip(), f"allowlist entry for {path} has no reason"
        if path == rel and (snippet == "*" or snippet in line_text):
            return True
    return False


def _report(offenders: list[str], lesson: str, rule: str) -> None:
    assert not offenders, (
        f"{lesson}: {rule}\n"
        "Fix the call, or add an allowlist entry WITH A REASON in tests/test_guardrails.py.\n  "
        + "\n  ".join(offenders)
    )


# ----------------------------------------------------------------------------------------------
# LES-G1  every outbound HTTP call carries a timeout
#   Sweeps: AG11 (doctor probes), AG54 (TLS context), MN86 (inbox fetch). A fetch without a timeout
#   hangs a worker, a hook or a dashboard request forever when the peer stalls.
# ----------------------------------------------------------------------------------------------

_HTTP_FUNCS = ("httpx.get", "httpx.post", "httpx.put", "httpx.patch", "httpx.delete", "httpx.head",
               "httpx.request", "httpx.stream", "httpx.Client", "httpx.AsyncClient",
               "requests.get", "requests.post", "requests.put", "requests.delete", "requests.request",
               "urlopen")
ALLOW_G1: list[Allow] = []


def _is_http_call(name: str) -> bool:
    # `_httpx.post`, `httpx.post`, `urllib.request.urlopen`, `_ur.urlopen` all count.
    short = name.split(".")[-1]
    if short == "urlopen":
        return True
    return any(name == f or name.endswith("." + f) or name.endswith(f) and "." in name for f in _HTTP_FUNCS)


def test_g1_outbound_http_calls_have_a_timeout():
    offenders = []
    for p in _py_files(SRC):
        rel = _rel(p)
        for node in ast.walk(_parse(p)):
            if not isinstance(node, ast.Call):
                continue
            name = _dotted(node.func)
            if not _is_http_call(name) or _has_kw(node, "timeout"):
                continue
            text = _line(p, node.lineno)
            if _allowed(rel, text, ALLOW_G1):
                continue
            offenders.append(f"{rel}:{node.lineno}: {name}(...) without timeout=")
    _report(offenders, "LES-G1", "every httpx/requests/urlopen call passes timeout= (or the Client was built with one)")


# ----------------------------------------------------------------------------------------------
# LES-G2  every blocking subprocess call carries a timeout
#   Sweeps: AG36 (resilience), MN85 (stuck pip), the stop hook lanes. Popen is exempt (long-lived
#   children are waited on elsewhere); run/check_output/check_call/call block the caller.
# ----------------------------------------------------------------------------------------------

_SUBPROCESS_FUNCS = ("subprocess.run", "subprocess.check_output", "subprocess.check_call", "subprocess.call")
ALLOW_G2: list[Allow] = [
    ("src/atlas_plus/summarizer/review.py", "subprocess.run([editor, tmp]",
     "opens the user's editor and waits until they close it; a timeout would discard their edit"),
]


def test_g2_blocking_subprocess_calls_have_a_timeout():
    offenders = []
    for p in _py_files(SRC):
        rel = _rel(p)
        for node in ast.walk(_parse(p)):
            if not isinstance(node, ast.Call):
                continue
            name = _dotted(node.func)
            if not any(name == f or name.endswith("." + f.split(".")[-1]) and name.startswith(("subprocess", "_subprocess", "sp."))
                       for f in _SUBPROCESS_FUNCS):
                continue
            if _has_kw(node, "timeout"):
                continue
            text = _line(p, node.lineno)
            if _allowed(rel, text, ALLOW_G2):
                continue
            offenders.append(f"{rel}:{node.lineno}: {name}(...) without timeout=")
    _report(offenders, "LES-G2", "subprocess.run/check_output/check_call/call pass timeout=")


# LES-G2, second half (KL1 follow-up, sweep 26): a child never inherits stdin. A service, a hook or a
# scheduled task has no terminal: a child that prompts (git credentials, an elevation question, a CLI
# that reads a prompt from stdin when it is a pipe) waits forever on input nobody can give, and a
# detached worker keeps the client's pipe open. Every call closes stdin (stdin=subprocess.DEVNULL)
# or feeds it (input=, stdin=PIPE). Popen is included here: inheriting is the default there too.
ALLOW_G2_STDIN: list[Allow] = [
    ("src/atlas_plus/summarizer/review.py", "subprocess.run([editor, tmp]",
     "opens the user's editor in their terminal: it needs the real stdin"),
]


def test_g2_subprocess_calls_close_or_feed_stdin():
    offenders = []
    for p in _py_files(SRC):
        rel = _rel(p)
        for node in ast.walk(_parse(p)):
            if not isinstance(node, ast.Call):
                continue
            name = _dotted(node.func)
            root, _, last = name.rpartition(".")
            if last not in ("run", "check_output", "check_call", "call", "Popen") \
                    or root.split(".")[-1] not in _SUBPROCESS_ROOTS:
                continue
            if _has_kw(node, "stdin") or _has_kw(node, "input"):
                continue
            text = _line(p, node.lineno)
            if _allowed(rel, text, ALLOW_G2_STDIN):
                continue
            offenders.append(f"{rel}:{node.lineno}: {name}(...) inherits stdin")
    _report(offenders, "LES-G2", "every subprocess call passes stdin=subprocess.DEVNULL (or input= / stdin=PIPE "
                                 "when it feeds the child)")


# ----------------------------------------------------------------------------------------------
# LES-G3  state directories are created through the private-dir helper
#   Sweep 25 MN86 finding 6 / AG98: under umask 022 a lazily created state or corpus root was 0755.
#   `_fsutil.make_private_dir` is the one place that knows the fix; AG98 left about eight lazy
#   `STATE_DIR.mkdir` callers, and the private modules were never checked.
# ----------------------------------------------------------------------------------------------

_STATE_RECEIVER = re.compile(r"state", re.IGNORECASE)
ALLOW_G3: list[Allow] = [
    ("src/atlas_plus/_fsutil.py", "*", "the helper itself"),
]


def test_g3_state_dirs_are_created_private():
    offenders = []
    for p in _py_files(SRC):
        rel = _rel(p)
        module_is_state = p.stem.startswith("state")
        for node in ast.walk(_parse(p)):
            if not isinstance(node, ast.Call):
                continue
            name = _dotted(node.func)
            if name.endswith(".mkdir"):
                receiver = name[: -len(".mkdir")]
            elif name in ("os.makedirs", "makedirs"):
                receiver = _dotted(node.args[0]) if node.args else ""
            else:
                continue
            if not (_STATE_RECEIVER.search(receiver) or module_is_state):
                continue
            text = _line(p, node.lineno)
            if _allowed(rel, text, ALLOW_G3):
                continue
            offenders.append(f"{rel}:{node.lineno}: {name}(...) on a state path; use _fsutil.make_private_dir")
    _report(offenders, "LES-G3", "a state directory is created with atlas_plus._fsutil.make_private_dir, never a bare mkdir")


# ----------------------------------------------------------------------------------------------
# LES-G4  no workspace paths in source or tests
#   The release gate's workspace-path category covers shipped files (MN86 finding 10 found 16 of
#   them). Private modules and private tests were outside it; a hard-coded operator path is a
#   portability bug and a leak waiting for the day a module moves to the public list.
# ----------------------------------------------------------------------------------------------

_WORKSPACE = re.compile(r"C:[\\/]+Tools", re.IGNORECASE)
ALLOW_G4: list[Allow] = [
    ("tests/test_brain_worker_launcher.py", "Set-Location",
     "asserts the literal working directory written into the private deploy wrapper"),
    ("src/atlas_plus/telegram/slcene_tip.py", "ATLAS_SLCENE_ROOT",
     "private module; ATLAS_SLCENE_ROOT overrides, the default documents the operator's layout"),
]


def test_g4_no_workspace_paths_in_source_or_tests():
    offenders = []
    for p in _py_files(SRC, TESTS):
        if p.name == Path(__file__).name:
            continue
        rel = _rel(p)
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _WORKSPACE.search(line) and not _allowed(rel, line, ALLOW_G4):
                offenders.append(f"{rel}:{i}: {line.strip()[:100]}")
    _report(offenders, "LES-G4", "no operator workspace path in src/ or tests/; use env or a neutral path")


# ----------------------------------------------------------------------------------------------
# LES-G5  vendored static files are checked out byte-for-byte
#   AG98: `git apply` on Windows wrote CRLF into tailwind-3.4.17.js and the SRI hash no longer
#   matched, so the browser refused the script. Every file under web/static needs a `-text`
#   attribute; a new vendored file without one repeats the bug on the next Windows checkout.
# ----------------------------------------------------------------------------------------------


def test_g5_vendored_static_files_have_text_disabled():
    attrs = (REPO / ".gitattributes").read_text(encoding="utf-8").splitlines()
    patterns = []
    for line in attrs:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if "-text" in parts[1:] or "binary" in parts[1:]:
            patterns.append(parts[0])
    static = SRC / "web" / "static"
    offenders = []
    for f in static.rglob("*"):
        if f.is_dir():
            continue
        rel = _rel(f)
        if not any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(f.name, pat) for pat in patterns):
            offenders.append(rel)
    _report(offenders, "LES-G5", "every file under src/atlas_plus/web/static matches a `-text` or `binary` line in .gitattributes")


# ----------------------------------------------------------------------------------------------
# LES-G6  the session override is read by its own predicate only
#   ATL-068 (2026-11-14): a session-variable early return inside the general gate let every Brain
#   job write with autonomy off (47 confidence flips, 290 tags, 35 tickets). The fix scoped the
#   variable to atlas_plus.autonomous. This test keeps every other module from reading it as a
#   gate again; display-only readers are allowlisted with their reason.
# ----------------------------------------------------------------------------------------------

_SESSION_VAR = "ATLAS_SESSION_AUTONOMOUS"
ALLOW_G6: list[Allow] = [
    ("src/atlas_plus/doctor/cli.py", "*", "doctor displays the raw value; it never gates a write on it"),
    ("src/atlas_plus/summarizer/hook_entry.py", "_raw = ",
     "diagnoses a garbage value for the error message (AG43); the gate is is_session_autonomous()"),
]


def test_g6_session_override_is_read_only_by_autonomous_py():
    offenders = []
    for p in _py_files(SRC):
        if p.name == "autonomous.py":
            continue
        rel = _rel(p)
        for node in ast.walk(_parse(p)):
            if not isinstance(node, ast.Call):
                continue
            name = _dotted(node.func)
            if not (name.endswith("environ.get") or name.endswith("getenv")):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant) or node.args[0].value != _SESSION_VAR:
                continue
            text = _line(p, node.lineno)
            if _allowed(rel, text, ALLOW_G6):
                continue
            offenders.append(f"{rel}:{node.lineno}: reads {_SESSION_VAR} directly")
    _report(offenders, "LES-G6", "only atlas_plus.autonomous reads ATLAS_SESSION_AUTONOMOUS (use is_session_autonomous / require_session_write)")


# ----------------------------------------------------------------------------------------------
# LES-G7  text I/O names its encoding
#   LES-016 (86 raw rows: AG17, AG22, AG46 ...): the default text encoding is the machine's locale
#   (cp1252, cp932, ASCII under LC_ALL=C), so a note, a config or a child's output that is UTF-8
#   turns into mojibake or a UnicodeDecodeError on someone else's machine. `git` writes UTF-8
#   whatever the console says; a hook that decoded it with the locale died on the first commit
#   message with a byte cp1252 does not define. Every text open/read/write and every subprocess
#   call that decodes output passes encoding=; the subprocess ones also pass errors=, because a
#   child's bytes are never ours to trust.
# ----------------------------------------------------------------------------------------------

_SUBPROCESS_ROOTS = ("subprocess", "_subprocess", "_sp", "sp")
_NOT_FILE_OPENERS = ("os", "webbrowser", "tarfile", "zipfile", "gzip", "bz2", "lzma", "wave", "shelve", "dbm",
                     "socket", "sqlite3", "urllib", "Image")
ALLOW_G7: list[Allow] = []


def _open_mode(call: ast.Call, pos: int):
    for kw in call.keywords:
        if kw.arg == "mode":
            return kw.value.value if isinstance(kw.value, ast.Constant) else None
    if len(call.args) > pos:
        arg = call.args[pos]
        return arg.value if isinstance(arg, ast.Constant) else None
    return "r"


def _g7_problem(node: ast.Call) -> str:
    name = _dotted(node.func)
    root, short = name.split(".")[0], name.split(".")[-1]
    if short in ("read_text", "write_text") and isinstance(node.func, ast.Attribute):
        positional = 2 if short == "write_text" else 1          # Path.write_text(data, encoding)
        if not _has_kw(node, "encoding") and len(node.args) < positional:
            return f"{short}(...) without encoding="
        return ""
    if root in _SUBPROCESS_ROOTS and short in ("run", "check_output", "check_call", "call", "Popen"):
        decodes = any(kw.arg in ("text", "universal_newlines")
                      and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
                      for kw in node.keywords)
        if decodes and not (_has_kw(node, "encoding") and _has_kw(node, "errors")):
            return f"{name}(text=True) without encoding= and errors="
        return ""
    if name in ("open", "io.open", "codecs.open") or (
            short == "open" and isinstance(node.func, ast.Attribute) and root not in _NOT_FILE_OPENERS):
        mode = _open_mode(node, 1 if name in ("open", "io.open", "codecs.open") else 0)
        if isinstance(mode, str) and "b" not in mode and not _has_kw(node, "encoding"):
            return f"{name}(..., {mode!r}) without encoding="
    return ""


def test_g7_text_io_names_its_encoding():
    offenders = []
    for p in _py_files(SRC):
        rel = _rel(p)
        for node in ast.walk(_parse(p)):
            if not isinstance(node, ast.Call):
                continue
            problem = _g7_problem(node)
            if problem and not _allowed(rel, _line(p, node.lineno), ALLOW_G7):
                offenders.append(f"{rel}:{node.lineno}: {problem}")
    _report(offenders, "LES-G7", "text open/read_text/write_text pass encoding=; subprocess text capture passes "
                                 "encoding= and errors=")


def test_the_g7_scan_sees_each_shape():
    """The scan itself is exercised: one offender and one clean call per shape."""
    bad = [
        "p.read_text()", "p.write_text(data)", "open(p)", "open(p, 'w')", "p.open('a')",
        "subprocess.run(cmd, text=True, timeout=5)", "_sp.check_output(cmd, universal_newlines=True)",
        "subprocess.run(cmd, text=True, encoding='utf-8')",
    ]
    good = [
        "p.read_text(encoding='utf-8')", "p.write_text(data, 'utf-8')", "open(p, 'rb')", "p.open('wb')",
        "open(p, 'w', encoding='utf-8')", "os.open(p, flags)", "zipfile.open(name)",
        "subprocess.run(cmd, capture_output=True, timeout=5)",
        "subprocess.run(cmd, text=True, encoding='utf-8', errors='replace')", "subprocess.run(cmd, **kw)",
    ]
    for src in bad:
        assert _g7_problem(ast.parse(src).body[0].value), src
    for src in good:
        assert not _g7_problem(ast.parse(src).body[0].value), src


# ----------------------------------------------------------------------------------------------
# the allowlists themselves stay honest
# ----------------------------------------------------------------------------------------------


def test_allowlist_entries_point_at_real_lines():
    """An allowlist entry whose path or snippet no longer exists is dead weight that hides the
    next regression under the same path. Remove it when the exception goes away."""
    # The public Sextant is a filtered build of this repo: an entry for a private-only module is
    # legitimately absent there. Only the full (private) checkout, recognisable by its build inputs,
    # treats a missing path as stale.
    full_checkout = (REPO / "scripts" / "public_exclude.txt").exists()
    stale = []
    for name, allow in (("G1", ALLOW_G1), ("G2", ALLOW_G2), ("G2-stdin", ALLOW_G2_STDIN), ("G3", ALLOW_G3), ("G4", ALLOW_G4), ("G6", ALLOW_G6),
                        ("G7", ALLOW_G7), ("G9", ALLOW_G9), ("G10", ALLOW_G10), ("G11", ALLOW_G11), ("G12", ALLOW_G12), ("G13", ALLOW_G13)):
        for path, snippet, _reason in allow:
            f = REPO / path
            if not f.exists():
                if full_checkout:
                    stale.append(f"{name}: {path} does not exist")
            elif snippet != "*" and snippet not in f.read_text(encoding="utf-8", errors="replace"):
                stale.append(f"{name}: {path} no longer contains {snippet!r}")
    assert not stale, "stale allowlist entries:\n  " + "\n  ".join(stale)


# ----------------------------------------------------------------------------------------------
# LES-G8  every failure exit names the next step (LES-054)
#   Owner's by-hand install, 2026-11-21: "A command fails ... and the screen names what went wrong
#   (or nothing) without what to type next." AG102 (sweep 26): every argparse and Click usage error
#   ended on the bare complaint, `atlas-narrative --help` ran the report, `sextant verify` before
#   init ended at `ROUND-TRIP: FAILED`. FUNCTIONAL: every console entry point (pyproject) and every
#   `sextant` subcommand (read from the Typer app at test time, so a new command is covered the
#   day it lands) runs in-process in ONE child interpreter on an empty home, with (i) a bad
#   argument and (ii) no corpus. A failure must not print a traceback, and its last three lines
#   must name a next step: a backticked command, an indented `sextant ...` line, `--help`, a
#   docs/*.md page or a URL. A usage error goes to stderr.
# ----------------------------------------------------------------------------------------------

# (command as typed, case "bad-arg" | "no-corpus" | "*", reason)
ALLOW_G8: list[Allow] = [
    ("atlas-summarize-session", "*", "the transcript CLI summarises every transcript of the day under the real "
                                     "~/.claude/projects with the configured model: never run from a test"),
    ("sextant-summarize-session", "*", "the public name of atlas-summarize-session (SEX-043, AG130): never run "
                                       "from a test, as above"),
    ("atlas-session-start", "bad-arg", "SessionStart hook: ignores arguments so a client's hook line never breaks"),
    ("sextant-session-start", "bad-arg", "the public name of the SessionStart hook: ignores arguments, as above"),
    ("atlas-pre-compact", "bad-arg", "PreCompact hook: ignores arguments so a client's hook line never breaks"),
    ("sextant-pre-compact", "bad-arg", "the public name of the PreCompact hook: ignores arguments, as above"),
    ("atlas-prime", "bad-arg", "every argument is a task word (`atlas-prime fix the login bug`)"),
    ("sextant-prime", "bad-arg", "the public name of atlas-prime: every argument is a task word"),
    ("sextant context", "bad-arg", "with words it is atlas-prime: every argument is a task word"),
    ("sextant prime", "bad-arg", "alias of `context`: every argument is a task word"),
    ("sextant verify", "no-corpus", "starts the MCP server as a child process (seconds); its no-corpus "
                                    "next step is tested in tests/test_ag102.py"),
    ("python -m atlas_plus.summarizer.hook_entry", "*", "the Stop hook's entry: reads the client's JSON on stdin, "
                                                        "ignores arguments and must never fail the client"),
    ("python -m atlas_plus.hooks.prompt_clock", "*", "a prompt hook: ignores arguments and exits 0 whatever "
                                                     "happens, so a client's prompt is never blocked"),
]
# Long-running services and model-calling jobs: the bad-argument case runs (argparse refuses
# before anything starts); a bare run would serve or call a model, so the no-corpus case is skipped.
_G8_SERVES = {"atlas-memory-mcp", "sextant-mcp", "atlas-telegram-bot", "atlas-job-poller", "atlas-ntfy-listener",
              "atlas-web-ui", "sextant-web", "atlas-brain", "sextant-brain", "atlas-brain-chat", "atlas-brain-worker",
              "atlas-reflexion-loop", "atlas-weekly-synthesis", "atlas-dream", "sextant-dream",
              "atlas-contradiction-check", "atlas-suggest-facts", "atlas-narrative", "atlas-intake", "sextant-intake",
              # init CREATES the corpus (it is the next step every other case names): run last it would be
              # fine, run first it would end the no-corpus premise for the cases after it
              "atlas-init", "sextant-init"}
_G8_BOX = str.maketrans("", "", "─│┌┐└┘╭╮╯╰")
_G8_NEXT = re.compile(r"`[^`\n]*(sextant|atlas|python)[^`\n]*`|--help\b|docs/[\w./-]+\.md|https?://\S+"
                      r"|^\s+(sextant|atlas-|python -m atlas_plus)\S*", re.M)


def _g8_names_next_step(text: str) -> bool:
    lines = [ln.translate(_G8_BOX).strip() for ln in text.splitlines()]
    tail = [ln for ln in lines if ln][-3:]
    raw_tail = [ln.translate(_G8_BOX) for ln in text.splitlines() if ln.translate(_G8_BOX).strip()][-3:]
    # a console wraps where it likes: with a long temp path (macOS) the break fell inside a backticked command
    return bool(_G8_NEXT.search("\n".join(tail)) or _G8_NEXT.search("\n".join(raw_tail))
                or _G8_NEXT.search(" ".join(tail)))


# The public build adds the sextant-* names for the commands a stranger runs; the private tree's
# pyproject has only the atlas-* originals. G8 runs the same cases, under the same names, on both.
_G8_PUBLIC_NAMES = {"atlas-plus": "sextant", "atlas-init": "sextant-init", "atlas-doctor": "sextant-doctor",
                    "atlas-memory-mcp": "sextant-mcp", "atlas-web-ui": "sextant-web", "atlas-brain": "sextant-brain",
                    "atlas-backup": "sextant-backup", "atlas-transcripts": "sextant-transcripts",
                    "atlas-dream": "sextant-dream", "atlas-sync": "sextant-sync",
                    "atlas-uninstall": "sextant-uninstall", "atlas-reset": "sextant-reset",
                    "atlas-import": "sextant-import", "atlas-intake": "sextant-intake",
                    "atlas-embed-index": "sextant-embed-index"}


def _console_scripts() -> dict[str, str]:
    import tomllib

    scripts = dict(tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"])
    for private, public in _G8_PUBLIC_NAMES.items():
        if private in scripts:
            scripts.setdefault(public, scripts[private])
    return scripts


def _g8_module_entry_points() -> list[str]:
    """Every `python -m <module>` entry point that no console script reaches: a package's `__main__.py`
    or a module with a main guard (SEX-047 row 8: github_intake, trading, gatekeeper, budget and the
    transcript sources were outside G8 because pyproject does not name them). Read from the tree at
    test time, so a new one is covered the day it lands."""
    script_mods = {t.partition(":")[0] for t in _console_scripts().values()}
    out = set()
    for p in _py_files(SRC):
        mod = ".".join(p.relative_to(SRC.parent).with_suffix("").parts)
        if p.name == "__main__.py":
            mod = mod.removesuffix(".__main__")
        elif not re.search(r"^if __name__ == ['\"]__main__['\"]:", p.read_text(encoding="utf-8", errors="replace"), re.M):
            continue
        if mod not in script_mods:
            out.add(mod)
    return sorted(out)


def _g8_cases() -> list[dict]:
    scripts = _console_scripts()
    cases, seen = [], set()
    for mod in _g8_module_entry_points():
        name = f"python -m {mod}"
        cases.append({"name": name, "case": "bad-arg", "target": f"-m {mod}", "argv": [name, "--no-such-option-g8"]})
        cases.append({"name": name, "case": "help", "target": f"-m {mod}", "argv": [name, "--help"]})
    for name, target in scripts.items():
        cases.append({"name": name, "case": "bad-arg", "target": target, "argv": [name, "--no-such-option-g8"]})
        cases.append({"name": name, "case": "help", "target": target, "argv": [name, "--help"]})  # (AG103)
        if name not in _G8_SERVES and target not in seen:
            cases.append({"name": name, "case": "no-corpus", "target": target, "argv": [name]})
        seen.add(target)
    cases.append({"name": "sextant", "case": "subcommands", "target": scripts["sextant"], "argv": ["sextant"]})
    return [c for c in cases if not any(a[0] == c["name"] and a[1] in ("*", c["case"]) for a in ALLOW_G8)]


def _g8_child(cases: list[dict]) -> dict:
    """Runs in the child interpreter: each case in-process, stdout/stderr captured, exit code as the
    console-script wrapper would set it (`sys.exit(main())`)."""
    import importlib
    import io
    import sys
    import traceback

    from atlas_plus import cli

    expanded = []
    for c in cases:
        if c["case"] != "subcommands":
            expanded.append(c)
            continue
        for cmd in cli.app.registered_commands:  # every subcommand, hidden ones and redirects included
            sub = cmd.name or cmd.callback.__name__
            for case in ("bad-arg", "no-corpus", "help"):
                name = f"sextant {sub}"
                if any(a[0] == name and a[1] in ("*", case) for a in ALLOW_G8) or (case == "no-corpus" and sub == "init"):
                    continue
                if case == "help" and sub in cli._REDIRECTS:
                    continue  # a guessed name, not a command: every argument (--help too) gets the redirect, exit 2
                if case == "bad-arg":
                    argv = ["sextant", sub, "--no-such-option-g8"]
                elif case == "help":
                    argv = ["sextant", sub, "--help"]
                else:
                    import inspect
                    required = [p for p in inspect.signature(cmd.callback).parameters.values()
                                if p.default is not inspect.Parameter.empty and getattr(p.default, "default", None) is ...]
                    argv = ["sextant", sub] + ["g8probe"] * len(required)
                    if sub in ("web", "dashboard"):
                        argv = ["sextant", sub]  # no key in this env: it refuses before binding
                expanded.append({"name": name, "case": case, "target": c["target"], "argv": argv})
    results = []
    for c in expanded:
        mod, _, attr = c["target"].partition(":")
        # real text streams over bytes: hooks read sys.stdin.buffer, the console helpers reconfigure stdout
        out, err = (io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="replace", write_through=True) for _ in "oe")
        saved = sys.argv, sys.stdout, sys.stderr, sys.stdin
        sys.argv, sys.stdout, sys.stderr = list(c["argv"]), out, err
        sys.stdin = io.TextIOWrapper(io.BytesIO(b""), encoding="utf-8")
        try:
            if c["target"].startswith("-m "):  # a `python -m` entry point: run the module as __main__
                import runpy
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)  # "found in sys.modules": imported by an earlier case
                    runpy.run_module(c["target"][3:], run_name="__main__", alter_sys=True)
                rv = None
            else:
                func = importlib.import_module(mod)
                for part in attr.split("."):
                    func = getattr(func, part)
                rv = func()
            rc = 0 if rv is None else (rv if isinstance(rv, int) else 1)
        except SystemExit as exc:
            code = exc.code
            rc = 0 if code is None else (code if isinstance(code, int) else 1)
            if not isinstance(code, (int, type(None))):
                err.write(f"{code}\n")
        except BaseException:  # noqa: BLE001 - what the interpreter would print
            rc = 1
            err.write(traceback.format_exc())
        finally:
            sys.argv, sys.stdout, sys.stderr, sys.stdin = saved
        got = []
        for stream in (out, err):
            try:
                stream.flush()
            except ValueError:  # closed by the command (a broken-pipe guard)
                pass
            got.append(stream.buffer.getvalue().decode("utf-8", "replace")[-4000:])
        results.append({**c, "rc": rc, "stdout": got[0], "stderr": got[1]})
    subs = [f"sextant {cmd.name or cmd.callback.__name__}" for cmd in cli.app.registered_commands]
    return {"results": results, "subs": subs}


def test_g8_every_failure_names_the_next_step(tmp_path):
    import json
    import os
    import subprocess
    import sys

    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("ATLAS_", "RESEARCH_"))}
    dead = "http://127.0.0.1:9"
    env.update({"HOME": str(home), "USERPROFILE": str(home), "ATLAS_WORKSPACE_ROOT": str(home / "ws"),
                "ATLAS_MEMORY_ROOT": str(home / "ws" / ".memory"), "ATLAS_STATE_DIR": str(home / "ws" / ".atlas"),
                "ATLAS_CLAUDE_CODE_ROOT": str(home / "cc"), "APPDATA": str(home / "appdata"),
                "LOCALAPPDATA": str(home / "localappdata"), "XDG_CONFIG_HOME": str(home / "xdg"),
                "CLAUDE_CONFIG_DIR": str(home / "claude"), "CODEX_HOME": str(home / "codex"),
                "ATLAS_MCP_PORT": "9", "ATLAS_WEB_PORT": "9", "ATLAS_BRAIN_PORT": "9", "ATLAS_SYNC_PORT": "9",
                "ATLAS_BRAIN_URL": dead, "ATLAS_MCP_URL": dead, "ATLAS_WEB_URL": dead, "ATLAS_SYNC_URL": dead,
                "ATLAS_OLLAMA_URL": dead, "ATLAS_EMBED_URL": dead, "ATLAS_BRAIN_OLLAMA_URL": dead,
                "ATLAS_NTFY_URL": dead, "ATLAS_RESEARCH_API_URL": dead, "OLLAMA_HOST": "127.0.0.1:9",
                "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "COLUMNS": "120", "TERM": "dumb"})
    # a checkout that is not pip-installed finds the package through pytest's pythonpath: so must the child
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(SRC.parent), env.get("PYTHONPATH", "")) if p)
    cases = _g8_cases()
    proc = subprocess.run([sys.executable, __file__, "--g8-child"], input=json.dumps(cases), env=env,
                          cwd=str(home), capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=300)
    assert proc.returncode == 0, f"G8 child failed:\n{proc.stderr[-3000:]}"
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    results = payload["results"]
    assert len(results) > 100, f"G8 ran only {len(results)} cases"
    offenders = []
    for r in results:
        text = r["stdout"] + r["stderr"]
        label = f"{' '.join(r['argv'])} [{r['case']}] rc={r['rc']}"
        if "Traceback (most recent call last)" in text:
            offenders.append(f"{label}: printed a traceback: {text.strip().splitlines()[-1][:160]}")
        elif r["case"] == "bad-arg" and r["rc"] == 0:
            offenders.append(f"{label}: a bad argument exited 0")
        elif r["case"] != "help" and r["rc"] != 0 and not _g8_names_next_step(r["stderr"] or r["stdout"]):
            offenders.append(f"{label}: the last lines name no next step: "
                             f"{(r['stderr'] or r['stdout']).strip().splitlines()[-1:]!r}")
        elif r["case"] == "help" and (r["rc"] != 0 or not (r["stdout"] + r["stderr"]).strip()):
            offenders.append(f"{label}: --help did not exit 0 with text")
        elif r["case"] != "help" and r["rc"] != 0 and not r["stderr"].strip():
            # every non-zero exit, not only bad arguments (AG103): scripts read stderr
            offenders.append(f"{label}: the error text went to stdout only")
    _report(offenders, "LES-G8", "a failure exits non-zero without a traceback and its last three lines name the "
                                 "next step (a `command`, `--help`, a docs/*.md page or a URL)")
    known = set(_console_scripts()) | {f"python -m {m}" for m in _g8_module_entry_points()}
    # the source tree carries only the atlas-* names; an exemption for a public sextant-* name the build adds
    # is not stale there (SEX-043, AG130)
    known |= _g18_real_commands()[0]
    stale =[a[0] for a in ALLOW_G8 if a[0] not in known | set(payload["subs"])]
    assert not stale, f"stale G8 allowlist entries (the command is gone): {stale}"


def test_the_g8_check_sees_each_shape():
    bad = ["sextant-init: error: unrecognized arguments: --x", "Error\nNo such option: --x",
           "ROUND-TRIP: FAILED", "path not found: g8probe"]
    good = ["error: x\nRun `sextant-init --help` to see what it takes.", "Next: run `sextant init` first.",
            "see docs/GETTING-STARTED.md", "Usage:\n  sextant search \"words\"", "https://example-org.test/x",
            "No corpus yet at /a/long/path, and `sextant\nsetup` prints the registration with them baked in"]
    for text in bad:
        assert not _g8_names_next_step(text), text
    for text in good:
        assert _g8_names_next_step(text), text


# ----------------------------------------------------------------------------------------------
# LES-G9  a script handed to an interpreter is built from constants only (LES-050)
#   KL1 (sweep 26, BLOCKER): an issue title reached `powershell -Command` inside a here-string and
#   ran. PowerShell accepts U+2018..U+201B as single quotes, so no quote replacement makes
#   interpolation safe. Rule: the text after `-Command`, `sh -c`, `cmd /c`, `python -c`, a
#   `shell=True` string or os.system is built from constants, module tables and data-only helpers
#   (a literal-quoting helper, base64, int); run-time text reaches the script as base64, an
#   environment variable or a file. A parameter that becomes script text is followed to every
#   caller (three hops); a function nobody in the tree calls (a route, a tool) is the outside world.
# ----------------------------------------------------------------------------------------------

ALLOW_G9: list[Allow] = []

_G9_INTERPRETERS = ("powershell", "pwsh", "cmd", "sh", "bash", "zsh", "dash", "wsl", "osascript", "wscript", "cscript",
                 "node", "perl", "ruby", "python", "python3", "py")
_G9_SCRIPT_FLAGS = ("-command", "-c", "/c", "/k", "-e", "-encodedcommand")
# calls whose RESULT is data that cannot be script source: quoting helpers, encoders, numbers
_G9_SAFE_CALLS = ("_ps_literal", "quote", "b64encode", "int", "float", "len", "hexdigest", "env_int")
_G9_MAX_HOPS = 3


def _g9_interp_name(node) -> str:
    """'powershell' for "powershell.exe" / "C:/x/pwsh.exe"; 'python' for sys.executable; '' otherwise."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        base = node.value.replace("\\", "/").rsplit("/", 1)[-1].lower()
        base = base[:-4] if base.endswith(".exe") else base
        return base if base in _G9_INTERPRETERS else ""
    if _dotted(node).endswith("sys.executable"):
        return "python"
    return ""


def _g9_parents(tree):
    out = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            out[child] = parent
    return out


_G9_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _g9_enclosing(node, parents):
    while node in parents:
        node = parents[node]
        if isinstance(node, _G9_FUNCS):
            return node
    return None


def _g9_assigned_values(scope, name):
    """Every value bound to ``name`` directly in ``scope`` (a function or the module), or None if a binding is opaque."""
    vals = []
    body_nodes = ast.walk(scope) if isinstance(scope, _G9_FUNCS) else scope.body
    walk = body_nodes if isinstance(scope, _G9_FUNCS) else [n for stmt in scope.body for n in _g9_shallow(stmt)]
    for n in walk:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    vals.append(n.value)
                elif isinstance(t, (ast.Tuple, ast.List)) and any(isinstance(e, ast.Name) and e.id == name for e in t.elts):
                    return None
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)) and isinstance(n.target, ast.Name) and n.target.id == name:
            if n.value is not None:
                vals.append(n.value)
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)) and any(
                isinstance(e, ast.Name) and e.id == name for e in ast.walk(n.target)):
            vals.append(ast.Subscript(value=n.iter, slice=ast.Constant(0), ctx=ast.Load()))  # an element of the iterable
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if item.optional_vars is not None and any(
                        isinstance(e, ast.Name) and e.id == name for e in ast.walk(item.optional_vars)):
                    return None
        elif isinstance(n, ast.NamedExpr) and n.target.id == name:
            vals.append(n.value)
    return vals


def _g9_shallow(stmt):
    """Module-level statements, descending into if/try/with blocks but not into functions or classes."""
    yield stmt
    for child in ast.iter_child_nodes(stmt):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(child, ast.stmt):
            yield from _g9_shallow(child)


def _g9_params(fn):
    a = fn.args
    return [x.arg for x in (a.posonlyargs + a.args)], [x.arg for x in a.kwonlyargs]


class _g9_Ctx:
    def __init__(self, tree, parents):
        self.tree, self.parents = tree, parents


def _g9_why_not_constant(expr, ctx, fn, seen, params_hit):
    """'' when ``expr`` is built from constants only; otherwise a short reason. A bare parameter of the enclosing
    function is not judged here: it is recorded in ``params_hit`` and every caller is checked instead."""
    if isinstance(expr, ast.Constant):
        return ""
    if isinstance(expr, ast.JoinedStr):
        for v in expr.values:
            if isinstance(v, ast.FormattedValue):
                r = _g9_why_not_constant(v.value, ctx, fn, seen, params_hit)
                if r:
                    return r
        return ""
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Add, ast.Mod, ast.Mult)):
        return (_g9_why_not_constant(expr.left, ctx, fn, seen, params_hit)
                or _g9_why_not_constant(expr.right, ctx, fn, seen, params_hit))
    if isinstance(expr, ast.IfExp):
        return (_g9_why_not_constant(expr.body, ctx, fn, seen, params_hit)
                or _g9_why_not_constant(expr.orelse, ctx, fn, seen, params_hit))
    if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
        for e in expr.elts:
            r = _g9_why_not_constant(e, ctx, fn, seen, params_hit)
            if r:
                return r
        return ""
    if isinstance(expr, ast.Dict):
        for e in list(expr.keys) + list(expr.values):
            if e is not None:
                r = _g9_why_not_constant(e, ctx, fn, seen, params_hit)
                if r:
                    return r
        return ""
    if isinstance(expr, ast.Starred):
        return _g9_why_not_constant(expr.value, ctx, fn, seen, params_hit)
    if isinstance(expr, ast.Subscript):
        return _g9_why_not_constant(expr.value, ctx, fn, seen, params_hit)
    if isinstance(expr, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
        return _g9_why_not_constant(expr.elt, ctx, fn, seen, params_hit)
    if isinstance(expr, ast.Call):
        name = _dotted(expr.func)
        last = name.rsplit(".", 1)[-1]
        if last in _G9_SAFE_CALLS:
            return ""
        if last in ("decode", "strip", "lower", "upper", "format", "join", "replace") and isinstance(expr.func, ast.Attribute):
            r = _g9_why_not_constant(expr.func.value, ctx, fn, seen, params_hit)
            for a in expr.args:
                r = r or _g9_why_not_constant(a, ctx, fn, seen, params_hit)
            for kw in expr.keywords:
                r = r or _g9_why_not_constant(kw.value, ctx, fn, seen, params_hit)
            return r
        if last == "str" and len(expr.args) == 1:
            return _g9_why_not_constant(expr.args[0], ctx, fn, seen, params_hit)
        return f"the result of {name}(...)"
    if isinstance(expr, ast.Name):
        key = (id(fn), expr.id)
        if key in seen:
            return ""
        seen = seen | {key}
        if fn is not None:
            pos, kwonly = _g9_params(fn)
            local = _g9_assigned_values(fn, expr.id)
            if local is None:
                return f"`{expr.id}` (bound by unpacking or a with block)"
            if local:
                for v in local:
                    r = _g9_why_not_constant(v, ctx, fn, seen, params_hit)
                    if r:
                        return r
                if expr.id not in pos + kwonly:
                    return ""
            if expr.id in pos + kwonly:
                params_hit.append((fn, expr.id))
                return ""
            # a comprehension variable inside this function was handled through _g9_assigned_values (ast.comprehension)
        vals = _g9_assigned_values(ctx.tree, expr.id)
        if not vals:
            return f"`{expr.id}` (not a constant of this module)"
        for v in vals:
            r = _g9_why_not_constant(v, ctx, None, seen, params_hit)
            if r:
                return r
        return ""
    return f"`{_dotted(expr)[:60]}`"


def _g9_script_exprs(tree):
    """(node, [script expressions], what) for every place a script is handed to an interpreter."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) >= 2:
            interp = _g9_interp_name(node.elts[0])
            if not interp:
                continue
            for i, e in enumerate(node.elts[1:], start=1):
                if isinstance(e, ast.Constant) and isinstance(e.value, str) and e.value.lower() in _G9_SCRIPT_FLAGS:
                    if interp == "python" and e.value != "-c":
                        continue
                    if e.value.lower() == "-encodedcommand":
                        break
                    rest = node.elts[i + 1:]
                    if rest:
                        yield node, (rest if interp in ("powershell", "pwsh", "cmd") else rest[:1]), f"{interp} {e.value}"
                    break
        elif isinstance(node, ast.Call):
            name = _dotted(node.func)
            if name in ("os.system", "os.popen") and node.args:
                yield node, node.args[:1], name
            elif any(kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                     for kw in node.keywords) and node.args:
                yield node, node.args[:1], "shell=True"


def _g9_scan(sources):
    """``sources`` maps a path (anything with .name) to Python source text. Returns sorted (path, line, message)."""
    trees = {}
    for p, text in sources.items():
        try:
            tree = ast.parse(text, filename=str(p))
        except SyntaxError:
            continue
        trees[p] = _g9_Ctx(tree, _g9_parents(tree))
    offenders = []
    runners = []   # (function name, parameter name, position, hop, origin "file:line")
    for p, ctx in trees.items():
        for node, exprs, what in _g9_script_exprs(ctx.tree):
            fn = _g9_enclosing(node, ctx.parents)
            for expr in exprs:
                hit = []
                why = _g9_why_not_constant(expr, ctx, fn, frozenset(), hit)
                if why:
                    offenders.append((p, node.lineno, f"{what}: script text includes {why}"))
                for f, param in hit:
                    pos, _ = _g9_params(f)
                    runners.append((f.name, param, pos.index(param) if param in pos else None, 1, (p, node.lineno)))
    done = set()
    while runners:
        fname, param, index, hop, origin = runners.pop()
        if (fname, param) in done:
            continue
        done.add((fname, param))
        callers = 0
        for p, ctx in trees.items():
            for node in ast.walk(ctx.tree):
                if not isinstance(node, ast.Call) or _dotted(node.func).rsplit(".", 1)[-1] != fname:
                    continue
                arg = next((kw.value for kw in node.keywords if kw.arg == param), None)
                if arg is None and index is not None and len(node.args) > index:
                    arg = node.args[index]
                if arg is None:
                    continue
                callers += 1
                fn = _g9_enclosing(node, ctx.parents)
                hit = []
                why = _g9_why_not_constant(arg, ctx, fn, frozenset(), hit)
                if why:
                    offenders.append((p, node.lineno, f"{fname}({param}=...) runs it as a script ({origin[0].name}:{origin[1]}): includes {why}"))
                for f, prm in hit:
                    if hop >= _G9_MAX_HOPS:
                        offenders.append((p, node.lineno, f"{fname}({param}=...): script text passes through more than {_G9_MAX_HOPS} functions"))
                        continue
                    pos, _ = _g9_params(f)
                    runners.append((f.name, prm, pos.index(prm) if prm in pos else None, hop + 1, origin))
        if not callers:
            # nobody in the tree calls it with that argument: a route handler, a tool or a public entry point,
            # so the parameter is whatever the outside sends
            offenders.append((origin[0], origin[1],
                              f"parameter `{param}` of {fname}() becomes script text and no caller in the tree supplies it"))
    return sorted(set(offenders), key=lambda o: (str(o[0]), o[1]))


def test_g9_interpreter_scripts_are_built_from_constants():
    sources = {p: p.read_text(encoding="utf-8", errors="replace") for p in _py_files(SRC)}
    offenders = []
    for p, lineno, msg in _g9_scan(sources):
        rel = _rel(p)
        if _allowed(rel, _line(p, lineno), ALLOW_G9):
            continue
        offenders.append(f"{rel}:{lineno}: {msg}")
    _report(offenders, "LES-G9", "script text for an interpreter is built from constants; pass run-time text as "
                                 "base64, an environment variable or a file, never inside the script")


def test_the_g9_scan_sees_each_shape():
    from pathlib import PurePosixPath

    def hits(src, extra=None):
        sources = {PurePosixPath("m.py"): src}
        if extra:
            sources[PurePosixPath("n.py")] = extra
        return [(p.name, line, msg) for p, line, msg in _g9_scan(sources)]

    # the KL1 shape: stranger text interpolated into a PowerShell -Command through a helper
    bad = (
        "import subprocess\n"
        "def page(text):\n"
        "    subprocess.run(['powershell', '-NoProfile', '-Command', f\"Send-It -Text '{text}'\"], timeout=5)\n"
        "def on_issue(item):\n"
        "    page(item.title)\n"
    )
    assert any("item.title" in h[2] for h in hits(bad)), hits(bad)
    # quote replacement is not a fix: still flagged
    assert hits(bad.replace("{text}", "{text.replace(chr(39), chr(39) * 2)}"))
    # base64 and a literal-quoting helper are data, not source
    good = (
        "import base64, subprocess\n"
        "def _ps_literal(v):\n    return repr(v)\n"
        "LIB = 'notify.ps1'\n"
        "def page(text):\n"
        "    b64 = base64.b64encode(text.encode('utf-8')).decode('ascii')\n"
        "    subprocess.run(['powershell', '-Command', f\". {_ps_literal(LIB)}; Send-It ([Convert]::FromBase64String('{b64}'))\"], timeout=5)\n"
        "def on_issue(item):\n"
        "    page(item.title)\n"
    )
    assert not hits(good), hits(good)
    # constants and module tables are fine; a caller in another module is followed
    ok = (
        "import subprocess\n"
        "TASKS = [{'task': 'A'}, {'task': 'B'}]\n"
        "PORT = 8765\n"
        "def run_ps(cmd, timeout=5):\n"
        "    return subprocess.run(['powershell', '-Command', cmd], timeout=timeout)\n"
        "def status():\n"
        "    names = \"','\".join(s['task'] for s in TASKS)\n"
        "    return run_ps(\"@('\" + names + f\"') | Get-Thing -Port {PORT}\")\n"
    )
    assert not hits(ok), hits(ok)
    other = ("from m import run_ps\ndef restart(name):\n    return run_ps('Restart-Thing ' + name)\n"
             "def go(req):\n    restart(req.query['n'])\n")
    assert any(h[0] == "n.py" and "req.query" in h[2] for h in hits(ok, other)), hits(ok, other)
    # a handler nobody calls: its parameter is the outside world
    route = "import os\ndef handler(name: str):\n    os.system('sc start ' + name)\n"
    assert any("no caller" in h[2] for h in hits(route)), hits(route)
    # sh -c, cmd /c, python -c and shell=True are the same door
    for line in ("subprocess.run(['bash', '-c', 'ls ' + d], timeout=5)",
                 "subprocess.run(['cmd.exe', '/c', 'dir', d], timeout=5)",
                 "subprocess.run([sys.executable, '-c', f'print({d!r})'], timeout=5)",
                 "subprocess.run('ls ' + d, shell=True, timeout=5)"):
        assert hits("import subprocess, sys\ndef f():\n    d = input()\n    " + line + "\n"), line
    # not a script: git's -c option, a -File script with data arguments, an argv list without a script flag
    for line in ("subprocess.run(['git', '-c', f'core.hooksPath={d}', 'status'], timeout=5)",
                 "subprocess.run(['powershell.exe', '-File', 'x.ps1', '-RunTime', d], timeout=5)",
                 "subprocess.run(['wsl', '-d', d, '--', 'python3', '-m', 'atlas_plus'], timeout=5)"):
        src = "import subprocess, sys\ndef f():\n    d = input()\n    " + line + "\n"
        assert not hits(src), (line, hits(src))


# ----------------------------------------------------------------------------------------------
# LES-G10  a test reads the public README under both of its names (LES-065)
#   The private tree keeps the public README at docs/README-PUBLIC.md; the public build ships it
#   as README.md. A test that opens only the private name is green here and red on the public CI
#   (mirror e589cb4, 2026-11-23: all four test jobs). A test function that names the private file
#   must also name README.md (the fallback) or check that the file exists (and skip).
# ----------------------------------------------------------------------------------------------

ALLOW_G10: list[Allow] = []
_G10_PRIVATE_NAME = "README-" + "PUBLIC.md"


def _g10_offenders(sources: dict) -> list[tuple]:
    out = []
    for p, text in sources.items():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        nested = set()
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for fn in funcs:
            for inner in ast.walk(fn):
                if inner is not fn and isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nested.add(inner)
        for fn in funcs:
            if fn in nested:
                continue
            strings = [(n.lineno, n.value) for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            hits = [ln for ln, v in strings if v.endswith(_G10_PRIVATE_NAME) and len(v) < 60 and "\n" not in v]
            if not hits:
                continue
            falls_back = any(v == "README.md" or v.endswith("/README.md") for _, v in strings)
            checks = any(isinstance(n, ast.Call) and _dotted(n.func).rsplit(".", 1)[-1] in ("exists", "is_file")
                         for n in ast.walk(fn))
            if not (falls_back or checks):
                out.append((p, hits[0], fn.name))
    return out


def test_g10_tests_read_the_public_readme_under_both_names():
    sources = {p: p.read_text(encoding="utf-8", errors="replace") for p in _py_files(TESTS)}
    offenders = []
    for p, lineno, fn in _g10_offenders(sources):
        rel = _rel(p)
        if _allowed(rel, _line(p, lineno), ALLOW_G10):
            continue
        offenders.append(f"{rel}:{lineno}: {fn}() opens docs/{_G10_PRIVATE_NAME} with no README.md fallback and no exists() check")
    _report(offenders, "LES-G10", "a test that reads the public README by its private-tree name falls back to "
                                  "README.md (the name the public build ships) or skips when the file is absent")


def test_the_g10_scan_sees_each_shape():
    name = _G10_PRIVATE_NAME
    bad = f"def test_x():\n    t = (ROOT / 'docs' / '{name}').read_text(encoding='utf-8')\n"
    good1 = (f"def test_x():\n    src = ROOT / 'docs' / '{name}'\n"
             "    t = (src if src.exists() else ROOT / 'README.md').read_text(encoding='utf-8')\n")
    good2 = f"def test_x():\n    src = ROOT / 'docs' / '{name}'\n    if not src.is_file():\n        return\n"
    prose = f"def test_x():\n    \"\"\"The private tree keeps it at docs/{name}; nothing is opened here.\n    \"\"\"\n"
    assert [o[2] for o in _g10_offenders({"a.py": bad})] == ["test_x"]
    for src in (good1, good2, prose):
        assert not _g10_offenders({"a.py": src}), src


# ----------------------------------------------------------------------------------------------
# LES-G11  nothing in the tree installs into the environment it runs from (LES-066)
#   2026-11-23: a `pip install --no-deps -e .` typed into the checkout's own venv, the one the
#   running services import from, half-uninstalled the live package (the .pth and 28 wrappers were
#   gone, pip stopped on a locked exe) and no new process could import it for 25 minutes. An
#   installer belongs in a venv it builds for itself. Two scans: Python (src, tests, scripts) for a
#   child process that runs `pip install|uninstall` under sys.executable or a literal .venv path, or
#   a bare `uv sync|pip|run|add|remove`; shell scripts (scripts/, deploy/) for the same typed as a
#   command. A message that only NAMES the command (Write-Error "... Run: uv sync") is not a run.
#   This catches code. A person or an agent at a prompt is covered by the release docs, not here.
# ----------------------------------------------------------------------------------------------

ALLOW_G11: list[Allow] = []
_G11_VERBS = {"install", "uninstall"}
_G11_UV = {"sync", "pip", "run", "add", "remove"}
_G11_SPAWN = ("run", "check_call", "check_output", "call", "Popen")
_G11_SHELL_SUFFIXES = (".ps1", ".cmd", ".bat", ".sh")
_G11_SAYS = re.compile(r"^\s*(#|rem\b|::|write-\w+|echo\b|throw\b|step\b)", re.I)
_G11_SHELL = re.compile(
    r"\.venv[\\/](?:Scripts|bin)[\\/](?:python[\w.]*[\"']?\s+-m\s+pip|pip[\w.]*[\"']?)\s+(?:install|uninstall)\b"
    r"|(?:^|[;&|({]\s*)(?:&\s*)?uv(?:\.exe)?\s+(?:sync|pip|run|add|remove)\b", re.I)


def _g11_py_hits(text: str) -> list[tuple]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], (ast.List, ast.Tuple))):
            continue
        if _dotted(node.func).rsplit(".", 1)[-1] not in _G11_SPAWN:
            continue
        elts = node.args[0].elts
        if not elts:
            continue
        consts = [e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        first = elts[0]
        first_text = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else _dotted(first)
        own_env = first_text == "sys.executable" or ".venv" in first_text.replace("\\", "/")
        if own_env and "pip" in "".join(consts[:3] + [first_text]).lower() and _G11_VERBS & set(consts):
            out.append((node.lineno, "pip " + sorted(_G11_VERBS & set(consts))[0] + " into the running environment"))
        elif first_text in ("uv", "uv.exe") and len(consts) > 1 and consts[1] in _G11_UV:
            out.append((node.lineno, f"uv {consts[1]} against the project environment"))
    return out


def _g11_shell_hits(text: str) -> list[int]:
    return [i for i, line in enumerate(text.splitlines(), 1)
            if not _G11_SAYS.match(line) and _G11_SHELL.search(line)]


def test_g11_nothing_installs_into_the_environment_it_runs_from():
    offenders = []
    for p in _py_files(SRC, TESTS, *(d for d in (REPO / "scripts",) if d.is_dir())):
        for lineno, what in _g11_py_hits(p.read_text(encoding="utf-8", errors="replace")):
            if not _allowed(_rel(p), _line(p, lineno), ALLOW_G11):
                offenders.append(f"{_rel(p)}:{lineno}: {what}: {_line(p, lineno)[:110]}")
    for root in (REPO / "scripts", REPO / "deploy"):
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.suffix.lower() not in _G11_SHELL_SUFFIXES or "archive" in p.parts or not p.is_file():
                continue
            for lineno in _g11_shell_hits(p.read_text(encoding="utf-8", errors="replace")):
                if not _allowed(_rel(p), _line(p, lineno), ALLOW_G11):
                    offenders.append(f"{_rel(p)}:{lineno}: {_line(p, lineno)[:110]}")
    _report(offenders, "LES-G11", "an installer never runs against the environment the program (and its running "
                                  "services) import from: build a separate venv and install there")


def test_the_g11_scan_sees_each_shape():
    bad_py = ("import subprocess, sys\n"
              "subprocess.run([sys.executable, '-m', 'pip', 'install', '-e', '.'], timeout=600)\n"
              "subprocess.check_call(['.venv/Scripts/pip.exe', 'uninstall', '-y', 'x'], timeout=60)\n"
              "subprocess.run(['uv', 'sync'], timeout=600)\n")
    good_py = ("import subprocess, sys\n"
               "subprocess.run([sys.executable, '-m', 'pip', '--version'], timeout=60)\n"
               "subprocess.run([str(other_venv_python), '-m', 'pip', 'install', 'x'], timeout=600)\n"
               "subprocess.run([sys.executable, '-m', 'venv', str(target)], timeout=600)\n"
               "print('run: pip install -e . or uv sync')\n")
    assert [ln for ln, _ in _g11_py_hits(bad_py)] == [2, 3, 4]
    assert not _g11_py_hits(good_py)
    bad_sh = ("& .\\.venv\\Scripts\\python.exe -m pip install -e .\n"
              "uv sync --extra telegram\n"
              "if ($x) { uv run pytest }\n"
              ".venv/bin/pip uninstall -y thing\n"
              '& "$repo\\.venv\\Scripts\\python.exe" -m pip install -U pip\n')
    good_sh = ('Write-Error "Python venv not found. Run: uv sync"\n'
               "# uv sync would do it\n"
               "& $venvPy -m pip install --quiet -e $clone\n"
               '$hint = "then: uv run atlas-brain"\n'
               "& .\\.venv\\Scripts\\python.exe -m pytest -q\n")
    assert _g11_shell_hits(bad_sh) == [1, 2, 3, 4, 5]
    assert not _g11_shell_hits(good_sh)


# ----------------------------------------------------------------------------------------------
# LES-G12  a printed command that starts an interpreter comes from one helper (LES-067)
#   MN88 (sweep 26), AG104 (sweep 27): eight messages told the user to run `python -c "..."`.
#   Ubuntu has no `python` outside an activated venv, so `export KEY=$(python -c ...)` set an
#   EMPTY key and the server printed the same hint again. `_next_step.keygen_command()` prints
#   the interpreter this install runs under; no other string in src spells `python -c`.
# ----------------------------------------------------------------------------------------------

ALLOW_G12: list[Allow] = [
    ("src/atlas_plus/_next_step.py", "*", "the helper itself: the Windows form and the docstring that explains the rule"),
]
_G12_ONE_LINER = re.compile(r"\bpython3?(?:\.exe)? -c\b")


def _g12_hits(text: str) -> list[int]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    return sorted({n.lineno for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str) and _G12_ONE_LINER.search(n.value)})


def test_g12_interpreter_one_liners_come_from_the_helper():
    offenders = []
    for p in _py_files(SRC):
        for lineno in _g12_hits(p.read_text(encoding="utf-8", errors="replace")):
            if not _allowed(_rel(p), _line(p, lineno), ALLOW_G12):
                offenders.append(f"{_rel(p)}:{lineno}: {_line(p, lineno)[:110]}")
    _report(offenders, "LES-G12", "a message never spells `python -c ...` itself: there is no `python` on a stock "
                                  "Linux; print atlas_plus._next_step.keygen_command() (or how_to_run for a command)")


def test_the_g12_scan_sees_each_shape():
    bad = ('print("Generate one: python -c \\"import secrets\\"")\n'
           "log.error(f'export KEY=$(python3 -c {code})')\n")
    good = ("print(f'Generate one: {keygen_command()}')\n"
            "# python -c in a comment is not printed\n"
            "argv = [sys.executable, '-c', code]\n"
            "print('run python -m atlas_plus.doctor')\n")
    assert _g12_hits(bad) == [1, 2]
    assert not _g12_hits(good)


# ----------------------------------------------------------------------------------------------
# LES-G13  a test that removes a package module from sys.modules puts it back (LES-068)
#   2026-11-09 (memory_server) and again 2026-11-24 (tickets.store): a test popped a module to
#   prove a lazy import and left it out. The next import made a SECOND copy of the module; a
#   later test patched one copy while the code under test imported the other, and failed only
#   in some file orders. `monkeypatch.delitem(sys.modules, name, raising=False)` restores it.
# ----------------------------------------------------------------------------------------------

ALLOW_G13: list[Allow] = [
    ("tests/test_ag95.py", "redaction_ag95_probe", "a probe name the test itself creates: no real module carries it"),
    ("tests/test_mcp_package_import.py", "sys.modules.pop(", "the file's autouse fixture restores the whole "
                                                             "memory_server package graph after each test"),
]


def _g13_hits(text: str) -> list[int]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and _dotted(n.func).endswith("modules.pop") and n.args
                and isinstance(n.args[0], ast.Constant) and str(n.args[0].value).startswith("atlas_plus")):
            out.add(n.lineno)
        if isinstance(n, ast.Delete):
            for t in n.targets:
                if (isinstance(t, ast.Subscript) and _dotted(t.value).endswith("sys.modules")
                        and isinstance(t.slice, ast.Constant) and str(t.slice.value).startswith("atlas_plus")):
                    out.add(n.lineno)
    return sorted(out)


def test_g13_tests_put_back_the_modules_they_remove():
    offenders = []
    for p in _py_files(TESTS):
        if p.name == "test_guardrails.py":
            continue
        for lineno in _g13_hits(p.read_text(encoding="utf-8", errors="replace")):
            if not _allowed(_rel(p), _line(p, lineno), ALLOW_G13):
                offenders.append(f"{_rel(p)}:{lineno}: {_line(p, lineno)[:110]}")
    _report(offenders, "LES-G13", "remove a package module from sys.modules with monkeypatch.delitem(sys.modules, "
                                  "name, raising=False), which restores it; a bare pop or del leaves two copies")


def test_the_g13_scan_sees_each_shape():
    bad = ("import sys\n"
           "sys.modules.pop('atlas_plus.tickets.store', None)\n"
           "del sys.modules['atlas_plus.web.app']\n")
    good = ("import sys\n"
            "monkeypatch.delitem(sys.modules, 'atlas_plus.tickets.store', raising=False)\n"
            "sys.modules.pop('some_benchmark_helper', None)\n")
    assert _g13_hits(bad) == [2, 3]
    assert not _g13_hits(good)


# ----------------------------------------------------------------------------------------------
# LES-G14  a command line from a setting is split by atlas_plus._cmdline (LES-070)
#   KL2, sweep 27: TOOL_CMD=C:\Projects\app\run.exe went through POSIX shlex.split, lost every
#   backslash, and the panel said the program was "not found" at a path nobody typed. The verify
#   command had learned the same thing for --command years of sweeps earlier; the second caller
#   had not. One splitter that knows Windows paths; no other shlex.split in the package.
# ----------------------------------------------------------------------------------------------

ALLOW_G14: list[Allow] = [
    ("src/atlas_plus/_cmdline.py", "shlex.split(", "the one splitter"),
    ("src/atlas_plus/setup_cmd.py", "shlex.split(command, posix=True)",
     "parses a POSIX hook line this module generated itself, never text a person typed"),
]


def _g14_hits(text: str) -> list[int]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    return sorted({n.lineno for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and _dotted(n.func).split(".")[-2:] in (["shlex", "split"], ["_shlex", "split"])})


def test_g14_command_lines_are_split_by_the_shared_helper():
    offenders = []
    for p in _py_files(SRC):
        for lineno in _g14_hits(p.read_text(encoding="utf-8", errors="replace")):
            if not _allowed(_rel(p), _line(p, lineno), ALLOW_G14):
                offenders.append(f"{_rel(p)}:{lineno}: {_line(p, lineno)[:110]}")
    _report(offenders, "LES-G14", "split a command line with atlas_plus._cmdline.split_command(text): POSIX "
                                  "shlex.split eats the backslashes of a Windows path")


def test_the_g14_scan_sees_each_shape():
    bad = ("import shlex\n"
           "argv = shlex.split(os.environ['TOOL_CMD'])\n"
           "import shlex as _shlex\n"
           "argv = _shlex.split(raw, posix=False)\n")
    good = ("from atlas_plus import _cmdline\n"
            "argv = _cmdline.split_command(raw)\n"
            "quoted = shlex.quote(path)\n"
            "parts = line.split()\n")
    assert _g14_hits(bad) == [2, 4]
    assert not _g14_hits(good)


def test_the_shared_splitter_keeps_a_windows_path_whole(monkeypatch):
    import pytest

    from atlas_plus import _cmdline
    path = "C:\\Projects\\app\\.venv\\Scripts\\run.exe"
    assert _cmdline.split_command(path) == [path]
    assert _cmdline.split_command(f'"{path}" --profile x') == [path, "--profile", "x"]
    assert _cmdline.split_command('"C:\\Program Files\\app\\run.exe" siem') == ["C:\\Program Files\\app\\run.exe", "siem"]
    assert _cmdline.split_command("   ") == [] and _cmdline.split_command("") == []
    with pytest.raises(ValueError):
        _cmdline.split_command('"C:\\Projects\\app\\run.exe', strict_quotes=True)
    monkeypatch.setattr(_cmdline.os, "name", "posix")                # the POSIX rules for everything else
    assert _cmdline.split_command("python3 -m tool 'a b'") == ["python3", "-m", "tool", "a b"]
    assert _cmdline.split_command(path) == [path]                    # a drive path keeps its separators anywhere
    with pytest.raises(ValueError):
        _cmdline.split_command("tool 'unclosed")


if __name__ == "__main__" and sys.argv[1:2] == ["--g8-child"]:
    import json as _json

    _cases = _json.loads(sys.stdin.read())
    _out = _g8_child(_cases)
    sys.__stdout__.write(_json.dumps(_out) + "\n")


# ----------------------------------------------------------------------------------------------
# LES-G15  a child whose output is captured runs through the bounded helper
#   Sweeps: KL2 (sweep 27), AG105 (sweep 28). subprocess.run(timeout=) ends only the direct child and
#   then reads the pipes again without a bound: a grandchild holding stdout kept a 2 s timeout waiting
#   30 s, and the whole output is in memory before any size check. atlas_plus._proc.run_bounded ends
#   the process tree and caps what it keeps. The calls that predate the helper are listed below, each
#   with the reason it is still there; a NEW capturing call outside the helper fails this test.
# ----------------------------------------------------------------------------------------------

ALLOW_G15: list[Allow] = [
    ("src/atlas_plus/_proc.py", "*", "the helper itself"),
    ("src/atlas_plus/gitsafe.py", "*", "the git helper: one short git command, hooks and helpers neutralised, 5 to 10 s timeout"),
    ("src/atlas_plus/hooks/pre_compact.py", "*", "one short git command with a timeout, through the git helper's environment"),
    ("src/atlas_plus/hooks/session_start.py", "*", "one short git command with a timeout, through the git helper's environment"),
    ("src/atlas_plus/brain/learning.py", "*", "git log with -n: the output is bounded by the command itself"),
    ("src/atlas_plus/github_intake/gh.py", "*", "one gh API call with a timeout; no child tree"),
]


# The async ways of starting a child (KL6, sweep 35): both private async callers killed the direct child only
# at their timeout and left a grandchild running. atlas_plus._proc.run_bounded_async is the one place for them.
_G15_ASYNC = ("create_subprocess_exec", "create_subprocess_shell", "subprocess_exec", "subprocess_shell")


def _g15_hits(text: str) -> list[int]:
    """Lines of blocking subprocess calls that capture the child's output, and of every async child start."""
    out = set()
    for n in ast.walk(ast.parse(text)):
        if not isinstance(n, ast.Call):
            continue
        name = _dotted(n.func)
        if name.split(".")[-1] in _G15_ASYNC:
            out.add(n.lineno)
            continue
        if not (name.startswith(("subprocess.", "_subprocess.", "sp.")) and name.split(".")[-1] in ("run", "check_output")):
            continue
        kws = {k.arg: _dotted(k.value) for k in n.keywords if k.arg}
        if (name.endswith("check_output") or kws.get("capture_output") == "True"
                or kws.get("stdout", "").endswith("PIPE") or kws.get("stderr", "").endswith("PIPE")):
            out.add(n.lineno)
    return sorted(out)


def _g15_call_text(p: Path, lineno: int) -> str:
    """The call's lines joined: the allowlist snippet may sit on a continuation line."""
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return " ".join(s.strip() for s in lines[max(0, lineno - 6):lineno + 6])


def test_g15_captured_child_output_goes_through_the_bounded_helper():
    offenders = []
    for p in _py_files(SRC):
        for lineno in _g15_hits(p.read_text(encoding="utf-8", errors="replace")):
            if not _allowed(_rel(p), _g15_call_text(p, lineno), ALLOW_G15):
                offenders.append(f"{_rel(p)}:{lineno}: {_line(p, lineno)[:110]}")
    _report(offenders, "LES-G15", "(async code: await atlas_plus._proc.run_bounded_async) "
                                  "run a program whose output you read with atlas_plus._proc.run_bounded(argv, "
                                  "timeout=..., max_bytes=...): subprocess.run does not end the child's tree at the "
                                  "timeout and reads the whole output before any size check")


def test_the_g15_scan_sees_each_shape():
    bad = ("import subprocess\n"
           "r = subprocess.run(argv, capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)\n"
           "out = subprocess.check_output(argv, timeout=5, stdin=subprocess.DEVNULL)\n"
           "r = subprocess.run(argv, stdout=subprocess.PIPE, timeout=5, stdin=subprocess.DEVNULL)\n"
           "r = _subprocess.run(argv, stderr=_subprocess.PIPE, timeout=5, stdin=_subprocess.DEVNULL)\n"
           "proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE)\n"
           "proc = await asyncio.create_subprocess_shell(cmd)\n"
           "transport, protocol = await loop.subprocess_exec(factory, *argv)\n")
    good = ("from atlas_plus import _proc\n"
            "res = _proc.run_bounded(argv, timeout=5, max_bytes=1_000_000)\n"
            "subprocess.run(argv, timeout=5, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)\n"
            "subprocess.run(argv, timeout=5, stdin=subprocess.DEVNULL, stdout=log_file)\n"
            "p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)\n")
    assert _g15_hits(bad) == [2, 3, 4, 5, 6, 7, 8]
    assert not _g15_hits(good)


# ----------------------------------------------------------------------------------------------
# LES-G16  the MCP SDK's server class is imported in one module (LES-078)
#   AG109, sweep 31: mcp 2.x renamed mcp.server.fastmcp.FastMCP to mcp.server.mcpserver.MCPServer
#   and the old module raises on import. Two modules imported the old name, so on 2.x the server
#   did not start at all. The class comes from memory_server/_sdk.py, which knows both names; a
#   second import of either SDK module is the next major's outage.
# ----------------------------------------------------------------------------------------------

_G16_HOME = "src/atlas_plus/mcp/memory_server/_sdk.py"
_G16_MODULES = ("mcp.server.fastmcp", "mcp.server.mcpserver")


def _g16_hits(text: str) -> list[int]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    hits = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            if any(n.module == m or n.module.startswith(m + ".") for m in _G16_MODULES):
                hits.add(n.lineno)
            if n.module == "mcp.server" and any(a.name in ("fastmcp", "mcpserver", "FastMCP", "MCPServer")
                                                for a in n.names):
                hits.add(n.lineno)
        elif isinstance(n, ast.Import):
            if any(a.name == m or a.name.startswith(m + ".") for a in n.names for m in _G16_MODULES):
                hits.add(n.lineno)
    return sorted(hits)


def test_g16_the_mcp_sdk_server_class_is_imported_in_one_module():
    offenders = []
    for p in _py_files(SRC):
        if _rel(p) == _G16_HOME:
            continue
        for lineno in _g16_hits(p.read_text(encoding="utf-8", errors="replace")):
            offenders.append(f"{_rel(p)}:{lineno}: {_line(p, lineno)[:110]}")
    _report(offenders, "LES-G16", "import the server class from atlas_plus.mcp.memory_server._sdk (FastMCP, "
                                  "SDK_MAJOR): the SDK renamed its module between majors")
    assert _g16_hits((REPO / _G16_HOME).read_text(encoding="utf-8")), "the one home no longer imports the SDK"


def test_the_g16_scan_sees_each_shape():
    bad = ("from mcp.server.fastmcp import FastMCP\n"
           "from mcp.server.mcpserver import MCPServer\n"
           "import mcp.server.fastmcp as f\n"
           "from mcp.server import fastmcp\n"
           "from mcp.server.fastmcp.server import Context\n")
    good = ("from atlas_plus.mcp.memory_server._sdk import FastMCP, SDK_MAJOR\n"
            "from mcp.server.transport_security import TransportSecuritySettings\n"
            "from mcp import types\n"
            "from .fastmcp import helper\n")
    assert _g16_hits(bad) == [1, 2, 3, 4, 5]
    assert not _g16_hits(good)


def test_g17_no_test_finds_a_real_agent_program(tmp_path, monkeypatch):
    """LES-087: inside the suite, `shutil.which` never resolves `claude`, `codex` or `opencode`, even
    when a program of that name is first on PATH; other programs resolve as usual. A test that
    wants one present patches `shutil.which` itself."""
    import os
    import shutil
    import stat

    bindir = tmp_path / "bin"
    bindir.mkdir()
    names = ["claude", "codex", "opencode", "sextant-g17-probe"]
    for name in names:
        for fname in ((name + ".cmd", name + ".exe") if os.name == "nt" else (name,)):
            f = bindir / fname
            f.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
            f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))

    assert shutil.which("sextant-g17-probe"), "the probe program must resolve: the fixture hides too much"
    for name in ("claude", "codex", "opencode", "claude.cmd", "CLAUDE"):
        assert shutil.which(name) is None, f"a test could start the real {name}"


# ----------------------------------------------------------------------------------------------
# LES-G18  every command a message names exists (LES-089)
#   2026-11-25 (AG115): the MCP reply and the dashboard told a stranger to run `sextant fts-rebuild`,
#   which is not a subcommand (the program is `sextant-fts-rebuild`). A message that names a command
#   is checked against the real ones: the console scripts in pyproject.toml and the subcommands and
#   groups of the umbrella app (atlas_plus/cli.py, its hidden redirects included: they answer).
#   Docstrings are not messages; a name built at run time (`_next_step.script()`) is not a literal.
# ----------------------------------------------------------------------------------------------

# (name a literal spells, reason it is not a command to run)
ALLOW_G18: list[tuple[str, str]] = [
    ("atlas-memory", "the project name in a Brain prompt example (`atlas-memory+`), not a program"),
    ("atlas-tasks", "the dashboard's CSV download file name, not a program"),
    ("atlas-theme", "the dashboard's localStorage key, not a program"),
    ("sextant-ai", "the PyPI package name in a `pip install \"sextant-ai[...]\"` line, not a program"),
]

# an opener (backtick, quote, <code>) then `sextant <word>`, `sextant-<word>` or `atlas-<word>`
_G18_NAME = re.compile(r"""(?:`|<code>|(?<![\w-])["'])((?:sextant|atlas)-[a-z][a-z0-9-]*[a-z0-9]|sextant[ \t]+[a-z][a-z0-9-]*)""")


def _g18_real_commands() -> tuple[set[str], set[str]]:
    import tomllib

    scripts = set(tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"])
    builder = REPO / "scripts" / "build_public_repo.py"
    if "sextant" not in scripts and builder.exists():
        # The source tree carries only the atlas-* names; the build adds the sextant names from its alias table.
        scripts |= set(re.findall(r'\("atlas-[a-z-]+", "(sextant[a-z-]*)"\)', builder.read_text(encoding="utf-8")))
    from atlas_plus import cli

    subs = {c.name or c.callback.__name__.replace("_", "-") for c in cli.app.registered_commands}
    subs |= {g.name or "" for g in cli.app.registered_groups}
    return scripts, subs


def _g18_hits(text: str, scripts: set[str], subs: set[str]) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    docs = set()
    for d in ast.walk(tree):
        if (isinstance(d, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and d.body
                and isinstance(d.body[0], ast.Expr) and isinstance(d.body[0].value, ast.Constant)):
            docs.add(id(d.body[0].value))
    allowed = {name for name, _reason in ALLOW_G18}
    out = set()
    for n in ast.walk(tree):
        if id(n) in docs or not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
            continue
        for m in _G18_NAME.finditer(n.value):
            name = m.group(1)
            if name.startswith("sextant") and name[7:8] in (" ", "\t"):
                ok = name.split()[1] in subs
            else:
                ok = name in scripts or name in allowed
            if not ok:
                out.add((n.lineno, name))
    return sorted(out)


def test_g18_every_command_a_message_names_exists():
    for name, reason in ALLOW_G18:
        assert reason.strip(), f"ALLOW_G18 entry {name} has no reason"
    scripts, subs = _g18_real_commands()
    assert "sextant" in scripts and {"init", "setup", "verify", "doctor"} <= subs, "the command lists did not load"
    offenders = []
    for p in _py_files(SRC):
        for lineno, name in _g18_hits(p.read_text(encoding="utf-8", errors="replace"), scripts, subs):
            offenders.append(f"{_rel(p)}:{lineno}: names `{name}`, which is not a command")
    _report(offenders, "LES-G18", "a message names only commands that exist: a console script from "
                                  "pyproject.toml or a subcommand of `sextant`; build standalone names "
                                  "with atlas_plus._next_step.script() and subcommands with sub()")


def test_the_g18_scan_sees_each_shape():
    scripts, subs = {"sextant", "sextant-fts-rebuild"}, {"init", "verify"}
    bad = ('print("Run `sextant fts-rebuild` to rebuild")\n'
           "html = '<p>Run <code>sextant fts-rebuild</code></p>'\n"
           "msg = 'then run \"sextant-rebuild\"'\n"
           "x = f'try `atlas-nothing --help` ({n})'\n")
    good = ('"""Docstring: `sextant fts-rebuild` used to be named here."""\n'
            'print("Run `sextant-fts-rebuild`, then `sextant verify`")\n'
            "print(f'Run {script(\"fts-rebuild\")}')\n"
            "theme = 'atlas-theme'\n"
            "# `sextant nothing` in a comment is not printed\n")
    assert [ln for ln, _ in _g18_hits(bad, scripts, subs)] == [1, 2, 3, 4]
    assert not _g18_hits(good, scripts, subs)


# The dashboard's own pages are Python strings (scanned above); a file under web/static that names a
# command is served to a stranger too (SEX-052 item 17). The vendored minified files are skipped by
# name: they are third-party code, never a message of this project.
_G18_STATIC = SRC / "web" / "static"
ALLOW_G18_STATIC: list[tuple[str, str]] = [
    ("tailwind-*.js", "vendored Tailwind CSS runtime (third-party, minified), not a message of this project"),
]


def _g18_text_hits(text: str, scripts: set[str], subs: set[str]) -> list[tuple[int, str]]:
    allowed = {name for name, _reason in ALLOW_G18}
    out = set()
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in _G18_NAME.finditer(line):
            name = m.group(1)
            if name.startswith("sextant") and name[7:8] in (" ", "\t"):
                ok = name.split()[1] in subs
            else:
                ok = name in scripts or name in allowed
            if not ok:
                out.add((lineno, name))
    return sorted(out)


def test_g18_static_files_name_only_commands_that_exist():
    for pattern, reason in ALLOW_G18_STATIC:
        assert reason.strip(), f"ALLOW_G18_STATIC entry {pattern} has no reason"
    scripts, subs = _g18_real_commands()
    files = sorted(p for p in _G18_STATIC.rglob("*") if p.suffix.lower() in (".js", ".html", ".htm")) \
        if _G18_STATIC.is_dir() else []
    offenders, skipped = [], []
    for p in files:
        if any(fnmatch.fnmatch(p.name, pat) for pat, _r in ALLOW_G18_STATIC):
            skipped.append(p.name)
            continue
        for lineno, name in _g18_text_hits(p.read_text(encoding="utf-8", errors="replace"), scripts, subs):
            offenders.append(f"{_rel(p)}:{lineno}: names `{name}`, which is not a command")
    for pat, _r in ALLOW_G18_STATIC:   # a stale skip entry is removed, not kept
        assert not files or any(fnmatch.fnmatch(n, pat) for n in skipped), f"ALLOW_G18_STATIC {pat} matches no file"
    _report(offenders, "LES-G18", "a page under web/static names only commands that exist")


def test_the_g18_static_scan_sees_each_shape():
    scripts, subs = {"sextant", "sextant-fts-rebuild"}, {"init", "verify"}
    bad = ("<p>Run <code>sextant fts-rebuild</code></p>\n"
           "el.textContent = 'run \"sextant-rebuild\" first';\n"
           "const t = `try sextant nothing`;\n")
    good = ("<p>Run <code>sextant-fts-rebuild</code>, then <code>sextant verify</code></p>\n"
            "localStorage.getItem('atlas-theme');\n")
    assert [ln for ln, _ in _g18_text_hits(bad, scripts, subs)] == [1, 2]
    assert _g18_text_hits("x = `sextant nothing`", scripts, subs) == [(1, "sextant nothing")]
    assert not _g18_text_hits(good, scripts, subs)


# ----------------------------------------------------------------------------------------------
# LES-G19  no daemon thread reads through sys.stdin (LES-090)
#   2026-11-25 (AG116): `sextant context` read its payload on a daemon thread through
#   sys.stdin.buffer; when the pipe never closed, that thread held the BufferedReader's lock and
#   interpreter shutdown died on it ("Fatal Python error: _enter_buffered_busy", exit 139). A daemon
#   reader reads the file descriptor (os.read(sys.stdin.fileno(), ...)), as hooks/scope.py does.
#   Reading a CHILD's stdout on a daemon thread (roundtrip.py's _pump) is fine: that stream belongs
#   to the child and is not the interpreter's sys.stdin. The scan reads the target function's own
#   body (nested defs included) and, since 2026-11-25 (SEX-053 item 6), every function of the
#   same module it reaches by a plain call (`helper()`, `self.helper()`), a thread made a daemon
#   after construction (`t.daemon = True`, `t.setDaemon(True)`), a positional target and the
#   `run` of a Thread subclass that is a daemon. A reader in ANOTHER module is out of its sight.
# ----------------------------------------------------------------------------------------------

# (relative path, snippet on the line, reason)
ALLOW_G19: list[Allow] = [
    ("src/atlas_plus/hooks/scope.py", "sys.stdin.read(1_000_000)",
     "the fallback only when stdin has no file descriptor (a replaced in-memory stream): no "
     "BufferedReader over a pipe, so no lock held at shutdown"),
]
_G19_SAFE_ATTRS = ("fileno", "isatty", "encoding", "errors", "closed", "name", "mode")


def _g19_is_stdin(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "stdin"
            and isinstance(node.value, ast.Name) and node.value.id.lstrip("_") == "sys")


def _g19_is_thread_call(n: ast.AST) -> bool:
    return isinstance(n, ast.Call) and _dotted(n.func).split(".")[-1] == "Thread"


def _g19_true(node) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _g19_target_name(call: ast.Call):
    target = {kw.arg: kw.value for kw in call.keywords if kw.arg}.get("target")
    if target is None and len(call.args) >= 2:
        target = call.args[1]            # Thread(group, target, ...)
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _g19_daemon_targets(tree: ast.AST) -> set[str]:
    """Names of the functions a daemon thread of this module starts in."""
    names, held = set(), {}
    for n in ast.walk(tree):
        if _g19_is_thread_call(n):
            kws = {kw.arg: kw.value for kw in n.keywords if kw.arg}
            if _g19_true(kws.get("daemon")) and _g19_target_name(n):
                names.add(_g19_target_name(n))
        # t = Thread(target=f) ... made a daemon later
        if isinstance(n, ast.Assign) and _g19_is_thread_call(n.value) and _g19_target_name(n.value):
            for tgt in n.targets:
                held[_dotted(tgt)] = _g19_target_name(n.value)
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and _g19_true(n.value):
            for tgt in n.targets:
                if isinstance(tgt, ast.Attribute) and tgt.attr == "daemon" and _dotted(tgt.value) in held:
                    names.add(held[_dotted(tgt.value)])
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "setDaemon"
                and n.args and _g19_true(n.args[0]) and _dotted(n.func.value) in held):
            names.add(held[_dotted(n.func.value)])
    return names


def _g19_daemon_functions(tree: ast.AST) -> list[ast.AST]:
    """The function nodes a daemon thread runs: the named targets, the `run` of a Thread subclass
    that is a daemon, and every function of the module they reach by a plain call."""
    targets = _g19_daemon_targets(tree)
    defs: dict[str, list[ast.AST]] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.setdefault(fn.name, []).append(fn)
    todo = [fn for name in targets for fn in defs.get(name, [])]
    for cls in ast.walk(tree):
        if not (isinstance(cls, ast.ClassDef)
                and any(_dotted(b).split(".")[-1] == "Thread" for b in cls.bases)):
            continue
        daemon = any(isinstance(s, ast.Assign) and _g19_true(s.value)
                     and any(isinstance(t, ast.Name) and t.id == "daemon" for t in s.targets)
                     for s in cls.body)
        for n in ast.walk(cls):
            if isinstance(n, ast.Assign) and _g19_true(n.value) and any(
                    isinstance(t, ast.Attribute) and t.attr == "daemon" and _dotted(t.value) == "self"
                    for t in n.targets):
                daemon = True
            if isinstance(n, ast.Call) and any(kw.arg == "daemon" and _g19_true(kw.value) for kw in n.keywords) \
                    and _dotted(n.func).endswith("__init__"):
                daemon = True
        if daemon:
            todo += [s for s in cls.body if isinstance(s, ast.FunctionDef) and s.name == "run"]
    seen, out = set(), []
    while todo:
        fn = todo.pop()
        if id(fn) in seen:
            continue
        seen.add(id(fn))
        out.append(fn)
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call):
                continue
            if isinstance(n.func, ast.Name):
                todo += defs.get(n.func.id, [])
            elif isinstance(n.func, ast.Attribute) and _dotted(n.func.value) in ("self", "cls"):
                todo += defs.get(n.func.attr, [])
    return out


def _g19_hits(text: str) -> list[int]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    hits = set()
    for fn in _g19_daemon_functions(tree):
        parents = {}
        for p in ast.walk(fn):
            for c in ast.iter_child_nodes(p):
                parents[id(c)] = p
        for n in ast.walk(fn):
            if not _g19_is_stdin(n):
                continue
            up = parents.get(id(n))
            if isinstance(up, ast.Attribute) and up.value is n and up.attr in _G19_SAFE_ATTRS:
                continue   # sys.stdin.fileno() / .isatty(): no read through the stream
            hits.add(n.lineno)
    return sorted(hits)


def test_g19_no_daemon_thread_reads_through_sys_stdin():
    for _path, _snip, reason in ALLOW_G19:
        assert reason.strip()
    offenders, seen = [], set()
    for p in _py_files(SRC):
        rel = _rel(p)
        for lineno in _g19_hits(p.read_text(encoding="utf-8", errors="replace")):
            seen.add(rel)
            if not _allowed(rel, _line(p, lineno), ALLOW_G19):
                offenders.append(f"{rel}:{lineno}: {_line(p, lineno)[:110]}")
    _report(offenders, "LES-G19", "a daemon thread reads the descriptor (os.read(sys.stdin.fileno(), n)), "
                                  "never sys.stdin or sys.stdin.buffer: a reader blocked at exit kills "
                                  "the interpreter (exit 139)")
    for path, _snip, _reason in ALLOW_G19:   # a stale entry is removed, not kept
        assert path in seen or not (REPO / path).exists(), f"ALLOW_G19 entry {path} matches nothing"


def test_the_g19_scan_sees_each_shape():
    bad = ("import sys, threading\n"
           "def _read():\n"
           "    box['raw'] = sys.stdin.buffer.read()\n"
           "threading.Thread(target=_read, daemon=True).start()\n"
           "class W:\n"
           "    def _loop(self):\n"
           "        for line in sys.stdin:\n"
           "            pass\n"
           "    def go(self):\n"
           "        import threading as _th\n"
           "        _th.Thread(target=self._loop, daemon=True).start()\n"
           "def _r2():\n"
           "    import sys as _sys\n"
           "    return _sys.stdin.readline()\n"
           "threading.Thread(target=_r2, name='x', daemon=True)\n")
    assert _g19_hits(bad) == [3, 7, 14]
    good = ("import os, sys, threading\n"
            "def _read():\n"
            "    fd = sys.stdin.fileno()\n"
            "    if sys.stdin.isatty(): return\n"
            "    box['raw'] = os.read(fd, 65536)\n"
            "threading.Thread(target=_read, daemon=True).start()\n"
            "def _fg():\n"
            "    return sys.stdin.read()\n"
            "threading.Thread(target=_fg).start()\n"
            "def _pump():\n"
            "    return proc.stdout.readline()\n"
            "threading.Thread(target=_pump, daemon=True).start()\n")
    assert _g19_hits(good) == []


def test_the_g19_scan_sees_the_wider_shapes():
    """SEX-053 item 6: a daemon made after construction, a subclass, a positional target, and a
    reader the target reaches through a call in the same module."""
    later = ("import sys, threading\n"
             "def _read():\n"
             "    return sys.stdin.read()\n"
             "t = threading.Thread(target=_read)\n"
             "t.daemon = True\n"
             "t.start()\n")
    assert _g19_hits(later) == [3]
    set_daemon = later.replace("t.daemon = True", "t.setDaemon(True)")
    assert _g19_hits(set_daemon) == [3]
    held_on_self = ("import sys, threading\n"
                    "class W:\n"
                    "    def _loop(self):\n"
                    "        return sys.stdin.buffer.read()\n"
                    "    def go(self):\n"
                    "        self._t = threading.Thread(target=self._loop)\n"
                    "        self._t.daemon = True\n")
    assert _g19_hits(held_on_self) == [4]
    subclass = ("import sys, threading\n"
                "class Reader(threading.Thread):\n"
                "    daemon = True\n"
                "    def run(self):\n"
                "        self.raw = sys.stdin.readline()\n"
                "class Reader2(threading.Thread):\n"
                "    def __init__(self):\n"
                "        super().__init__(daemon=True)\n"
                "    def run(self):\n"
                "        self._pull()\n"
                "    def _pull(self):\n"
                "        return sys.stdin.read()\n")
    assert _g19_hits(subclass) == [5, 12]
    positional = ("import sys, threading\n"
                  "def _read():\n"
                  "    return sys.stdin.read()\n"
                  "threading.Thread(None, _read, daemon=True).start()\n")
    assert _g19_hits(positional) == [3]
    through_a_call = ("import sys, threading\n"
                      "def _payload():\n"
                      "    return sys.stdin.buffer.read()\n"
                      "def _deeper():\n"
                      "    return _payload()\n"
                      "def _read():\n"
                      "    box['raw'] = _deeper()\n"
                      "threading.Thread(target=_read, daemon=True).start()\n")
    assert _g19_hits(through_a_call) == [3]
    fine = ("import sys, threading\n"
            "def _payload():\n"
            "    return sys.stdin.read()\n"          # read in the foreground only
            "def _work():\n"
            "    return 1\n"
            "t = threading.Thread(target=_work)\n"
            "t.daemon = True\n"
            "class Fg(threading.Thread):\n"           # not a daemon: it is joined at exit
            "    def run(self):\n"
            "        return sys.stdin.read()\n"
            "u = threading.Thread(target=_payload)\n"
            "u.daemon = False\n")
    assert _g19_hits(fine) == []


# --- G20 (AG119, sweep 35): a note's frontmatter goes through atlas_plus._yaml.load_mapping ---------
# A note may open with a rule line over a sentence, a list, a number: YAML gives a str/list/int, and
# a caller's `.get` raised AttributeError (the orphan scan, `review stale`, `review report` ...).
# Every YAML load outside _yaml.py is a finding unless it is on this reviewed list (config and data
# files where a non-mapping is legitimate, or a load followed by its own isinstance(..., dict)
# check). The list only shrinks. Keys: (path under atlas_plus, enclosing function) -> reason.
_G20_REVIEWED = {
    ("mcp/memory_server/routing.py", "_load_routes"):
        "the routing CONFIG file, not a note; a non-mapping is caught by the isinstance below it",
    ("mcp/memory_server/tools/anti.py", "list_anti"):
        "anti-memory entries: each block is checked with isinstance(entry, dict) before use",
    ("intake/frontmatter.py", "parse_frontmatter"):
        "checked with isinstance(data, dict) right after the load; non-mapping is text",
    ("import_cmd.py", "_existing_bodies"): "one scalar value (origin:), checked with isinstance(v, str)",
    ("import_cmd.py", "_corpus_index"): "one scalar value (origin:), checked with isinstance(v, str)",
    ("import_cmd.py", "_import_copies"): "one scalar value (original_path:), checked with isinstance(stamped, str)",
    ("import_cmd.py", "_stamp_existing_frontmatter"):
        "None becomes {} and anything but a mapping returns the note unchanged (isinstance right after the load)",
    ("curation/health.py", "health_from_text"):
        "checked with isinstance(fm, dict) right after the load (no_frontmatter)",
}
_G20_NAMES = {"fast_safe_load", "_fast_yaml_load"}
_G20_ATTRS = {"safe_load", "full_load", "unsafe_load", "load_all", "safe_load_all"}


def _g20_hits(tree):
    """(line, enclosing function) of every YAML load call in a parsed module."""
    hits = []

    def walk(node, func):
        for child in ast.iter_child_nodes(node):
            f = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if isinstance(child, ast.Call):
                fn = child.func
                if isinstance(fn, ast.Name) and fn.id in _G20_NAMES:
                    hits.append((child.lineno, func))
                elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and (
                        fn.attr in _G20_ATTRS
                        or (fn.attr == "load" and fn.value.id.lstrip("_").lower() in ("yaml", "yaml2", "y"))):
                    hits.append((child.lineno, func))
            walk(child, f)

    walk(tree, "<module>")
    return hits


def test_g20_frontmatter_loads_go_through_load_mapping():
    found, offenders = set(), []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel == "_yaml.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, func in _g20_hits(tree):
            if (rel, func) in _G20_REVIEWED:
                found.add((rel, func))
            else:
                offenders.append(f"{rel}:{line} in {func}()")
    assert not offenders, (
        "G20: a YAML load outside atlas_plus/_yaml.py; a note's frontmatter goes through "
        "_yaml.load_mapping (or load_mapping_or_none), a config/data load goes on _G20_REVIEWED "
        "with its reason:\n  " + "\n  ".join(offenders))
    # A reviewed entry whose load is gone is removed (the list only shrinks). On a tree without one of
    # these modules (a public build) the entry is simply absent.
    stale = [k for k in _G20_REVIEWED if (SRC / k[0]).exists() and k not in found]
    assert not stale, f"G20: reviewed entries with no YAML load any more: {stale}"


def test_g20_self_test_flags_planted_offenders():
    bad = ("import yaml\nimport yaml as _yaml\nfrom atlas_plus._yaml import fast_safe_load as _fast_yaml_load\n"
           "def a(t):\n    return yaml.safe_load(t) or {}\n"
           "def b(t):\n    return _yaml.load(t, Loader=_yaml.SafeLoader).get('x')\n"
           "def c(t):\n    return _fast_yaml_load(t) or {}\n"
           "def d(t):\n    return fast_safe_load(t)['k']\n")
    good = ("from atlas_plus._yaml import load_mapping\nimport json\n"
            "def a(t):\n    return load_mapping(t).get('x')\n"
            "def b(t):\n    return json.loads(t)\n"
            "def c(p):\n    return p.load()\n")
    assert [f for _, f in _g20_hits(ast.parse(bad))] == ["a", "b", "c", "d"]
    assert not _g20_hits(ast.parse(good))


# --- G21 (AG121, lesson LES-092): a change to what a note stores in the keyword index raises the parse revision ---
# An index row is rewritten only when a note's (mtime, size) changes, a format bump rebuilds every note, and a
# parse revision makes the next incremental update re-index the notes whose row would differ. A parser change
# with none of the three left 0.1.0's rows in place after an upgrade (MN93 F1: words in a rule-line top block
# gave "No hits" until a forced rebuild). The snapshot below is the digest of the rows a fixture corpus stores;
# it sits next to the revision it was taken with.
_G21_PARSE_REVISION = "1"
_G21_INDEX_FORMAT = "11"
_G21_SNAPSHOT = "e17704a087d65293f627326b8555ac13f199a149fdb26d05e9937a77ff9f44aa"


def _g21_fixture():
    from atlas_plus.init_cmd import _IDENTITY_TEMPLATE
    return {
        "rules/rule-top.md": "---\nThis top block is a plain sentence about the lighthouse.\n---\nThe body.\n",
        "rules/list-top.md": "---\n- alpha item\n- beta item\n---\nBody under a list top block.\n",
        "identity/user.md": _IDENTITY_TEMPLATE,
        "identity/user-plus.md": _IDENTITY_TEMPLATE + "\nGoes by Sam; on call for payments.\n",
        "notes/cjk.md": "---\ntitle: 東京\n---\n東京の天気は晴れです。\n",
        "notes/frontmatter.md": "---\ntitle: Router\ntags: [homelab, net]\nconfirmed: 2026-01-02\n---\nThe router firmware is 4.2.\n",
        "notes/plain.md": "# Plain\n\nThe staging box reboots on Fridays.\n",
        "notes/provenance.md": "Kayak paddle is 220 cm. <!-- source:remember -->\n",
        "notes/crlf.md": "---\r\ntitle: Windows\r\n---\r\nA note written on Windows.\r\n",
    }


def _g21_digest(fts) -> str:
    import hashlib
    h = hashlib.sha256()
    for rel, raw in sorted(_g21_fixture().items()):
        fm_json, body, confirmed = fts._parse_md(raw)
        for part in (rel, fm_json, fts._index_body(fm_json, body), repr(confirmed)):
            h.update(part.encode("utf-8") + b"\x00")
    return h.hexdigest()


def _g21_check(fts) -> str:
    """'' when the stored rows match the snapshot of this revision, else the message the guardrail fails with."""
    if (fts.PARSE_REVISION, fts.INDEX_FORMAT) != (_G21_PARSE_REVISION, _G21_INDEX_FORMAT):
        return ("G21: the parse revision or INDEX_FORMAT changed: take the new snapshot "
                f"(_G21_SNAPSHOT = {_g21_digest(fts)!r}) and record the revision beside it")
    got = _g21_digest(fts)
    if got != _G21_SNAPSHOT:
        return ("G21: what a note stores in the keyword index changed without a new parse revision: "
                "raise the parse revision so old indexes heal (fts.PARSE_REVISION), or bump INDEX_FORMAT, "
                f"then set _G21_SNAPSHOT = {got!r} (lesson LES-092)")
    return ""


def test_g21_a_changed_index_row_raises_the_parse_revision():
    from atlas_plus.mcp.memory_server import fts
    msg = _g21_check(fts)
    assert not msg, msg


def test_g21_self_test_a_parser_that_drops_a_word_trips_it(monkeypatch):
    from atlas_plus.mcp.memory_server import fts
    real = fts._parse_md

    def drops_a_word(raw):
        fm_json, body, confirmed = real(raw)
        return fm_json, body.replace("lighthouse", ""), confirmed
    monkeypatch.setattr(fts, "_parse_md", drops_a_word)
    assert "raise the parse revision so old indexes heal" in _g21_check(fts)


# --- G22 (AG122, SEX-048): a corpus path printed by `review` and `versions` goes through _term.safe ---
# Functional: the commands below run in process on a tiny corpus whose file name holds U+202E; no raw
# format (Cf) or C0 character may reach the output. Covered: review stale, versions, versions --prune.
def test_g22_printed_corpus_paths_go_through_term_safe(tmp_path, monkeypatch, capsys):
    import unicodedata as _ud

    import pytest

    from typer.testing import CliRunner as _Runner
    root = tmp_path / "mem"
    (root / "reference").mkdir(parents=True)
    (root / "versions" / "reference").mkdir(parents=True)
    name = "g22-\u202egnp"
    try:
        (root / "reference" / f"{name}.md").write_text("---\nupdated: 2020-01-01\n---\nx\n", encoding="utf-8")
        (root / "versions" / "reference" / f"{name}_2020-01-01_v1.md").write_text("x\n", encoding="utf-8")
    except OSError:
        pytest.skip("file system refuses U+202E in a name")
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(root))
    monkeypatch.setenv("COLUMNS", "200")
    from atlas_plus import versions_cmd
    from atlas_plus.curation import cli as _ccli
    from atlas_plus.curation import stale as _cstale
    for mod in (_ccli, _cstale):
        if hasattr(mod, "MEMORY_ROOT"):
            monkeypatch.setattr(mod, "MEMORY_ROOT", root)
    outs = {"review stale": _Runner().invoke(_ccli.app, ["stale"]).output}
    versions_cmd.main([])
    versions_cmd.main(["--prune", "--older-than", "0", "--keep-last", "0"])
    outs["versions"] = capsys.readouterr().out
    for cmd, out in outs.items():
        assert "g22-" in out, (cmd, out)
        raw = [hex(ord(c)) for c in out if _ud.category(c) == "Cf" or (_ud.category(c) == "Cc" and c not in "\n\r\t")]
        assert not raw, (cmd, raw)


# --- G23 (AG125, ATL-074): a dashboard panel that fails never shows the exception's own text ---
# AST scan of every file under src/atlas_plus/web/ (no hard-coded file list: the private tree's extra
# panels are scanned too). Inside an `except ... as <name>:` block, an HTML body (an f-string holding
# "<", or a call to HTMLResponse / a *_html renderer) may not read <name> except for its class
# (`type(<name>).__name__`, `<name>.__class__`) or an HTTP status (`<name>.response.status_code`).
# JSON routes that return a `detail` / `error` key are a different surface and are not scanned.
_G23_OK_ATTRS = {"__class__", "response", "status_code"}
# Reviewed list, only shrinks. Key: "<file>::<enclosing function>". Reason required.
_G23_REVIEWED = {
    "app.py::watchtower_panel": "WatchtowerError messages are written for the user by the SIEM client and "
                                "render_error_html adds its NEXT_STEP; they never carry a raw exception's text",
}


def _g23_leaks(node, name):
    parents = {c: p for p in ast.walk(node) for c in ast.iter_child_nodes(p)}
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id == name:
            par = parents.get(n)
            if isinstance(par, ast.Attribute) and par.attr in _G23_OK_ATTRS:
                continue
            if isinstance(par, ast.Call) and isinstance(par.func, ast.Name) and par.func.id == "type":
                continue
            return True
    return False


def _g23_htmlish(n):
    if isinstance(n, ast.JoinedStr):
        return any(isinstance(v, ast.Constant) and "<" in str(v.value) for v in n.values)
    if isinstance(n, ast.Call):
        f = n.func
        nm = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
        return nm == "HTMLResponse" or nm.endswith("_html")
    return False


def _g23_offenders(source, fname):
    found = set()

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            f2 = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if isinstance(child, ast.ExceptHandler) and child.name:
                for sub in child.body:
                    if any(_g23_htmlish(n) and _g23_leaks(n, child.name) for n in ast.walk(sub)):
                        found.add(f"{fname}::{f2 or '<module>'}")
            visit(child, f2)

    visit(ast.parse(source), "")
    return found


def _g23_scan(web_dir):
    found = set()
    for path in sorted(web_dir.rglob("*.py")):
        rel = path.relative_to(web_dir).as_posix()
        found |= _g23_offenders(path.read_text(encoding="utf-8"), rel)
    return found


def test_g23_panel_failure_text_never_shows_the_exception_message():
    web_dir = SRC / "web"
    assert web_dir.is_dir(), web_dir
    found = _g23_scan(web_dir)
    new = sorted(found - set(_G23_REVIEWED))
    assert not new, (
        "A dashboard panel shows the exception's own text on the page (a host path, a module name, an SQL "
        "message, and no next step): " + ", ".join(new) + ". Write `return _panel_failure(\"<Panel name>\", "
        "exc)` instead (web/app.py: it names the panel, the class and the next step, and logs the detail).")
    stale = sorted(k for k in _G23_REVIEWED if k not in found)
    assert not stale, f"G23 reviewed entries no longer offend; remove them (the list only shrinks): {stale}"
    assert all(str(v).strip() for v in _G23_REVIEWED.values()), "every G23 reviewed entry needs a reason"


def test_g23_self_test_a_planted_offender_trips_the_scan():
    planted = (
        "def panel(request):\n"
        "    try:\n"
        "        read()\n"
        "    except Exception as err:\n"
        "        return HTMLResponse(f'<div>Error: {escape(str(err))}</div>')\n"
        "def fine(request):\n"
        "    try:\n"
        "        read()\n"
        "    except Exception as err:\n"
        "        return HTMLResponse(f'<div>{type(err).__name__}</div>')\n"
        "def bare(request):\n"
        "    try:\n"
        "        read()\n"
        "    except Exception as e:\n"
        "        html = f'<p>{e}</p>'\n"
        "        return HTMLResponse(html)\n"
    )
    assert _g23_offenders(planted, "planted.py") == {"planted.py::panel", "planted.py::bare"}


# --- G24 (lesson LES-082, AG126): every question goes through atlas_plus/_prompt.py ------------------------------
# On Windows the NUL device passes isatty(): a question asked behind `isatty()` alone is printed to nobody, then
# refused. `_prompt.is_terminal` checks the console mode; `_prompt.confirm` / `_prompt.ask_line` print no question
# without a console, read a piped line, and end an empty or closed stdin with a next step. This block is
# self-contained (its own helpers, prefixed _g24_).

_G24_QUESTION_ATTRS = {("typer", "confirm"), ("typer", "prompt"), ("click", "confirm"), ("click", "prompt"),
                       ("Confirm", "ask"), ("Prompt", "ask"), ("getpass", "getpass"), ("console", "input")}
_G24_QUESTION_NAMES = {"input", "getpass"}

# (file relative to src/atlas_plus, enclosing function): reason. This list only shrinks.
_G24_REVIEWED_QUESTIONS = {
    ("_prompt.py", "confirm"): "the helper itself (typer.confirm only behind is_terminal)",
    ("brain/cli.py", "run"): "the Brain chat loop reads commands, not a yes/no; EOF and Ctrl-C end it with 'Bye.'",
}
# `.isatty()` that does NOT decide whether to ask a question: (file, enclosing function): reason. Only shrinks.
_G24_REVIEWED_ISATTY = {
    ("_prompt.py", "is_terminal"): "the one console check",
    ("_console.py", "utf8_stdout_when_piped"): "chooses an output encoding, asks nothing",
    ("_console.py", "write_text_out"): "chooses an output encoding (console, hook pipe or plain pipe), asks nothing",
    ("doctor/cli.py", "_plain_if_piped"): "colours or plain text for the report",
    ("hooks/scope.py", "read_payload"): "a hook reads its JSON payload; a console (or NUL) means no payload",
    ("hooks/session_start.py", "main"): "colours or plain text for the printed context",
    ("summarizer/hook_entry.py", "_transcript_root_from_payload"): "the Stop payload; a console (or NUL) means none",
    ("summarizer/hook_entry.py", "main"): "chooses an output encoding, asks nothing",
}


def _g24_enclosing(tree):
    """Map each node to the name of the innermost function that holds it ("<module>" outside any)."""
    owner = {}

    def walk(node, name):
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else name
            owner[child] = inner
            walk(child, inner)
    walk(tree, "<module>")
    return owner


def _g24_scan_source(text):
    """(kind, function, call) for every question call and every `.isatty()` in one module's source."""
    tree = ast.parse(text)
    owner = _g24_enclosing(tree)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id in _G24_QUESTION_NAMES:
            found.append(("question", owner.get(node, "<module>"), f.id))
        elif isinstance(f, ast.Attribute):
            base = f.value
            base_name = base.id if isinstance(base, ast.Name) else (base.attr if isinstance(base, ast.Attribute) else "")
            if (base_name, f.attr) in _G24_QUESTION_ATTRS:
                found.append(("question", owner.get(node, "<module>"), f"{base_name}.{f.attr}"))
            elif f.attr == "isatty":
                found.append(("isatty", owner.get(node, "<module>"), "isatty"))
    return found


def _g24_scan_tree(root):
    out = {"question": {}, "isatty": {}}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for kind, func, call in _g24_scan_source(text):
            out[kind].setdefault((rel, func), []).append(call)
    return out


def test_g24_questions_go_through_the_prompt_helper():
    found = _g24_scan_tree(SRC)
    new_q = sorted(k for k in found["question"] if k not in _G24_REVIEWED_QUESTIONS)
    assert not new_q, (
        "G24 (LES-082): a question is asked outside atlas_plus/_prompt.py: " + ", ".join(
            f"{f}:{fn} ({'/'.join(found['question'][(f, fn)])})" for f, fn in new_q)
        + ". On Windows `< NUL` passes isatty(), so the question is printed to nobody. Write instead: "
        "`from atlas_plus import _prompt` then `_prompt.confirm(question, rerun=<the --yes form, built with "
        "_next_step / branding>)` for a yes/no, or `_prompt.ask_line(question)` (None = no answer: print the next "
        "step) for free text.")
    new_t = sorted(k for k in found["isatty"] if k not in _G24_REVIEWED_ISATTY)
    assert not new_t, (
        "G24 (LES-082): `.isatty()` outside the reviewed list: " + ", ".join(f"{f}:{fn}" for f, fn in new_t)
        + ". If it decides whether to ASK, use `_prompt.is_terminal(stream)` (the console-mode check); if it only "
        "chooses an encoding or colours, add it to _G24_REVIEWED_ISATTY with the reason.")
    stale = sorted(k for k in list(_G24_REVIEWED_QUESTIONS) + list(_G24_REVIEWED_ISATTY)
                   if k not in found["question"] and k not in found["isatty"])
    assert not stale, f"G24: reviewed entries that no longer match anything (remove them; the list only shrinks): {stale}"


def test_g24_self_test_flags_planted_offenders():
    planted = (
        "import typer\n"
        "def ask_it():\n"
        "    if sys.stdin.isatty():\n"
        "        typer.confirm('Go?', abort=True)\n"
        "    return input('name? ')\n"
        "def harmless():\n"
        "    _capture_input({}, 'x', None)\n"
        "    console.print('input(')\n"
    )
    hits = _g24_scan_source(planted)
    assert ("question", "ask_it", "typer.confirm") in hits
    assert ("question", "ask_it", "input") in hits
    assert ("isatty", "ask_it", "isatty") in hits
    assert not [h for h in hits if h[1] == "harmless"], hits
