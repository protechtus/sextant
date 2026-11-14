


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
