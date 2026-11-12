"""atlas-doctor -- one-command system health dashboard (ATL-AUD-06).

Usage:
  atlas-doctor
  atlas-doctor --json
  atlas-doctor --watch
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from atlas_plus import paths as atlas_paths

BRAIN_URL    = os.environ.get("ATLAS_BRAIN_URL",   "http://127.0.0.1:8767")
MCP_PORT     = atlas_paths.env_int("ATLAS_MCP_PORT", 8765)
MCP_URL      = os.environ.get("ATLAS_MCP_URL",     f"http://127.0.0.1:{MCP_PORT}")
WEB_URL      = os.environ.get("ATLAS_WEB_URL",     f"http://127.0.0.1:{atlas_paths.env_int('ATLAS_WEB_PORT', 8766)}")
SYNC_URL     = os.environ.get("ATLAS_SYNC_URL",    f"http://127.0.0.1:{atlas_paths.env_int('ATLAS_SYNC_PORT', 8768)}")
OLLAMA_URL   = __import__('atlas_plus.llm.ollama_url', fromlist=['default_ollama_url']).default_ollama_url()
STATE_DIR    = atlas_paths.state_dir()
MEMORY_ROOT  = atlas_paths.memory_root()
PROJECTS_ROOT = atlas_paths.projects_root()
BRAIN_API_KEY = os.environ.get("ATLAS_BRAIN_API_KEY", "")
MCP_TOKEN    = os.environ.get("ATLAS_MCP_TOKEN", "")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _http_get(url: str, token: str = "", timeout: float = 3.0) -> tuple[int, dict]:
    try:
        # A plain TCP probe first: a refused loopback connect through httpx cost 2.3-3 s on
        # Windows and a proxy variable routed 127.0.0.1 through the proxy (AG11, sweep 10).
        import socket
        from urllib.parse import urlsplit
        u = urlsplit(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        try:
            with socket.create_connection((host, port), timeout=min(timeout, 0.75)):
                pass
        except OSError as exc:
            return 0, {"error": f"connect: {exc}"}
        import httpx
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        r = httpx.get(url, headers=headers, timeout=timeout, trust_env=False)
        try:
            body = r.json()
        except Exception:
            body = {}
        return r.status_code, body
    except Exception as exc:
        return 0, {"error": str(exc)}


def _powershell_json(script: str, timeout: float = 8.0):
    """Run a PowerShell snippet with NO console window and parse its JSON stdout.

    Returns the parsed object, or None on any failure (powershell missing, timeout,
    empty/non-JSON output). CREATE_NO_WINDOW keeps the probe from flashing a console /
    stealing focus -- see .memory/feedback/no-interrupting-refresh-scripts.md.
    """
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (completed.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _hub_has_devices() -> bool:
    try:
        from atlas_plus.sync import devices as _dev
        return any(not d.revoked for d in _dev.load_devices())
    except Exception:  # noqa: BLE001
        return False


def _tunnel_check_relevant() -> bool:
    """Only probe for a tunnel where one can exist: Windows with cloudflared installed,
    or when the operator opts in with ATLAS_TUNNEL_CHECK=on. Strangers never see a dead panel."""
    # Opt-in only: a stranger's doctor showed another install's cloudflared service as THEIR
    # tunnel (persona rerun 2026-11-10). The operator who runs one sets ATLAS_TUNNEL_CHECK=on.
    return os.environ.get("ATLAS_TUNNEL_CHECK", "").lower() in ("on", "1", "true")


def check_tunnel() -> dict:
    """Cloudflare tunnel status -- the only external ingress to the MCP origin (127.0.0.1:8765).

    When the tunnel is down, remote MCP access is closed (local stdio clients are unaffected).
    Probes the cloudflared Windows service + connector
    process + origin port binding in one hidden PowerShell call. Returns {running, service,
    service_state, proc_count, origin_listening, origin_loopback_only} or {error}; never raises.
    """
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$svc=Get-Service | Where-Object { $_.Name -match 'cloudflar' -or $_.DisplayName -match 'cloudflar' } | Select-Object -First 1;"
        "$proc=@(Get-Process cloudflared -ErrorAction SilentlyContinue);"
        f"$listen=@(Get-NetTCPConnection -State Listen -LocalPort {MCP_PORT} -ErrorAction SilentlyContinue);"
        "$loop=$true; foreach($l in $listen){ if($l.LocalAddress -ne '127.0.0.1' -and $l.LocalAddress -ne '::1'){ $loop=$false } };"
        "[pscustomobject]@{ service=if($svc){$svc.Name}else{$null}; service_state=if($svc){\"$($svc.Status)\"}else{$null};"
        " proc_count=$proc.Count; origin_listening=($listen.Count -gt 0); origin_loopback_only=$loop } | ConvertTo-Json -Compress"
    )
    out = _powershell_json(script, timeout=8)
    if not isinstance(out, dict):
        return {"error": "tunnel probe returned no data"}
    svc_state = out.get("service_state")
    proc_count = out.get("proc_count") or 0
    return {
        "running": (svc_state == "Running") or (proc_count > 0),
        "service": out.get("service"),
        "service_state": svc_state,
        "proc_count": proc_count,
        "origin_listening": bool(out.get("origin_listening")),
        "origin_loopback_only": bool(out.get("origin_loopback_only")),
    }


# Optional policy-gate probe: lives in the private extension package; absent -> no check.
try:
    from atlas_plus.gatekeeper.doctor_check import check_gatekeeper
except ImportError:  # pragma: no cover - public build
    check_gatekeeper = None  # type: ignore[assignment]

def check_services() -> list[dict]:
    """Ping Brain, MCP, and Ollama concurrently. Returns [{name, status, detail}] in fixed order."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    targets = [
        ("Brain",  f"{BRAIN_URL}/health",  ""),
        ("MCP",    f"{MCP_URL}/health",    MCP_TOKEN),
        ("Web",    f"{WEB_URL}/health",    ""),
        ("Sync",   f"{SYNC_URL}/health",   ""),
        ("Ollama", f"{OLLAMA_URL}/api/tags", ""),
    ]

    def _check(name_url_token):
        name, url, token = name_url_token
        code, body = _http_get(url, token)
        if code == 200:
            status = "up"
            detail = f"{len(body.get('models', []))} model(s) loaded" if name == "Ollama" else ""
            # The Brain answers 200 while alive but reports a dead model
            # backend in the body (2026-10-10); surface that instead of
            # calling a half-broken stack "up".
            if name == "Brain" and body.get("status") == "degraded":
                status = "degraded"
                detail = f"backend {body.get('backend', 'unreachable')}"
        elif code == 0:
            status = "down"
            err = str(body.get("error", "no connection"))
            if "10061" in err or "refused" in err.lower() or "connect" in err.lower():
                err = "nothing listening (connection refused); optional, start it when wanted"
            detail = err[:72]
        else:
            status = "degraded"
            detail = f"HTTP {code}"
        return {"name": name, "status": status, "detail": detail}

    # Every probe at once; worst-case latency = max(individual) not sum (three workers for five
    # targets made two waves, AG11 sweep 10).
    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        futures = {ex.submit(_check, t): t for t in targets}
        results_map = {}
        for fut in as_completed(futures):
            r = fut.result()
            results_map[r["name"]] = r

    # Return in original order
    return [results_map[n] for n, _, _ in targets]


def check_brain_diagnostic() -> dict:
    """Fetch Brain /diagnostic endpoint. Returns the full diagnostic dict or {_error} on failure."""
    code, body = _http_get(f"{BRAIN_URL}/diagnostic", BRAIN_API_KEY)
    if code != 200:
        return {"_error": f"HTTP {code}" if code else body.get("error", "unreachable")}
    return body


def check_routing() -> dict:
    """Return routing config from Brain's /routing/stats endpoint.

    Returns smart_routing, local_only, cache_size, and category_distribution.
    Empty dict if Brain is unreachable.
    """
    code, body = _http_get(f"{BRAIN_URL}/routing/stats", BRAIN_API_KEY)
    if code != 200:
        return {}
    return body


def check_autonomous() -> dict:
    """Return current ATLAS_AUTONOMOUS and ATLAS_SESSION_AUTONOMOUS env var values."""
    return {
        "ATLAS_AUTONOMOUS":         os.environ.get("ATLAS_AUTONOMOUS", "(not set)"),
        "ATLAS_SESSION_AUTONOMOUS": os.environ.get("ATLAS_SESSION_AUTONOMOUS", "(not set)"),
    }


def check_disk() -> dict:
    """Return drive usage stats (free_gb, total_gb, pct_used) for the PROJECTS_ROOT drive."""
    try:
        import shutil as _shutil
        total, used, free = _shutil.disk_usage(str(PROJECTS_ROOT.anchor))
        return {
            "drive":    str(PROJECTS_ROOT.anchor),
            "free_gb":  round(free / 1e9, 1),
            "total_gb": round(total / 1e9, 1),
            "pct_used": round(used / total * 100, 1),
        }
    except Exception:
        return {}


def check_temporal() -> dict:
    """Return temporal graph stats: entity count, active fact count, last build time, and staleness flag.

    Considered stale when last_built is older than 7 days or manifest is missing.
    """
    import json as _json
    from datetime import datetime, timezone, timedelta

    try:
        from atlas_plus.temporal_graph import file_provider as _fp
        from atlas_plus.temporal_graph.file_provider import FileTemporalProvider
        # A report never creates the graph directory: constructing the provider mkdirs it,
        # which left `.atlas/temporal_graph` behind a pre-init doctor run (sweep 2026-11-11).
        if not _fp._DEFAULT_ROOT.exists():
            return {"entities": 0, "facts_active": 0, "last_built": None, "stale": True}
        provider = FileTemporalProvider()
        s = provider.stats()
        result: dict = {
            "entities": s.get("entities", 0),
            "facts_active": s.get("facts_active", 0),
            "last_built": None,
            "stale": True,
        }

        # Check manifest for last build timestamp
        manifest_path = provider._manifest_file
        if manifest_path.exists():
            try:
                data = _json.loads(manifest_path.read_text(encoding="utf-8"))
                last_built = data.get("last_built")
                if last_built:
                    result["last_built"] = last_built[:10]
                    built_dt = datetime.fromisoformat(last_built.replace("Z", "+00:00"))
                    age_days = (datetime.now(timezone.utc) - built_dt).days
                    result["stale"] = age_days > 7
                    result["age_days"] = age_days
            except Exception:
                pass

        return result
    except Exception:
        return {}


def check_tickets() -> dict:
    """Return ticket counts and flag tickets with high reopen counts.

    Returns {total, by_status, by_assignee, high_reopen: list[{id, reopen_count, title}]}.
    Tickets reopened 3+ times are flagged for human review -- they indicate
    systematic failures in the quality gate or verifier for that ticket type.
    """
    try:
        from atlas_plus.tickets.store import list_tickets
        tickets = list_tickets()
        by_assignee: dict[str, int] = {}
        by_status: dict[str, int] = {}
        high_reopen = []
        for t in tickets:
            by_assignee[t.assignee] = by_assignee.get(t.assignee, 0) + 1
            by_status[t.status] = by_status.get(t.status, 0) + 1
            reopen_count = getattr(t, "reopen_count", 0)
            if reopen_count >= 3:
                high_reopen.append({
                    "id": t.id,
                    "reopen_count": reopen_count,
                    "title": t.title[:50],
                })
        high_reopen.sort(key=lambda x: x["reopen_count"], reverse=True)
        return {
            "total": len(tickets),
            "by_assignee": by_assignee,
            "by_status": by_status,
            "high_reopen": high_reopen[:5],
        }
    except Exception:
        return {}


def check_backup() -> dict:
    """Read the last line of backup.log. Returns {last_run, result} or {last_run:'never'} if absent."""
    log_file = STATE_DIR / "backup.log"
    if not log_file.exists():
        return {"last_run": "never", "result": "unknown"}
    try:
        lines = log_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        last = lines[-1] if lines else ""
        return {"last_run": last[:80], "result": "ok" if "success" in last.lower() else "unknown"}
    except Exception:
        return {"last_run": "error reading log", "result": "unknown"}


def check_db_backup() -> dict:
    """Return metadata about the most recent SQLite state.sqlite snapshot.

    Returns {last_ts, size_kb, age_hours, total_snapshots} or empty dict on error.
    A snapshot older than 26h (missed yesterday's 02:00 job by >2h) is flagged stale.
    """
    try:
        from atlas_plus.db_backup.core import last_backup_info, list_backups, _BACKUP_DIR
        info = last_backup_info()
        total = len(list_backups())
        if not info:
            return {"last_ts": "never", "total_snapshots": total, "stale": True}
        from datetime import timedelta
        last_modified_str = info["modified"]  # ISO 8601
        last_dt = datetime.fromisoformat(last_modified_str.replace("+00:00", "")).replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        return {
            "last_ts": info["ts_str"],
            "size_kb": info["size_kb"],
            "age_hours": round(age_hours, 1),
            "total_snapshots": total,
            "stale": age_hours > 26,
        }
    except Exception as exc:
        return {"last_ts": "error", "total_snapshots": 0, "stale": False, "error": str(exc)}


def check_nightly() -> dict:
    """Return last-run status for each nightly scheduler job from brain_tasks (session_id LIKE 'scheduler-%')."""
    db_path = STATE_DIR / "state.sqlite"
    if not db_path.exists():
        return {}
    try:
        import sqlite3
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT session_id, status, finished_at FROM brain_tasks "
                "WHERE session_id LIKE 'scheduler-%' ORDER BY rowid DESC LIMIT 9"
            ).fetchall()
        jobs: dict[str, dict] = {}
        for session_id, status, finished_at in rows:
            job = session_id.replace("scheduler-", "")
            if job not in jobs:
                jobs[job] = {"status": status, "last": (finished_at or "")[:19]}
        return jobs
    except Exception:
        return {}


def check_scheduler_freshness() -> dict:
    """P-1: warn when the nightly job family has not run in >26h.

    An outage during the nightly window silently swallows the whole hygiene
    run (observed 2026-08-05); the scheduler now catches up on start, and
    this check surfaces the gap if it ever persists anyway.
    """
    daily_ids = (
        "db-backup", "temporal-build", "suggest-facts", "curation",
        "research-batch", "patterns-rebuild", "dream", "memory-trends",
        "research-quality", "suggest-tags",
    )
    db_path = STATE_DIR / "state.sqlite"
    if not db_path.exists():
        return {}
    try:
        import sqlite3
        from datetime import datetime, timezone

        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT job_id, last_run_at FROM scheduler_runs"
            ).fetchall()
        stamps = [r[1] for r in rows if r[0] in daily_ids and r[1]]
        if not stamps:
            return {}
        newest = max(stamps)
        dt = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
        return {
            "newest": str(newest)[:19],
            "age_hours": round(age_hours, 1),
            "stale": age_hours > 26,
        }
    except Exception:
        return {}


def check_fsdiff_count() -> int:
    """Count *.fsdiff.json files in the fsdiffs state directory (git-diff capture artifacts)."""
    fsdiff_dir = STATE_DIR / "fsdiffs"
    if not fsdiff_dir.exists():
        return 0
    return sum(1 for _ in fsdiff_dir.rglob("*.fsdiff.json"))


def check_mcp_stats() -> dict:
    """Return MCP search quality metrics from the search log."""
    try:
        from atlas_plus.mcp.memory_server.tools.search_log import load_log
        records = load_log(since_hours=168.0)
        if not records:
            return {}
        total = len(records)
        misses = sum(1 for r in records if r.get("miss"))
        miss_rate = round(misses / total * 100, 1) if total else 0.0
        tool_counts: dict[str, int] = {}
        for r in records:
            tool_counts["memory_search"] = tool_counts.get("memory_search", 0) + 1
        return {
            "total_searches_7d": total,
            "miss_count_7d": misses,
            "miss_rate_pct": miss_rate,
        }
    except Exception:
        return {}


def check_worker_status() -> dict:
    """Check whether the Brain task worker appears to be alive.

    Returns pending/running counts, last claimed/finished timestamps, stale
    running task count, and a human-readable assessment:
      ok      -- a task finished or was claimed within the last 30 min
      idle    -- no recent activity AND no pending tasks (worker sleeping normally)
      warning -- pending tasks exist but nothing claimed in over 30 min
      stale   -- running tasks exist with heartbeat_at older than 30 min
      unknown -- DB not accessible
    """
    db_path = STATE_DIR / "state.sqlite"
    if not db_path.exists():
        return {"assessment": "unknown",
                "reason": "no task database yet (state.sqlite appears with the first Brain task; not an error)"}
    try:
        import sqlite3
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        cutoff_iso = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            r = conn.execute(
                "SELECT "
                "  COUNT(*) FILTER (WHERE status='pending') AS pending,"
                "  COUNT(*) FILTER (WHERE status='running') AS running,"
                "  MAX(CASE WHEN status IN ('done','failed','blocked') THEN finished_at END) AS last_finished,"
                "  MAX(CASE WHEN status='running' THEN started_at END) AS last_claimed,"
                "  COUNT(*) FILTER (WHERE status='running' AND heartbeat_at IS NOT NULL AND heartbeat_at < ?) AS stale_running"
                " FROM brain_tasks",
                (cutoff_iso,),
            ).fetchone()
        finally:
            conn.close()

        pending = r["pending"] or 0
        running = r["running"] or 0
        stale = r["stale_running"] or 0
        last_finished = r["last_finished"] or ""
        last_claimed = r["last_claimed"] or ""

        # Determine assessment
        if stale > 0:
            assessment = "stale"
        elif last_finished > cutoff_iso or last_claimed > cutoff_iso:
            assessment = "ok"
        elif pending > 0:
            assessment = "warning"
        else:
            assessment = "idle"

        return {
            "pending": pending,
            "running": running,
            "stale_running": stale,
            "last_finished_at": last_finished[:19] if last_finished else "never",
            "last_claimed_at": last_claimed[:19] if last_claimed else "never",
            "assessment": assessment,
        }
    except Exception as exc:
        return {"assessment": "unknown", "reason": str(exc)}


def check_worker_detail() -> dict:
    """Return per-worker granular stats: active worker IDs, task lists, queue depth, zombies.

    Supplements check_worker_status() with individual worker instance visibility.
    Returns {active_workers, pending_by_agent, zombie_count, queue_depth}.
    """
    if not (STATE_DIR / "state.sqlite").exists():
        # A health check must not create the task database (persona rerun 2026-11-10).
        return {"active_workers": [], "pending_by_agent": {}, "zombie_count": 0, "queue_depth": 0}
    try:
        from atlas_plus.brain.tasks import worker_detail
        return worker_detail()
    except Exception as exc:
        return {"active_workers": [], "pending_by_agent": {}, "zombie_count": 0, "queue_depth": 0, "error": str(exc)}


# Heavy non-memory subtrees that every memory check skips anyway. Excluding them from the
# shared scan is a pure performance optimisation -- it never changes any consumer's result,
# because each consumer already filters these out of its own walk.
_SCAN_SKIP_DIRS = {"archive", "_sync", "versions", "_atlas"}


@dataclass(frozen=True)
class MemoryFile:
    """One markdown file from a single shared .memory/ scan pass.

    ``text`` is None when the file could not be read (preserves each consumer's
    skip-on-read-error behaviour). Frontmatter is intentionally NOT parsed here -- each
    consumer parses ``text`` with its own exact gate, so the dedup saves the file READ
    (the expensive part) with zero behavioural drift.
    """
    rel: str             # posix-style path relative to the scan root
    parts: tuple         # relative path parts
    name: str            # file name
    text: Optional[str]  # full file text, or None if unreadable


def scan_memory_files(root: Optional[Path] = None) -> list["MemoryFile"]:
    """Walk .memory/ ONCE, reading each markdown file a single time.

    Heavy subtrees (archive/_sync/versions/_atlas) are excluded -- every memory check skips
    them anyway. The result feeds check_convergence + check_memory_quality so collect() reads
    each file once instead of once per check. Never raises; unreadable files get text=None.
    """
    from atlas_plus.curation.scan import is_excluded_mirror

    base = root if root is not None else MEMORY_ROOT
    out: list[MemoryFile] = []
    if not base.exists():
        return out
    for md in base.rglob("*.md"):
        try:
            rel_path = md.relative_to(base)
        except ValueError:
            continue
        if any(p in _SCAN_SKIP_DIRS for p in rel_path.parts) or is_excluded_mirror(rel_path.parts):
            continue
        try:
            text: Optional[str] = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = None
        out.append(MemoryFile(
            rel=str(rel_path).replace("\\", "/"),
            parts=rel_path.parts,
            name=md.name,
            text=text,
        ))
    return out


def check_memory_corpus(scan: Optional[list["MemoryFile"]] = None) -> dict:
    """First-run visibility: does the memory corpus exist, and is there anything in it?

    A fresh install has no corpus at all; every other doctor section assumes one.
    This is the section that tells a new user the fix is `atlas-init` (alias
    `sextant-init`) rather than leaving them staring at empty panels.
    """
    root = MEMORY_ROOT
    exists = root.exists()
    files = scan if scan is not None else (scan_memory_files(root) if exists else [])
    identity_present = any(f.parts and f.parts[0] == "identity" for f in files)
    result: dict = {
        "root": str(root),
        "exists": exists,
        "md_files": len(files),
        "identity_present": identity_present,
    }
    try:
        from atlas_plus.mcp.memory_server import fts as _fts
        from atlas_plus.mcp.memory_server.embeddings import index_status

        if not _fts.FTS_DB.exists():
            raise FileNotFoundError(_fts.FTS_DB)  # a report never creates the search index
        status = index_status()
        if status.get("indexed"):
            result["embed_index"] = status
    except Exception:
        pass
    if STATE_DIR.exists() and not STATE_DIR.is_dir():
        result["state_dir_is_file"] = True
        result["hint"] = (f"the state directory {STATE_DIR} is a file, not a directory: search and the "
                          "index cannot work until it is moved aside")
        return result
    try:
        from atlas_plus.mcp.memory_server.fts import fts5_available
        if not fts5_available():
            result["fts5"] = False
            result["hint"] = ("this Python's SQLite has no FTS5 module, so keyword search cannot work: use a "
                              "python.org, uv or distro Python whose sqlite3 includes FTS5")
            return result
    except Exception:  # noqa: BLE001
        pass
    if not exists:
        result["hint"] = "memory root does not exist -- run atlas-init (alias: sextant-init) to scaffold it"
    elif not files:
        result["hint"] = "memory root is empty -- run atlas-init (alias: sextant-init), then fill identity/user.md"
    elif not identity_present:
        result["hint"] = "no identity/ files yet -- sessions prime from identity/user.md; run atlas-init or create it"
    return result


def check_sync() -> dict:
    """Sextant Sync visibility: paired principals, last check-ins, pending work.

    Empty dict when sync is unused (no devices, no conflicts, no inbox) so
    the render stays quiet for non-sync installs.
    """
    try:
        from atlas_plus.sync import devices as sync_devices
        from atlas_plus.sync import store as sync_store

        rows = sync_devices.load_devices()
        checkins = sync_store.load_checkins()
        principals = [
            {
                "name": d.name,
                "kind": d.kind,
                "scope": d.scope,
                "revoked": d.revoked,
                "last_checkin": checkins.get(d.device_id, {}).get("received_utc"),
            }
            for d in rows
        ]

        def _count_files(base: Path) -> int:
            if not base.exists():
                return 0
            return sum(
                1
                for p in base.rglob("*")
                if p.is_file() and not p.name.endswith(".meta.json")
            )

        conflicts = _count_files(MEMORY_ROOT / "_atlas" / "sync-conflicts")
        inbox = _count_files(MEMORY_ROOT / "_atlas" / "sync-inbox")
        if not principals and not conflicts and not inbox:
            return {}
        return {"principals": principals, "conflicts": conflicts, "inbox": inbox}
    except Exception:
        return {}


def check_convergence(scan: Optional[list["MemoryFile"]] = None) -> dict:
    """Return count of memory files with unresolved contradiction flags.

    A contradiction flag (contradictions: [...] in frontmatter) means the
    nightly contradiction check found a divergence that has not been resolved.
    Returns {flagged: int, paths: list[str]}.
    """
    try:
        import yaml as _yaml
        records = scan if scan is not None else scan_memory_files(MEMORY_ROOT)
        flagged = []
        for rec in records:
            if any(p in {"archive", "_sync", "versions", "_atlas"} for p in rec.parts):
                continue
            if rec.name.startswith("_"):
                continue
            text = rec.text
            if text is None:
                continue
            if "contradictions:" not in text:
                continue
            if not text.startswith("---"):
                continue
            end = text.find("\n---", 3)
            if end == -1:
                continue
            try:
                fm = _yaml.safe_load(text[3:end]) or {}
            except Exception:
                continue
            contras = fm.get("contradictions", [])
            if contras and isinstance(contras, list) and len(contras) > 0:
                flagged.append(rec.rel)
        return {"flagged": len(flagged), "paths": flagged[:5]}
    except Exception as exc:
        return {"flagged": 0, "error": str(exc)}


def check_decisions() -> dict:
    """Return count of decision records and how many are stale (>90d without review)."""
    decisions_dir = MEMORY_ROOT / "decisions"
    if not decisions_dir.exists():
        return {"total": 0, "stale": 0}
    try:
        import yaml as _yaml
        from datetime import date
        today = date.today()
        total = 0
        stale = 0
        stale_titles: list[str] = []
        for path in sorted(decisions_dir.glob("*.md")):
            if path.name.startswith("_"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not text.startswith("---"):
                continue
            end = text.find("\n---", 3)
            if end == -1:
                continue
            try:
                fm = _yaml.safe_load(text[3:end]) or {}
            except Exception:
                continue
            total += 1
            title = str(fm.get("title", path.stem))[:40]
            made_str = str(fm.get("made", ""))[:10]
            lr_str = str(fm.get("last_reviewed", ""))[:10]
            age_days = None
            if made_str:
                try:
                    age_days = (today - date.fromisoformat(made_str)).days
                except ValueError:
                    pass
            lr_days = None
            if lr_str:
                try:
                    lr_days = (today - date.fromisoformat(lr_str)).days
                except ValueError:
                    pass
            is_stale = (lr_days is None and age_days is not None and age_days > 90) or \
                       (lr_days is not None and lr_days > 90)
            if is_stale:
                stale += 1
                stale_titles.append(title)
        return {"total": total, "stale": stale, "stale_titles": stale_titles[:3]}
    except Exception as exc:
        return {"total": 0, "stale": 0, "error": str(exc)}


def check_memory_gaps() -> dict:
    """Return count of unique miss queries and top 3 worst gaps in the last 7 days."""
    try:
        from atlas_plus.mcp.memory_server.tools.search_log import top_miss_queries
        gaps = top_miss_queries(since_hours=168.0, limit=20)
        if not gaps:
            return {"unique_miss_queries": 0, "top": []}
        top3 = [
            {"query": g["query"][:40], "miss_rate_pct": round(g["miss_count"] / g["total"] * 100)}
            for g in gaps[:3]
        ]
        return {"unique_miss_queries": len(gaps), "top": top3}
    except Exception:
        return {}


def check_pending_facts() -> dict:
    """Return count of fact candidates awaiting review.

    Returns {count: int, oldest: str|None} where oldest is the created_at
    of the oldest pending fact (ISO 8601).
    """
    if not (STATE_DIR / "state.sqlite").exists():
        return {"count": 0, "oldest": None}  # a report never creates the task database
    try:
        from atlas_plus.brain.task_facts import pending_count, list_pending_facts
        n = pending_count()
        if n == 0:
            return {"count": 0, "oldest": None}
        # Get oldest pending fact for staleness indicator
        facts = list_pending_facts(limit=100)
        if facts:
            oldest = min(f.created_at for f in facts if f.created_at)
        else:
            oldest = None
        return {"count": n, "oldest": oldest}
    except Exception as exc:
        return {"count": 0, "error": str(exc)}


def check_sla_breaches() -> dict:
    """Return count of tasks currently running past ATLAS_SLA_MINUTES threshold.

    Returns {count: int, task_ids: list[str]}.
    """
    import os
    from datetime import datetime, timedelta, timezone

    sla_minutes = int(os.environ.get("ATLAS_SLA_MINUTES", "15"))
    if sla_minutes <= 0:
        return {"count": 0, "task_ids": []}

    if not (STATE_DIR / "state.sqlite").exists():
        return {"count": 0, "task_ids": []}  # a report never creates the task database
    try:
        from atlas_plus.brain import tasks as task_store
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=sla_minutes)
        cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
        breached = [
            t for t in task_store.list_tasks(status="running", limit=50)
            if t.started_at and t.started_at[:19] <= cutoff_iso
        ]
        return {"count": len(breached), "task_ids": [t.task_id[:20] for t in breached[:5]]}
    except Exception as exc:
        return {"count": 0, "error": str(exc)}


def check_memory_quality(scan: Optional[list["MemoryFile"]] = None) -> dict:
    """Scan .memory/ for quality signals in one pass.

    Returns:
        {
            total_files: int,
            confirmed_pct: float,     # % with last_confirmed frontmatter
            tagged_pct: float,        # % with tags: frontmatter
            confidence_dist: {high, medium, low, none},  # file counts
            avg_age_days: float,      # average days since last updated
            stale_confirmed: int,     # files with last_confirmed > 90d
            anti_flagged: int,        # files with anti_flagged: true
        }
    """
    import yaml as _yaml
    from datetime import date as _date
    skip_dirs = {"archive", "_sync", "versions", "_atlas", "anti"}
    today = _date.today()

    total = 0
    confirmed = 0
    tagged = 0
    stale_confirmed = 0
    anti_flagged = 0
    confidence_dist = {"high": 0, "medium": 0, "low": 0, "none": 0}
    age_sum = 0.0
    age_count = 0

    records = scan if scan is not None else scan_memory_files(MEMORY_ROOT)
    for rec in records:
        rel_parts = rec.parts
        if any(p in skip_dirs for p in rel_parts):
            continue
        if rec.name.startswith("_"):
            continue
        total += 1
        raw = rec.text
        if raw is None:
            continue
        if not raw.startswith("---"):
            continue
        end = raw.find("\n---", 3)
        if end == -1:
            continue
        try:
            fm = _yaml.safe_load(raw[3:end]) or {}
        except Exception:
            continue

        # Confirmation
        lc = fm.get("last_confirmed")
        if lc:
            confirmed += 1
            try:
                lc_date = _date.fromisoformat(str(lc)[:10])
                if (today - lc_date).days > 90:
                    stale_confirmed += 1
            except Exception:
                pass

        # Tags
        tags = fm.get("tags") or fm.get("tag")
        if tags:
            tagged += 1

        # Confidence
        conf = str(fm.get("confidence", "")).lower()
        if conf in confidence_dist:
            confidence_dist[conf] += 1
        else:
            confidence_dist["none"] += 1

        # Age (from updated field)
        updated = fm.get("updated")
        if updated:
            try:
                upd_date = _date.fromisoformat(str(updated)[:10])
                age_sum += (today - upd_date).days
                age_count += 1
            except Exception:
                pass

        # Anti-flagged
        if fm.get("anti_flagged"):
            anti_flagged += 1

    untagged = total - tagged
    return {
        "total_files": total,
        "confirmed_pct": round(confirmed / total * 100, 1) if total else 0.0,
        "tagged_pct": round(tagged / total * 100, 1) if total else 0.0,
        "confidence_dist": confidence_dist,
        "avg_age_days": round(age_sum / age_count, 1) if age_count else 0.0,
        "stale_confirmed": stale_confirmed,
        "anti_flagged": anti_flagged,
        "untagged_files": untagged,
    }


def check_untagged_by_scope(scan: Optional[list["MemoryFile"]] = None) -> dict[str, int]:
    """Return {scope_name: untagged_file_count} by scanning .memory/ subdirectories.

    A file is considered untagged when its YAML frontmatter lacks a non-empty 'tags' field,
    or when it has no frontmatter at all. Only immediate child directories of .memory/ are
    counted as scopes (e.g. 'feedback', 'projects', 'reference'). Hidden dirs are skipped.
    Returns an empty dict when the memory directory is missing or unreadable.
    """
    import os as _os
    import yaml as _yaml
    from pathlib import Path as _Path

    # Reuse the shared scan when collect() provides it (this was the one
    # check in the fleet still doing its own full corpus read); standalone
    # callers get a fresh scan at the env-resolved root.
    if scan is None:
        mem_dir = _Path(_os.environ.get("ATLAS_MEMORY_ROOT", str(MEMORY_ROOT)))
        if not mem_dir.exists():
            return {}
        scan = scan_memory_files(mem_dir)

    scope_counts: dict[str, int] = {}
    for f in scan:
        if len(f.parts) < 2:
            continue  # top-level files are not scoped
        scope = f.parts[0]
        if scope.startswith(".") or scope == "anti" or f.text is None:
            continue
        raw = f.text
        try:
            if not raw.startswith("---"):
                scope_counts[scope] = scope_counts.get(scope, 0) + 1
                continue
            end = raw.find("\n---", 3)
            fm = _yaml.safe_load(raw[3:end]) if end != -1 else {}
            tags = (fm or {}).get("tags")
            if not tags:
                scope_counts[scope] = scope_counts.get(scope, 0) + 1
        except Exception:
            continue
    return scope_counts


TRENDS_FILE = STATE_DIR / "memory-trends.jsonl"
_TRENDS_MAX_LINES = 30  # rolling 30-day window


def record_trends_snapshot() -> dict:
    """Append a memory quality snapshot to .atlas/memory-trends.jsonl.

    Captures {ts, total_files, confirmed_pct, tagged_pct} in a single rglob pass.
    Prunes file to at most _TRENDS_MAX_LINES lines after appending.
    Called daily by the Brain scheduler at 04:05 UTC.
    Returns the snapshot dict written.
    """
    import yaml as _yaml
    from atlas_plus.curation.scan import is_excluded_mirror
    skip_dirs = {"archive", "_sync", "versions", "_atlas", "anti"}
    today = datetime.now(timezone.utc)

    total = 0
    confirmed = 0
    tagged = 0

    if MEMORY_ROOT.exists():
        for md_file in MEMORY_ROOT.rglob("*.md"):
            rel_parts = md_file.relative_to(MEMORY_ROOT).parts
            if any(p in skip_dirs for p in rel_parts) or is_excluded_mirror(rel_parts):
                continue
            if md_file.name.startswith("_"):
                continue
            total += 1
            try:
                raw = md_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not raw.startswith("---"):
                continue
            end = raw.find("\n---", 3)
            if end == -1:
                continue
            try:
                fm = _yaml.safe_load(raw[3:end]) or {}
            except Exception:
                continue
            if fm.get("last_confirmed"):
                confirmed += 1
            if fm.get("tags") or fm.get("tag"):
                tagged += 1

    snapshot = {
        "ts": today.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_files": total,
        "confirmed_pct": round(confirmed / total * 100, 1) if total else 0.0,
        "tagged_pct": round(tagged / total * 100, 1) if total else 0.0,
    }

    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        existing = []
        if TRENDS_FILE.exists():
            try:
                existing = TRENDS_FILE.read_text(encoding="utf-8").splitlines()
            except OSError:
                pass
        existing.append(json.dumps(snapshot))
        # Keep only the last _TRENDS_MAX_LINES entries
        if len(existing) > _TRENDS_MAX_LINES:
            existing = existing[-_TRENDS_MAX_LINES:]
        TRENDS_FILE.write_text("\n".join(existing) + "\n", encoding="utf-8")
    except OSError:
        pass

    return snapshot


def read_trends(days: int = 7) -> list[dict]:
    """Read the last N days of snapshots from memory-trends.jsonl.

    Returns list of snapshot dicts sorted oldest-first, or [] on error.
    """
    if not TRENDS_FILE.exists():
        return []
    try:
        lines = TRENDS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    results = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except Exception:
            continue
    return results[-days:]


def _sparkline(values: list[float]) -> str:
    """Return a compact Unicode sparkline for a list of numeric values."""
    blocks = " ▁▂▃▄▅▆▇█"
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo
    out = []
    for v in values:
        if span == 0:
            idx = 4  # midpoint block
        else:
            idx = round((v - lo) / span * (len(blocks) - 2)) + 1
            idx = max(1, min(len(blocks) - 1, idx))
        out.append(blocks[idx])
    return "".join(out)


def check_memory_health_overview() -> dict:
    """Quick health overview of .memory/ files -- distribution of health scores.

    Runs compute_file_health() on all .memory/ files (single rglob pass).
    Returns:
        {
            total_files: int,
            score_0:   int,   # completely unadorned (score = 0.0)
            score_25:  int,   # score = 0.25
            score_50:  int,   # score = 0.50
            score_75:  int,   # score = 0.75
            score_100: int,   # fully healthy (score = 1.0)
            avg_score: float, # mean health score
        }
    """
    try:
        from atlas_plus.curation.health import scan_memory_health
        results = scan_memory_health(MEMORY_ROOT, limit=9999)  # all files
        if not results:
            return {"total_files": 0, "score_0": 0, "score_25": 0, "score_50": 0,
                    "score_75": 0, "score_100": 0, "avg_score": 0.0}
        counts = {0.0: 0, 0.25: 0, 0.50: 0, 0.75: 0, 1.0: 0}
        for r in results:
            s = round(r["score"], 2)
            if s in counts:
                counts[s] += 1
            # Anything > 0.75 and < 1.0 rounds to 0.75 (partial health)
            # Anything 1.0 = perfect
        avg = round(sum(r["score"] for r in results) / len(results), 3)
        return {
            "total_files": len(results),
            "score_0":   counts[0.0],
            "score_25":  counts[0.25],
            "score_50":  counts[0.50],
            "score_75":  counts[0.75],
            "score_100": counts[1.0],
            "avg_score": avg,
        }
    except Exception as exc:
        return {"total_files": 0, "avg_score": 0.0, "error": str(exc)}


def check_research_quality() -> dict:
    """Scan .atlas/reports/ for research reports with quality_score: frontmatter.

    Returns:
        {
            total_reports: int,      # all .md files in reports/
            scored_reports: int,     # files with quality_score: frontmatter
            avg_score: float,        # mean quality_score across scored files
            low_quality_count: int,  # scored files with score < 0.5
            min_score_file: str,     # filename of lowest-scoring report, or ""
        }
    """
    import yaml as _yaml
    reports_dir = STATE_DIR / "reports"
    if not reports_dir.exists():
        return {}

    total = 0
    scored = []
    min_file = ""

    for md_file in reports_dir.rglob("*.md"):
        if md_file.name.startswith("_"):
            continue
        total += 1
        try:
            raw = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not raw.startswith("---"):
            continue
        end = raw.find("\n---", 3)
        if end == -1:
            continue
        try:
            fm = _yaml.safe_load(raw[3:end]) or {}
        except Exception:
            continue
        qs = fm.get("quality_score")
        if qs is None:
            continue
        try:
            score = float(qs)
            scored.append((score, md_file.name))
        except (TypeError, ValueError):
            continue

    if not scored:
        return {"total_reports": total, "scored_reports": 0, "avg_score": 0.0,
                "low_quality_count": 0, "min_score_file": ""}

    avg = round(sum(s for s, _ in scored) / len(scored), 3)
    low = sum(1 for s, _ in scored if s < 0.5)
    scored.sort(key=lambda x: x[0])
    min_file = scored[0][1] if scored else ""

    return {
        "total_reports": total,
        "scored_reports": len(scored),
        "avg_score": avg,
        "low_quality_count": low,
        "min_score_file": min_file,
    }


def check_dispatcher() -> dict:
    """Return ticket dispatcher health: enabled state, last run, queue depth.

    Returns:
        {
            enabled: bool,          # ATLAS_TICKET_DISPATCH is active
            last_run_at: str|None,  # ISO-8601 UTC, or None if never run
            last_dispatched: int,   # tickets dispatched on last run
            total_dispatched: int,  # cumulative since process start
            in_flight: int,         # tickets currently in-progress
        }
    """
    try:
        from atlas_plus.brain.dispatcher import get_dispatch_stats
        return get_dispatch_stats()
    except Exception as exc:
        return {"enabled": False, "error": str(exc)}


def check_ref_graph() -> dict:
    """Return the top hub files in .memory/ (most-referenced by other memory files).

    Returns {hub_count: int, top: list[{stem, inbound_count}], total_linked_files: int}.
    Quick proxy for memory interconnectedness -- well-referenced hub files are
    high-value and should be kept current.
    """
    try:
        from atlas_plus.curation.ref_graph import build_reference_graph, find_hub_files
        graph = build_reference_graph(MEMORY_ROOT)
        hubs = find_hub_files(graph, top_n=5)
        return {
            "total_linked_files": len(graph),
            "hub_count": len(hubs),
            "top": hubs,
        }
    except Exception as exc:
        return {"total_linked_files": 0, "hub_count": 0, "top": [], "error": str(exc)}


def check_orphans() -> dict:
    """Return count of memory orphan candidates (no inbound refs, low health, old).

    Uses conservative thresholds: min_age_days=180, max_health=0.5, max_refs=0.
    Returns {candidates: int, top: list[{path, age_days, score, inbound_refs}]}.
    """
    try:
        from atlas_plus.curation.orphan import find_orphan_candidates
        results = find_orphan_candidates(
            MEMORY_ROOT, min_age_days=180, max_health=0.5, max_inbound_refs=0, limit=50
        )
        top3 = [
            {
                "path": r["path"][:45],
                "age_days": r["age_days"],
                "score": round(r["score"], 2),
                "inbound_refs": r["inbound_refs"],
            }
            for r in results[:3]
        ]
        return {"candidates": len(results), "top": top3}
    except Exception as exc:
        return {"candidates": 0, "error": str(exc)}


def check_rederivable() -> dict:
    """Return count of memory files that overlap heavily with project doc files.

    Uses the same 0.40 Jaccard threshold as the weekly curation report.
    Returns {candidates: int, top: list[{path, similarity, matched_doc}]}.
    """
    try:
        from atlas_plus.curation.rederive import find_rederivable
        ws_root = MEMORY_ROOT.parent
        candidates = find_rederivable(threshold=0.40, workspace_root=ws_root)
        top3 = [
            {"path": c.path[:45], "similarity": round(c.similarity, 2), "matched_doc": c.matched_doc[:40]}
            for c in candidates[:3]
        ]
        return {"candidates": len(candidates), "top": top3}
    except Exception as exc:
        return {"candidates": 0, "error": str(exc)}


def check_exhausted_tasks() -> dict:
    """Return tasks that burned through all retry attempts without success.

    These are tasks where status in ('failed','blocked') AND attempts >= max_attempts.
    The queue will not re-claim them -- they need /retry or human triage.
    Returns {count: int, tasks: [{task_id, agent, instruction_preview, finished_at}]}.
    """
    if not (STATE_DIR / "state.sqlite").exists():
        # Opening the task store creates state.sqlite; a report must not (sweep 2026-11-11).
        return {"count": 0, "tasks": []}
    try:
        from atlas_plus.brain import tasks as task_store
        exhausted = task_store.list_exhausted_tasks(limit=10)
        task_list = [
            {
                "task_id": t.task_id[:20],
                "agent": t.agent or "(none)",
                "instruction": (t.instruction or "")[:60],
                "status": t.status,
                "finished_at": (t.finished_at or "")[:19],
            }
            for t in exhausted
        ]
        return {"count": len(exhausted), "tasks": task_list}
    except Exception as exc:
        return {"count": 0, "error": str(exc)}


def check_research_gaps() -> dict:
    """Return unresolved research gap tickets awaiting investigation.

    Scans the ticket store for todo tickets whose title starts with 'Research gap:'.
    Returns {count: int, top: list[{id, title, priority}]} with up to 5 entries
    ordered by priority (critical first). A non-zero count indicates topics where
    the memory gaps report has filed tickets but no research has run yet.
    """
    try:
        from atlas_plus.tickets.store import list_tickets
        tickets = list_tickets(status="todo")
        gaps = [
            {"id": t.id, "title": t.title[:60], "priority": t.priority}
            for t in tickets
            if t.title.strip().startswith("Research gap:")
        ]
        return {"count": len(gaps), "top": gaps[:5]}
    except Exception as exc:
        return {"count": 0, "error": str(exc)}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt_status(s: str) -> str:
    if s == "up":    return "\033[32mup\033[0m"
    if s == "down":  return "\033[31mdown\033[0m"
    return "\033[33m" + s + "\033[0m"


def _fmt_ok(v: bool) -> str:
    return "\033[32mon\033[0m" if v else "\033[31moff\033[0m"


_ANSI_RE = None


def _plain(text: str) -> str:
    """The dashboard without colour codes."""
    global _ANSI_RE
    if _ANSI_RE is None:
        import re
        _ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
    return _ANSI_RE.sub("", text)


def _plain_if_piped(text: str) -> str:
    """Colour only on a terminal; a captured report or NO_COLOR gets plain text (AG1 and AG2,
    pipeline sweep 2026-11-11: agents saw raw escapes)."""
    try:
        tty = sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        tty = False
    if os.environ.get("NO_COLOR") or not tty:
        return _plain(text)
    return text


def render_text(data: dict) -> str:
    """Format a collect() snapshot as a colour-coded ANSI text dashboard string."""
    # Brand-aware header: invoked as sextant-doctor (public alias), speak
    # Sextant (Codex audit: first-hour naming whiplash).
    import sys as _sys
    from pathlib import Path as _Path

    _invoked = _Path(_sys.argv[0]).stem.lower() if _sys.argv and _sys.argv[0] else ""
    # `python -m atlas_plus.doctor.cli` (the agent runbook's form) must not switch identity
    # on a public install: the distribution name decides when argv does not (sweep 2026-11-11).
    _public = False
    if not _invoked.startswith("sextant"):   # argv decides first; the metadata lookup costs 0.4 s (AG11)
        try:
            from atlas_plus.cli import _dist_name as _dn
            _public = _dn() == "sextant-ai"
        except Exception:  # noqa: BLE001
            _public = False
    _title = "Sextant Doctor" if (_invoked.startswith("sextant") or _public) else "Atlas Doctor"
    lines = ["", f"\033[1;36m=== {_title} ===\033[0m", ""]
    lines.append("\033[2m(service rows are local port probes on 127.0.0.1 -- another"
                 " install's services on this machine will also show here)\033[0m")

    # Services
    lines.append("\033[1mServices\033[0m")
    for svc in data.get("services", []):
        status_str = _fmt_status(svc["status"])
        detail = f"  {svc['detail']}" if svc.get("detail") else ""
        lines.append(f"  {svc['name']:<10} {status_str}{detail}")

    # Brain diagnostic
    diag = data.get("brain_diagnostic", {})
    if diag:
        err = diag.get("_error")
        if err:
            lines.append(f"  Brain diagnostic: \033[33m{err}\033[0m")
        else:
            model = diag.get("model", "?")
            auto = diag.get("autonomous", True)
            exhausted = diag.get("credit_exhausted_agents", [])
            lines.append(f"  Brain model: {model}  autonomous: {_fmt_ok(auto)}")
            if exhausted:
                lines.append(f"  \033[33mCredit exhausted: {', '.join(exhausted)}\033[0m")
    lines.append("")

    # Routing stats
    rt = data.get("routing", {})
    if rt and not rt.get("_error"):
        smart = rt.get("smart_routing", False)
        local = rt.get("local_only", False)
        cache = rt.get("cache_size", 0)
        cap   = rt.get("cache_capacity", 500)
        rt_color = "\033[32m" if smart else "\033[33m"
        lo_color = "\033[33m" if local else "\033[36m"
        lines.append("\033[1mRouting\033[0m")
        lines.append(f"  smart: {rt_color}{'on' if smart else 'off'}\033[0m  local-only: {lo_color}{'on' if local else 'off'}\033[0m  cache: {cache}/{cap}")
        dist = rt.get("category_distribution", {})
        if dist:
            top3 = sorted(dist.items(), key=lambda x: -x[1])[:3]
            lines.append(f"  top categories: {', '.join(f'{k}:{v}' for k,v in top3)}")
        lines.append("")

    # Memory corpus (first-run visibility: the fix for "everything is empty")
    mc = data.get("memory_corpus", {})
    if mc and not mc.get("_error"):
        lines.append("\033[1mMemory corpus\033[0m")
        if mc.get("exists") and mc.get("md_files", 0) > 0:
            ident = _fmt_ok(bool(mc.get("identity_present")))
            lines.append(
                f"  \033[32mok\033[0m  {mc.get('md_files', 0)} file(s) at {mc.get('root')}  identity: {ident}"
            )
        else:
            lines.append(f"  \033[33mempty\033[0m  root: {mc.get('root')}")
        if mc.get("hint"):
            lines.append(f"  \033[33m{mc['hint']}\033[0m")
        emb = mc.get("embed_index") or {}
        if emb.get("indexed"):
            gap = emb.get("corpus_files", 0) - emb.get("indexed", 0)
            color = "\033[32m" if gap <= max(5, emb.get("corpus_files", 0) // 20) else "\033[33m"
            lines.append(
                f"  semantic index: {color}{emb['indexed']}/{emb.get('corpus_files', '?')}"
                f"\033[0m file(s) embedded ({emb.get('model', '?')})"
            )
        lines.append("")

    # Sextant Sync (rendered only when in use)
    sy = data.get("sync", {})
    if sy and not sy.get("_error"):
        lines.append("\033[1mSync\033[0m")
        for p in sy.get("principals", []):
            seen = p.get("last_checkin") or "never"
            flags = " REVOKED" if p.get("revoked") else ""
            lines.append(
                f"  {p.get('name', '?'):<20} {p.get('kind', '?'):<7} "
                f"{p.get('scope', '?'):<3} last check-in: {seen}{flags}"
            )
        if sy.get("conflicts"):
            lines.append(
                f"  \033[33mUNRESOLVED CONFLICTS: {sy['conflicts']} file(s) "
                f"under _atlas/sync-conflicts/\033[0m"
            )
        if sy.get("inbox"):
            lines.append(
                f"  \033[36minbox proposals awaiting review: {sy['inbox']}\033[0m"
            )
        lines.append("")

    # Worker status
    wk = data.get("worker", {})
    if wk:
        lines.append("\033[1mWorker\033[0m")
        assessment = wk.get("assessment", "unknown")
        _colors = {"ok": "\033[32m", "idle": "\033[36m", "warning": "\033[33m", "stale": "\033[31m", "unknown": "\033[33m"}
        color = _colors.get(assessment, "\033[33m")
        lines.append(f"  Status:     {color}{assessment}\033[0m")
        if assessment not in ("unknown",):
            lines.append(f"  Pending:    {wk.get('pending', '?')}  Running: {wk.get('running', '?')}")
            if wk.get("stale_running", 0) > 0:
                lines.append(f"  \033[31mStale running tasks: {wk['stale_running']} (heartbeat >30 min old)\033[0m")
            lines.append(f"  Last claim: {wk.get('last_claimed_at', 'never')}")
            lines.append(f"  Last done:  {wk.get('last_finished_at', 'never')}")
        else:
            lines.append(f"  Reason: {wk.get('reason', '?')}")

    # Per-worker detail (supplementary)
    wkd = data.get("worker_detail", {})
    active = wkd.get("active_workers", [])
    pending_ba = wkd.get("pending_by_agent", {})
    zombie_count = wkd.get("zombie_count", 0)
    queue_depth = wkd.get("queue_depth", 0)
    if wkd and not wkd.get("error"):
        if active:
            for w in active[:3]:  # show up to 3 workers
                wid_short = w["worker_id"][-20:] if len(w["worker_id"]) > 20 else w["worker_id"]
                hb = (w.get("last_heartbeat") or "")[:19]
                lines.append(f"  Worker: ...{wid_short}  tasks: {w['running_tasks']}  last-hb: {hb or 'none'}")
        if pending_ba:
            ba_str = "  ".join(f"{a}:{n}" for a, n in list(pending_ba.items())[:4])
            lines.append(f"  Queue by agent: {ba_str}")
        if zombie_count > 0:
            lines.append(f"  \033[31mZombies: {zombie_count}\033[0m")
    lines.append("")

    # Autonomous gate
    lines.append("\033[1mAutonomous gate\033[0m")
    for k, v in data.get("autonomous", {}).items():
        lines.append(f"  {k}: {v}")
    lines.append("")

    # Tickets
    tk = data.get("tickets", {})
    if tk:
        lines.append("\033[1mTickets\033[0m")
        lines.append(f"  Total: {tk.get('total', 0)}")
        by_s = tk.get("by_status", {})
        open_count = by_s.get("todo", 0) + by_s.get("in-progress", 0) + by_s.get("blocked", 0)
        lines.append(f"  Open:  {open_count}  (done: {by_s.get('done', 0)})")
        by_a = tk.get("by_assignee", {})
        lines.append(f"  claude-code: {by_a.get('claude-code', 0)}  human: {by_a.get('human', 0)}")
        high_reopen = tk.get("high_reopen", [])
        if high_reopen:
            lines.append(f"  \033[33mHigh reopen count (>=3x):\033[0m")
            for item in high_reopen[:3]:
                lines.append(f"    \033[31m{item['id']}[/] ({item['reopen_count']}x): {item['title']}")
        lines.append("")

    # Ticket dispatcher
    disp = data.get("dispatcher", {})
    if disp:
        lines.append("\033[1mTicket dispatcher\033[0m")
        enabled = disp.get("enabled", False)
        state_str = "\033[32menabled\033[0m" if enabled else "\033[33moff (ATLAS_TICKET_DISPATCH=on to enable)\033[0m"
        lines.append(f"  State:      {state_str}")
        last_run = disp.get("last_run_at")
        if last_run:
            try:
                import datetime as _dt
                run_dt = _dt.datetime.fromisoformat(last_run)
                age_min = int(
                    (_dt.datetime.now(_dt.timezone.utc) - run_dt).total_seconds() / 60
                )
                lines.append(f"  Last run:   {age_min}m ago")
            except Exception:
                lines.append(f"  Last run:   {last_run}")
        else:
            lines.append("  Last run:   never (no run since process start)")
        lines.append(f"  In-flight:  {disp.get('in_flight', 0)} ticket(s) in-progress")
        lines.append(f"  Dispatched: {disp.get('total_dispatched', 0)} total this session")
        lines.append("")

    # Cloudflare tunnel (remote MCP ingress)
    tun = data.get("tunnel", {})
    if tun and not tun.get("error"):
        running = tun.get("running")
        col = "\033[32m" if running else "\033[33m"
        state = tun.get("service_state") or ("up" if running else "down")
        svc = tun.get("service") or "no cloudflared service"
        lines.append("\033[1mTunnel (remote MCP ingress)\033[0m")
        lines.append(f"  {col}{'up' if running else 'down'}\033[0m  service: {svc} ({state})")
        if tun.get("origin_listening") and not tun.get("origin_loopback_only"):
            lines.append(f"  \033[31mMCP origin {MCP_PORT} NOT loopback-only -- investigate exposure\033[0m")
        lines.append("")

    # Optional policy-gate service (private extension)
    gk = data.get("gatekeeper", {})
    if gk and not gk.get("error"):
        lines.append("\033[1mGatekeeper\033[0m")
        if gk.get("installed"):
            active = gk.get("active")
            col = "\033[32m" if active else "\033[33m"
            label = "active" if active else "installed (filter not running)"
            lines.append(
                f"  {col}{label}\033[0m  filter: {gk.get('filter_state') or '-'}  "
                f"service: {gk.get('usermode_state') or '-'}"
            )
        else:
            lines.append("  \033[36mnot deployed\033[0m (kernel minifilter not installed -- expected pre-v1.0)")
        lines.append("")

    # Temporal graph
    tg = data.get("temporal", {})
    if tg:
        lines.append("\033[1mTemporal graph\033[0m")
        stale_note = ""
        never_built = not tg.get("last_built") and not tg.get("entities") and not tg.get("facts_active")
        if never_built:
            stale_note = "  \033[36mnot built yet (optional: atlas-temporal build)\033[0m"
        elif tg.get("stale"):
            age = tg.get("age_days")
            stale_note = f"  \033[33mSTALE ({age}d old)\033[0m" if age else "  \033[33mSTALE (no manifest)\033[0m"
        elif tg.get("last_built"):
            stale_note = f"  (built {tg.get('last_built')})"
        lines.append(f"  Entities: {tg.get('entities', 0)}  Facts: {tg.get('facts_active', 0)}{stale_note}")
        lines.append("")

    # Nightly jobs
    nightly = data.get("nightly", {})
    if nightly:
        lines.append("\033[1mNightly jobs (last run)\033[0m")
        fresh = data.get("scheduler_freshness", {})
        if fresh.get("stale"):
            lines.append(
                f"  \033[33mSTALE: nightly family last ran {fresh.get('age_hours')}h ago "
                f"({fresh.get('newest')}) -- catch-up fires on next Brain start\033[0m"
            )
        for job, info in nightly.items():
            status = info.get("status", "?")
            last = info.get("last", "never")
            color = "\033[32m" if status == "done" else "\033[31m"
            lines.append(f"  {job:<20} {color}{status}\033[0m  {last}")
        lines.append("")

    # Backup
    bk = data.get("backup", {})
    lines.append("\033[1mBackup\033[0m")
    lines.append(f"  Last run: {bk.get('last_run', 'unknown')}")

    # DB snapshot status (atlas-db-backup)
    dbbk = data.get("db_backup", {})
    if dbbk:
        if dbbk.get("last_ts") == "never":
            lines.append("  \033[33mDB snapshot: none yet (atlas-db-backup snapshot; schedule it yourself -- no job ships with the public build)\033[0m")
        elif dbbk.get("stale"):
            lines.append(
                f"  \033[31mDB snapshot STALE: {dbbk.get('last_ts')} "
                f"({dbbk.get('age_hours')}h ago)\033[0m"
            )
        else:
            lines.append(
                f"  DB snapshot: {dbbk.get('last_ts')}  "
                f"{dbbk.get('size_kb', '?')} KB  "
                f"({dbbk.get('age_hours')}h ago)  "
                f"total: {dbbk.get('total_snapshots', 0)}"
            )
    lines.append("")

    # Disk
    disk = data.get("disk", {})
    if disk:
        free = disk.get("free_gb", 0)
        pct = disk.get("pct_used", 0)
        color = "\033[31m" if free < 20 else "\033[32m"
        lines.append("\033[1mDisk\033[0m")
        lines.append(f"  {disk.get('drive')}  free: {color}{free} GB\033[0m  used: {pct}%")
        lines.append("")

    # Memory quality overview
    mq = data.get("memory_quality", {})
    if mq and mq.get("total_files", 0) > 0:
        total = mq["total_files"]
        conf_pct = mq.get("confirmed_pct", 0)
        tag_pct  = mq.get("tagged_pct", 0)
        avg_age  = mq.get("avg_age_days", 0)
        stale    = mq.get("stale_confirmed", 0)
        anti     = mq.get("anti_flagged", 0)
        dist     = mq.get("confidence_dist", {})
        conf_color = "\033[32m" if conf_pct >= 60 else ("\033[33m" if conf_pct >= 30 else "\033[31m")
        lines.append("\033[1mMemory Quality\033[0m")
        lines.append(f"  {total} files  confirmed: {conf_color}{conf_pct}%\033[0m  tagged: {tag_pct}%  avg-age: {avg_age:.0f}d")
        dist_str = "  ".join(f"{k}:{v}" for k, v in dist.items() if v > 0)
        if dist_str:
            lines.append(f"  confidence: {dist_str}")
        if stale > 0:
            lines.append(f"  \033[33mStale confirmed: {stale} (>90d without re-confirm)\033[0m")
        if anti > 0:
            lines.append(f"  Anti-flagged: {anti}")
        untagged = mq.get("untagged_files", 0)
        if untagged > 0:
            lines.append(
                f"  \033[33mUntagged: {untagged} file(s) -- run: atlas-review-memory suggest-tags --write\033[0m"
            )
            # Top-3 untagged scopes for quick remediation targeting
            try:
                scope_counts = data.get("untagged_by_scope") or check_untagged_by_scope()
                if scope_counts:
                    top3 = sorted(scope_counts.items(), key=lambda x: -x[1])[:3]
                    scope_parts = ", ".join(f"{s}({n})" for s, n in top3)
                    lines.append(f"  \033[33mTop untagged scopes: {scope_parts}\033[0m")
            except Exception:
                pass
        # Memory health distribution (if available)
        mh = data.get("memory_health", {})
        if mh and mh.get("total_files", 0) > 0:
            avg_h = mh.get("avg_score", 0)
            n0  = mh.get("score_0",   0)
            n25 = mh.get("score_25",  0)
            n50 = mh.get("score_50",  0)
            h_color = "\033[32m" if avg_h >= 0.75 else ("\033[33m" if avg_h >= 0.5 else "\033[31m")
            dist_parts = []
            if n0:  dist_parts.append(f"\033[31m{n0} bare\033[0m")
            if n25: dist_parts.append(f"\033[33m{n25} partial\033[0m")
            if n50: dist_parts.append(f"\033[33m{n50} half\033[0m")
            dist_str = "  " + ", ".join(dist_parts) if dist_parts else ""
            lines.append(
                f"  Health avg: {h_color}{avg_h:.2f}\033[0m{dist_str}"
            )
        # 7-day trend sparkline (populated after first daily snapshot)
        trends = data.get("memory_trends", [])
        if len(trends) >= 2:
            file_counts = [t.get("total_files", 0) for t in trends]
            conf_series = [t.get("confirmed_pct", 0) for t in trends]
            spark = _sparkline(file_counts)
            delta_files = file_counts[-1] - file_counts[0]
            delta_conf = round(conf_series[-1] - conf_series[0], 1)
            delta_files_str = (f"+{delta_files}" if delta_files >= 0 else str(delta_files))
            delta_conf_str  = (f"+{delta_conf}%" if delta_conf >= 0 else f"{delta_conf}%")
            lines.append(
                f"  7d trend: {spark}  files {delta_files_str}  confirmed {delta_conf_str}"
            )
        lines.append("")

    # MCP search stats
    mcp = data.get("mcp_stats", {})
    if mcp and mcp.get("total_searches_7d", 0) > 0:
        miss_pct = mcp.get("miss_rate_pct", 0)
        color = "\033[31m" if miss_pct >= 50 else ("\033[33m" if miss_pct >= 25 else "\033[32m")
        lines.append("\033[1mMCP search (7d)\033[0m")
        lines.append(
            f"  {mcp['total_searches_7d']} searches  "
            f"miss rate: {color}{miss_pct}%\033[0m  "
            f"({mcp['miss_count_7d']} misses)"
        )
        lines.append("")

    # Memory gaps
    gaps = data.get("memory_gaps", {})
    if gaps and gaps.get("unique_miss_queries", 0) > 0:
        lines.append("\033[1mMemory Gaps\033[0m")
        lines.append(f"  Unique miss queries (7d): {gaps['unique_miss_queries']}")
        for g in gaps.get("top", []):
            lines.append(f"  {g['miss_rate_pct']:>3}% miss  {g['query']}")
        lines.append("")

    # Decision staleness
    dec = data.get("decisions", {})
    if dec and dec.get("total", 0) > 0:
        total_d = dec["total"]
        stale_d = dec.get("stale", 0)
        if stale_d > 0:
            lines.append("\033[1mDecisions\033[0m")
            lines.append(f"  \033[33m{stale_d}/{total_d} decision(s) stale (>90d without review)\033[0m")
            for t in dec.get("stale_titles", [])[:3]:
                lines.append(f"    {t}")
            lines.append("  Run: atlas-review-memory decisions")
            lines.append("")

    # Convergence flags
    conv = data.get("convergence", {})
    if conv and conv.get("flagged", 0) > 0:
        flagged_n = conv["flagged"]
        lines.append("\033[1mConvergence Flags\033[0m")
        lines.append(f"  \033[33m{flagged_n} file(s) with unresolved contradiction flag(s)\033[0m")
        for p in conv.get("paths", [])[:3]:
            lines.append(f"    {p}")
        if flagged_n > 3:
            lines.append(f"    (+{flagged_n - 3} more)")
        lines.append("  Run: atlas-review-memory convergence")
        lines.append("")

    # Memory orphans
    orphans = data.get("orphans", {})
    if orphans and orphans.get("candidates", 0) > 0:
        n = orphans["candidates"]
        lines.append("\033[1mMemory Orphans\033[0m")
        lines.append(f"  \033[33m{n} file(s) unreferenced + low health + old (>180d)\033[0m")
        for item in orphans.get("top", [])[:3]:
            lines.append(
                f"    {item['path']}  (age={item['age_days']}d  health={item['score']:.2f})"
            )
        lines.append("  Run: atlas-review-memory orphans")
        lines.append("")

    # Memory reference graph hubs
    rg = data.get("ref_graph", {})
    if rg and rg.get("hub_count", 0) > 0:
        linked = rg.get("total_linked_files", 0)
        lines.append("\033[1mMemory Hub Files\033[0m")
        lines.append(f"  {linked} file(s) contain reference links")
        for hub in rg.get("top", [])[:3]:
            lines.append(f"    {hub['stem']}  ({hub['inbound_count']} inbound refs)")
        lines.append("  Run: atlas-review-memory ref-graph")
        lines.append("")

    # Re-derivable facts
    rederiv = data.get("rederivable", {})
    if rederiv and rederiv.get("candidates", 0) > 0:
        n = rederiv["candidates"]
        lines.append("\033[1mRe-derivable Facts\033[0m")
        lines.append(f"  \033[33m{n} file(s) overlap with project docs (threshold 0.40)\033[0m")
        for item in rederiv.get("top", [])[:3]:
            lines.append(f"    {item['path']}  (sim={item['similarity']:.2f}  matches: {item['matched_doc']})")
        lines.append("  Run: atlas-review-memory rederive")
        lines.append("")

    # Pending fact candidates
    pf = data.get("pending_facts", {})
    pf_count = pf.get("count", 0)
    if pf_count > 0:
        lines.append("\033[1mPending Fact Candidates\033[0m")
        oldest = pf.get("oldest", "")
        oldest_str = f"  (oldest: {oldest[:10]})" if oldest else ""
        lines.append(
            f"  \033[33m{pf_count} extracted fact(s) await review{oldest_str}\033[0m"
        )
        lines.append("  Use /pending in Brain CLI or Memory tab in Web UI to review.")
        lines.append("")

    # SLA breaches
    sla = data.get("sla_breaches", {})
    sla_count = sla.get("count", 0)
    if sla_count > 0:
        lines.append("\033[1mSLA Breaches\033[0m")
        lines.append(f"  \033[31m{sla_count} task(s) running past SLA threshold\033[0m")
        for tid in sla.get("task_ids", []):
            lines.append(f"    {tid}")
        lines.append("  Use /kill <task-id> in Brain CLI to cancel.")
        lines.append("")

    # Exhausted tasks (burned through all retries)
    ex = data.get("exhausted_tasks", {})
    ex_count = ex.get("count", 0)
    if ex_count > 0:
        lines.append("\033[1mExhausted Tasks\033[0m")
        lines.append(f"  \033[33m{ex_count} task(s) exhausted all retry attempts\033[0m")
        for t in ex.get("tasks", [])[:5]:
            lines.append(
                f"    [{t['status']}] {t['task_id']}  {t['agent']}  {t['instruction'][:45]}"
            )
        lines.append("  Use /retry <task-id> in Brain CLI to requeue.")
        lines.append("")

    # Research quality summary
    rq = data.get("research_quality", {})
    if rq and rq.get("scored_reports", 0) > 0:
        avg = rq.get("avg_score", 0)
        low = rq.get("low_quality_count", 0)
        scored = rq.get("scored_reports", 0)
        total_r = rq.get("total_reports", 0)
        avg_color = "\033[32m" if avg >= 0.7 else ("\033[33m" if avg >= 0.4 else "\033[31m")
        lines.append("\033[1mResearch Quality\033[0m")
        lines.append(
            f"  {scored}/{total_r} scored  avg: {avg_color}{avg:.2f}\033[0m"
            + (f"  \033[33mlow-quality: {low}\033[0m" if low > 0 else "")
        )
        min_f = rq.get("min_score_file", "")
        if min_f and low > 0:
            lines.append(f"  Lowest: {min_f[:60]}")
        lines.append("[dim]  Run: atlas-review-memory quality --write[/dim]" if scored < total_r else "")
        lines.append("")

    # Research gaps (unresolved gap tickets)
    rg = data.get("research_gaps", {})
    rg_count = rg.get("count", 0)
    if rg_count > 0:
        lines.append("\033[1mResearch Gaps\033[0m")
        lines.append(f"  \033[33m{rg_count} unresolved gap ticket(s)\033[0m  (run atlas-brain /gap-report <topic>)")
        for g in rg.get("top", [])[:3]:
            lines.append(f"  {g['id']}: {g['title'][:55]}")
        lines.append("")

    # TUI ecosystem activity
    tui = data.get("tui_activity", {})
    tui_apps = tui.get("apps", [])
    if tui_apps:
        total_ev = tui.get("total_events_today", 0)
        active = tui.get("apps_active_today", 0)
        lines.append("\033[1mTUI Activity\033[0m")
        lines.append(f"  {active} app(s) active today  |  {total_ev} events")
        for a in tui_apps:
            date_tag = "" if a["date"] == str(__import__("datetime").date.today()) else f" ({a['date']})"
            lines.append(f"  {a['app']}: {a['last_time']}  {a['last_msg'][:50]}{date_tag}")
        lines.append("")

    # Token spend (7d, provider-seam ledger)
    sp_data = data.get("spend", {})
    if sp_data.get("available") and sp_data.get("total_turns", 0) > 0:
        pt = sp_data.get("total_prompt_tokens", 0)
        ct = sp_data.get("total_completion_tokens", 0)
        cost = sp_data.get("est_cost_usd", 0.0)
        cost_txt = f"  ~${cost:.2f} est" if cost else "  (local models: $0)"
        lines.append("\033[1mToken Spend (7d)\033[0m")
        lines.append(
            f"  {pt:,} prompt + {ct:,} completion tokens over "
            f"{sp_data.get('total_turns', 0)} turn(s){cost_txt}"
        )
        lines.append("")

    # fsdiff count
    fsdiff = data.get("fsdiff_count", 0)
    if fsdiff:
        lines.append(f"  Session fsdiffs captured: {fsdiff}")

    lines.append(f"\n  Generated: {data.get('ts', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TUI ecosystem activity
# ---------------------------------------------------------------------------

# Which TUI/companion apps to scan session logs for. Operator-specific by
# nature, so it comes from the environment -- default is none (the paranoid
# stranger pass 2026-09-23 flagged the previous hardcoded roster as a leak
# of the maintainer's unrelated private projects).
_TUI_APPS = [
    a.strip()
    for a in os.environ.get("ATLAS_TUI_APPS", "").split(",")
    if a.strip()
]


def check_spend(days: int = 7) -> dict:
    """Token-spend summary straight from the Brain's ledger DB (spend v2)."""
    if not (STATE_DIR / "state.sqlite").exists():
        return {}  # a report never creates the DB (sweep 2026-11-11)
    from atlas_plus.brain import spend as _spend
    return _spend.summary(days=days)


def check_tui_activity() -> dict:
    """Scan today's (and yesterday's) session log for atlas-tui app events.

    Looks for lines matching ``- HH:MM <app_name>: <message>`` in
    ``.memory/sessions/<YYYY-MM-DD>.md`` files. Returns per-app last event
    plus aggregate counts. Returns empty dict when the sessions dir is missing.
    """
    import re
    import os as _os
    from datetime import date as _date, timedelta as _timedelta

    mem_root = Path(_os.environ.get("ATLAS_MEMORY_ROOT", str(MEMORY_ROOT)))
    sessions_dir = mem_root / "sessions"
    if not sessions_dir.is_dir():
        return {}

    today = _date.today()
    dates_to_check = [today.isoformat(), (today - _timedelta(days=1)).isoformat()]

    _LINE_RE = re.compile(r"^-\s+(\d{2}:\d{2})\s+([\w\-]+):\s+(.+)$")

    # Per-app: {"last_time": "HH:MM", "last_msg": str, "events": int, "date": str}
    app_data: dict[str, dict] = {}
    total_today = 0

    for date_str in dates_to_check:
        log_file = sessions_dir / f"{date_str}.md"
        if not log_file.exists():
            continue
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            m = _LINE_RE.match(line.strip())
            if not m:
                continue
            hhmm, app_name, message = m.group(1), m.group(2), m.group(3)
            if app_name not in _TUI_APPS:
                continue
            if date_str == today.isoformat():
                total_today += 1
            if app_name not in app_data:
                app_data[app_name] = {"last_time": hhmm, "last_msg": message, "events": 0, "date": date_str}
            else:
                # Update only if this entry is newer (same file = later line wins)
                if date_str <= app_data[app_name]["date"]:
                    app_data[app_name]["last_time"] = hhmm
                    app_data[app_name]["last_msg"] = message
                    app_data[app_name]["date"] = date_str
            if date_str == today.isoformat():
                app_data[app_name]["events"] = app_data[app_name].get("events", 0) + 1

    apps_active_today = sum(1 for v in app_data.values() if v.get("date") == today.isoformat())
    return {
        "apps": [
            {
                "app": app,
                "last_time": data["last_time"],
                "last_msg": data["last_msg"][:60],
                "events_today": data.get("events", 0),
                "date": data["date"],
            }
            for app, data in sorted(app_data.items())
        ],
        "total_events_today": total_today,
        "apps_active_today": apps_active_today,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def collect() -> dict:
    """Run all health checks CONCURRENTLY and return a unified snapshot dict keyed by check name.

    Checks are mostly I/O-bound (HTTP probes to Brain/MCP/Ollama, SQLite reads, filesystem
    scans), so a thread pool turns wall-clock time from sum(checks) into roughly max(checks).
    A single shared .memory/ scan feeds the contradiction + quality checks (one read pass, not
    one per check). Each check is isolated: one that raises yields {"_error": ...} rather than
    sinking the whole snapshot. Result key order is preserved for stable JSON output.
    """
    from concurrent.futures import ThreadPoolExecutor

    # One shared .memory/ read pass feeds the memory-derived checks below (was 1 pass each).
    mem_scan = scan_memory_files(MEMORY_ROOT)

    # (key, thunk) -- order here is the JSON output order.
    tasks = [
        ("services",          check_services),
        ("memory_corpus",     lambda: check_memory_corpus(mem_scan)),
        ("sync",              check_sync),
        ("brain_diagnostic",  check_brain_diagnostic),
        ("routing",           check_routing),
        ("worker",            check_worker_status),
        ("worker_detail",     check_worker_detail),
        ("autonomous",        check_autonomous),
        ("disk",              check_disk),
        ("temporal",          check_temporal),
        ("tickets",           check_tickets),
        ("dispatcher",        check_dispatcher),
        *([("tunnel", check_tunnel)] if _tunnel_check_relevant() else []),
        *([("gatekeeper", check_gatekeeper)] if check_gatekeeper is not None else []),
        ("memory_gaps",       check_memory_gaps),
        ("research_gaps",     check_research_gaps),
        ("convergence",       lambda: check_convergence(mem_scan)),
        ("decisions",         check_decisions),
        ("orphans",           check_orphans),
        ("ref_graph",         check_ref_graph),
        ("rederivable",       check_rederivable),
        ("memory_quality",    lambda: check_memory_quality(mem_scan)),
        ("pending_facts",     check_pending_facts),
        ("sla_breaches",      check_sla_breaches),
        ("exhausted_tasks",   check_exhausted_tasks),
        ("mcp_stats",         check_mcp_stats),
        ("backup",            check_backup),
        ("db_backup",         check_db_backup),
        ("nightly",           check_nightly),
        ("scheduler_freshness", check_scheduler_freshness),
        ("fsdiff_count",      check_fsdiff_count),
        ("memory_trends",     lambda: read_trends(days=7)),
        ("research_quality",  check_research_quality),
        ("memory_health",     check_memory_health_overview),
        ("untagged_by_scope", lambda: check_untagged_by_scope(mem_scan)),
        ("tui_activity",      check_tui_activity),
        ("spend",             check_spend),
    ]

    def _run(task):
        key, fn = task
        try:
            return key, fn()
        except Exception as exc:  # one bad check must not sink the whole snapshot
            return key, {"_error": f"{type(exc).__name__}: {exc}"}

    results: dict = {"ts": datetime.now(timezone.utc).isoformat()}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for key, value in ex.map(_run, tasks):
            results[key] = value
    return results


def main() -> int:
    """Entry point for the atlas-doctor CLI: collect and render (or JSON-dump) system health."""
    import argparse
    # Windows pipes default to cp1252, which cannot encode the sparkline glyphs;
    # never let an encoding gap crash a health report.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
    from atlas_plus.branding import product as _product
    parser = argparse.ArgumentParser(description=f"{_product()} health check")
    parser.add_argument("--json",  action="store_true", help="Output JSON instead of text")
    parser.add_argument("--watch", action="store_true", help="Refresh every 30s")
    parser.add_argument("--strict", action="store_true",
                        help="Exit 1 if any check reports down or an error (default: always exit 0)")
    args = parser.parse_args()

    def _unhealthy(data: dict) -> list[str]:
        """Names of SERVICES that are down (strict mode cares about the processes, not about
        the advisory sections, which may legitimately report nothing to say)."""
        services = data.get("services")
        if isinstance(services, dict):
            if "_error" in services:
                return ["services"]
            services = list(services.values())
        if not isinstance(services, list):
            return ["services"]
        # A service that is DOWN counts only when this install is configured to run it: a
        # stdio-only MCP install, or one without a Brain key, is healthy with those rows down
        # (persona rerun 2026-11-10). "degraded"/"error" always count.
        expected = {
            "Brain": bool(os.environ.get("ATLAS_BRAIN_API_KEY")),
            "MCP": bool(os.environ.get("ATLAS_MCP_TOKEN")),
            "Web": bool(os.environ.get("ATLAS_WEB_API_KEY")),
            "Sync": bool(os.environ.get("ATLAS_SYNC_DEVICE_TOKEN")) or _hub_has_devices(),
            "Ollama": bool(os.environ.get("ATLAS_BRAIN_OLLAMA_URL") or os.environ.get("OLLAMA_BASE_URL")
                           or os.environ.get("OLLAMA_HOST")),
        }
        why = {
            "Brain": "ATLAS_BRAIN_API_KEY is set", "MCP": "ATLAS_MCP_TOKEN is set",
            "Web": "ATLAS_WEB_API_KEY is set", "Sync": "a sync token or paired device exists",
            "Ollama": "an Ollama URL variable is set",
        }
        bad = []
        for val in services:
            if not isinstance(val, dict):
                continue
            name = str(val.get("name", "?"))
            if val.get("status") == "degraded":
                bad.append(f"{name} degraded")
            elif val.get("status") == "down" and expected.get(name, True):
                bad.append(f"{name} down, expected because {why.get(name, 'it is configured')}")
        # "or an error", as the flag promises: a section that reports an error counts too
        # (persona pass 2026-11-10, "Marco": brain_diagnostic {"_error": "HTTP 401"} exited 0),
        # unless the section only exists for a service this install does not run.
        brain_sections = {"brain_diagnostic", "routing"}
        for section, val in data.items():
            if section == "services" or not isinstance(val, dict):
                continue
            if section in brain_sections and not expected["Brain"]:
                continue
            if val.get("_error") or (isinstance(val.get("error"), str) and val.get("error")):
                bad.append(f"{section} error")
        return sorted(set(bad))

    def run_once():
        """Collect and print one health snapshot (JSON or ANSI text)."""
        data = collect()
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print(_plain_if_piped(render_text(data)))
        return data

    data = run_once()
    if args.strict and not args.watch:
        bad = _unhealthy(data)
        if bad:
            # On stdout as the last line: PowerShell renders stderr as a red NativeCommandError
            # and the reason got lost after a long report (persona rerun 2026-11-10).
            print(f"strict: FAIL ({', '.join(bad)})")
            return 1
        print("strict: OK")
    if args.watch:
        try:
            while True:
                time.sleep(30)
                if not args.json:
                    print("\033[2J\033[H", end="")  # clear screen
                run_once()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
