"""worker_detail() must release its sqlite connection even when a query raises (B1).

Before the fix, conn.close() ran only on the happy path; any execute() exception hit the
bare `except` and returned without closing -- leaking the handle on every failure (FD/handle
starvation over time). The fix closes in a finally.
"""
from __future__ import annotations

import sqlite3


def test_worker_detail_closes_conn_on_query_error(tmp_path, monkeypatch):
    from atlas_plus.brain import tasks

    db = tmp_path / "state.sqlite"
    db.write_bytes(b"not-a-real-db")  # exists, so we pass the db_path.exists() guard

    closed = {"v": False}
    real_connect = sqlite3.connect

    class Spy:
        def __init__(self, c):
            self._c = c

        def __getattr__(self, name):
            return getattr(self._c, name)

        def execute(self, *a, **k):
            raise sqlite3.OperationalError("boom")  # force the exception path

        def close(self):
            closed["v"] = True
            self._c.close()

    # worker_detail does `import sqlite3 as _sl` internally, so patching the module works.
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: Spy(real_connect(*a, **k)))

    out = tasks.worker_detail(db)

    # graceful empty result on error, AND the connection was closed despite the error
    assert out == {"active_workers": [], "pending_by_agent": {}, "zombie_count": 0, "queue_depth": 0}
    assert closed["v"] is True
