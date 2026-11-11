"""Tests for atlas_plus.redaction (ATL-017 / audit MEDIUM-3/4)."""
from __future__ import annotations

from atlas_plus.redaction import redact


class TestRedact:
    def test_none_and_empty_pass_through(self):
        assert redact(None) is None
        assert redact("") == ""

    def test_exact_env_secret_value_masked(self):
        env = {"ATLAS_MCP_TOKEN": "s3cr3t-token-abcdef123456"}
        out = redact("the token is s3cr3t-token-abcdef123456 ok", env=env)
        assert "s3cr3t-token-abcdef123456" not in out
        assert "[REDACTED:ATLAS_MCP_TOKEN]" in out

    def test_short_env_value_not_masked(self):
        # very short values (e.g. "on"/ports) must not be masked to avoid clobbering normal text
        env = {"ATLAS_NTFY_TOKEN": "on"}
        assert redact("turn it on please", env=env) == "turn it on please"

    def test_bearer_header_masked(self):
        out = redact("Authorization: Bearer abcDEF123456_-=ghijkl", env={})
        assert "abcDEF123456_-=ghijkl" not in out
        assert "Bearer [REDACTED]" in out

    def test_telegram_token_masked(self):
        out = redact("bot 7312456789:AAHdqTcvCHhvQiPbMhB8p_xyz0123456789ab", env={})
        assert "AAHdqTcvCHhvQiPbMhB8p" not in out
        assert "[REDACTED:telegram-token]" in out

    def test_key_value_secret_masked(self):
        for s, needle in [
            ("api_key=supersecretvalue", "supersecretvalue"),
            ("password: hunter2hunter2", "hunter2hunter2"),
            ("client_secret = abcdef123456", "abcdef123456"),  # gitleaks:allow (fake fixture token)
        ]:
            out = redact(s, env={})
            assert needle not in out
            assert "[REDACTED]" in out

    def test_normal_prose_unaffected(self):
        s = "Refactored the auth token flow and fixed the bearer handling docs."
        # No '=' / ':' value after the keyword -> prose stays intact.
        assert redact(s, env={}) == s

    def test_git_sha_not_overredacted(self):
        # Conservative design: no broad high-entropy sweep, so identifiers survive.
        s = "commit 5fd8e15c0ffee1234567890abcdef0123456789 landed"
        assert redact(s, env={}) == s


class TestQuotedAndProviderShapes:
    """Gap found 2026-07-02: _KV_RE's value class excluded quotes, so JSON-shaped and
    quoted secrets -- the dominant shape in Brain task results -- passed through
    unredacted. Provider-prefixed keys (sk-/ghp_) pasted verbatim were also invisible
    to the env-value rule when they are not this machine's own keys."""

    def test_json_shaped_secret_masked(self):
        out = redact('{"api_key": "supersecretvalue123"}', env={})
        assert "supersecretvalue123" not in out
        assert '"api_key": "[REDACTED]"' in out   # structure preserved

    def test_single_quoted_secret_masked(self):
        out = redact("token='quotedsecretvalue99'", env={})
        assert "quotedsecretvalue99" not in out
        assert "[REDACTED]" in out

    def test_double_quoted_assignment_masked(self):
        out = redact('client_secret="abcdef123456xyz"', env={})  # gitleaks:allow (fake fixture token)
        assert "abcdef123456xyz" not in out

    def test_pasted_anthropic_key_masked_without_env(self):
        out = redact("my key is sk-ant-api03-AbCdEf1234567890xyz", env={})
        assert "sk-ant" not in out
        assert "[REDACTED:provider-key]" in out

    def test_pasted_github_tokens_masked(self):
        for tok in ("ghp_AbCdEfGhIjKlMnOpQrStUvWx12345678",
                    "github_pat_11ABCDEFG0abcdefghijklmnop"):
            out = redact(f"push with {tok} please", env={})
            assert tok not in out

    def test_rclone_config_pass_env_value_masked(self):
        env = {"RCLONE_CONFIG_PASS": "crypt-passphrase-abc123"}
        out = redact("rclone said: pass=crypt-passphrase-abc123", env=env)
        assert "crypt-passphrase-abc123" not in out
        assert "[REDACTED:RCLONE_CONFIG_PASS]" in out

    def test_no_overredaction_of_lookalikes(self):
        # short sk- prefix (sk-learn) and ordinary quoted prose must survive
        s = 'we use sk-learn here; the "token" concept: explained simply'
        assert redact(s, env={}) == s
