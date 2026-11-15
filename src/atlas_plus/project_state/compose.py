"""P6-T3: Docker Compose state reader for a project directory."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

_COMPOSE_FILENAMES = [
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
]


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
        # AG67: with capture_output a Windows timeout killed docker.exe and then waited for ever on the pipe its
        # docker-compose.exe child still held (project_state_tool hung the MCP server 147 s with Docker Desktop wedged);
        # a temp file has no pipe to wait on, and stdin=DEVNULL keeps the child off the MCP stdio pipe
        import tempfile as _tf
        import types as _types
        with _tf.TemporaryFile() as _out:
            _r = subprocess.run(
                ["docker", "compose", "ps", "--format", "json"],
                cwd=str(project_dir),
                stdin=subprocess.DEVNULL,
                stdout=_out,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            _out.seek(0)
            _text = _r.stdout if isinstance(_r.stdout, str) else _out.read().decode("utf-8", errors="replace")
            proc = _types.SimpleNamespace(returncode=_r.returncode, stdout=_text)
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
