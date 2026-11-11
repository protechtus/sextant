"""ATL-026 step 2 tests -- inbox summarize logic (deps injected; no network/model) + executor glue."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from atlas_plus.inbox.store import InboxItem, create_item, get_item
from atlas_plus.inbox.summarize import _split_summary_tags, _strip_html, summarize_item


@pytest.fixture()
def inbox_root(tmp_path) -> Path:
    root = tmp_path / "inbox"
    root.mkdir()
    return root


class TestSummarizeItem:
    def test_summarizes_url(self, inbox_root):
        it = create_item(url="https://x.com/a", inbox_root=inbox_root)
        out = summarize_item(
            it.id,
            fetch_url=lambda u: "<html><body>Hello <b>world</b> content</body></html>",
            complete=lambda p: "A concise summary.\nTags: ai, voip",
            inbox_root=inbox_root,
        )
        assert out.status == "summarized"
        assert out.summary == "A concise summary."
        assert out.tags == ["ai", "voip"]
        assert get_item(it.id, inbox_root=inbox_root).summary == "A concise summary."

    def test_fetch_failure_falls_back_to_body(self, inbox_root):
        it = create_item(text="some captured blurb about SIP", inbox_root=inbox_root)
        seen = {}

        def bad_fetch(u):
            raise RuntimeError("network down")

        def complete(p):
            seen["prompt"] = p
            return "Summary from body."

        out = summarize_item(it.id, fetch_url=bad_fetch, complete=complete, inbox_root=inbox_root)
        assert out.status == "summarized"
        assert "SIP" in seen["prompt"]  # the captured body was used as the source

    def test_no_url_uses_body(self, inbox_root):
        it = create_item(text="blurb text", inbox_root=inbox_root)
        out = summarize_item(it.id, fetch_url=lambda u: "x", complete=lambda p: "S.", inbox_root=inbox_root)
        assert out.status == "summarized" and out.summary == "S."

    def test_missing_item(self, inbox_root):
        with pytest.raises(FileNotFoundError):
            summarize_item("INB-9999", fetch_url=lambda u: "", complete=lambda p: "x", inbox_root=inbox_root)

    def test_gated_off_does_not_call_model(self, inbox_root):
        it = create_item(text="blurb", inbox_root=inbox_root)
        calls = {"n": 0}

        def complete(p):
            calls["n"] += 1
            return "x"

        with patch.dict(os.environ, {"ATLAS_AUTONOMOUS": "off"}):
            with pytest.raises(RuntimeError, match="blocked"):
                summarize_item(it.id, fetch_url=lambda u: "x", complete=complete, inbox_root=inbox_root)
        assert calls["n"] == 0  # gated before any model call


class TestHelpers:
    def test_strip_html(self):
        assert _strip_html("<p>Hi <b>there</b></p><script>bad()</script>") == "Hi there"

    def test_split_tags(self):
        summary, tags = _split_summary_tags("Line one.\nLine two.\nTags: a, b; c")
        assert summary == "Line one.\nLine two."
        assert tags == ["a", "b", "c"]

    def test_split_no_tags(self):
        summary, tags = _split_summary_tags("Just a summary.")
        assert summary == "Just a summary." and tags == []


class TestExecutor:
    def _ctx(self, instruction, tmp_path):
        from atlas_plus.brain.executors import ExecutorContext
        from atlas_plus.brain.tasks import Task

        task = Task(
            task_id="t1",
            session_id="s1",
            instruction=instruction,
            status="running",
            created_at="2026-06-08T00:00:00Z",
            kind="inbox_summarize",
        )
        return ExecutorContext(
            task=task, artifact_root=tmp_path, db_path=tmp_path / "db.sqlite", worker_id="w1"
        )

    def test_executor_delegates(self, tmp_path):
        from atlas_plus.brain import executors

        fake = InboxItem(id="INB-0001", summary="done summary", status="summarized")
        with patch("atlas_plus.inbox.summarize.summarize_item", return_value=fake):
            res = executors._inbox_summarize(self._ctx(json.dumps({"item_id": "INB-0001"}), tmp_path))
        assert res.status == "done"
        assert "done summary" in res.text

    def test_executor_no_item_id(self, tmp_path):
        from atlas_plus.brain import executors

        res = executors._inbox_summarize(self._ctx("{}", tmp_path))
        assert res.status == "failed"
