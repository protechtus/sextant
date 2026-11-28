"""MN96 (sweep 39): a note a Windows editor saved as UTF-16 ("Unicode" in Notepad) was indexed as
replacement-character junk, so a search silently missed every fact in it, while `sextant import` already
decodes UTF-16 by its byte-order mark. The corpus reader now decodes a BOM-led UTF-16 file the same way."""
from __future__ import annotations

import codecs


def _corpus(tmp_path, monkeypatch):
    from atlas_plus.mcp.memory_server import fts
    mem = tmp_path / "mem"
    (mem / "reference").mkdir(parents=True)
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "fts.sqlite")
    return fts, mem


def test_a_utf16_note_is_found_by_a_word_inside_it(tmp_path, monkeypatch):
    fts, mem = _corpus(tmp_path, monkeypatch)
    (mem / "reference" / "hokkaido.md").write_bytes(
        codecs.BOM_UTF16_LE + "# Hokkaido\r\n\r\n北海道札幌市の雪まつりは二月に大通公園で\r\n".encode("utf-16-le"))
    (mem / "reference" / "plain.md").write_text("# plain\n\n東京タワーの高さ\n", encoding="utf-8")
    for word in ("大通公園", "雪まつり", "Hokkaido"):
        hits = fts.search(word)
        assert any("hokkaido.md" in str(h) for h in hits), (word, hits)
    assert any("plain.md" in str(h) for h in fts.search("高さ"))


def test_a_big_endian_utf16_note_and_a_utf8_bom_note_are_read_as_text(tmp_path):
    from atlas_plus import _readahead
    be = tmp_path / "be.md"
    be.write_bytes(codecs.BOM_UTF16_BE + "締め切りは来月末".encode("utf-16-be"))
    sig = tmp_path / "sig.md"
    sig.write_bytes(codecs.BOM_UTF8 + "佐藤さん".encode("utf-8"))
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"ok \xff\xfe? not a bom at the start")
    assert _readahead.read_or_error(be, "utf-8-sig") == "締め切りは来月末"
    assert _readahead.read_or_error(sig, "utf-8-sig") == "佐藤さん"
    assert "�" in _readahead.read_or_error(bad, "utf-8-sig")   # not UTF-16: replaced as before
    missing = _readahead.read_or_error(tmp_path / "nope.md", "utf-8-sig")
    assert isinstance(missing, OSError)


def test_an_index_the_old_reader_built_heals_on_the_first_search(tmp_path, monkeypatch):
    """The upgrade: an index built before the fix holds the UTF-16 note as replacement characters and the note
    itself did not change, so only the parse revision can bring it back (lesson LES-092: the heal ships with
    the fix). Built here with the old reader and the old revision, then searched with the current code."""
    from atlas_plus import _readahead
    fts, mem = _corpus(tmp_path, monkeypatch)
    (mem / "reference" / "hokkaido.md").write_bytes(
        codecs.BOM_UTF16_LE + "# Hokkaido\r\n\r\nsapporo snowfestival odori\r\n".encode("utf-16-le"))
    (mem / "reference" / "plain.md").write_text("# plain\n\ntokyo tower\n", encoding="utf-8")
    now_rev = fts.PARSE_REVISION
    assert int(now_rev) >= 2
    with monkeypatch.context() as old:
        old.setattr(_readahead, "read_note_text", lambda path, encoding="utf-8": path.read_text(encoding=encoding, errors="replace"))
        old.setattr(fts, "PARSE_REVISION", "1")
        fts.rebuild(force=True)
        assert not any("hokkaido.md" in str(h) for h in fts.search("snowfestival"))   # the defect, as 0.1.1 had it
    assert fts.PARSE_REVISION == now_rev
    # The first search of the upgraded install is a new process: it runs the index update (a search in THIS
    # process within the freshness window, 10 s by default, skips it, so the update is called as that search would).
    changed = fts.update_incremental()
    assert changed >= 1 and fts._HEAL_STATS.get("queued") == 1, (changed, fts._HEAL_STATS)   # only the UTF-16 note
    hits = fts.search("snowfestival")
    assert any("hokkaido.md" in str(h) for h in hits), hits
    assert any("plain.md" in str(h) for h in fts.search("tower"))
