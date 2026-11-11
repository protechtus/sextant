"""Telegram bot allowlist gate (_is_allowed) -- fail-closed contract (fixed 2026-07-02).

An empty allowlist used to `return True` (allow EVERYONE). main() refuses to start
without ATLAS_TELEGRAM_ALLOWED_USERS so the branch was unreachable in production, but
it was a fail-open trap for any direct caller or refactor that skips main()'s guard --
the same class as the ATLAS_AUTONOMOUS denylist fixed the same day.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


def _update_from(user_id):
    u = MagicMock()
    u.effective_user = MagicMock(id=user_id) if user_id is not None else None
    return u


class TestIsAllowedFailClosed:
    def test_empty_allowlist_denies_everyone(self):
        from atlas_plus.telegram import bot
        with patch.object(bot, "ALLOWED_USERS", set()):
            assert bot._is_allowed(_update_from(12345)) is False

    def test_listed_user_allowed(self):
        from atlas_plus.telegram import bot
        with patch.object(bot, "ALLOWED_USERS", {12345}):
            assert bot._is_allowed(_update_from(12345)) is True

    def test_unlisted_user_denied(self):
        from atlas_plus.telegram import bot
        with patch.object(bot, "ALLOWED_USERS", {12345}):
            assert bot._is_allowed(_update_from(99999)) is False

    def test_missing_effective_user_denied(self):
        from atlas_plus.telegram import bot
        with patch.object(bot, "ALLOWED_USERS", {12345}):
            assert bot._is_allowed(_update_from(None)) is False
