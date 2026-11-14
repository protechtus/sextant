"""Claude Code Stop hook entry point.

Invoked by Claude Code when a session ends. Launches background processes
without blocking the session close:

  1. Summarizer        -- writes session log to .memory/sessions/ and the updates
                         file (<state>/updates.txt unless ATLAS_UPDATES_FILE is set)
  2. Suggest-facts     -- scans transcript for high-confidence facts and auto-writes them
  3. Git diff capture  -- snapshots git status + diff for recently changed projects
  4. Transcript mining -- incremental atlas-temporal build --source mining

Kill switches (set to 1 to disable):
  ATLAS_DISABLE_AUTOSUMMARIZE=1
  ATLAS_DISABLE_SUGGEST_FACTS=1
  ATLAS_DISABLE_TRANSCRIPT_MINING=1

Write gate:
  All subprocesses are skipped if is_session_autonomous() returns False.
  Set ATLAS_SESSION_AUTONOMOUS=on to allow session writes while keeping
  ATLAS_AUTONOMOUS=off for all other write paths.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from atlas_plus.autonomous import is_session_autonomous
from atlas_plus import paths as atlas_paths

_STATE_DIR = atlas_paths.state_dir()
_PROJECTS_ROOT = atlas_paths.projects_root()


def _git_out(args, cwd, timeout=5) -> str:
    """Hardened git read (round-B re-audit 2026-11-09, N-B): repo-local config may not
    execute anything while the Stop hook looks at every repo under the projects root."""
    from atlas_plus import gitsafe
    import subprocess as _sp
    return _sp.check_output(gitsafe.argv(args, cwd, timeout=timeout), cwd=str(cwd),
                            stderr=_sp.DEVNULL, text=True, timeout=timeout, env=gitsafe._env())


def _capture_fsdiffs() -> None:
    """Snapshot git status + diff for any project with recent changes."""
    if not _PROJECTS_ROOT.exists():
        return
    ts = datetime.now(timezone.utc)
    date_str = ts.strftime("%Y-%m-%d")
    stamp = ts.strftime("%Y%m%dT%H%M%S")
    out_dir = _STATE_DIR / "fsdiffs" / date_str
    out_dir.mkdir(parents=True, exist_ok=True)

    projects = []
    for proj_dir in sorted(_PROJECTS_ROOT.iterdir()):
        if not (proj_dir / ".git").is_dir():
            continue
        try:
            dirty = _git_out(
                ["git", "status", "--porcelain"], proj_dir, timeout=5).strip()
            recent_log = _git_out(
                ["git", "log", "--oneline", "-5", "--since=24.hours"], proj_dir, timeout=5).strip()
        except Exception:
            continue
        if not dirty and not recent_log:
            continue
        try:
            branch = _git_out(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], proj_dir, timeout=5).strip()
            commit = _git_out(
                ["git", "rev-parse", "--short", "HEAD"], proj_dir, timeout=5).strip()
            diff = _git_out(
                ["git", "diff", "HEAD"], proj_dir, timeout=10)[:4000]
        except Exception:
            branch, commit, diff = "unknown", "unknown", ""

        dirty_files = [l.strip() for l in dirty.splitlines() if l.strip()][:20]
        projects.append({
            "project": proj_dir.name,
            "branch": branch,
            "commit": commit,
            "dirty_files": dirty_files,
            "diff_summary": diff,
            "recent_commits": recent_log,
        })

    if not projects:
        return

    record = {
        "conversation_id": f"stop-hook-{stamp}",
        "projects": projects,
        "ts": ts.isoformat(),
    }
    out_file = out_dir / f"{stamp}.fsdiff.json"
    out_file.write_text(json.dumps(record, indent=2), encoding="utf-8")
    try:
        # one snapshot per Stop, Stop fires per reply: keep the newest 200 across the day folders (MN39)
        snaps = sorted(out_dir.parent.rglob("*.fsdiff.json"), key=lambda p: p.name)
        for old in snaps[:-200]:
            old.unlink()
    except OSError:
        pass


def _find_script(name: str) -> Path | None:
    """Locate a console_script entry point relative to the current Python."""
    scripts_dir = Path(sys.executable).parent
    for candidate in [f"{name}.exe", name]:
        p = scripts_dir / candidate
        if p.exists():
            return p
    # a `pip install --user` puts the scripts in ~/.local/bin beside no interpreter (MN39: the facts
    # stage was skipped with no trace); PATH and the user scripts dir are the next places to look
    import shutil as _shutil
    found = _shutil.which(name)
    if found:
        return Path(found)
    for p in (Path.home() / ".local" / "bin" / name, Path.home() / ".local" / "bin" / f"{name}.exe"):
        if p.exists():
            return p
    return None


_SESSION: dict[str, str] = {"id": ""}  # the Stop payload's session id, for the summarizer
_PAYLOAD = {"seen": False}


def _dry_run_flag() -> list[str]:
    """`--dry-run` for the summarizer child when ATLAS_SUMMARIZER_DRY_RUN is on (AG60)."""
    return ["--dry-run"] if os.environ.get("ATLAS_SUMMARIZER_DRY_RUN", "").strip().lower() in ("on", "1", "true") else []


def _payload_seen() -> bool:
    return _PAYLOAD["seen"]


def _summarizer_unavailable_reason() -> str:
    """'' when a summarizing model is reachable, else one line saying what is missing."""
    try:
        from atlas_plus.llm.ollama_url import default_ollama_url
        import urllib.request
        url = (os.environ.get("ATLAS_OLLAMA_URL") or "").strip().rstrip("/") or default_ollama_url()  # the summarizer's own variable first (AG34)
        try:
            with urllib.request.urlopen(f"{url}/api/tags", timeout=1.5) as resp:
                body = resp.read(200_000)
        except Exception:  # noqa: BLE001
            body = None
        if body is not None:
            # the server answered; a model that is not pulled fails the summarizer silently in a
            # detached child (MN28: the docs say `ollama pull qwen3:8b`, the summarizer asked for another)
            try:
                import json as _json
                names = [str(m.get("name") or m.get("model") or "") for m in (_json.loads(body.decode("utf-8", "replace")) or {}).get("models", [])]
            except Exception:  # noqa: BLE001
                return ""
            from atlas_plus.summarizer.providers import OLLAMA_MODEL as _want
            # exact tag, or `name:latest` for a bare name: a pulled sibling tag (qwen3:4b for qwen3:8b)
            # passed the probe and then every chat answered 404 (AG40)
            have = set(names)
            _want_full = _want if ":" in _want else _want + ":latest"
            if names and _want not in have and _want_full not in have:
                return (f"model {_want} is not pulled at {url} (`ollama pull {_want}`, or set ATLAS_SUMMARIZER_MODEL "
                        f"to one you have: {', '.join(names[:6])})")
            return ""
        local_only = os.environ.get("ATLAS_LOCAL_ONLY", "on").strip().lower() not in ("off", "0", "false")
        if local_only:
            return (f"no local model at {url} (start Ollama or set ATLAS_OLLAMA_URL; "
                    "cloud providers need ATLAS_LOCAL_ONLY=off and a key)")
        if any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")):
            return ""
        return f"no local model at {url} and no provider key (ANTHROPIC_API_KEY / OPENAI_API_KEY)"
    except Exception:  # noqa: BLE001
        return ""


def _posix_path_for(path: str) -> str:
    """A Windows path from a Windows Claude Code, received by a WSL install, as its /mnt form
    (MN33: `E:\\x\\sess.jsonl` was one relative file name on Linux and the root became the cwd)."""
    import os as _os
    import re as _re
    m = _re.match(r"^([A-Za-z]):[\\/](.*)$", path)
    if _os.name != "nt" and m:
        return f"/mnt/{m.group(1).lower()}/" + m.group(2).replace("\\", "/")
    return path


def _transcript_root_from_payload() -> str:
    """``<root>`` from the Stop payload's ``transcript_path`` (``<root>/<project>/<session>.jsonl``), or ''."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            _PAYLOAD["seen"] = True
            _PAYLOAD["empty"] = True  # a terminal, no payload: never summarise the whole day (MN51)
            return ""
        raw = sys.stdin.read()
        if not raw.strip():
            _PAYLOAD["seen"] = True
            _PAYLOAD["empty"] = True
            return ""
        import json as _json
        try:
            payload = _json.loads(raw.lstrip("\ufeff"))  # a BOM from a Windows pipe is not mangled quotes (MN57)
        except ValueError:
            _PAYLOAD["seen"] = True  # something arrived on stdin that is not a payload: refuse, never summarise the whole day (MN45: PowerShell 5.1 mangles the quoted JSON)
            _PAYLOAD["garbled"] = True
            return ""
        if isinstance(payload, dict) and isinstance(payload.get("session_id"), str):
            _SESSION["id"] = payload["session_id"].strip()[:120]
        _PAYLOAD["seen"] = True
        tp = payload.get("transcript_path") if isinstance(payload, dict) else None
        if not tp:
            return ""
        from pathlib import Path as _Path
        _p = _Path(_posix_path_for(str(tp))).resolve()
        root = _p.parents[1]
        if not _p.is_file() or not root.name.endswith("projects"):
            _PAYLOAD["refused"] = str(_p)[:200]  # named below, never a silent fallback to a full scan (MN57)
            return ""  # a payload must not choose the scanned tree: the transcript must exist and sit under a .../projects root (AG64: a nonexistent path outside the root made the hook scan its parent tree)
        if _p.is_file():
            _PAYLOAD["transcript_file"] = str(_p)
        return str(root) if root.is_dir() else ""
    except Exception:  # noqa: BLE001 - a hook never fails the session over its payload
        return ""


def main() -> int:
    """Entry point for the transcript summarizer hook: read stdin transcript, produce summaries, write session log."""
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] in ("-h", "--help"):
        print("Usage: python -m atlas_plus.hooks.run --env ATLAS_SESSION_AUTONOMOUS=on stop\n"
              "       (the Claude Code Stop hook; `sextant setup --client hooks` prints the full command)\n\n"
              "Reads the Stop payload (session_id, transcript_path) from stdin, summarises that session with the\n"
              "configured local model and writes its entry into the memory corpus day log and the state dir's\n"
              "updates file, then runs the facts stage. ATLAS_SUMMARIZER_DRY_RUN=on calls the model and writes\n"
              "nothing: the entry goes to <state>/logs/summarizer-dry-run.log and the facts stage is skipped.\n"
              "`atlas-summarize-session` is the transcript CLI, not this hook: it ignores stdin.")
        return 0
    project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    _SESSION["id"] = ""
    _PAYLOAD["seen"] = False  # per run: a previous payload in this process must not stand in (the sweep-14 test)
    _PAYLOAD["garbled"] = False
    _PAYLOAD["empty"] = False
    _PAYLOAD["transcript_file"] = ""
    _PAYLOAD["refused"] = ""

    if not is_session_autonomous():
        _raw = os.environ.get("ATLAS_SESSION_AUTONOMOUS", "").strip()
        if _raw and _raw.lower() not in ("off", "0", "false", "no"):  # an explicit opt-out is not a typo (MN39)
            # `--env ATLAS_SESSION_AUTONOMOUS=yes` was a silent no-op (AG43): only on/1/true opens the gate
            _sys.stderr.write(f"Sextant: ATLAS_SESSION_AUTONOMOUS={_raw!r} is not one of on/1/true; the Stop hook wrote nothing.\n")
            try:
                from atlas_plus import activity as _act
                _act.record("hook", "summarizer_skipped", reason=f"gate value {_raw!r} is not on/1/true")
            except Exception:  # noqa: BLE001
                pass
        return 0

    # The hook payload names the transcript that just ended; its projects root is where the
    # background jobs should look (ATLAS_CLAUDE_CODE_ROOT when the operator set it wins). A
    # scratch workspace must not mine the operator's real transcripts (power-user pass
    # 2026-11-10).
    _child_env = dict(os.environ)
    root = _transcript_root_from_payload()  # always read: it carries the session id (MN25/MN28: with the
    if _PAYLOAD.get("refused") and not os.environ.get("ATLAS_CLAUDE_CODE_ROOT"):
        _sys.stderr.write(f"Sextant: the Stop payload's transcript_path {_PAYLOAD['refused']!r} does not exist or does not sit under a .../projects folder; nothing summarised (set ATLAS_CLAUDE_CODE_ROOT to the transcripts root if the path form differs).\n")
        try:
            from atlas_plus import activity as _act
            _act.record("hook", "summarizer_skipped", reason="transcript path refused", path=_PAYLOAD["refused"])
        except Exception:  # noqa: BLE001
            pass
        return 0
    if not _SESSION.get("id") and _payload_seen():
        # a payload without a session id would summarise the whole day on every Stop, one more entry each
        # time (AG50); Claude Code always sends session_id, so a missing one is a broken payload
        _sys.stderr.write("Sextant: no Stop payload arrived on stdin; run the hook the way Claude Code does (echo '{\"session_id\": ..., \"transcript_path\": ...}' | <stop command>); nothing summarised.\n" if _PAYLOAD.get("empty") else "Sextant: the Stop payload is not JSON (a shell mangled the quotes?); nothing summarised.\n" if _PAYLOAD.get("garbled") else "Sextant: the Stop payload carries no session_id; nothing summarised.\n")
        try:
            from atlas_plus import activity as _act
            _act.record("hook", "summarizer_skipped", reason=("no payload on stdin" if _PAYLOAD.get("empty") else "payload is not JSON" if _PAYLOAD.get("garbled") else "payload has no session_id"))  # the recorded reason matches stderr (AG60)
        except Exception:  # noqa: BLE001
            pass
        return 0
    if root and not _child_env.get("ATLAS_CLAUDE_CODE_ROOT"):  # root set, every session of the day was summarised)
        _child_env["ATLAS_CLAUDE_CODE_ROOT"] = root
    if _PAYLOAD.get("transcript_file"):
        _child_env["ATLAS_TRANSCRIPT_FILE"] = _PAYLOAD["transcript_file"]  # the loader reads this file only (AG64)

    # --- 1. Summarizer ---
    # Say so when it cannot run: the docs promise a summary, and a silent exit 0 let a user
    # believe sessions were being summarized when no model was reachable (persona rerun
    # 2026-11-10). One stderr line and one activity event, then the snapshot still runs.
    reason = ""
    if os.environ.get("ATLAS_DISABLE_AUTOSUMMARIZE", "").lower() not in ("1", "true", "yes"):
        reason = _summarizer_unavailable_reason()
        if reason:
            print(f"[sextant stop hook] summarizer skipped: {reason}", file=sys.stderr)
            try:
                from atlas_plus import activity
                activity.record("hook", "summarizer_skipped", reason=reason)
            except Exception:  # noqa: BLE001
                pass
    _dry_log = None
    if _dry_run_flag() and not reason and os.environ.get("ATLAS_DISABLE_AUTOSUMMARIZE", "").lower() not in ("1", "true", "yes"):  # no child, no claim (AG61)
        try:
            from atlas_plus import activity as _act2
            _dry_path = atlas_paths.state_dir() / "logs" / "summarizer-dry-run.log"
            _dry_path.parent.mkdir(parents=True, exist_ok=True)
            _dry_log = open(_dry_path, "w" if (_dry_path.exists() and _dry_path.stat().st_size > 1_000_000) else "a", encoding="utf-8")  # starts over past 1 MB (AG61)  # the child's output has somewhere to go (MN51: it went to DEVNULL)
            _dry_log.write(f"\n--- {__import__('datetime').datetime.now().isoformat(timespec='seconds')} session {_SESSION.get('id') or '?'} ---\n")  # each run stamped (AG64)
            _dry_log.flush()
            _act2.record("hook", "summarizer_dry_run", log=str(_dry_path), session=_SESSION.get("id") or "")
            _sys.stderr.write(f"Sextant: ATLAS_SUMMARIZER_DRY_RUN is on: the model is called, nothing is written; the entry goes to {_dry_path}\n")
        except Exception:  # noqa: BLE001
            _dry_log = None
    if os.environ.get("ATLAS_DISABLE_AUTOSUMMARIZE", "").lower() not in ("1", "true", "yes") and not reason:
        subprocess.Popen(
            [
                sys.executable, "-m", "atlas_plus.summarizer.cli",
                "--source", "claude-code",
                "--since", "today",
                *(["--conversation-id", _SESSION["id"]] if _SESSION.get("id") else []),  # the finished session only (AG34)
                "--auto-commit",
                *_dry_run_flag(),  # ATLAS_SUMMARIZER_DRY_RUN=on: the help advertised it, nothing read it (AG60)
            ],
            cwd=project_dir,
            start_new_session=True,
            env=_child_env,
            stdout=_dry_log or subprocess.DEVNULL,
            stderr=_dry_log or subprocess.DEVNULL,
        )

    # --- 2. Suggest-facts (auto mode: only writes confidence >= 0.85) ---
    if _dry_run_flag():
        reason = reason or "dry run"  # a dry run writes nothing: the facts stage is skipped (MN51: it wrote a fact and a .done marker)
    if os.environ.get("ATLAS_DISABLE_SUGGEST_FACTS", "").lower() not in ("1", "true", "yes") and not reason:  # no model, no facts stage (MN31)
        exe = _find_script("atlas-suggest-facts")
        if not exe:
            _sys.stderr.write("Sextant: atlas-suggest-facts is not installed beside this interpreter, on PATH or in ~/.local/bin; the facts stage was skipped.\n")  # it was silent (AG50)
            try:
                from atlas_plus import activity as _act
                _act.record("hook", "suggest_facts_skipped", reason="atlas-suggest-facts not found")
            except Exception:  # noqa: BLE001
                pass
        if exe:
            subprocess.Popen(
                [str(exe), "--auto", "--since", "2h",  # no --skip-similar: the whole-note gate dropped corrections before the per-line rule ran (AG64)
                 *(["--conversation-id", _SESSION["id"]] if _SESSION.get("id") else [])],  # this session only (MN28)
                cwd=project_dir,
                start_new_session=True,
                env=_child_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    # --- 3. Git diff capture (ATL-R03) ---
    try:
        _capture_fsdiffs()
    except Exception:
        pass

    # --- 4. Transcript mining -- incremental update of tool-pattern facts ---
    if os.environ.get("ATLAS_DISABLE_TRANSCRIPT_MINING", "").lower() not in ("1", "true", "yes"):
        atlas_temporal = _find_script("atlas-temporal")
        if atlas_temporal:
            subprocess.Popen(
                [str(atlas_temporal), "build", "--source", "mining"],
                cwd=project_dir,
                start_new_session=True,
                env=_child_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
