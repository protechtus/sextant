"""SEX-060 (the fold of AG127): each caller that starts another party's program is DRIVEN with a fake program.

The lane changed the git, rclone, docker and PowerShell callers by reading only. Here a fake program of that name
is first on PATH (a real `.exe` on Windows, where a bare program name never resolves to a script), the real
caller starts it, and the fake records the NAMES of its environment plus the values of a few location names.

Two things are proven per caller: the program still receives what it needs to work (its config location, the
proxy, the temp folder, its own variables, among them its own password names), and no secret of this package
or of a third party reaches it. Every value here is a sentinel; names are assembled from pieces.
"""
from __future__ import annotations

import io
import json
import os
import platform
import shutil
import stat
import sys
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from atlas_plus import _proc

_SENTINEL = "sentinel-" + "not-a-value"
_T, _K, _S, _P = "TOK" + "EN", "K" + "EY", "SEC" + "RET", "PASS"

#: this package's secrets under both spellings: never in any other party's program
_OURS = ["ATLAS_MCP_" + _T, "SEXTANT_WEB_API_" + _K, "ATLAS_TELEGRAM_BOT_" + _T, "ATLAS_BRAIN_API_" + _K,
         "SEXTANT_SYNC_DEVICE_" + _T, "ATLAS_NTFY_CMD_" + _S]
#: a third party's secrets: no business in rclone, git, PowerShell, docker, an editor or a model CLI
_THEIRS = ["GH_" + _T, "AWS_" + _S + "_ACCESS_" + _K, "NPM_" + _T, "SOMEVENDOR_API_" + _K]
#: names whose VALUE the fake reports (locations and switches, never a secret)
_VALUE_NAMES = ["APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "TEMP", "TMP", "TMPDIR", "HTTPS_PROXY", "NO_PROXY",
                "SSL_CERT_FILE", "RCLONE_CONFIG", "XDG_CONFIG_HOME", "PSMODULEPATH", "COMPUTERNAME", "GIT_ASKPASS",
                "GIT_TERMINAL_PROMPT", "PYTHONUTF8", "PYTHONIOENCODING", "HF_HOME", "PIP_CACHE_DIR", "DOCKER_HOST",
                "EDITOR", "ATLAS_WORKSPACE_ROOT", "SYSTEMROOT", "PATH"]

_FAKE_SOURCE = """\
import json, os, sys
OUT = {out!r}
VALUES = {values!r}
rec = {{"argv": sys.argv[1:], "names": sorted(os.environ),
       "values": {{k.upper(): v for k, v in os.environ.items() if k.upper() in VALUES}}}}
with open(OUT, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec) + "\\n")
sys.stdout.write({reply!r})
"""


def _windows_launcher() -> Path | None:
    """The console-script launcher pip ships (the stub in front of every `Scripts\\*.exe`); None when absent."""
    import importlib.util

    spec = importlib.util.find_spec("pip")
    if spec is None or not spec.submodule_search_locations:
        return None
    name = "t64-arm.exe" if platform.machine().lower() in ("arm64", "aarch64") else "t64.exe"
    launcher = Path(list(spec.submodule_search_locations)[0]) / "_vendor" / "distlib" / name
    return launcher if launcher.is_file() else None


class Fake:
    """A folder of fake programs; `calls(name)` is what each one saw."""

    def __init__(self, root: Path):
        self.bin = root / "fakebin"
        self.bin.mkdir()
        self.root = root

    @staticmethod
    def _shadowed(name: str) -> str | None:
        """Windows starts a bare program name from the interpreter's folder, the current folder and the SYSTEM
        folders before it looks at PATH: a real program there cannot be shadowed by a fake on PATH (GitHub's
        Windows runners keep a docker.exe in System32; CI run 46125043496)."""
        if os.name != "nt" or "." in name and not name.lower().endswith(".exe"):
            return None
        exe = name if name.lower().endswith(".exe") else name + ".exe"
        root = os.environ.get("SystemRoot", r"C:\Windows")
        for folder in (os.path.dirname(sys.executable), os.getcwd(), os.path.join(root, "System32"), root):
            if os.path.isfile(os.path.join(folder, exe)):
                return folder
        return None

    def add(self, name: str, reply: str = "", bare: bool = True) -> str:
        """`bare`: the caller starts the program by its bare name (not by the path this returns)."""
        if bare and self._shadowed(name):
            pytest.skip(f"a real {name} sits in {self._shadowed(name)}, which Windows searches before PATH: "
                        "a fake cannot be first here")
        out = self.root / f"{name}.calls.jsonl"
        source = _FAKE_SOURCE.format(out=str(out), values=_VALUE_NAMES, reply=reply)
        if os.name == "nt":
            launcher = _windows_launcher()
            if launcher is None:
                pytest.skip("no pip launcher stub in this interpreter: a fake .exe cannot be built")
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w") as zf:
                zf.writestr("__main__.py", source)
            py = sys.executable
            shebang = (f'#!"{py}"' if " " in py else f"#!{py}").encode("utf-8") + b"\r\n"
            prog = self.bin / f"{name}.exe"
            prog.write_bytes(launcher.read_bytes() + shebang + payload.getvalue())
        else:
            script = self.root / f"{name}.fake.py"
            script.write_text(source, encoding="utf-8")
            prog = self.bin / name
            prog.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
            prog.chmod(prog.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return str(prog)

    def calls(self, name: str) -> list[dict]:
        out = self.root / f"{name}.calls.jsonl"
        assert out.is_file(), f"the fake {name} was never started"
        return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Fake programs first on PATH, and a parent environment that holds secrets, settings and locations."""
    f = Fake(tmp_path)
    monkeypatch.setenv("PATH", str(f.bin) + os.pathsep + os.environ.get("PATH", ""))
    for name in _OURS + _THEIRS:
        monkeypatch.setenv(name, _SENTINEL)
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "ca.pem"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("PIP_CACHE_DIR", str(tmp_path / "pipcache"))
    if os.name != "nt":  # a POSIX session may lack these; the program's config search starts from them
        for sub in ("home", "xdg", "tmp"):
            (tmp_path / sub).mkdir()
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    return f


def _upper(call: dict) -> set[str]:
    return {n.upper() for n in call["names"]}


def _assert_no_foreign_secret(call: dict, *, own_prefixes: tuple[str, ...] = (), settings: bool = False) -> None:
    names = _upper(call)
    leaked = sorted(n for n in names if n in {s.upper() for s in _OURS + _THEIRS})
    assert not leaked, f"secrets reached another party's program: {leaked}"
    if not settings:
        ours = sorted(n for n in names if n.startswith(("ATLAS_", "SEXTANT_")))
        assert not ours, f"this package's settings reached a program that does not start this package: {ours}"
    words = ("TOKEN", "KEY", "SECRET", "PASS", "CREDENTIAL", "COOKIE")
    shaped = sorted(n for n in names if any(w in n for w in words) and not n.startswith(own_prefixes)
                    and not n.startswith(("XDG_", "LC_"))  # the session's own names, kept for every program
                    and n not in ("MAX_THINKING_TOKENS", "MAX_MCP_OUTPUT_TOKENS"))
    assert not shaped, f"secret-shaped names that are not the program's own: {shaped}"


def _assert_locations_reach(call: dict) -> None:
    """Config location, proxy and temp folder arrive with the parent's values."""
    wanted = ["HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "PATH"]
    wanted += ["APPDATA", "LOCALAPPDATA", "USERPROFILE", "TEMP", "TMP", "SYSTEMROOT"] if os.name == "nt" else [
        "HOME", "XDG_CONFIG_HOME", "TMPDIR"]
    parent = {k.upper(): v for k, v in os.environ.items()}
    for name in wanted:
        if name not in parent:
            continue  # a machine without the name cannot hand it on
        assert call["values"].get(name) == parent[name], f"{name} did not reach the program unchanged"
    assert "PATH" in call["values"]


# ---- the backup's rclone ------------------------------------------------------------------------------------


def test_backup_rclone_gets_its_config_and_its_own_passwords_and_nothing_else(fake, monkeypatch, tmp_path):
    from atlas_plus import backup

    own = {"RCLONE_CONFIG": str(tmp_path / "rclone.conf"), "RCLONE_CONFIG_" + _P: _SENTINEL,
           "RCLONE_CRYPT_" + _P + "WORD": _SENTINEL, "RCLONE_CRYPT_" + _P + "WORD2": _SENTINEL,
           "RCLONE_DRIVE_CLIENT_" + _S: _SENTINEL, "RCLONE_TRANSFERS": "4"}
    for name, value in own.items():
        monkeypatch.setenv(name, value)
    fake.add("rclone", reply="gdrive: drive\n")

    result = backup._rclone("listremotes", "--long", capture=True)

    assert result.returncode == 0 and "gdrive" in result.stdout
    (call,) = fake.calls("rclone")
    assert call["argv"] == ["--ask-password=false", "listremotes", "--long"]
    names = _upper(call)
    missing = sorted(set(own) - names)
    assert not missing, f"rclone lost its own variables (the crypt and config settings, by name): {missing}"
    assert call["values"]["RCLONE_CONFIG"] == own["RCLONE_CONFIG"]
    _assert_locations_reach(call)
    _assert_no_foreign_secret(call, own_prefixes=("RCLONE_",))


def test_backup_task_installer_powershell_gets_settings_but_no_secret(fake, monkeypatch, tmp_path):
    from atlas_plus import backup

    installer = tmp_path / "install_backup_task.ps1"
    installer.write_text("# never run: the program is a fake\n", encoding="utf-8")
    monkeypatch.setattr(backup, "PS_INSTALL", installer)
    fake.add("powershell.exe" if os.name != "nt" else "powershell")

    try:
        backup.install(time="03:30")
    except (SystemExit, Exception) as exc:  # typer.Exit after the report; the call is what matters
        assert type(exc).__name__ in ("Exit", "SystemExit"), exc

    (call,) = fake.calls("powershell.exe" if os.name != "nt" else "powershell")
    assert call["argv"][-4:] == ["-File", str(installer), "-RunTime", "03:30"]
    assert call["values"].get("ATLAS_WORKSPACE_ROOT") == os.environ["ATLAS_WORKSPACE_ROOT"]  # the task needs its settings
    _assert_locations_reach(call)
    _assert_no_foreign_secret(call, settings=True)


# ---- git ----------------------------------------------------------------------------------------------------


def test_gitsafe_git_gets_its_helpers_variables_and_no_secret(fake, monkeypatch, tmp_path):
    from atlas_plus import gitsafe

    own = {"GIT_SSH": "ssh", "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"), "SSH_AUTH_SOCK": str(tmp_path / "agent"),
           "GCM_CREDENTIAL_STORE": "cache", "GNUPGHOME": str(tmp_path / "gnupg"),
           "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent"}
    for name, value in own.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -o Something")  # the hardened caller removes this one on purpose
    fake.add("git")

    result = gitsafe.run(["status", "--porcelain"], cwd=tmp_path)

    assert result.returncode == 0
    calls = fake.calls("git")
    assert calls and calls[-1]["argv"][-2:] == ["status", "--porcelain"]
    for call in calls:  # the config listing and the command itself
        names = _upper(call)
        missing = sorted(set(own) - names)
        assert not missing, f"git lost what its credential helper, ssh or gpg needs: {missing}"
        assert "GIT_SSH_COMMAND" not in names
        assert call["values"]["GIT_ASKPASS"] == "echo" and call["values"]["GIT_TERMINAL_PROMPT"] == "0"
        _assert_locations_reach(call)
        _assert_no_foreign_secret(call, own_prefixes=("GIT_", "SSH_", "GCM_"))


def test_learning_git_log_goes_through_the_same_environment(fake, tmp_path):
    from atlas_plus.brain import learning

    fake.add("git", reply="abc1234 a subject\n")
    out = learning._git_commits("2026-01-05", [tmp_path])
    assert out, "the fake git's line was not read"
    (call,) = fake.calls("git")
    _assert_locations_reach(call)
    _assert_no_foreign_secret(call, own_prefixes=("GIT_", "SSH_", "GCM_"))


# ---- PowerShell ---------------------------------------------------------------------------------------------


def test_doctor_and_dashboard_powershell_get_the_module_path_and_no_secret(fake, monkeypatch):
    from atlas_plus.doctor import cli as doctor_cli
    from atlas_plus.web import app as web_app

    if "PSMODULEPATH" not in {k.upper() for k in os.environ}:
        monkeypatch.setenv("PSModulePath", "/opt/microsoft/powershell/Modules")
    module_path = {k.upper(): v for k, v in os.environ.items()}["PSMODULEPATH"]
    fake.add("powershell", reply='{"ok": 1}')

    assert doctor_cli._powershell_json("Get-Thing | ConvertTo-Json") == {"ok": 1}
    code, text = web_app._run_powershell("Get-Thing")
    assert code == 0 and '"ok"' in text

    calls = fake.calls("powershell")
    assert len(calls) == 2
    for call in calls:
        assert call["values"].get("PSMODULEPATH") == module_path, "PowerShell lost its module path"
        _assert_locations_reach(call)
        _assert_no_foreign_secret(call)


@pytest.mark.skipif(os.name != "nt" or shutil.which("powershell") is None, reason="needs the real Windows PowerShell")
def test_the_real_powershell_still_loads_a_module_with_the_allowlisted_environment():
    """Not a fake: the allowlist must leave the real program able to auto-load a module and answer."""
    from atlas_plus.doctor import cli as doctor_cli

    answer = doctor_cli._powershell_json(
        "Get-CimInstance Win32_OperatingSystem | Select-Object Caption | ConvertTo-Json", timeout=60)
    assert isinstance(answer, dict) and answer.get("Caption"), answer


# ---- the model CLI and the research runner ------------------------------------------------------------------


def test_supervisor_claude_gets_its_own_variables_the_settings_and_no_secret(fake, monkeypatch, tmp_path):
    from atlas_plus.brain import executors

    own = {"ANTHROPIC_BASE_URL": "https://api.invalid", "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
           "CLAUDE_CODE_OAUTH_" + _T: _SENTINEL, "DISABLE_TELEMETRY": "1", "MAX_THINKING_TOKENS": "1000",
           "MCP_TIMEOUT": "30000"}
    for name, value in own.items():
        monkeypatch.setenv(name, value)
    program = fake.add("claude", reply="the report", bare=False)
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: program if name == "claude" else real_which(name, *a, **k))

    text, how = executors._run_supervisor_model("the prompt", "some-model", timeout=60)

    assert text == "the report", how
    (call,) = fake.calls("claude")
    assert call["argv"] == ["--print", "--model", "some-model", "--max-turns", "1"]
    names = _upper(call)
    missing = sorted(set(own) - names)
    assert not missing, f"the model CLI lost its own variables (its sign-in, its privacy switch, its limits): {missing}"
    assert call["values"]["PYTHONUTF8"] == "1"
    assert call["values"].get("ATLAS_WORKSPACE_ROOT") == os.environ["ATLAS_WORKSPACE_ROOT"]  # it starts our MCP server
    assert call["values"].get("PIP_CACHE_DIR") == os.environ["PIP_CACHE_DIR"]  # caches stay where the user put them
    _assert_locations_reach(call)
    _assert_no_foreign_secret(call, own_prefixes=("ANTHROPIC_", "CLAUDE_"), settings=True)


def test_model_cli_env_passes_a_cloud_sign_in_only_when_the_cli_is_switched_to_that_cloud():
    cloud_secret = "AWS_" + _S + "_ACCESS_" + _K
    base = {"PATH": "/usr/bin", "AWS_PROFILE": "work", cloud_secret: _SENTINEL, "GOOGLE_CLOUD_PROJECT": "p",
            "DISABLE_TELEMETRY": "1", "OPENAI_API_" + _K: _SENTINEL, "ATLAS_MCP_" + _T: _SENTINEL,
            "ATLAS_STATE_DIR": "/s"}
    plain = _proc.model_cli_env("claude", base=base)
    assert set(plain) == {"PATH", "DISABLE_TELEMETRY", "ATLAS_STATE_DIR"}
    bedrock = _proc.model_cli_env("claude-cli", base={**base, "CLAUDE_CODE_USE_BEDROCK": "1"})
    assert {"AWS_PROFILE", cloud_secret} <= set(bedrock) and "GOOGLE_CLOUD_PROJECT" not in bedrock
    vertex = _proc.model_cli_env("claude", base={**base, "CLAUDE_CODE_USE_VERTEX": "1"})
    assert "GOOGLE_CLOUD_PROJECT" in vertex and cloud_secret not in vertex
    codex = _proc.model_cli_env("codex-cli", base=base)
    assert "OPENAI_API_" + _K in codex and "DISABLE_TELEMETRY" not in codex
    for env in (plain, bedrock, vertex, codex):
        assert "ATLAS_MCP_" + _T not in env  # never this package's secret, whatever the CLI


def test_research_runner_script_gets_the_model_names_the_caches_and_no_secret(fake, monkeypatch, tmp_path):
    from atlas_plus.brain import executors
    from atlas_plus.brain.executors import ExecutorContext
    from atlas_plus.brain.tasks import Task

    ws = tmp_path / "ws"
    (ws / "research").mkdir(parents=True)
    out = tmp_path / "runner.calls.jsonl"
    (ws / "research" / "research_runner.py").write_text(
        _FAKE_SOURCE.format(out=str(out), values=_VALUE_NAMES, reply="queue: ok"), encoding="utf-8")
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(ws))
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.invalid")
    monkeypatch.setattr(executors, "_pytest_python", lambda root: sys.executable)
    task = MagicMock(spec=Task)
    task.task_id, task.kind, task.instruction, task.session_id = "sex060", "research_runner", "status", None
    ctx = ExecutorContext(task=task, artifact_root=tmp_path / "artifacts", db_path=tmp_path / "state.sqlite",
                          worker_id="sex060")

    result = executors._research_runner(ctx)

    assert result.status == "done", result.text
    call = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    names = _upper(call)
    assert {"OLLAMA_HOST", "ANTHROPIC_BASE_URL", "HF_HOME", "ATLAS_WORKSPACE_ROOT"} <= names
    assert call["values"]["HF_HOME"] == os.environ["HF_HOME"] and call["values"]["PYTHONUTF8"] == "1"
    _assert_locations_reach(call)
    _assert_no_foreign_secret(call, own_prefixes=("ANTHROPIC_", "OLLAMA_", "HF_"), settings=True)


# ---- docker and the editor ----------------------------------------------------------------------------------


def test_docker_compose_gets_its_host_and_no_secret(fake, monkeypatch, tmp_path):
    from atlas_plus.project_state import compose

    monkeypatch.setenv("DOCKER_HOST", "npipe:////./pipe/nowhere")
    fake.add("docker", reply="[]")
    result = compose._run_ps(tmp_path)
    if os.name == "nt" and not (fake.root / "docker.calls.jsonl").exists():
        # CI run 46125043496: a Windows runner's own docker.exe answered (exit 1, no output) and the fake was never
        # started; Windows resolves a bare name in places a test cannot put a fake first
        pytest.skip("a real docker answered before the fake on this Windows machine")
    assert result.returncode == 0 and result.stdout.strip() == "[]"
    (call,) = fake.calls("docker")
    assert call["values"]["DOCKER_HOST"] == "npipe:////./pipe/nowhere"
    _assert_locations_reach(call)
    _assert_no_foreign_secret(call, own_prefixes=("DOCKER_", "COMPOSE_"))


def test_the_editor_gets_no_secret(fake, monkeypatch):
    from atlas_plus.summarizer import review

    program = fake.add("fakeeditor", bare=False)
    monkeypatch.setenv("EDITOR", program)
    assert review._open_in_editor("some text") == "some text"
    (call,) = fake.calls("fakeeditor")
    assert call["values"]["EDITOR"] == program
    _assert_locations_reach(call)
    _assert_no_foreign_secret(call)


# ---- the base list ------------------------------------------------------------------------------------------


def test_the_base_list_keeps_machine_session_and_cache_names_and_nothing_secret_shaped():
    base = {n: "x" for n in ("COMPUTERNAME", "PSModulePath", "OPENSSL_CONF", "DBUS_SESSION_BUS_ADDRESS", "HF_HOME",
                             "PIP_CACHE_DIR", "UV_CACHE_DIR", "PLAYWRIGHT_BROWSERS_PATH", "Path", "SystemRoot")}
    base.update({n: _SENTINEL for n in _OURS + _THEIRS})
    env = _proc.other_party_env(base=base)
    assert set(env) == set(base) - set(_OURS) - set(_THEIRS)
    words = ("TOKEN", "KEY", "SECRET", "PASS", "CREDENTIAL", "COOKIE")
    assert not [n for n in _proc.CHILD_ENV_NAMES if any(w in n for w in words)], "a secret-shaped name is on the base list"
