"""MN81 (sweep 23 manual wave): the Stop hook on a long session, day eleven.

1. With something new, the facts stage redacted EVERY turn of the session to send the model the last
   6,000 characters (103 s and 218 MB per Stop on a 60 MB, 34,000-turn session). It now renders only a
   suffix comfortably longer than the window, and the window is the same text.
2. The mining cache took a same-size in-place rewrite for "grown by nothing" and kept the old counts
   (the CHANGELOG says a rewritten transcript is read whole).
3. Without a payload cwd, Stop decided ATLAS_HOOK_SCOPE on the transcript's project folder and
   SessionStart on the process cwd: one session primed by nobody and summarised by this install.
"""
from __future__ import annotations

import io
import json
import os
import re

import pytest

from tests.test_ag87 import (  # noqa: F401  (fixtures and helpers of the AG87 tests)
    _clean_env, _fact_texts, _mine, _mining_env, _session_start, _turn, _workspace,
)


# --------------------------------------------------------------------------- (1) facts: the window only

def _long_session(tmp_path, monkeypatch, n):
    ws = _workspace(tmp_path)
    _clean_env(monkeypatch, ws)
    cc = tmp_path / "cc" / "projects"
    (cc / "p").mkdir(parents=True)
    t = cc / "p" / "sess.jsonl"
    lines = [_turn(i, "sess") for i in range(n)]
    secret = [json.dumps({"type": "user", "uuid": "s1", "sessionId": "sess", "timestamp": "2026-11-14T10:00:00Z",
                          "message": {"role": "user", "content": "and the password is"}}) + "\n",
              json.dumps({"type": "user", "uuid": "s2", "sessionId": "sess", "timestamp": "2026-11-14T10:00:00Z",
                          "message": {"role": "user", "content": "Natsu2024"}}) + "\n"]
    t.write_text("".join(lines[:-6] + secret + lines[-6:]), encoding="utf-8")
    from atlas_plus.summarizer import loader
    monkeypatch.setattr(loader, "CLAUDE_CODE_ROOT", cc)
    monkeypatch.setenv("ATLAS_CLAUDE_CODE_ROOT", str(cc))
    monkeypatch.setenv("ATLAS_TRANSCRIPT_FILE", str(t))
    return ws


def test_facts_with_something_new_redacts_only_what_the_window_can_reach(tmp_path, monkeypatch):
    _long_session(tmp_path, monkeypatch, 3000)
    from atlas_plus import redaction
    from atlas_plus.curation import suggest_facts as sf
    calls = []
    real = redaction.redact
    monkeypatch.setattr(redaction, "redact", lambda s, *a, **k: (calls.append(1), real(s, *a, **k))[1])
    text = sf._load_transcript_text(conversation_id="sess")
    assert len(calls) < 1000, f"{len(calls)} redaction calls for a 3,000-turn session"
    assert "Natsu2024" not in text  # the cross-turn carry still masks a value on the next turn
    calls.clear()
    monkeypatch.setattr(sf, "_WINDOW", 10 ** 12, raising=False)  # force the whole render
    whole = sf._load_transcript_text(conversation_id="sess")
    assert len(calls) > 2900
    assert len(text) >= 24000 and text[-24000:] == whole[-24000:]


def test_facts_on_a_short_session_render_whole(tmp_path, monkeypatch):
    _long_session(tmp_path, monkeypatch, 40)
    from atlas_plus.curation import suggest_facts as sf
    text = sf._load_transcript_text(conversation_id="sess")
    assert text.startswith("USER: turn 0 ") and "Natsu2024" not in text


# --------------------------------------------------------------------------- (2) mining: same-size rewrite

def test_mining_rereads_a_same_size_rewrite(_mining_env):
    root, t, hashed, tmp = _mining_env
    _mine(root, tmp / "g")
    b = t.read_bytes()
    b2 = b.replace(b'"name": "Read"', b'"name": "Grep"', 2)  # early lines, outside the 4 KB tail
    assert len(b2) == len(b) and b2 != b
    st = os.stat(t)
    t.write_bytes(b2)
    os.utime(t, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    _, prov, _ = _mine(root, tmp / "g")
    _, fresh, _ = _mine(root, tmp / "fresh")
    assert _fact_texts(prov) == _fact_texts(fresh)


# --------------------------------------------------------------------------- (3) scope parity, no payload cwd

def _stop(monkeypatch, payload):
    from atlas_plus.summarizer import hook_entry as he
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(he, "_summarizer_unavailable_reason", lambda: "")
    monkeypatch.setattr(he, "_capture_fsdiffs", lambda: None)
    spawned = []
    monkeypatch.setattr(he.subprocess, "Popen", lambda args, **kw: spawned.append(args))
    monkeypatch.setattr(he.sys, "argv", ["hook"])
    assert he.main() == 0
    return bool(spawned)


@pytest.mark.parametrize("proc_in_scope", [False, True])
def test_both_hooks_make_one_decision_without_a_payload_cwd(tmp_path, monkeypatch, capsys, proc_in_scope):
    ws = _workspace(tmp_path)
    _clean_env(monkeypatch, ws)
    inside = tmp_path / "code" / "beta"
    outside = tmp_path / "code" / "alpha"
    inside.mkdir(parents=True)
    outside.mkdir(parents=True)
    monkeypatch.setenv("ATLAS_HOOK_SCOPE", str(inside))
    cc = tmp_path / "cc" / "projects"
    folder = cc / re.sub(r"[^A-Za-z0-9]", "-", str(inside.resolve()))
    folder.mkdir(parents=True)
    t = folder / "s.jsonl"
    t.write_text("".join(_turn(i, "s") for i in range(8)), encoding="utf-8")
    monkeypatch.chdir(inside if proc_in_scope else outside)
    payload = {"session_id": "s", "transcript_path": str(t)}  # an older client: no cwd
    stop_acts = _stop(monkeypatch, payload)
    capsys.readouterr()
    primed = "ATLAS SESSION CONTEXT" in _session_start(monkeypatch, payload, capsys).out
    assert stop_acts == primed, f"Stop acted={stop_acts}, SessionStart primed={primed}"
