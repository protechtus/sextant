"""ATL-017 / audit MEDIUM-4: task results must be redacted before they hit the task DB."""
from __future__ import annotations

from unittest.mock import patch

from atlas_plus.brain import worker
from atlas_plus.brain.executors import ExecutionResult


def _finish(text, status="done", tmp_path=None):
    res = ExecutionResult(status=status, text=text, artifact_path=None)
    target = "mark_done" if status == "done" else ("mark_blocked" if status == "blocked" else "mark_failed")
    with patch.object(worker.task_store, target) as marker:
        worker._finish_task("task-x", res, db_path=(tmp_path or worker.task_store._DB_PATH), task=None)
    return marker.call_args.args[1]  # the persisted text (2nd positional arg)


def test_secret_in_result_is_redacted(tmp_path):
    stored = _finish("oops Authorization: Bearer abcDEF123456_-ghijklmnop tail", tmp_path=tmp_path)
    assert "abcDEF123456_-ghijklmnop" not in stored
    assert "Bearer [REDACTED]" in stored


def test_env_secret_value_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_MCP_TOKEN", "s3cr3t-tok-abcdef123456")  # gitleaks:allow (fake fixture token)
    stored = _finish("the token leaked: s3cr3t-tok-abcdef123456 here", tmp_path=tmp_path)  # gitleaks:allow (fake fixture token)
    assert "s3cr3t-tok-abcdef123456" not in stored
    assert "[REDACTED:ATLAS_MCP_TOKEN]" in stored


def test_failed_result_redacted(tmp_path):
    stored = _finish("api_key=supersecretvalue boom", status="failed", tmp_path=tmp_path)
    assert "supersecretvalue" not in stored


def test_clean_result_unchanged(tmp_path):
    stored = _finish("Gaps report complete: wrote 3 stubs to .memory/gaps/", tmp_path=tmp_path)
    assert stored == "Gaps report complete: wrote 3 stubs to .memory/gaps/"
