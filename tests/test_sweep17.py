"""Sweep 17 (2026-11-13 night): the agentic wave's findings against the sweep-16 fixes."""
from __future__ import annotations

import inspect
import sys

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
    assert "if(!window._sextantExpired&&!window._sextantStopped)setTimeout(cwS,3000)" in page
    assert "function cwS(){{\n  if(window._sextantExpired||window._sextantStopped)return;" in page
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
    assert "_looks_like_chunk_summary(_parsed)" in src and '" ".join(str(x).split())' in src
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
    assert "more than 5 clients via %s are locked out this window" in inspect.getsource(api)
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
    assert '"suggest_facts_failed", "suggest_facts_skipped", "fact_needs_review"' in src and "is not one of on/1/true" in src
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

    assert fts.INDEX_FORMAT == "3" and fts._index_format_tag() in ("", "3", None)
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


# ---------------------------------------------------------------- MN46: format tag on the search path, marks folded, more key names, RTL
def test_index_format_is_honoured_on_the_search_path(tmp_path, monkeypatch):
    _pin_corpus(tmp_path, monkeypatch)
    from atlas_plus.mcp.memory_server import fts
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "search.sqlite", raising=False)
    monkeypatch.setattr(fts, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    memory_remember("reference:thai", "ฉันกินข้าวที่ร้านอาหารใกล้บ้านทุกวัน", source="agent")
    fts.rebuild(force=True)
    conn = fts._connect()
    conn.execute("DELETE FROM atlas_meta WHERE key='index_format'")  # an index built by a build before the tag
    conn.commit()
    conn.close()
    fts.update_incremental()                                                   # the search path, not the rebuild command
    conn = fts._connect()
    row = conn.execute("SELECT value FROM atlas_meta WHERE key='index_format'").fetchone()
    conn.close()
    assert row and row[0] == fts.INDEX_FORMAT


def test_duplicate_tokens_survive_vowel_marks_and_more_key_names_are_masked():
    from atlas_plus.curation import duplicates as dup
    from atlas_plus.redaction import redact

    ar_voc = dup._tokenize("---\nid: a\n---\nالْخَادِمُ الرَّئِيسِيُّ يَعْمَلُ عَلَى الْمَنْفَذِ\n")
    hi = dup._tokenize("---\nid: b\n---\nमैंने उसे बदला और सर्वर को फिर से शुरू किया\n")
    assert len(ar_voc) >= 3 and len(hi) >= 2 and not (ar_voc & hi)             # real tokens, nothing shared (MN46: both were {'2026'})
    v = "Zq8vN3pLx7Rw"
    for s in (f"مفتاح_API={v}", f"رمز_الوصول: {v}", f"السر: {v}", f"كَلِمَةُ السِّرِّ = {v}", f"كَلِمَةُ_السِّرِّ={v}",
              f"טוקן: {v}", f"סוד: {v}", f"סִיסְמָה={v}", f"โทเคน={v}", f"รหัสลับ={v}", f"टोकन: {v}", f"गुप्त कुंजी: {v}"):
        assert v not in redact(s), s


def test_codec_dump_named_in_the_result_branch_and_rtl_dir_present():
    from atlas_plus import cli
    from atlas_plus.curation import suggest_facts as sf
    from atlas_plus.web import app as web

    assert "codec can't encode" in inspect.getsource(cli)
    assert len(sf._tokenize("الْخَادِمُ الرَّئِيسِيُّ يَعْمَلُ عَلَى الْمَنْفَذِ")) >= 3  # marks stay inside words (AG60)
    page = inspect.getsource(web)
    assert 'id="mf-edit" dir="auto"' in page and page.count('dir="auto"') >= 3


# ---------------------------------------------------------------- MN43: the seven regressions
def test_dashed_flags_and_camel_pass_names_mask_again():
    from atlas_plus.redaction import redact

    v = "Qm7rT2kW9pLx"
    for s in (f"--secret-key={v}", f"--access-key={v}", f"--my-app-prod-db-password={v}", f"java -Dpassword={v}",
              f"-DdbPassword={v}", f"-DDB_PASSWORD={v}", f"-ApiKey:{v}", f"-Token:{v}", f"dbPass={v}", f"dbPwd: {v}",
              f"rootPass={v}", f"adminPwd={v}", f"PGPASS={v}", f"MYSQLPWD={v}", f"smtpPass: {v}"):
        assert v not in redact(s), s
    for s in ("bypass = true", "compass: north", "pass: 3 of 4"):
        assert redact(s) == s, s


def test_prose_rule_is_linear_on_a_long_single_line():
    import time
    from atlas_plus.redaction import redact

    line = "The token: refreshing the page " * 8000                                   # 256 KB, no newline
    t0 = time.perf_counter()
    redact(line)
    assert time.perf_counter() - t0 < 2.0                                           # 26 s before MN43


def test_bitset_table_holds_indexes_and_hebrew_punctuation_survives_the_fold():
    from atlas_plus.curation import duplicates as dup
    from atlas_plus.mcp.memory_server import fts

    src = inspect.getsource(dup)
    assert "shared_bit[tok] = len(shared_bit)" in src and "ba[bit >> 3] |= 1 << (bit & 7)" in src
    assert fts._fold_marks("\u05d1\u05d9\u05ea\u05be\u05e1\u05e4\u05e8") == "\u05d1\u05d9\u05ea\u05be\u05e1\u05e4\u05e8"   # maqaf kept
    assert fts._fold_marks("\u05e9\u05c1\u05b8\u05dc\u05d5\u05b9\u05dd") == "\u05e9\u05dc\u05d5\u05dd"                     # points folded
    assert fts.INDEX_FORMAT == "3"


def test_unchanged_large_save_condition_is_live_and_breaker_clears_on_any_answer():
    from atlas_plus.web import app as web

    src = inspect.getsource(web)
    assert 'if len(content.encode("utf-8")) > 1_048_576 and not _same_bytes_as_disk(content, safe_path):' in src
    assert "self._down.pop(host, None)" in src
    assert "-m atlas_plus.brain.server</code>" not in src


def test_echo_needs_two_markers_and_fences_and_back_to_back_entries():
    from atlas_plus.summarizer.cli import _is_prompt_echo
    from atlas_plus.summarizer.renderer import remove_session_entry, remove_updates_entry

    assert not _is_prompt_echo({"completed": ["removed 'Think step by step' from the prompt"]})
    assert not _is_prompt_echo({"completed": ["You are summarizing", "things that were finished"]})  # AG58: two bare phrases are a quotation; the echo test is six whole template lines
    from atlas_plus.summarizer.prompts import map_prompt as _mp, reduce_prompt as _rp
    assert _is_prompt_echo({"completed": [_mp("t", 0, 2)]}) and _is_prompt_echo({"completed": [_rp(["a", "b"], "d")]})
    quoted = "~~~\n## x\n<!-- session:abc turns:1 -->\n~~~\n"
    real = "## real\n<!-- session:abc turns:1 -->\nbody\n"
    out = remove_session_entry(quoted + real, "<!-- session:abc")
    assert quoted in out and "## real" not in out
    out = remove_session_entry("```\nq\n```\n" + real, "<!-- session:abc")
    assert "## real" not in out                                                    # a file that starts with a fence
    a = "-----\n<!-- session:abc turns:8 -->\n2026-11-13 | A\n-----\nCOMPLETED:\n  - x\n"
    b = "-----\n<!-- session:def turns:8 -->\n2026-11-13 | B\n-----\nCOMPLETED:\n  - y\n"
    out = remove_updates_entry(a + b, "<!-- session:abc")
    assert out == b                                                                # the second entry keeps its opening rule
    assert remove_updates_entry(out, "<!-- session:def") == ""


# ---------------------------------------------------------------- MN44, MN45, MN47
def test_stopped_page_does_not_retry_and_garbled_payload_is_refused(monkeypatch, capsys):
    import io
    from atlas_plus.summarizer import hook_entry
    from atlas_plus.web import app as web

    src = inspect.getsource(web)
    assert "if(!window._sextantExpired&&!window._sextantStopped)setTimeout(cwS,3000);" in src
    assert 'not in ("on", "1", "true")' in src
    hook_entry._PAYLOAD["seen"] = False
    hook_entry._PAYLOAD["garbled"] = False
    monkeypatch.setattr(sys, "stdin", io.StringIO("{session_id:m7,transcript_path:garbled}"))
    assert hook_entry._transcript_root_from_payload() == ""
    assert hook_entry._payload_seen() and hook_entry._PAYLOAD["garbled"]


def test_unreadable_cert_bundle_is_named_by_the_sync_client(tmp_path, monkeypatch):
    from atlas_plus.sync.client import SyncClient, SyncClientError

    bad = tmp_path / "nope.pem"
    monkeypatch.setenv("SSL_CERT_FILE", str(bad))
    with pytest.raises(SyncClientError) as e:
        SyncClient("https://hub.example:8443", token="t")
    assert "SSL_CERT_FILE" in str(e.value) and "Traceback" not in str(e.value)
    bad.write_text("not a certificate", encoding="utf-8")
    with pytest.raises(SyncClientError):
        SyncClient("https://hub.example:8443", token="t")


# ---------------------------------------------------------------- MN48: the hybrid duplicate scan
def test_duplicate_scan_hybrid_matches_bitsets_and_brute_force(tmp_path, monkeypatch):
    import itertools
    import random
    from atlas_plus.curation import duplicates as dup

    rng = random.Random(48)
    vocab = [f"tok{i}" for i in range(400)]
    root = tmp_path / "mem"
    root.mkdir()
    texts = []
    for i in range(60):
        words = rng.sample(vocab, 25)
        if i % 7 == 0 and texts:
            words = texts[-1][:20] + rng.sample(vocab, 5)                  # a near copy
        texts.append(words)
        (root / f"n{i:03d}.md").write_text("---\nid: n\n---\n" + " ".join(words) + "\n", encoding="utf-8")

    def run():
        return sorted((*sorted((p.file1, p.file2)), round(p.similarity, 6)) for p in dup.find_duplicates(root, threshold=0.4))

    with_bits = run()
    monkeypatch.setattr(dup, "_BITSET_MAX_TOKENS", 0)                       # force the set-intersection path
    with_sets = run()
    assert with_bits == with_sets and with_bits
    toks = {f"n{i:03d}.md": dup._tokenize((root / f"n{i:03d}.md").read_text(encoding="utf-8")) for i in range(60)}
    brute = sorted((a, b, round(len(toks[a] & toks[b]) / len(toks[a] | toks[b]), 6))
                   for a, b in itertools.combinations(sorted(toks), 2)
                   if len(toks[a] & toks[b]) / len(toks[a] | toks[b]) >= 0.4)
    assert with_bits == brute


def test_long_dashed_slug_masks_and_breaker_is_ten_seconds():
    from atlas_plus.redaction import redact
    from atlas_plus.web import app as web

    v = "Qm7rT2kW9pLx"
    assert v not in redact(f"a-b-c-d-e-f-g-h-i-password={v}")
    assert v not in redact(f"-password={v}") and v not in redact(f"mysql -password={v}")
    assert web._PeerClient.PEER_DOWN_SECONDS == 10.0


# ---------------------------------------------------------------- AG60: the expert's sixth sitting
def test_marks_stay_inside_words_everywhere():
    from atlas_plus.curation import duplicates as dup
    from atlas_plus.curation import suggest_facts as sf

    down = dup._tokenize("---\nid: p\n---\nसर्वर बंद है। दिन में काम नहीं हुआ।\n")
    up = dup._tokenize("---\nid: q\n---\nसर्वर चालू है। दान में कम नहीं दिया।\n")
    assert down != up and len(down & up) / len(down | up) < 0.5                  # AG60: both had become {'सरवर'} and scored 1.00
    assert "दिन" in down and "दान" in up
    assert len(dup._tokenize("आज मैंने दिन भर काम किया और फिर दान दिया")) >= 4
    plain = sf._fact_tokens("الخادم الرئيسي يعمل على المنفذ")
    voc = sf._fact_tokens("الْخَادِمُ الرَّئِيسِيُّ يَعْمَلُ عَلَى الْمَنْفَذِ")
    assert len(voc) >= 4 and len(plain) >= 4                                     # not 17 single-letter fragments
    assert dup._tokenize("plain ascii text only here") == {"plain", "ascii", "text", "only", "here"}


def test_pass_assignments_mask_and_render_pass_words_do_not():
    from atlas_plus.redaction import redact

    v = "Zq8vN3pLx7Rw"
    for s in (f"PASS={v}", f"pass={v}", f"export PASS={v}", f"رمـز الوصول: {v}", f"مفتـاح API={v}", f"كلمة المـرور: {v}", f"סיסמא: {v}"):
        assert v not in redact(s), s
    for s in ("renderPass: forward_pass", "firstPass: analysis", "secondPass = resolve", "onePass: true", "--compass=northwest", "pass: 3 of 4 tests"):
        assert redact(s) == s, s


def test_fence_tracker_and_dry_run_flag(monkeypatch):
    from atlas_plus.summarizer import hook_entry
    from atlas_plus.summarizer.renderer import _inside_fence, remove_session_entry

    real = "## real\n<!-- session:abc turns:1 -->\nbody\n"
    assert "## real" not in remove_session_entry("```\n~~~\n```\n" + real, "<!-- session:abc")      # `~~~` inside a backtick block
    assert "## real" not in remove_session_entry("````\n```\n````\n" + real, "<!-- session:abc")    # three inside four
    assert "## real" not in remove_session_entry("\t```\ncode\n" + real, "<!-- session:abc")        # a tab-indented line is indented code
    assert _inside_fence("```\nx\n", 5) and not _inside_fence("```\nx\n```\n", 9)
    monkeypatch.delenv("ATLAS_SUMMARIZER_DRY_RUN", raising=False)
    assert hook_entry._dry_run_flag() == []
    monkeypatch.setenv("ATLAS_SUMMARIZER_DRY_RUN", "on")
    assert hook_entry._dry_run_flag() == ["--dry-run"]


def test_concurrent_searches_rebuild_a_stale_format_once(tmp_path, monkeypatch):
    import threading
    from atlas_plus.mcp.memory_server import fts
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    _pin_corpus(tmp_path, monkeypatch)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "search.sqlite", raising=False)
    monkeypatch.setattr(fts, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    memory_remember("reference:x", "a note to index", source="agent")
    fts.rebuild(force=True)
    conn = fts._connect()
    conn.execute("UPDATE atlas_meta SET value='2' WHERE key='index_format'")
    conn.commit()
    conn.close()
    calls = []
    real_rebuild = fts.rebuild

    def counting(force=False):
        calls.append(force)
        return real_rebuild(force=force)

    monkeypatch.setattr(fts, "rebuild", counting)
    threads = [threading.Thread(target=fts.update_incremental) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert calls.count(True) == 1                                                # AG60: four searches made four full rebuilds
    assert fts._index_format_tag() == fts.INDEX_FORMAT


# ---------------------------------------------------------------- AG56, AG57
def test_same_file_repeat_is_a_duplicate(tmp_path, monkeypatch):
    _pin_corpus(tmp_path, monkeypatch)
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    first = memory_remember("project:dupseq", "the walrus operator landed in version three point eight", source="agent")
    second = memory_remember("project:dupseq", "the walrus operator landed in version three point eight", source="agent")
    assert first["ok"] and not first["possible_duplicate"]
    assert second["ok"] and second["possible_duplicate"] and "dupseq" in (second["duplicate_hint"] or "")


def test_decision_and_anti_tools_cap_their_text_and_doctor_flags_duplicate_keys(tmp_path, monkeypatch):
    _pin_corpus(tmp_path, monkeypatch)
    from atlas_plus.mcp.memory_server.tools.anti import anti_remember
    from atlas_plus.mcp.memory_server.tools.decision import memory_remember_decision

    big = "x" * 70_000
    r = memory_remember_decision(title="t", decision=big, interactive=True)
    assert not r["ok"] and "limit is 65,536" in r["error"]
    r = anti_remember(scope="libraries", reject_pattern=big, interactive=True)
    assert not r["ok"] and "limit is 65,536" in r["error"]
    from atlas_plus.doctor import cli as doctor
    assert "duplicate_keys_hint" in inspect.getsource(doctor) and '"duplicate_keys"' in inspect.getsource(doctor)


def test_verify_hints_and_footer_wording():
    from atlas_plus import roundtrip
    from atlas_plus.curation import cli as ccli

    assert roundtrip.main(["--url", "ftp://127.0.0.1:1/mcp"]) == 2
    assert "for the rest" not in inspect.getsource(ccli)


def test_day_log_with_markers_is_refused_by_the_writer(tmp_path, monkeypatch):
    from atlas_plus.summarizer import renderer

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(renderer, "MEMORY_SESSIONS_DIR", sessions)
    monkeypatch.setattr(renderer, "UPDATES_TXT", tmp_path / "updates.txt")
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "on")
    (sessions / "2026-11-14.md").write_text("<<<<<<< HEAD\n## a\n=======\n## b\n>>>>>>> theirs\n", encoding="utf-8")
    with pytest.raises(RuntimeError) as e:
        renderer.write_outputs("## new\n<!-- session:zzz turns:2 -->\nbody\n", "x", "2026-11-14", session_marker="<!-- session:zzz")
    assert "conflict markers" in str(e.value)


# ---------------------------------------------------------------- MN54: the newcomer
def test_guessed_commands_redirect_in_one_bounce():
    from typer.testing import CliRunner
    from atlas_plus.cli import app

    r = CliRunner()
    res = r.invoke(app, ["install"])
    assert res.exit_code == 2 and "sextant init" in res.output and "uninstall" in res.output
    for guess in ("save", "store"):
        res = r.invoke(app, [guess])
        assert res.exit_code == 2 and "remember" in res.output, guess
    assert "remember" in r.invoke(app, ["recall"]).output or "search" in r.invoke(app, ["recall"]).output


def test_report_before_init_refuses_and_port_check_and_expired_text(tmp_path, monkeypatch):
    import socket
    from typer.testing import CliRunner
    from atlas_plus.curation import cli as ccli
    from atlas_plus.mcp.memory_server import routing as _routing
    from atlas_plus.web import app as web

    monkeypatch.setattr(_routing, "MEMORY_ROOT", tmp_path / "nope", raising=False)
    res = CliRunner().invoke(ccli.app, ["report"])
    assert res.exit_code == 1 and not (tmp_path / "nope").exists()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert not web._port_free("127.0.0.1", port)
    assert web._port_free("127.0.0.1", 0)
    assert "You have not signed in from this browser yet" in inspect.getsource(web)


# ---------------------------------------------------------------- MN53
def test_git_children_never_inherit_stdin_and_delegate_propagates_rc(monkeypatch):
    import subprocess
    import typer
    from atlas_plus import cli, gitsafe

    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", list(sys.argv))  # _delegate rewrites argv[0]; a leaked 'sextant doctor' flipped the banner for later tests
    gitsafe.run(["--version"])
    assert seen.get("stdin") == subprocess.DEVNULL
    with pytest.raises(typer.Exit) as e:
        cli._delegate(lambda: 1, "sextant doctor", ["--strict"])
    assert e.value.exit_code == 1
    cli._delegate(lambda: 0, "sextant doctor", [])                                # a clean run raises nothing


def test_verify_never_follows_a_redirect_with_the_token():
    import http.server
    import threading
    from atlas_plus import roundtrip

    captured = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            captured.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            self.do_POST()

        def log_message(self, *a):
            pass

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(301)
            self.send_header("Location", f"http://127.0.0.1:{target.server_address[1]}/elsewhere")
            self.end_headers()

        def log_message(self, *a):
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)
    redir = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    for srv in (target, redir):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        skipped: list[str] = []
        rpc, close = roundtrip._http_rpc(f"http://127.0.0.1:{redir.server_address[1]}/mcp", "SECRET-TOKEN-1", 5, skipped)
        with pytest.raises(RuntimeError) as e:
            rpc("initialize", {})
        assert "redirected to" in str(e.value) and "token was not sent" in str(e.value)
        assert captured == []                                                     # nothing reached the redirect target
    finally:
        target.shutdown()
        redir.shutdown()


# ---------------------------------------------------------------- MN49, MN51
def test_search_keeps_hindi_vowel_signs_inside_words(tmp_path, monkeypatch):
    _pin_corpus(tmp_path, monkeypatch)
    from atlas_plus.mcp.memory_server import fts
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "search.sqlite", raising=False)
    monkeypatch.setattr(fts, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    memory_remember("reference:hindi-din", "आज का दिन अच्छा था", source="agent")
    memory_remember("reference:hindi-daan", "उसने दान दिया", source="agent")
    fts.rebuild(force=True)
    paths = [r["path"] for r in fts.search("दिन", limit=5)]
    assert any("hindi-din" in p for p in paths) and not any("hindi-daan" in p for p in paths)   # MN49: the donation note came first


def test_day_log_hand_line_survives_and_partial_retry_is_capped(tmp_path):
    from atlas_plus.summarizer.renderer import find_session_entry, remove_session_entry

    entry = "## 2026-11-14 - work\n<!-- session:abc turns:12 -->\nWorked on: x\n<!-- end session:abc -->\n"
    text = entry + "MY OWN LINE under the entry\n\n## 2026-11-13 - older\nbody\n"
    out = remove_session_entry(text, "<!-- session:abc")
    assert "MY OWN LINE under the entry" in out and "Worked on: x" not in out
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    for n, want in ((1, 0), (2, 0), (3, 12), (0, 12)):
        marker = f"<!-- session:abc turns:12 partial:{n} -->" if n else "<!-- session:abc turns:12 -->"
        (sessions / "2026-11-14.md").write_text(f"## t\n{marker}\nbody\n", encoding="utf-8")
        assert find_session_entry(sessions, "abc")[1] == want, n


def test_facts_rewording_vs_correction_and_redaction_fixes(tmp_path, monkeypatch):
    _pin_corpus(tmp_path, monkeypatch)
    from atlas_plus.curation import suggest_facts as sf
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember
    from atlas_plus.redaction import redact

    memory_remember("reference:standup", "The daily standup is at 10am in room B", source="agent")
    assert sf._fact_already_there("reference:standup", "the daily standup is at 10am in room B", 0.8)          # the same fact
    assert sf._fact_already_there("reference:standup", "Daily standup is at 10am in the room B", 0.8)          # function words differ: a rewording
    assert not sf._fact_already_there("reference:standup", "The daily standup is at 10am in room C", 0.8)      # a content word differs: a correction (MN49)
    v = "Hx9kQ2mZp77Qq"
    assert v not in redact(f'<property name="spring.datasource.password" value="{v}"/>')
    assert redact("AccountKey=xxxxxxxxxxxxxxxxxxxxxxxx") == "AccountKey=xxxxxxxxxxxxxxxxxxxxxxxx"
    for s in ('the token is "expired"', "Your password is 'incorrect'.", "The login page password field is required.",
              "find /var/lib -name '*.mysql' -print", "After login type password twice."):
        assert redact(s) == s, s
    assert v not in redact(f"mysql -u root -p{v} shop") and v not in redact(f"machine h login u password {v}")


def test_empty_stdin_payload_is_refused(monkeypatch):
    import io
    from atlas_plus.summarizer import hook_entry

    hook_entry._PAYLOAD["seen"] = False
    hook_entry._PAYLOAD["empty"] = False
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert hook_entry._transcript_root_from_payload() == ""
    assert hook_entry._payload_seen() and hook_entry._PAYLOAD["empty"]                                  # MN51: it summarised the whole day


# ---------------------------------------------------------------- MN52
def test_sara_am_recomposed_and_more_key_names():
    from atlas_plus.mcp.memory_server import fts
    from atlas_plus.redaction import redact

    assert "\u0e33" in fts._norm_index_text("\u0e19\u0e49\u0e33") and "\u0e4d\u0e32" not in fts._norm_index_text("\u0e17\u0e33")
    assert "\u0e33" in fts._prepare_query("\u0e17\u0e33")
    v = "Xk9#mQ2vLp7Zt"
    for s in (f"API-Schlüssel={v}", f"Geheimnis: {v}", f"คีย์ API={v}", f"एपीआई कुंजी: {v}", f"कूटशब्द={v}", f"מפתח API: {v}", f"קוד גישה={v}",
              f"توکن: {v}", f"کلید API={v}", f"رمز\u200cعبور={v}", f"رمز: {v}", f"سرّ={v}",
              f"รหัสผ่านของฉันคือ {v}", f"मेरा पासवर्ड {v} है", f"मेरा पासवर्ड है {v}", f"كلمة السر الخاصة بي هي {v}", f"הסיסמה שלי היא {v}",
              f"رمز عبور من {v} است", f"мой пароль {v}", f"Mein Passwort lautet {v}", f"meu token {v}"):
        assert v not in redact(s), s
    for s in ("รหัสผ่านของฉันคือ ความลับ", "मेरा पासवर्ड गलत है", "мой пароль неверный", "Mein Passwort lautet geheim", "meu token expirou"):
        assert redact(s) == s, s


# ---------------------------------------------------------------- MN50 (CSS only reachable by a browser lane; the rule text is pinned here, the next browser lane measures it)
def test_memory_search_row_and_tablet_wrap_in_css():
    from atlas_plus.web import app as web

    src = inspect.getsource(web)
    assert "div:has(> #mem-q){{flex-wrap:wrap}}\n#mem-q{{flex:1 1 100%!important;min-width:0}}\n@media(max-width:768px)" in src
    assert "@media (max-width: 768px) {{  /* through tablet portrait" in src
    assert "Routing stats unavailable: Brain not running or not reachable at" in src


# ---------------------------------------------------------------- AG61, AG63
def test_joiners_are_not_token_characters_and_sinhala_marks_are(tmp_path, monkeypatch):
    _pin_corpus(tmp_path, monkeypatch)
    from atlas_plus.mcp.memory_server import fts
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "search.sqlite", raising=False)
    monkeypatch.setattr(fts, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    memory_remember("reference:fa", "\u06a9\u062a\u0627\u0628\u200c\u0647\u0627 \u062e\u0648\u0628 \u0627\u0633\u062a", source="agent")                   # کتاب‌ها (ZWNJ)
    memory_remember("reference:si-lanka", "\u0dc1\u0dca\u200d\u0dbb\u0dd3 \u0dbd\u0d82\u0d9a\u0dcf", source="agent")                                      # ශ්‍රී ලංකා
    memory_remember("reference:si-other", "\u0dbd \u0d9a\u0dcf \u0dc3\u0dd2 \u0dc4\u0dbd", source="agent")                                            # ල කා සි හල
    fts.rebuild(force=True)
    assert any("reference/fa" in r["path"] for r in fts.search("\u06a9\u062a\u0627\u0628", limit=5))                                       # کتاب finds کتاب‌ها (AG61)
    hits = [r["path"] for r in fts.search("\u0dbd\u0d82\u0d9a\u0dcf", limit=5)]                                                            # ලංකා
    assert any("si-lanka" in p for p in hits) and not any("si-other" in p for p in hits)                                                 # AG63


def test_passphrases_and_dockerfile_mysql_mask_again():
    from atlas_plus.redaction import redact

    for s in ("my password is Tr0ub4dor&Rising", "my password is S3cure!Blessed", "the token is 9fK2xQ7LmZpWed",
              "RUN mysql -u root -pZq8vT3pLmK9", "run: mysql -u root -pZq8vT3pLmK9", "- mysql -u root -pZq8vT3pLmK9"):
        out = redact(s)
        assert "[REDACTED]" in out, s
    assert redact('the token is "expired"') == 'the token is "expired"'
    assert redact("find /var/lib -name '*.mysql' -print") == "find /var/lib -name '*.mysql' -print"
    assert redact("  login bob\n  password Zq8vT3pLmK9").startswith("  login bob\n  password [REDACTED]")                                # indentation kept (AG61)
    assert redact("AccountKey=XXXXXXXXXXXXXXXXXXXXXXXX") == "AccountKey=XXXXXXXXXXXXXXXXXXXXXXXX"


def test_redirects_take_arguments_and_status_is_components():
    from typer.testing import CliRunner
    from atlas_plus.cli import app

    r = CliRunner()
    res = r.invoke(app, ["save", "my", "fact", "here"])
    assert res.exit_code == 2 and "remember" in res.output and "you typed" in res.output
    res = r.invoke(app, ["install", "--client", "claude"])
    assert res.exit_code == 2 and "sextant init" in res.output
    assert "status" not in __import__("atlas_plus.cli", fromlist=["_REDIRECTS"])._REDIRECTS


def test_meaning_words_and_order_make_a_correction(tmp_path, monkeypatch):
    _pin_corpus(tmp_path, monkeypatch)
    from atlas_plus.curation import suggest_facts as sf
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    memory_remember("reference:ops", "The migration must finish in under two hours and runs from Postgres to MySQL", source="agent")
    assert not sf._fact_already_there("reference:ops", "The migration must finish in over two hours and runs from Postgres to MySQL", 0.8)     # under -> over
    assert not sf._fact_already_there("reference:ops", "The migration may finish in under two hours and runs from Postgres to MySQL", 0.8)      # must -> may
    assert not sf._fact_already_there("reference:ops", "The migration must finish in under two hours and runs from MySQL to Postgres", 0.8)     # reversed
    assert sf._fact_already_there("reference:ops", "Migration must finish in under two hours and runs from the Postgres to MySQL", 0.8)         # articles only


def test_lessons_are_one_line_and_lock_is_owned(tmp_path, monkeypatch):
    import os
    from atlas_plus.summarizer import renderer

    monkeypatch.setattr(renderer.atlas_paths, "state_dir", lambda: tmp_path)
    lock = tmp_path / "summarizer" / "write.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("999999", encoding="ascii")                                                                                            # another process's live lock
    os.utime(lock, None)
    with pytest.raises(RuntimeError):
        with renderer._write_lock(timeout=0.3, stale=120.0):
            pass
    assert lock.exists() and lock.read_text(encoding="ascii") == "999999"                                                                 # a live lock is never broken or stolen (AG64)
    lock.unlink()
    with renderer._write_lock(timeout=0.3, stale=120.0):
        assert lock.read_text(encoding="ascii") == str(os.getpid())
    assert not lock.exists()
    from atlas_plus.summarizer import cli as scli
    assert '"files_touched", "lessons"' in inspect.getsource(scli)


# ---------------------------------------------------------------- AG64
def test_gave_up_counter_and_quoted_heading_and_root_shape(tmp_path, monkeypatch):
    import io
    from atlas_plus.summarizer import cli as scli, hook_entry, renderer
    from atlas_plus import paths as atlas_paths

    monkeypatch.setattr(atlas_paths, "state_dir", lambda: tmp_path)
    assert not scli._gave_up("s1", 12)
    for _ in range(3):
        scli._note_failure("s1", 12)
    assert scli._gave_up("s1", 12) and not scli._gave_up("s1", 18)                                   # new turns lift it
    entry = "## 2026-11-14 - work\n<!-- session:q turns:3 -->\n## Quoted heading from the model. Worked on: x\n<!-- end session:q -->\n"
    out = renderer.remove_session_entry(entry + "MINE\n", "<!-- session:q")
    assert out == "MINE\n"                                                                           # the end marker wins over the body's heading
    hook_entry._PAYLOAD.update(seen=False, empty=False, garbled=False, transcript_file="")
    outside = tmp_path / "outside" / "a" / "b.jsonl"
    outside.parent.mkdir(parents=True)
    outside.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_id": "x", "transcript_path": "%s"}' % str(outside).replace("\\", "/")))
    assert hook_entry._transcript_root_from_payload() == ""                                          # not .claude/projects: refused
    good = tmp_path / ".claude" / "projects" / "slug" / "s.jsonl"
    good.parent.mkdir(parents=True)
    good.write_text("", encoding="utf-8")
    hook_entry._PAYLOAD.update(seen=False, transcript_file="")
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_id": "x", "transcript_path": "%s"}' % str(good).replace("\\", "/")))
    assert hook_entry._transcript_root_from_payload().endswith("projects") and hook_entry._PAYLOAD["transcript_file"]
    assert '"--skip-similar", "0.8"' not in inspect.getsource(hook_entry)


# ---------------------------------------------------------------- AG65
def test_verify_alert_wording_and_hub_names_each_client():
    from atlas_plus import roundtrip
    from atlas_plus.sync import api as sync_api
    from atlas_plus.web import app as web

    src = inspect.getsource(roundtrip)
    assert "refused the TLS handshake with an alert" in src and 'if detail.strip() else ""' in src
    assert "more than 5 clients via %s are locked out this window" in inspect.getsource(sync_api)
    assert "<div style='overflow-x:auto;max-width:100%;min-width:0'><table class='text-xs'" in inspect.getsource(web)

