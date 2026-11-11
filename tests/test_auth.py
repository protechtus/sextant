"""Unit tests for atlas_plus.auth -- the shared auth primitives (ROADMAP 6)."""
from __future__ import annotations

from atlas_plus.auth import (
    AuthFailureLog,
    SlidingWindowRateLimiter,
    bearer_token,
    constant_time_equals,
)


class TestConstantTimeEquals:
    def test_match(self):
        assert constant_time_equals("secret", "secret")

    def test_mismatch(self):
        assert not constant_time_equals("secret", "other")

    def test_empty_expected_never_matches(self):
        # A service with no configured token must not accept an empty candidate.
        assert not constant_time_equals("", "")
        assert not constant_time_equals("anything", "")


class TestBearerToken:
    def test_extracts(self):
        assert bearer_token("Bearer abc123 ") == "abc123"

    def test_non_bearer_is_empty(self):
        assert bearer_token("Basic abc") == ""
        assert bearer_token("") == ""

    def test_scheme_is_case_insensitive_token_is_not(self):
        # RFC 9110 section 11.1: the auth-scheme is case-insensitive; proxies and some
        # clients send "bearer" (wave-2 2026-11-09, SEC-06). The TOKEN keeps its case.
        assert bearer_token("bearer abc") == "abc"
        assert bearer_token("BEARER Abc") == "Abc"
        assert bearer_token("Bearerabc") == ""


class TestSlidingWindowRateLimiter:
    def _clock(self):
        state = {"t": 1000.0}
        def now():
            return state["t"]
        return state, now

    def test_allows_up_to_limit_then_blocks(self):
        state, now = self._clock()
        lim = SlidingWindowRateLimiter(2, 60.0, now_fn=now)
        assert lim.allow("a") == (True, 0.0)
        assert lim.allow("a") == (True, 0.0)
        allowed, retry = lim.allow("a")
        assert allowed is False and retry > 0

    def test_window_slides(self):
        state, now = self._clock()
        lim = SlidingWindowRateLimiter(1, 60.0, now_fn=now)
        assert lim.allow("a")[0] is True
        assert lim.allow("a")[0] is False
        state["t"] += 61
        assert lim.allow("a")[0] is True

    def test_identities_independent(self):
        state, now = self._clock()
        lim = SlidingWindowRateLimiter(1, 60.0, now_fn=now)
        assert lim.allow("a")[0] is True
        assert lim.allow("b")[0] is True

    def test_zero_limit_unlimited(self):
        lim = SlidingWindowRateLimiter(0, 60.0)
        for _ in range(100):
            assert lim.allow("x") == (True, 0.0)


class TestFailureLogMovedHome:
    def test_shim_import_is_same_class(self):
        from atlas_plus.web.auth_failures import AuthFailureLog as Shimmed
        assert Shimmed is AuthFailureLog

    def test_throttle_counts_within_window(self):
        log = AuthFailureLog(window_seconds=60, max_failures_per_window=2)
        log.record(path="/x", cf_ip="1.2.3.4", reason="bad_key", now=100.0)
        log.record(path="/x", cf_ip="1.2.3.4", reason="bad_key", now=110.0)
        assert log.is_rate_limited("1.2.3.4", now=120.0)
        assert not log.is_rate_limited("1.2.3.4", now=200.0)  # window slid


class TestBucketCeiling:
    def test_expired_buckets_swept_at_ceiling(self):
        state = {"t": 1000.0}
        lim = SlidingWindowRateLimiter(5, 60.0, now_fn=lambda: state["t"])
        lim._MAX_BUCKETS = 50
        for i in range(50):
            lim.allow(f"attacker-{i}")
        state["t"] += 120  # every bucket expires
        lim.allow("fresh-identity")  # insertion at ceiling triggers the sweep
        assert len(lim._buckets) <= 2  # fresh one (+ at most a straggler)
