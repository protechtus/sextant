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
    for name, allow in (("G1", ALLOW_G1), ("G2", ALLOW_G2), ("G3", ALLOW_G3), ("G4", ALLOW_G4), ("G6", ALLOW_G6),
                        ("G7", ALLOW_G7), ("G9", ALLOW_G9), ("G10", ALLOW_G10)):
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
    ("atlas-session-start", "bad-arg", "SessionStart hook: ignores arguments so a client's hook line never breaks"),
    ("sextant-session-start", "bad-arg", "the public name of the SessionStart hook: ignores arguments, as above"),
    ("atlas-pre-compact", "bad-arg", "PreCompact hook: ignores arguments so a client's hook line never breaks"),
    ("atlas-prime", "bad-arg", "every argument is a task word (`atlas-prime fix the login bug`)"),
    ("sextant context", "bad-arg", "with words it is atlas-prime: every argument is a task word"),
    ("sextant prime", "bad-arg", "alias of `context`: every argument is a task word"),
    ("sextant verify", "no-corpus", "starts the MCP server as a child process (seconds); its no-corpus "
                                    "next step is tested in tests/test_ag102.py"),
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


def _g8_cases() -> list[dict]:
    scripts = _console_scripts()
    cases, seen = [], set()
    for name, target in scripts.items():
        cases.append({"name": name, "case": "bad-arg", "target": target, "argv": [name, "--no-such-option-g8"]})
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
            for case in ("bad-arg", "no-corpus"):
                name = f"sextant {sub}"
                if any(a[0] == name and a[1] in ("*", case) for a in ALLOW_G8) or (case == "no-corpus" and sub == "init"):
                    continue
                if case == "bad-arg":
                    argv = ["sextant", sub, "--no-such-option-g8"]
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
        elif r["rc"] != 0 and not _g8_names_next_step(r["stderr"] or r["stdout"]):
            offenders.append(f"{label}: the last lines name no next step: "
                             f"{(r['stderr'] or r['stdout']).strip().splitlines()[-1:]!r}")
        elif r["case"] == "bad-arg" and not r["stderr"].strip():
            offenders.append(f"{label}: the usage error went to stdout only")
    _report(offenders, "LES-G8", "a failure exits non-zero without a traceback and its last three lines name the "
                                 "next step (a `command`, `--help`, a docs/*.md page or a URL)")
    known = set(_console_scripts())
    stale = [a[0] for a in ALLOW_G8 if a[0] not in known | set(payload["subs"])]
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


if __name__ == "__main__" and sys.argv[1:2] == ["--g8-child"]:
    import json as _json

    _cases = _json.loads(sys.stdin.read())
    _out = _g8_child(_cases)
    sys.__stdout__.write(_json.dumps(_out) + "\n")
