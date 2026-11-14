"""Sweep 16 (2026-11-13 evening): the agentic wave's findings against the manual-wave fixes."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest


def _pin_corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.mcp.memory_server import config as _config, routing as _routing, versioning as _versioning
    monkeypatch.setattr(_config, "MEMORY_ROOT", tmp_path / "mem")
    monkeypatch.setattr(_routing, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    monkeypatch.setattr(_routing, "ROUTING_CONFIG", tmp_path / "mem" / "_atlas" / "routing.yaml", raising=False)
    monkeypatch.setattr(_versioning, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    from atlas_plus import init_cmd
    init_cmd.run_init()


# ---------------------------------------------------------------- redaction (AG44 F1/F4/F5, AG46 F4, AG43 F8)
def test_redaction_masks_the_spellings_the_rewrite_missed():
    from atlas_plus.redaction import redact

    v = "Zq9wX7vB3nK1mP4t"
    for s in (f"dbPassword={v}", f"userPassword: {v}", f"AccessToken={v}", f"githubToken: {v}", f"stripeSecretKey={v}",
              f"SECRET_KEY_BASE={v}", f"secret_key_base={v}", f"ADMIN_PASSWORD_HASH={v}", f"api_key_v2={v}",
              f"csrftoken={v}", f"bearer: {v}", f"MY_APP_PROD_EU_WEST_DB_PASSWORD={v}", f"credentials={v}",
              f"--token {v}", f"Authorization: Basic {v}{v}", f"비밀번호={v}", f"비밀번호: {v}", f"시크릿: {v}", f"API키={v}",
              f"密码：{v}", f"密码={v}", f"口令: {v}", f"令牌：{v}", f"密钥={v}", f"シークレット＝{v}",
              f"DATABASE_PASSWORD=hunter2-Tr0ub4dor&3xyz{v}"):
        out = redact(s)
        assert v not in out and "3xyz" not in out.split("REDACTED]")[-1][:4] or v not in out, (s, out)
    # values with spaces and a password holding `@`
    assert redact('password="hunter2 hunter3"') == 'password="[REDACTED]"'
    out = redact("postgres://admin:p@ssZq9w@db.internal:5432/app")
    assert "ssZq9w" not in out and "@db.internal:5432/app" in out
    # still untouched (false positives fixed in sweep 15 and kept)
    for s in ("Raised max_tokens: 4096; tokenizer: bert; the secretary=bob", "passwords_are_hard = true", "password: ********", "PWD=/home/u/project"):
        assert redact(s) == s, s
    # the full-width fold touches only the masked value (AG44: x² became x2)
    s = f"Ran x² tests, ½ done; ＤＢ＿ＰＡＳＳＷＯＲＤ＝{v} then ｶﾅ"
    out = redact(s)
    assert "x²" in out and "½" in out and "ｶﾅ" in out and v not in out
    # linear on big blobs
    for blob in ("x_" * 500_000, "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo" * 30_000, ('{"k": "v", ' * 50_000) + "}"):
        t0 = time.perf_counter()
        redact(blob)
        assert time.perf_counter() - t0 < 2.0


# ---------------------------------------------------------------- facts: honest normalisation, junk raises (AG44 F2, AG43 F5)
def test_norm_fact_keeps_every_script_and_the_signs_that_matter():
    from atlas_plus.curation.suggest_facts import _norm_fact

    assert _norm_fact("The deploy target is web2.") == _norm_fact("the deploy target is web2 <!-- source: agent -->")
    pairs = [("Temperature is 5 C", "Temperature is -5 C"), ("Use C++ for the engine", "Use C for the engine"),
             ("Budget is €1,000", "Budget is $1,000"), ("Температура сервера 5 градусов", "Пароль 5"),
             ("Температура 5", "온도는 5도"), ("Ελάχιστη 5", "Пароль 5")]
    for a, b in pairs:
        assert _norm_fact(a) != _norm_fact(b), (a, b)


def test_extract_candidates_treats_a_non_json_reply_as_a_failed_run(monkeypatch):
    from atlas_plus.curation import suggest_facts as sf
    from atlas_plus.summarizer import providers

    monkeypatch.setattr(providers, "complete", lambda prompt: ("Sure! Here is what happened in the session.", "stub"))
    with pytest.raises(RuntimeError):
        sf._extract_candidates("USER: hello\nASSISTANT: hi", confidence_threshold=0.5)
    monkeypatch.setattr(providers, "complete", lambda prompt: ("[]", "stub"))
    assert sf._extract_candidates("USER: hello", confidence_threshold=0.5) == ([], 0)


def test_review_queue_is_deduplicated_and_facts_lock_is_exclusive(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.curation import suggest_facts as sf

    cand = {"scope": "identity", "fact": "The user's name is Sam", "confidence": 0.9}
    for _ in range(5):
        sf._queue_for_review(dict(cand), "sess-1")
    q = tmp_path / "state" / "suggest-facts" / "review.ndjson"
    assert len([ln for ln in q.read_text(encoding="utf-8").splitlines() if ln.strip()]) == 1
    rel = sf._acquire_facts_lock("sess-9")
    assert callable(rel) and sf._acquire_facts_lock("sess-9") is None
    rel()
    assert callable(sf._acquire_facts_lock("sess-9"))  # released per run, not at process exit
    assert callable(sf._acquire_facts_lock("CON"))  # a Windows device name still locks (AG44)


# ---------------------------------------------------------------- auth accounting (AG44 F3/F14, AG47 F2)
def test_forwarded_identity_must_be_an_ip_and_belongs_to_its_client_only():
    from atlas_plus.auth import AuthFailureLog, forwarded_ip, trusted_peer_matcher

    trusted = trusted_peer_matcher("192.168.96.1")
    assert forwarded_ip({"Cf-Connecting-Ip": "x" * 5000}, "192.168.96.1", trusted=trusted) == ""
    assert forwarded_ip({"Cf-Connecting-Ip": "203.0.113.7"}, "192.168.96.1", trusted=trusted) == "203.0.113.7"
    assert forwarded_ip({"X-Forwarded-For": "junk, 203.0.113.8"}, "127.0.0.1") == "203.0.113.8"
    assert trusted("::ffff:192.168.96.1") and trusted_peer_matcher("fe80::1")("fe80::1%eth0")
    log = AuthFailureLog(window_seconds=60, max_failures_per_window=10)
    now = time.time()
    for _ in range(10):
        log.record(path="/", client_ip="192.168.96.1", cf_ip="203.0.113.7", reason="bad_key", now=now)
    assert log.is_rate_limited("203.0.113.7", now=now)
    assert not log.is_rate_limited("192.168.96.1", now=now)        # the operator on the gateway is not locked by X (AG47)
    assert log.failures_via_peer("192.168.96.1", now=now) == 10      # but the proxy's own flood is counted (AG44)


def test_dashboard_peer_flood_rule_and_ticket_refs_are_in_the_source():
    import inspect

    from atlas_plus.web import app as web

    src = inspect.getsource(web._check_auth)
    assert "failures_via_peer(client_ip) >= 10 * AUTH_FAILURES.max_failures_per_window" in src
    assert web._norm_ref("alp-b/alp-001") == "alp-b/ALP-001" and web._norm_ref("alp-001") == "ALP-001"
    assert web._tref(SimpleNamespace(id="ALP-001", project="alp-a")) == "alp-a/ALP-001"
    page = inspect.getsource(web)
    assert "(location.protocol==='https:'?'wss://':'ws://')" in page
    assert '"/api/tickets/{ticket_id:path}/status"' in page and '"/api/operator/{ticket_id:path}/complete"' in page
    assert "textarea[data-dirty" in page and "tk-requires" in page


def test_dashboard_wrong_ticket_fixed_end_to_end(tmp_path, monkeypatch):
    """Two projects hold ALP-001: the board carries project/ID and the status route changes the right file."""
    _pin_corpus(tmp_path, monkeypatch)
    from atlas_plus.tickets import store
    monkeypatch.setattr(store, "TICKETS_ROOT", tmp_path / "tix")
    for proj, title in (("alp-a", "Legacy A one"), ("alp-b", "Legacy B one")):
        (tmp_path / "tix" / proj).mkdir(parents=True)
        (tmp_path / "tix" / proj / "ALP-001.md").write_text(f"---\nid: ALP-001\ntitle: {title}\nproject: {proj}\nstatus: todo\nassignee: human\npriority: normal\n---\n", encoding="utf-8")
    from fastapi.testclient import TestClient
    from atlas_plus.web import app as web
    monkeypatch.setattr(web, "API_KEY", "k" * 40, raising=False)
    client = TestClient(web.app)
    h = {"Authorization": "Bearer " + "k" * 40}
    r = client.patch("/api/tickets/alp-a/ALP-001/status", json={"status": "done"}, headers=h)
    assert r.status_code == 200, r.text
    assert "status: done" in (tmp_path / "tix" / "alp-a" / "ALP-001.md").read_text(encoding="utf-8")
    assert "status: todo" in (tmp_path / "tix" / "alp-b" / "ALP-001.md").read_text(encoding="utf-8")
    d = client.get("/api/tickets/alp-b/ALP-001/detail", headers=h)
    assert d.status_code == 200 and "Legacy B one" in d.text and "alp-b/ALP-001" in d.text
    # a browser at `/` with a dead session gets the expired page, API callers keep JSON (AG45)
    r = client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 401 and "<html" in r.text.lower()
    r = client.get("/", headers={"Accept": "application/json"})
    assert r.status_code == 401 and r.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------- summarizer refresh (AG43 F1/F2/F3, AG44 F7)
def test_summary_refresh_replaces_updates_entry_and_crosses_midnight(tmp_path, monkeypatch):
    from atlas_plus.summarizer import renderer as r

    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    sdir = tmp_path / "sessions"
    monkeypatch.setattr(r, "MEMORY_SESSIONS_DIR", sdir)
    monkeypatch.setattr(r, "UPDATES_TXT", tmp_path / "updates.txt")
    monkeypatch.setattr(r, "_version_file", lambda p: None)
    (tmp_path / "updates.txt").write_text("header\n====\n\nold entry\n\n", encoding="utf-8")
    r.write_outputs("## 2026-11-13 - first\n<!-- session:abc123 turns:6 -->\nsix\n", "2026-11-13 first\n<!-- session:abc123 turns:6 -->\nsix\n", "2026-11-13", session_marker="<!-- session:abc123 turns:6 -->")
    r.write_outputs("## 2026-11-13 - other\n<!-- session:abc123-def turns:3 -->\nother\n", "2026-11-13 other\n<!-- session:abc123-def turns:3 -->\nother\n", "2026-11-13", session_marker="<!-- session:abc123-def turns:3 -->")
    # the next day: the grown session moves, the look-alike id and the unrelated entry survive
    r.write_outputs("## 2026-11-14 - first, grown\n<!-- session:abc123 turns:30 -->\nthirty\n", "2026-11-14 grown\n<!-- session:abc123 turns:30 -->\nthirty\n", "2026-11-14",
                    session_marker="<!-- session:abc123 turns:30 -->", replace_session="<!-- session:abc123")
    d13 = (sdir / "2026-11-13.md").read_text(encoding="utf-8")
    d14 = (sdir / "2026-11-14.md").read_text(encoding="utf-8")
    assert "<!-- session:abc123 " not in d13 and "<!-- session:abc123-def turns:3 -->" in d13
    assert d14.count("<!-- session:abc123 turns:30 -->") == 1
    upd = (tmp_path / "updates.txt").read_text(encoding="utf-8")
    assert upd.count("<!-- session:abc123 ") == 1 and "thirty" in upd and "six" not in upd and "old entry" in upd and "other" in upd


def test_plain_text_model_reply_is_not_a_summary():
    from atlas_plus.summarizer import cli as scli

    s = scli._parse_json_response("Sure! Here is what happened in the session. Things went well.")
    assert not scli._looks_like_summary(s)
    assert scli._looks_like_summary({"title": "Fixed the build", "completed": ["x"]})


# ---------------------------------------------------------------- sync: hub config pulls (AG47 F1)
def test_hub_configuration_is_refused_on_the_hub_only():
    from atlas_plus.sync import policy

    assert policy.deny_reason("_atlas/routing.yaml", None, "write", hub=True) == "hub configuration (edit it on the hub)"
    assert policy.deny_reason("_atlas/routing.yaml", None, "write") is None        # a device applying a pull
    assert policy.deny_reason("_atlas/routing.yaml", None, "read") is None


# ---------------------------------------------------------------- console (AG46 F1)
def test_write_text_out_uses_the_console_stream_on_a_tty_and_utf8_for_hooks(monkeypatch):
    import io
    import sys

    from atlas_plus import _console

    class TTY(io.StringIO):
        def isatty(self):
            return True
    out = TTY()
    monkeypatch.setattr(sys, "stdout", out)
    _console.write_text_out("会議 café")
    assert out.getvalue() == "会議 café\n"

    class Pipe:
        def __init__(self):
            self.buffer = io.BytesIO()
        def isatty(self):
            return False
    pipe = Pipe()
    monkeypatch.setattr(sys, "stdout", pipe)
    _console.write_text_out("会議", hook=True)
    assert pipe.buffer.getvalue() == "会議\n".encode("utf-8")


def test_memory_confirm_refuses_a_conflict_marked_note(tmp_path, monkeypatch):
    _pin_corpus(tmp_path, monkeypatch)
    from atlas_plus.mcp.memory_server.tools import read as _read, remember as _rem
    monkeypatch.setattr(_read, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    note = tmp_path / "mem" / "projects" / "atlas.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("---\nid: atlas\nlast_confirmed: 2026-11-01\n---\n<<<<<<< HEAD\na\n=======\nb\n>>>>>>> theirs\n", encoding="utf-8")
    res = _rem.memory_confirm("projects/atlas.md", interactive=True)
    assert res["ok"] is False and "conflict markers" in res["error"]
    assert "last_confirmed: 2026-11-01" in note.read_text(encoding="utf-8")


def test_project_name_sentence_shows_the_characters_and_requires_is_stripped(tmp_path):
    from atlas_plus.tickets import store

    with pytest.raises(ValueError) as ei:
        store.create_ticket(title="t", project="東京", tickets_root=tmp_path, interactive=True)
    assert "'東京'" in str(ei.value) and "\\u6771" not in str(ei.value)
    t = store.create_ticket(title="t", project="proj", tickets_root=tmp_path, interactive=True, requires="ATL-001\n")
    assert t.requires == "ATL-001"


def test_colon_in_a_query_is_a_word_break():
    from atlas_plus.mcp.memory_server import fts

    assert fts._prepare_query("alpha:beta") == "alpha beta"
    assert ":" not in fts._prepare_query("会議:東京")
