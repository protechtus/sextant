


# --- AG48 performance ---


def test_duplicate_scan_matches_brute_force_and_caps_exactly(tmp_path, monkeypatch):
    import itertools
    import random

    from atlas_plus.curation import duplicates as dup

    rng = random.Random(16)
    vocab = [f"w{i}" for i in range(60)]
    root = tmp_path / "mem"
    (root / "reference").mkdir(parents=True)
    texts = {}
    for i in range(120):
        words = [rng.choice(vocab) for _ in range(rng.randint(20, 60))]
        texts[f"n{i}.md"] = " ".join(words)
        if i % 4 == 0 and i:
            texts[f"c{i}.md"] = " ".join(words[: len(words) * 9 // 10])  # a near copy
    for name, body in texts.items():
        (root / "reference" / name).write_text("---\nid: x\n---\n" + body + "\n", encoding="utf-8")
    monkeypatch.setattr(dup, "MEMORY_ROOT", root)
    pairs = dup.find_duplicates(memory_root=root, threshold=0.4)
    got = {(min(p.file1, p.file2), max(p.file1, p.file2)) for p in pairs}
    toks = {n: dup._tokenize("---\nid: x\n---\n" + b + "\n") for n, b in texts.items()}
    brute = set()
    for a, b in itertools.combinations(sorted(texts), 2):
        ta, tb = toks[a], toks[b]
        if ta and tb and len(ta & tb) / len(ta | tb) >= 0.4:
            brute.add((min(a, b), max(a, b)))
    names = lambda s: {(str(x).rsplit("/", 1)[-1].rsplit("\\", 1)[-1], str(y).rsplit("/", 1)[-1].rsplit("\\", 1)[-1]) for x, y in s}  # noqa: E731
    if len(brute) <= dup._MAX_FILE_PAIRS:
        assert names(got) == brute
    else:
        assert len(pairs) == dup._MAX_FILE_PAIRS and dup._LAST_CAPPED
        # the kept pairs are the strongest ones
        kept_min = min(p.similarity for p in pairs)
        assert all(len(toks[a] & toks[b]) / len(toks[a] | toks[b]) <= kept_min + 1e-9 for a, b in brute if (a, b) not in names(got))


def test_summarizer_chunk_cache_reuses_unchanged_chunks(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    from atlas_plus.summarizer import cli as scli

    k1 = scli._chunk_key("chunk one text")
    scli._save_chunk_cache("sess-c", {k1: "summary one"})
    assert scli._load_chunk_cache("sess-c") == {k1: "summary one"}
    assert scli._load_chunk_cache("other") == {}
    import inspect
    src = inspect.getsource(scli.main)
    assert "chunk_cache.get(key)" in src and "_save_chunk_cache(safe_id, used_cache)" in src


def test_json_curation_report_uses_the_isolated_cached_report():
    import inspect

    from atlas_plus.web import app as web

    src = inspect.getsource(web.memory_curation_report)
    assert '_cached_panel("curation-report", _generate_report_isolated)' in src
    assert '_cached_panel("untagged-scopes", _gather)' in inspect.getsource(web.memory_untagged_scopes)


def test_single_chunk_summary_needs_no_title_and_junk_is_never_cached():
    """MN39: the map schema has no title, so a one-chunk session arrived without one and was refused."""
    import inspect

    from atlas_plus.summarizer import cli as scli

    assert scli._looks_like_summary({"worked_on": ["the build"], "completed": [], "pending": []})
    assert not scli._looks_like_summary({"title": "x\ny", "completed": ["a"]})
    assert not scli._looks_like_summary(scli._parse_json_response("Sure! Here is a summary."))
    src = inspect.getsource(scli.main)
    assert 'if not _parse_json_response(text).get("_fallback"):' in src and "if safe_id and not dry_run:" in src


def test_gate_value_is_stripped_and_off_is_an_opt_out(monkeypatch):
    from atlas_plus import autonomous

    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", " on ")
    assert autonomous.is_session_autonomous()
    monkeypatch.setenv("ATLAS_SESSION_AUTONOMOUS", "off")
    monkeypatch.delenv("ATLAS_AUTONOMOUS", raising=False)
    assert not autonomous.is_session_autonomous()
    import inspect
    from atlas_plus.summarizer import hook_entry
    assert '("off", "0", "false", "no")' in inspect.getsource(hook_entry.main)


# --- manual wave 16 (MN37, MN38, MN41, MN42) ---


def test_redaction_long_quoted_values_unterminated_quotes_and_code_traps():
    import time

    from atlas_plus.redaction import redact

    jwt = "eyJ" + "A" * 830 + ".sig"
    for s in (f'{{"access_token": "{jwt}"}}', f"id_token: \"{jwt}\"", f"token = '{jwt}'", f"curl -d '{{\"refresh_token\":\"{jwt}\"}}'"):
        assert jwt not in redact(s), s[:40]
    assert "hunter2-correct-horse" not in redact("password='hunter2-correct-horse")      # an unterminated quote (MN37)
    for s in ("pwd=Zq9wX7vB3nK1", "DB_PWD=Zq9wX7vB3nK1", "DB_PASS=Zq9wX7vB3nK1", "MYSQL_PASS: Zq9wX7vB3nK1", "--db-password Zq9wX7vB3nK1",
              "--api-token Zq9wX7vB3nK1", "curl -u " + "admin" + ":" + "Zq9wX7vB3nK1 https://h/", "Authorization: Token Zq9wX7vB3nK1", "<password>Zq9wX7vB3nK1</password>",
              "password: >\n  Zq9wX7vB3nK1\n  more\nnext: 1"):
        assert "Zq9wX7vB3nK1" not in redact(s), s
    # code and prose that are not secrets
    for s in ("PWD=/home/u/project", "token = tokens[0]", "nexttoken = tokens[i+1]", "password = os.environ['DB_PASSWORD']", "The token: refreshing the page", "pass: yes we did"):
        assert redact(s) == s, s
    # the full-width pass is linear (MN37: 97 s per MB)
    big = "ＤＢ＿ＰＡＳＳＷＯＲＤ＝ｘｙｚｗｖｕ１２３ " * 30_000
    t0 = time.perf_counter()
    out = redact(big)
    assert time.perf_counter() - t0 < 3.0 and "ｘｙｚｗｖｕ" not in out


def test_forwarded_identity_port_suffix_and_junk_are_bounded_sources():
    from atlas_plus.auth import _valid_ip, forwarded_ip

    assert _valid_ip("1.2.3.4:5678") == "1.2.3.4" and _valid_ip("[::1]:443") == "::1"
    junk = _valid_ip("x" * 5000)
    assert junk.startswith("bad:") and len(junk) <= 44
    assert forwarded_ip({"Cf-Connecting-Ip": "banana"}, "127.0.0.1") == "bad:banana"   # its own source, never the loopback exemption (MN37)


def test_norm_fact_keeps_signs_and_combining_marks():
    from atlas_plus.curation.suggest_facts import _norm_fact

    for a, b in (("x < 5", "x > 5"), ("x ≥ 5", "x ≤ 5"), ("a != b", "a == b"), ("C#", "C"), ("−5 degrees", "5 degrees")):
        assert _norm_fact(a) != _norm_fact(b), (a, b)
    assert _norm_fact("उसे") != _norm_fact("उस")
    assert _norm_fact("The deploy target is web2.") == _norm_fact("the deploy target is web2")


def test_entry_removal_leaves_quoted_markers_and_hand_written_lines(tmp_path):
    from atlas_plus.summarizer.renderer import remove_session_entry, remove_updates_entry

    log = ("## 2026-11-12 - My notes on the summarizer\n\nIt writes `<!-- session:abc123 turns:6 -->` under the heading.\n\n"
           "## 2026-11-12 - real entry\n<!-- session:abc123 turns:6 -->\nsix turns\n\n## 2026-11-12 - other\n<!-- session:zzz turns:2 -->\nx\n")
    out = remove_session_entry(log, "<!-- session:abc123")
    assert "My notes on the summarizer" in out and "real entry" not in out and "session:zzz" in out
    upd = ("header\n====\n\n2026-11-12 10:00 -- sess\n<!-- session:abc123 turns:6 -->\n- a\n- b\n"
           "HAND-WRITTEN 2026-11-12: renewed the domain\n2026-11-12 09:00 -- earlier\n- c\n\n")
    out2 = remove_updates_entry(upd, "<!-- session:abc123")
    assert "HAND-WRITTEN" in out2 and "- a" not in out2 and "earlier" in out2


def test_tref_falls_back_to_bare_id_for_root_level_tickets(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from atlas_plus.tickets import store
    from atlas_plus.web import app as web

    monkeypatch.setattr(store, "TICKETS_ROOT", tmp_path)
    (tmp_path / "alp-a").mkdir()
    (tmp_path / "alp-a" / "ALP-001.md").write_text("---\nid: ALP-001\n---\n", encoding="utf-8")
    (tmp_path / "OLD-001.md").write_text("---\nid: OLD-001\n---\n", encoding="utf-8")
    assert web._tref(SimpleNamespace(id="ALP-001", project="alp-a")) == "alp-a/ALP-001"
    assert web._tref(SimpleNamespace(id="OLD-001", project="tickets")) == "OLD-001"     # the folder does not match: bare id (MN37)


def test_prose_no_facts_is_an_answer_and_dev_base_advances(monkeypatch, tmp_path):
    import importlib.util
    from pathlib import Path

    from atlas_plus.curation import suggest_facts as sf
    from atlas_plus.summarizer import providers

    monkeypatch.setattr(providers, "complete", lambda prompt: ("There are no new facts worth saving in this session.", "stub"))
    assert sf._extract_candidates("USER: hi", confidence_threshold=0.5) == ([], 0)
    import pytest
    builder = Path(__file__).resolve().parents[1] / "scripts" / "build_public_repo.py"
    if not builder.exists():
        pytest.skip("private builder")
    spec = importlib.util.spec_from_file_location("build_public_repo", builder)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import subprocess
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "x", "-c", "user.name=t", "-c", "user.email=t@example.com"], check=False)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    assert mod._dev_base(tmp_path, "0.1.0") == "0.1.0"
    subprocess.run(["git", "-C", str(tmp_path), "tag", "v0.1.0"], check=True)
    assert mod._dev_base(tmp_path, "0.1.0") == "0.1.1"                                    # after the tag, dev builds precede the next patch (MN37)


def test_dashboard_expired_session_stops_polling_and_phone_rules_target_real_elements():
    import inspect

    from atlas_plus.web import app as web

    page = inspect.getsource(web)
    assert "htmx:beforeRequest" in page and "window._sextantExpired" in page
    assert 'id="tabbar"' in page and "#tabbar{{flex-wrap:wrap" in page and "#tab-memory{{flex-direction:column" in page and ".tk-proj{{display:none}}" in page
