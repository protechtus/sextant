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


def _find_script(name: str) -> Path | None:
    """Locate a console_script entry point relative to the current Python."""
    scripts_dir = Path(sys.executable).parent
    for candidate in [f"{name}.exe", name]:
        p = scripts_dir / candidate
        if p.exists():
            return p
    return None


_SESSION: dict[str, str] = {"id": ""}  # the Stop payload's session id, for the summarizer


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
            have = {n for n in names} | {n.split(":")[0] for n in names}
            if names and _want not in have and _want.split(":")[0] not in have:
                return (f"model {_want} is not pulled at {url} (`ollama pull {_want}`, or set ATLAS_SUMMARIZER_MODEL "
                        f"to one you have: {', '.join(names[:6])})")
            return ""
        local_only = os.environ.get("ATLAS_LOCAL_ONLY", "on").strip().lower() not in ("off", "0", "false")
        if local_only:
            return (f"no local model at {url} (start Ollama or set ATLAS_BRAIN_OLLAMA_URL; "
                    "cloud providers need ATLAS_LOCAL_ONLY=off and a key)")
        if any(os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")):
            return ""
        return f"no local model at {url} and no provider key (ANTHROPIC_API_KEY / OPENAI_API_KEY)"
    except Exception:  # noqa: BLE001
        return ""


def _transcript_root_from_payload() -> str:
    """``<root>`` from the Stop payload's ``transcript_path`` (``<root>/<project>/<session>.jsonl``), or ''."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return ""
        raw = sys.stdin.read()
        if not raw.strip():
            return ""
        import json as _json
        payload = _json.loads(raw)
        if isinstance(payload, dict) and isinstance(payload.get("session_id"), str):
            _SESSION["id"] = payload["session_id"].strip()[:120]
        tp = payload.get("transcript_path") if isinstance(payload, dict) else None
        if not tp:
            return ""
        from pathlib import Path as _Path
        root = _Path(str(tp)).resolve().parents[1]
        return str(root) if root.is_dir() else ""
    except Exception:  # noqa: BLE001 - a hook never fails the session over its payload
        return ""


def main() -> int:
    """Entry point for the transcript summarizer hook: read stdin transcript, produce summaries, write session log."""
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] in ("-h", "--help"):
        print("Usage: atlas-summarize-session   (Claude Code Stop hook entry point)\n\n"
              "Reads the hook payload from stdin, summarizes the finished session with the\n"
              "configured local model, and writes the session log into the memory corpus and\n"
              "the updates file in the state dir. Dry run: ATLAS_SUMMARIZER_DRY_RUN=on.")
        return 0
    project_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

    if not is_session_autonomous():
        return 0

    # The hook payload names the transcript that just ended; its projects root is where the
    # background jobs should look (ATLAS_CLAUDE_CODE_ROOT when the operator set it wins). A
    # scratch workspace must not mine the operator's real transcripts (power-user pass
    # 2026-11-10).
    _child_env = dict(os.environ)
    root = _transcript_root_from_payload()  # always read: it carries the session id (MN25/MN28: with the
    if root and not _child_env.get("ATLAS_CLAUDE_CODE_ROOT"):  # root set, every session of the day was summarised)
        _child_env["ATLAS_CLAUDE_CODE_ROOT"] = root

    # --- 1. Summarizer ---
    # Say so when it cannot run: the docs promise a summary, and a silent exit 0 let a user
    # believe sessions were being summarized when no model was reachable (persona rerun
    # 2026-11-10). One stderr line and one activity event, then the snapshot still runs.
    if os.environ.get("ATLAS_DISABLE_AUTOSUMMARIZE", "").lower() not in ("1", "true", "yes"):
        reason = _summarizer_unavailable_reason()
        if reason:
            print(f"[sextant stop hook] summarizer skipped: {reason}", file=sys.stderr)
            try:
                from atlas_plus import activity
                activity.record("hook", "summarizer_skipped", reason=reason)
            except Exception:  # noqa: BLE001
                pass
    if os.environ.get("ATLAS_DISABLE_AUTOSUMMARIZE", "").lower() not in ("1", "true", "yes") and not reason:
        subprocess.Popen(
            [
                sys.executable, "-m", "atlas_plus.summarizer.cli",
                "--source", "claude-code",
                "--since", "today",
                *(["--conversation-id", _SESSION["id"]] if _SESSION.get("id") else []),  # the finished session only (AG34)
                "--auto-commit",
            ],
            cwd=project_dir,
            start_new_session=True,
            env=_child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # --- 2. Suggest-facts (auto mode: only writes confidence >= 0.85) ---
    if os.environ.get("ATLAS_DISABLE_SUGGEST_FACTS", "").lower() not in ("1", "true", "yes"):
        exe = _find_script("atlas-suggest-facts")
        if exe:
            subprocess.Popen(
                [str(exe), "--auto", "--since", "2h", "--skip-similar", "0.8",
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
