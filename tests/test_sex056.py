"""SEX-056: a search that looked at the index while another search's rebuild had just cleared it must not
rebuild it a second time.

CI run 46041335540 (macOS, Python 3.11) saw two forced rebuilds where one was expected. The cause was in the
product: `update_incremental` read "the index holds no notes" OUTSIDE the rebuild lock and acted on it after
waiting for the lock, although the format tag right beside it was re-read under the lock (AG60). A rebuild
clears the tables in one commit and fills them in the next, so a second search arriving in between saw an
empty index, waited, and then built everything again.

The order is forced with events (no sleeps): the first search is held right after its clear is committed, the
second search runs until it asks for the lock, then the first is released.
"""
import threading


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


class _WatchedLock:
    """The rebuild lock, telling the test when the thread named `who` asks for it."""

    def __init__(self, real, who, asked):
        self._real, self._who, self._asked = real, who, asked

    def _note(self):
        if threading.current_thread().name == self._who:
            self._asked.set()

    def acquire(self, *a, **kw):
        self._note()
        return self._real.acquire(*a, **kw)

    def release(self):
        return self._real.release()

    def __enter__(self):
        self._note()
        return self._real.__enter__()

    def __exit__(self, *exc):
        return self._real.__exit__(*exc)


def _race(tmp_path, monkeypatch, stale_format):
    from atlas_plus.mcp.memory_server import fts
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    _pin_corpus(tmp_path, monkeypatch)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "search.sqlite", raising=False)
    monkeypatch.setattr(fts, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    memory_remember("reference:x", "a note to index", source="agent")
    fts.rebuild(force=True)
    notes = fts._connect().execute("SELECT COUNT(*) FROM memory_files").fetchone()[0]
    assert notes >= 1
    if stale_format:
        conn = fts._connect()
        conn.execute("UPDATE atlas_meta SET value='2' WHERE key='index_format'")
        conn.commit()
        conn.close()

    forced = []
    real_rebuild = fts.rebuild

    def counting(force=False):
        forced.append((threading.current_thread().name, force))
        return real_rebuild(force=force)

    cleared, late_asked = threading.Event(), threading.Event()
    real_pass = fts._index_pass
    held = []

    def gated_pass(resuming=False):
        # the first search's rebuild has committed its clear and has not indexed a note yet
        if threading.current_thread().name == "first-search" and not held:
            held.append(True)
            cleared.set()
            assert late_asked.wait(30), "the late search never asked for the rebuild lock"
        return real_pass(resuming)

    monkeypatch.setattr(fts, "rebuild", counting)
    monkeypatch.setattr(fts, "_index_pass", gated_pass)
    monkeypatch.setattr(fts, "_rebuild_lock", _WatchedLock(fts._rebuild_lock, "late-search", late_asked))

    errors = []

    def run(first):
        try:
            if first:
                if stale_format:
                    fts.update_incremental()
                else:
                    fts.rebuild(force=True)   # an explicit rebuild (the command, the dashboard button)
            else:
                assert cleared.wait(30), "the first search never reached its index pass"
                conn = fts._connect()
                try:   # the window is real: the tables are empty and committed
                    assert conn.execute("SELECT COUNT(*) FROM memory_files").fetchone()[0] == 0
                finally:
                    conn.close()
                fts.update_incremental()
        except BaseException as exc:  # noqa: BLE001 - a thread's failure must fail the test
            errors.append(exc)
            cleared.set()
            late_asked.set()

    a = threading.Thread(target=run, args=(True,), name="first-search")
    b = threading.Thread(target=run, args=(False,), name="late-search")
    a.start()
    b.start()
    a.join(60)
    b.join(60)
    assert not a.is_alive() and not b.is_alive()
    assert not errors, errors
    assert fts._index_format_tag() == fts.INDEX_FORMAT
    conn = fts._connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM memory_files").fetchone()[0] == notes
        assert conn.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == notes
    finally:
        conn.close()
    return forced


def test_a_search_that_saw_the_index_cleared_mid_rebuild_does_not_rebuild_again(tmp_path, monkeypatch):
    forced = _race(tmp_path, monkeypatch, stale_format=True)
    assert [who for who, force in forced if force] == ["first-search"], forced


def test_a_search_arriving_during_an_explicit_rebuild_does_not_rebuild_again(tmp_path, monkeypatch):
    forced = _race(tmp_path, monkeypatch, stale_format=False)
    assert [who for who, force in forced if force] == ["first-search"], forced


def test_an_index_that_is_really_empty_is_still_built_by_the_first_search(tmp_path, monkeypatch):
    """The re-read under the lock must not lose the first build: a new index (no rows) is built once."""
    from atlas_plus.mcp.memory_server import fts
    from atlas_plus.mcp.memory_server.tools.remember import memory_remember

    _pin_corpus(tmp_path, monkeypatch)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "search.sqlite", raising=False)
    monkeypatch.setattr(fts, "MEMORY_ROOT", tmp_path / "mem", raising=False)
    memory_remember("reference:x", "a note to index", source="agent")
    forced = []
    real_rebuild = fts.rebuild
    monkeypatch.setattr(fts, "rebuild", lambda force=False: (forced.append(force), real_rebuild(force=force))[1])
    conn = fts._connect()
    try:
        conn.execute("DELETE FROM memory_fts")
        conn.execute("DELETE FROM memory_files")
        conn.commit()
    finally:
        conn.close()
    assert fts.update_incremental() >= 1
    assert forced == [True]
    assert fts._index_format_tag() == fts.INDEX_FORMAT
