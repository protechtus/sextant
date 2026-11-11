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
        proc = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
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
