"""AG81 (sweep 22): the Stop hook and `sextant setup --client hooks`.

1. Re-running `setup --client hooks --out settings.json` replaced this workspace's entries and
   dropped the `--env ATLAS_SESSION_AUTONOMOUS=on` that setup's own hint tells the user to add.
2. Two installs merged into one settings file both summarised (and primed) every session.
   ATLAS_HOOK_SCOPE scopes an install's hooks to its directories; setup warns when it merges a
   second, unscoped install.
3. The moved-workspace guard checked the corpus only: a missing state dir was created again, and
   a gate typo recorded activity into a workspace that is not there.
4. The facts stage counted text lines for its "enough new turns" gate: one long reply re-ran the
   model after every Stop.
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest


# --------------------------------------------------------------------------- helpers

def _clean_env(monkeypatch, ws):
    monkeypatch.setenv("ATLAS_WORKSPACE_ROOT", str(ws))
    for var in ("ATLAS_MEMORY_ROOT", "ATLAS_STATE_DIR", "ATLAS_CLAUDE_CODE_ROOT", "ATLAS_PROJECTS_ROOT",
                "ATLAS_UPDATES_FILE", "ATLAS_HOOK_SCOPE", "ATLAS_IDENTITY_PATH", "ATLAS_USER_NAME",
                "ATLAS_HOME_TZ", "ATLAS_TASK_HINT", "ATLAS_TRANSCRIPT_FILE", "ATLAS_SUMMARIZER_DRY_RUN",
                "ATLAS_DISABLE_AUTOSUMMARIZE", "ATLAS_DISABLE_SUGGEST_FACTS", "ATLAS_AUTONOMOUS"):
        monkeypatch.delenv(var, raising=False)


def _setup_out(monkeypatch, ws, out, scope=None):
    from atlas_plus import setup_cmd
    _clean_env(monkeypatch, ws)
    if scope:
        monkeypatch.setenv("ATLAS_HOOK_SCOPE", str(scope))
    assert setup_cmd.main(["--client", "hooks", "--out", str(out)]) == 0


def _stop_commands(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    return [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]


def _run_stop(monkeypatch, payload):
    from atlas_plus.summarizer import hook_entry as he
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(he, "_summarizer_unavailable_reason", lambda: "")
    monkeypatch.setattr(he, "_capture_fsdiffs", lambda: None)
    spawned = []

    class _P:
        def __init__(self, args, **kw):
            spawned.append(list(args))

    monkeypatch.setattr(he.subprocess, "Popen", _P)
    monkeypatch.setattr(he.sys, "argv", ["hook"])
    he._SESSION["id"] = ""
    assert he.main() == 0
    return spawned


def _workspace(tmp_path, name="ws"):
    ws = tmp_path / name
    (ws / ".memory").mkdir(parents=True)
    (ws / ".atlas").mkdir()
    return ws


def _transcript(tmp_path, slug, sid):
    d = tmp_path / "cc" / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text("{}\n", encoding="utf-8")
    return f


# --------------------------------------------------------------------------- (1) hand-added --env

def test_rerunning_setup_out_keeps_a_hand_added_env_flag(tmp_path, monkeypatch, capsys):
    ws = _workspace(tmp_path)
    out = tmp_path / "settings.json"
    _setup_out(monkeypatch, ws, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    hook = data["hooks"]["Stop"][0]["hooks"][0]
    assert hook["command"].endswith(" stop")
    hook["command"] = hook["command"][: -len(" stop")] + " --env ATLAS_SESSION_AUTONOMOUS=on --env 'ATLAS_OLLAMA_URL=http://127.0.0.1:9329' stop"
    data["permissions"] = {"allow": ["Bash(ls)"]}
    out.write_text(json.dumps(data), encoding="utf-8")

    _setup_out(monkeypatch, ws, out)
    stops = _stop_commands(out)
    assert len(stops) == 1
    assert "ATLAS_SESSION_AUTONOMOUS=on" in stops[0] and "ATLAS_OLLAMA_URL=http://127.0.0.1:9329" in stops[0]
    assert stops[0].endswith(" stop")
    # the carried flags still reach the hook: hooks.run parses them
    import shlex
    argv = shlex.split(stops[0])
    assert argv[argv.index("-m") + 1] == "atlas_plus.hooks.run" and argv[-1] == "stop"
    assert json.loads(out.read_text(encoding="utf-8"))["permissions"] == {"allow": ["Bash(ls)"]}
    # a third run changes nothing
    capsys.readouterr()
    _setup_out(monkeypatch, ws, out)
    assert "nothing written" in capsys.readouterr().err


def test_setup_own_variable_wins_over_a_hand_edited_copy(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    out = tmp_path / "settings.json"
    _setup_out(monkeypatch, ws, out, scope=tmp_path / "a")
    data = json.loads(out.read_text(encoding="utf-8"))
    hook = data["hooks"]["Stop"][0]["hooks"][0]
    hook["command"] = hook["command"].replace(str(tmp_path / "a"), str(tmp_path / "old"))
    out.write_text(json.dumps(data), encoding="utf-8")
    _setup_out(monkeypatch, ws, out, scope=tmp_path / "a")
    (cmd,) = _stop_commands(out)
    assert str(tmp_path / "old") not in cmd and cmd.count("ATLAS_HOOK_SCOPE=") == 1


# --------------------------------------------------------------------------- (2) two installs

def test_setup_warns_when_a_second_unscoped_install_is_merged(tmp_path, monkeypatch, capsys):
    out = tmp_path / "settings.json"
    _setup_out(monkeypatch, _workspace(tmp_path, "ws1"), out)
    capsys.readouterr()
    _setup_out(monkeypatch, _workspace(tmp_path, "ws2"), out)
    err = capsys.readouterr().err
    assert len(_stop_commands(out)) == 2
    assert "2 Sextant installs" in err and "ATLAS_HOOK_SCOPE" in err


def test_setup_bakes_the_scope_into_both_hook_commands(tmp_path, monkeypatch):
    out = tmp_path / "settings.json"
    _setup_out(monkeypatch, _workspace(tmp_path), out, scope=tmp_path / "code" / "alpha")
    data = json.loads(out.read_text(encoding="utf-8"))
    for event in ("Stop", "SessionStart"):
        assert "ATLAS_HOOK_SCOPE=" in data["hooks"][event][0]["hooks"][0]["command"]


def test_stop_hook_leaves_a_session_outside_its_scope_alone(tmp_path, monkeypatch, capsys):
    ws = _workspace(tmp_path)
    _clean_env(monkeypatch, ws)
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    monkeypatch.setenv("ATLAS_HOOK_SCOPE", str(tmp_path / "code" / "beta"))
    tf = _transcript(tmp_path, "slug-alpha", "sess-A")
    spawned = _run_stop(monkeypatch, {"session_id": "sess-A", "transcript_path": str(tf),
                                      "cwd": str(tmp_path / "code" / "alpha")})
    assert spawned == []
    assert "outside ATLAS_HOOK_SCOPE" in capsys.readouterr().err


def test_stop_hook_summarises_a_session_inside_its_scope(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    _clean_env(monkeypatch, ws)
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    monkeypatch.setenv("ATLAS_DISABLE_TRANSCRIPT_MINING", "1")
    monkeypatch.setenv("ATLAS_HOOK_SCOPE", str(tmp_path / "code" / "alpha"))
    tf = _transcript(tmp_path, "slug-alpha", "sess-B")
    spawned = _run_stop(monkeypatch, {"session_id": "sess-B", "transcript_path": str(tf),
                                      "cwd": str(tmp_path / "code" / "alpha" / "sub")})
    assert any("atlas_plus.summarizer.cli" in a for a in spawned[0])


def test_scope_falls_back_to_the_transcript_slug_without_a_cwd(tmp_path):
    from atlas_plus.hooks.scope import in_scope
    import re
    alpha = tmp_path / "code" / "alpha"
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(alpha.resolve()))
    env = {"ATLAS_HOOK_SCOPE": str(alpha)}
    assert in_scope("", str(tmp_path / "projects" / slug / "s.jsonl"), environ=env)
    assert not in_scope("", str(tmp_path / "projects" / "elsewhere" / "s.jsonl"), environ=env)
    assert not in_scope("", "", environ=env)  # a session placed nowhere is not ours
    assert in_scope("", "", environ={})  # no scope: every session (one install, unchanged)
    assert not in_scope(str(tmp_path / "code" / "alphabet"), "", environ=env)  # a prefix is not a parent


def test_session_start_primes_nothing_outside_its_scope(tmp_path, monkeypatch, capsys):
    ws = _workspace(tmp_path)
    _clean_env(monkeypatch, ws)
    outside = tmp_path / "code" / "alpha"
    outside.mkdir(parents=True)
    monkeypatch.chdir(outside)
    monkeypatch.setenv("ATLAS_HOOK_SCOPE", str(tmp_path / "code" / "beta"))
    from atlas_plus.hooks import session_start
    monkeypatch.setattr(session_start.sys, "argv", ["session-start"])
    session_start.main()
    assert "ATLAS SESSION CONTEXT" not in capsys.readouterr().out


# --------------------------------------------------------------------------- (3) state dir

def test_stop_hook_refuses_when_the_state_dir_is_missing(tmp_path, monkeypatch, capsys):
    ws = tmp_path / "ws"
    (ws / ".memory").mkdir(parents=True)  # the corpus came along, the state dir did not
    _clean_env(monkeypatch, ws)
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    tf = _transcript(tmp_path, "slug", "sess-S")
    spawned = _run_stop(monkeypatch, {"session_id": "sess-S", "transcript_path": str(tf)})
    assert spawned == []
    assert not (ws / ".atlas").exists()
    assert "state dir" in capsys.readouterr().err


def test_gate_typo_with_a_moved_workspace_creates_nothing(tmp_path, monkeypatch, capsys):
    gone = tmp_path / "old-place" / "ws"
    _clean_env(monkeypatch, gone)
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "yes")
    from atlas_plus import activity, paths
    # conftest redirects the feed to a per-test file; this test needs the real resolution
    monkeypatch.setattr(activity, "activity_path", lambda: paths.state_dir() / "logs" / "activity.ndjson")
    tf = _transcript(tmp_path, "slug", "sess-T")
    assert _run_stop(monkeypatch, {"session_id": "sess-T", "transcript_path": str(tf)}) == []
    assert not gone.exists()
    assert "not one of on/1/true" in capsys.readouterr().err


# --------------------------------------------------------------------------- (4) facts gate counts turns

def _turns(n, reply_lines):
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        text = "ask" if role == "user" else "\n".join(f"reply line {k}" for k in range(reply_lines))
        out.append(SimpleNamespace(role=role, content=text))
    return out


def test_facts_gate_counts_turns_not_lines(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    _clean_env(monkeypatch, ws)
    from atlas_plus.curation import suggest_facts as sf
    from atlas_plus.summarizer import loader
    state = {"turns": _turns(8, 4)}
    monkeypatch.setattr(loader, "load_turns", lambda **kw: iter(state["turns"]))
    calls = []
    monkeypatch.setattr(sf, "_extract_candidates", lambda text, **kw: (calls.append(text), ([], 0))[1])
    sf.suggest_facts(conversation_id="sess-F", auto=True, memory_root=ws / ".memory")
    assert len(calls) == 1
    marker = sf._session_done_marker("sess-F")
    assert marker.read_text(encoding="utf-8").split()[0] == "8"  # turns, as the summarizer counts them
    # one more exchange whose reply is twelve lines long: two new turns, below the six-turn gate
    state["turns"] = state["turns"] + _turns(2, 12)
    res = sf.suggest_facts(conversation_id="sess-F", auto=True, memory_root=ws / ".memory")
    assert len(calls) == 1, "one reply re-ran the facts model"
    assert res.get("already_done")
    # six new turns pass the gate
    state["turns"] = state["turns"] + _turns(6, 1)
    sf.suggest_facts(conversation_id="sess-F", auto=True, memory_root=ws / ".memory")
    assert len(calls) == 2


def test_facts_legacy_line_count_marker_runs_once_more(tmp_path, monkeypatch):
    ws = _workspace(tmp_path)
    _clean_env(monkeypatch, ws)
    from atlas_plus.curation import suggest_facts as sf
    from atlas_plus.summarizer import loader
    monkeypatch.setattr(loader, "load_turns", lambda **kw: iter(_turns(8, 4)))
    calls = []
    monkeypatch.setattr(sf, "_extract_candidates", lambda text, **kw: (calls.append(text), ([], 0))[1])
    marker = sf._session_done_marker("sess-L")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("40\n", encoding="utf-8")  # an older build's LINE count, larger than any turn count
    sf.suggest_facts(conversation_id="sess-L", auto=True, memory_root=ws / ".memory")
    assert len(calls) == 1
    assert marker.read_text(encoding="utf-8").strip() == "8 turns"
