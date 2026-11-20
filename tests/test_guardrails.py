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
    for name, allow in (("G1", ALLOW_G1), ("G2", ALLOW_G2), ("G3", ALLOW_G3), ("G4", ALLOW_G4), ("G6", ALLOW_G6)):
        for path, snippet, _reason in allow:
            f = REPO / path
            if not f.exists():
                if full_checkout:
                    stale.append(f"{name}: {path} does not exist")
            elif snippet != "*" and snippet not in f.read_text(encoding="utf-8", errors="replace"):
                stale.append(f"{name}: {path} no longer contains {snippet!r}")
    assert not stale, "stale allowlist entries:\n  " + "\n  ".join(stale)
