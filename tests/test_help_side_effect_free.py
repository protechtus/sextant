"""--help must be side-effect free (Codex artifact audit 2026-09-21 P0):
atlas-job-poller --help used to PROCESS QUEUED JOBS; bot/ntfy crashed on
missing env instead of printing usage."""
from __future__ import annotations

import pytest


def _help_exits_zero(main_func, prog, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [prog, "--help"])
    with pytest.raises(SystemExit) as exc:
        main_func()
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out.lower()


class TestHelpSideEffectFree:
    def test_job_poller_help_processes_nothing(self, monkeypatch, capsys):
        from atlas_plus.telegram import job_poller

        def boom(*a, **k):
            raise AssertionError("--help must not process jobs")

        monkeypatch.setattr(job_poller, "process_pending", boom)
        monkeypatch.setattr(job_poller, "_loop", boom, raising=False)
        _help_exits_zero(job_poller.main, "atlas-job-poller", monkeypatch, capsys)

    def test_telegram_bot_help_without_token(self, monkeypatch, capsys):
        monkeypatch.delenv("ATLAS_TELEGRAM_BOT_TOKEN", raising=False)
        from atlas_plus.telegram import bot

        _help_exits_zero(bot.main, "atlas-telegram-bot", monkeypatch, capsys)

    def test_ntfy_listener_help_without_env(self, monkeypatch, capsys):
        monkeypatch.delenv("ATLAS_NTFY_CMD_TOPIC", raising=False)
        monkeypatch.delenv("ATLAS_NTFY_CMD_SECRET", raising=False)
        from atlas_plus.ntfy import inbound

        _help_exits_zero(inbound.main, "atlas-ntfy-listener", monkeypatch, capsys)
