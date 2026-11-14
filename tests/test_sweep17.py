"""Sweep 17 (2026-11-13 night): the agentic wave's findings against the sweep-16 fixes."""
from __future__ import annotations

import inspect

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


# ---------------------------------------------------------------- redaction (AG49 F1/F8/F20, AG52 F7)
def test_colon_values_at_line_start_are_secrets_and_prose_is_not():
    from atlas_plus.redaction import redact

    v = "correcthorsebatterystaple"
    for s in (f"password: {v}", f"DB_PASSWORD: {v}", f"[db]\npassword: {v}", f"environment:\n  POSTGRES_PASSWORD: {v}",
              f"stringData:\n  password: {v}", f"secret_key_base: {v}", f"password：{v}", f"- password: {v}", f"\"password\": \"{v}\""):
        assert v not in redact(s), s
    for s in ("The token: refreshing the page", "pass: yes we did", "bypass = true_if_admin", "compass = northwest_bearing",
              "Note the password: changeme is a placeholder we rotate", "PWD=/home/u"):
        assert redact(s) == s, s
    out = redact("db:\n  password: |\n    secretline\n  host: dbhost.internal\n  port: 5432\n")
    assert "secretline" not in out and "host: dbhost.internal" in out and "port: 5432" in out   # siblings survive (AG49)
    for s in ("كلمة_السر=Zq9wX7vB3nK1", "סיסמה=Zq9wX7vB3nK1", "รหัสผ่าน=Zq9wX7vB3nK1", "पासवर्ड=Zq9wX7vB3nK1", "暗証番号: Zq9wX7vB3nK1", "비밀키: Zq9wX7vB3nK1"):
        assert "Zq9wX7vB3nK1" not in redact(s), s


def test_valid_ip_handles_bracketed_v6_without_port():
    from atlas_plus.auth import _valid_ip

    assert _valid_ip("[::1]") == "::1" and _valid_ip("[::1]:443") == "::1"


# ---------------------------------------------------------------- dashboard (AG49 F2/F9/F10)
def test_dashboard_dead_session_stops_the_chat_socket_and_hidden_drafts_count():
    from atlas_plus.web import app as web

    page = inspect.getsource(web)
    assert "if(!window._sextantExpired)setTimeout(cwS,3000)" in page
    assert "function cwS(){{\n  if(window._sextantExpired)return;" in page
    assert "some(t=>t.value.trim());" in page and "offsetParent" not in page.split("const dirty=")[1].split("\n")[0]


def test_tref_uses_the_file_location_and_bare_id_prefers_the_root_ticket(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from atlas_plus.tickets import store
    from atlas_plus.web import app as web

    monkeypatch.setattr(store, "TICKETS_ROOT", tmp_path)
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "ALP-001.md").write_text("---\nid: ALP-001\ntitle: Alpha ticket one\nproject: alpha\nstatus: todo\n---\n", encoding="utf-8")
    (tmp_path / "ALP-001.md").write_text("---\nid: ALP-001\ntitle: ROOT-LEVEL moved copy\nproject: alpha\nstatus: todo\n---\n", encoding="utf-8")
    root_t = SimpleNamespace(id="ALP-001", project="alpha", path=tmp_path / "ALP-001.md")
    folder_t = SimpleNamespace(id="ALP-001", project="alpha", path=tmp_path / "alpha" / "ALP-001.md")
    assert web._tref(root_t) == "ALP-001" and web._tref(folder_t) == "alpha/ALP-001"
    assert store.get_ticket("ALP-001", tickets_root=tmp_path).title == "ROOT-LEVEL moved copy"
    assert store.get_ticket("alpha/ALP-001", tickets_root=tmp_path).title == "Alpha ticket one"


# ---------------------------------------------------------------- summarizer (AG49 F3/F4/F11)
def test_only_chunk_summary_shaped_replies_are_cached_and_items_are_one_line():
    from atlas_plus.summarizer import cli as scli
    from atlas_plus.summarizer.renderer import remove_updates_entry

    assert not scli._looks_like_chunk_summary(["a", "b"])
    assert not scli._looks_like_chunk_summary({"echo": "You are summarizing chunk 1 of 1"})
    assert not scli._looks_like_chunk_summary(scli._parse_json_response("Sure! here"))
    assert scli._looks_like_chunk_summary({"worked_on": ["x"], "completed": []})
    src = inspect.getsource(scli.main)
    assert "_looks_like_chunk_summary(_parse_json_response(text))" in src and '" ".join(str(x).split())' in src
    upd = ("header\n====\n\n----------\n<!-- session:abc turns:6 -->\n2026-11-12 | sess\n----------\nCOMPLETED:\n  - a\n"
           "NOTES:\n  my own note\n----------\n2026-11-12 | earlier\n----------\nNEXT:\n  - c\n\n")
    out = remove_updates_entry(upd, "<!-- session:abc")
    assert "NOTES:" in out and "my own note" in out and "- a" not in out and "earlier" in out


# ---------------------------------------------------------------- doctor, hub, client (AG49 F5/F6/F13/F14)
def test_doctor_text_shows_unmerged_and_hub_warns_per_peer_and_client_sanitises():
    from atlas_plus.doctor import cli as doc
    from atlas_plus.sync import api, cli as scli

    assert '"unmerged_hint"' in inspect.getsource(doc)
    assert '"core.quotePath=false", "ls-files", "-u", "-z"' in inspect.getsource(doc)
    assert "_wk = peer_ip if fwd_ip else _src" in inspect.getsource(api)
    assert "[:300]" in inspect.getsource(scli)


# ---------------------------------------------------------------- facts and duplicates tokenise every script (AG52 F1/F2, AG49 F15)
def test_fact_dedupe_and_duplicate_scan_see_non_latin_text(tmp_path, monkeypatch):
    _pin_corpus(tmp_path, monkeypatch)
    from atlas_plus.curation import duplicates as dup, suggest_facts as sf
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    ar = "الخادم الرئيسي يعمل على المنفذ ثمانية آلاف"
    th = "เซิร์ฟเวอร์หลักทำงานบนพอร์ตแปดพัน"
    memory_remember("reference:dd-ar", ar, source="agent")
    memory_remember("reference:dd-th", th, source="agent")
    assert sf._fact_already_there("reference:dd-ar", ar)                       # exact repeat is present (AG52: never fired without ASCII)
    assert not sf._fact_already_there("reference:dd-ar", ar + " ليس")           # a negation is a different fact
    assert sf._fact_already_there("reference:dd-th", th)
    a = dup._tokenize("---\nid: x\n---\n" + ar + " <!-- source:human-direct -->\n")
    b = dup._tokenize("---\nid: y\n---\n" + th + " <!-- source:human-direct -->\n")
    assert a and b and not (a & b)                                              # unrelated notes share no tokens (they scored 1.00 before)
    assert "source" not in a and "human" not in a
    from atlas_plus.summarizer import providers
    monkeypatch.setattr(providers, "complete", lambda prompt: ('{"fact": "the billing table has no facts column yet", "scope": "pr', "stub"))
    with pytest.raises(RuntimeError):
        sf._extract_candidates("USER: x", confidence_threshold=0.5)             # a truncated object is a failed run (AG49)


# ---------------------------------------------------------------- search: Thai runs and Arabic marks (AG52 F3/F4)
def test_search_splits_thai_runs_and_folds_arabic_marks():
    from atlas_plus.mcp.memory_server import fts

    q = fts._prepare_query("บ้าน")
    assert fts._CJK_SEP in q                                                     # a Thai run is a phrase of its characters, like CJK
    assert fts._prepare_query("الْخَادِمُ") == fts._prepare_query("الخادم")       # vocalised and plain meet
    assert fts._norm_index_text("الْخَادِمُ الرئيسي") == fts._norm_index_text("الخادم الرئيسي")
    assert fts._CJK_SEP in fts._norm_index_text("ฉันกินข้าวที่ร้านอาหาร")


# ---------------------------------------------------------------- versioning (AG49 F16)
def test_drift_refuses_a_dev_build_of_a_released_version(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("bump_version", Path(__file__).resolve().parents[1] / "scripts" / "bump_version.py")
    bump = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bump)
    py = tmp_path / "pyproject.toml"; ini = tmp_path / "__init__.py"; cl = tmp_path / "CHANGELOG.md"; stamp = tmp_path / "_build.py"
    py.write_text('[project]\nname = "x"\nversion = "0.1.0.dev5"\n', encoding="utf-8")
    ini.write_text('def _installed_version():\n    return "0.1.0"\n', encoding="utf-8")
    cl.write_text("# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-11-13\n\n- released\n", encoding="utf-8")
    stamp.write_text('SOURCE = "abc1234"\n', encoding="utf-8")
    for name, val in (("PYPROJECT", py), ("INIT", ini), ("CHANGELOG", cl), ("BUILD_STAMP", stamp)):
        monkeypatch.setattr(bump, name, val)
    assert any("already released" in p for p in bump.drift())
    py.write_text('[project]\nname = "x"\nversion = "0.1.1.dev5"\n', encoding="utf-8")
    ini.write_text('def _installed_version():\n    return "0.1.1"\n', encoding="utf-8")
    assert bump.drift() == []


# ---------------------------------------------------------------- AG50: echoes, fenced quotes, doctor events; AG51: breaker, phone columns
def test_echoed_prompt_is_never_a_summary_and_fenced_quotes_survive():
    from atlas_plus.summarizer import cli as scli
    from atlas_plus.summarizer.renderer import remove_session_entry, remove_updates_entry

    echo = {"worked_on": ["short description of each thing worked on"], "completed": ["things that were finished and confirmed working"]}
    assert not scli._looks_like_chunk_summary(echo) and not scli._looks_like_summary(dict(echo, title="x"))
    assert scli._looks_like_chunk_summary({"worked_on": ["fixed the build"]})
    log = ("## 2026-11-12 - my notes\n\n```\n## 2026-11-12 - example\n<!-- session:hw turns:4 -->\nquoted\n```\nKeep this section, it is mine.\n\n"
           "## 2026-11-12 - real\n<!-- session:hw turns:4 -->\nreal body\n")
    out = remove_session_entry(log, "<!-- session:hw")
    assert "Keep this section, it is mine." in out and "quoted" in out and "real body" not in out
    upd = ("h\n====\n\n----------\n<!-- session:a turns:6 -->\n2026-11-12 | sess\n----------\nCOMPLETED:\n  - a\n2026-11-13 | standup notes from the team\n  - mine\n\n")
    out2 = remove_updates_entry(upd, "<!-- session:a")
    assert "standup notes" in out2 and "- mine" in out2 and "- a" not in out2


def test_doctor_facts_events_gate_value_and_hook_traces():
    from atlas_plus.doctor import cli as doc
    from atlas_plus.summarizer import hook_entry

    src = inspect.getsource(doc)
    assert '"suggest_facts_failed", "fact_needs_review"' in src and "is not one of on/1/true" in src
    h = inspect.getsource(hook_entry)
    assert "suggest_facts_skipped" in h and "payload has no session_id" in h


def test_facts_list_of_non_dicts_is_junk(monkeypatch):
    from atlas_plus.curation import suggest_facts as sf
    from atlas_plus.summarizer import providers

    monkeypatch.setattr(providers, "complete", lambda prompt: ("[1, 2, 3]", "stub"))
    with pytest.raises(RuntimeError):
        sf._extract_candidates("USER: x", confidence_threshold=0.5)


def test_index_format_tag_forces_a_rebuild_and_breaker_is_wired():
    from atlas_plus.mcp.memory_server import fts
    from atlas_plus.web import app as web

    assert fts.INDEX_FORMAT == "2" and "index_format" in inspect.getsource(fts._needs_rebuild)
    page = inspect.getsource(web)
    assert page.count("= httpx.AsyncClient(") == 1 and "async with httpx.AsyncClient(" not in page and "_PeerClient(" in page   # the one real client lives inside the breaker wrapper
    assert ".tk-id{{width:3.6rem" in page and "scrollIntoView" in page and "_same_bytes_as_disk" in page


@pytest.mark.asyncio
async def test_peer_client_breaker_skips_a_dead_host_for_a_while():
    import httpx

    from atlas_plus.web import app as web

    web._PeerClient._down.clear()
    async with web._PeerClient(timeout=httpx.Timeout(2.0, connect=0.5)) as c:
        with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
            await c.get("http://127.0.0.1:9/health")          # a dead port
        import time
        t0 = time.perf_counter()
        with pytest.raises(httpx.ConnectError) as ei:
            await c.get("http://127.0.0.1:9/health")
        assert time.perf_counter() - t0 < 0.1 and "circuit breaker" in str(ei.value)
    web._PeerClient._down.clear()


# ---------------------------------------------------------------- AG54: bitset scan, shared TLS context, dash-dense redaction, cache kept on failure
def test_bitset_scan_matches_set_intersection(tmp_path, monkeypatch):
    import itertools
    import random

    from atlas_plus.curation import duplicates as dup

    rng = random.Random(54)
    vocab = [f"w{i}" for i in range(400)]
    root = tmp_path / "mem"
    (root / "reference").mkdir(parents=True)
    texts = {}
    for i in range(150):
        words = [rng.choice(vocab) for _ in range(rng.randint(15, 50))]
        texts[f"n{i}.md"] = " ".join(words)
        if i % 5 == 0 and i:
            texts[f"c{i}.md"] = " ".join(words[: len(words) * 9 // 10]) + " uniqueword" + str(i)
    for name, body in texts.items():
        (root / "reference" / name).write_text("---\nid: x\n---\n" + body + "\n", encoding="utf-8")
    monkeypatch.setattr(dup, "MEMORY_ROOT", root)
    pairs = dup.find_duplicates(memory_root=root, threshold=0.5)
    got = {(min(p.file1, p.file2).rsplit("/", 1)[-1].rsplit(chr(92), 1)[-1], max(p.file1, p.file2).rsplit("/", 1)[-1].rsplit(chr(92), 1)[-1]): round(p.similarity, 6) for p in pairs}
    toks = {n: dup._tokenize("---\nid: x\n---\n" + b + "\n") for n, b in texts.items()}
    brute = {}
    for a, b in itertools.combinations(sorted(texts), 2):
        ta, tb = toks[a], toks[b]
        if ta and tb:
            s = len(ta & tb) / len(ta | tb)
            if s >= 0.5:
                brute[(min(a, b), max(a, b))] = round(s, 6)
    assert brute and got == brute                                                  # the bitset popcount is the set intersection, exactly


def test_shared_tls_context_cache_kept_and_dash_lookbehind():
    import time

    from atlas_plus.llm import ollama
    from atlas_plus.redaction import redact
    from atlas_plus.summarizer import cli as scli
    from atlas_plus.web import app as web

    assert web._shared_ssl_context() is web._shared_ssl_context()
    assert ollama._shared_ssl_context() is ollama._shared_ssl_context()
    assert 'kwargs.setdefault("verify", _shared_ssl_context())' in inspect.getsource(web._PeerClient.__init__)
    assert "_save_chunk_cache(safe_id, used_cache)  # the chunks already summarised are kept" in inspect.getsource(scli.main)
    big = "pass-" * 200_000
    t0 = time.perf_counter()
    redact(big)
    assert time.perf_counter() - t0 < 2.0                                            # dash-dense text (AG54: 3.75 s per MB)
    v = "Zq9wX7vB3nK1"
    assert v not in redact("X-Api-" + "Key: " + v) and v not in redact("a-b-c-" + "password=" + v)  # assembled: the pre-commit scan reads literal key lines as leaks

