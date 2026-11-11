"""P6-T2: Dependency manifest reader for a project directory."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path


def _read_pyproject(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception as exc:
        return {"error": str(exc)}

    project = data.get("project", {})
    deps = project.get("dependencies", [])
    optional = project.get("optional-dependencies", {})
    scripts = project.get("scripts", {})

    # uv dev-dependencies live under [tool.uv.dev-dependencies] or [dependency-groups]
    tool_uv = data.get("tool", {}).get("uv", {})
    uv_dev = tool_uv.get("dev-dependencies", [])
    dep_groups = data.get("dependency-groups", {})

    dev_deps: dict = {}
    if uv_dev:
        dev_deps["dev"] = uv_dev
    dev_deps.update(optional)
    dev_deps.update(dep_groups)

    python_req = project.get("requires-python", None)

    return {
        "deps": deps,
        "dev_deps": dev_deps,
        "scripts": scripts,
        "python_requires": python_req,
        "name": project.get("name"),
        "version": project.get("version"),
    }


def _read_requirements(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return [l.strip() for l in lines if l.strip() and not l.startswith("#")]
    except Exception:
        return []


def _read_package_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": str(exc)}

    return {
        "name": data.get("name"),
        "version": data.get("version"),
        "deps": list(data.get("dependencies", {}).keys()),
        "dev_deps": list(data.get("devDependencies", {}).keys()),
        "scripts": data.get("scripts", {}),
    }


def manifest_state(project_dir: Path) -> dict:
    """Return dependency manifest state for *project_dir*.

    Returns a dict with keys:
        detected (list of found manifest filenames),
        lang (primary language: 'python', 'node', or None),
        pyproject, requirements, package_json (per-manifest data, present only if found).
    """
    result: dict = {"detected": [], "lang": None}

    pyproject = project_dir / "pyproject.toml"
    if pyproject.exists():
        result["detected"].append("pyproject.toml")
        result["lang"] = "python"
        result["pyproject"] = _read_pyproject(pyproject)

    req = project_dir / "requirements.txt"
    if req.exists():
        result["detected"].append("requirements.txt")
        result["lang"] = result["lang"] or "python"
        result["requirements"] = _read_requirements(req)

    pkg = project_dir / "package.json"
    if pkg.exists():
        result["detected"].append("package.json")
        result["lang"] = result["lang"] or "node"
        result["package_json"] = _read_package_json(pkg)

    uv_lock = project_dir / "uv.lock"
    if uv_lock.exists():
        result["detected"].append("uv.lock")

    pkg_lock = project_dir / "package-lock.json"
    if pkg_lock.exists():
        result["detected"].append("package-lock.json")

    return result
