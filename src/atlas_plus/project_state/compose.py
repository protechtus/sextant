"""P6-T3: Docker Compose state reader for a project directory."""

from __future__ import annotations

import json
import subprocess

from atlas_plus._proc import other_party_env as _other_party_env
from pathlib import Path

_COMPOSE_FILENAMES = [
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
]


_PS_CMD = ("docker", "compose", "ps", "--format", "json")
_PS_TIMEOUT = 10.0


def _kill_tree(p: subprocess.Popen) -> None:
    """Kill a timed-out command and every child it started (MN61)."""
    import os as _os
    try:
        if _os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], stdin=subprocess.DEVNULL, env=_other_party_env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            import signal as _signal
            _os.killpg(p.pid, _signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pass
    try:
        p.kill()
        p.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _run_ps(project_dir: Path):
    """`docker compose ps --format json` in project_dir: (returncode, stdout); TimeoutExpired after the tree is killed."""
    # AG67: with capture_output a Windows timeout killed docker.exe and then waited for ever on the pipe its
    # docker-compose.exe child still held (project_state_tool hung the MCP server 147 s with Docker Desktop wedged);
    # a temp file has no pipe to wait on, and stdin=DEVNULL keeps the child off the MCP stdio pipe
    import tempfile as _tf
    import types as _types
    import os as _os
    with _tf.TemporaryFile() as _out:
        # its own process group, and the whole tree killed on timeout: killing docker.exe alone left its hung
        # docker-compose.exe child running, one per call (MN61)
        _p = subprocess.Popen(
            list(_PS_CMD),
            cwd=str(project_dir),
            stdin=subprocess.DEVNULL,
            stdout=_out,
            stderr=subprocess.DEVNULL,
            env=_other_party_env(keep_prefixes=("DOCKER_", "COMPOSE_")),  # docker: allowlisted (AG127)
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if _os.name == "nt" else {"start_new_session": True}),
        )
        try:
            _rc = _p.wait(timeout=_PS_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_tree(_p)
            raise
        _out.seek(0)
        return _types.SimpleNamespace(returncode=_rc, stdout=_out.read().decode("utf-8", errors="replace"))


def compose_state(project_dir: Path) -> dict:
    """Return docker-compose state for *project_dir*.

    Returns a dict with keys:
        has_compose (bool),
        compose_file (filename, if found),
        services (list of service dicts from `docker compose ps`),
        running (bool — True if any service is in 'running' state),
        docker_unavailable (bool, present only when Docker can't be reached).
    """
    compose_file = None
    for name in _COMPOSE_FILENAMES:
        candidate = project_dir / name
        if candidate.exists():
            compose_file = candidate
            break

    if compose_file is None:
        return {"has_compose": False}

    result: dict = {
        "has_compose": True,
        "compose_file": compose_file.name,
        "services": [],
        "running": False,
    }

    try:
        proc = _run_ps(project_dir)
        if proc.returncode == 0 and proc.stdout.strip():
            services = []
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    services.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
            result["services"] = services
            result["running"] = any(
                str(s.get("State", "")).lower() == "running" for s in services
            )
    except FileNotFoundError:
        result["docker_unavailable"] = True
    except subprocess.TimeoutExpired:
        result["docker_unavailable"] = True

    return result
