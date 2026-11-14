"""AG59 (sweep 18): forwarded loopback is the operator, `[::1]` is loopback, lockout lines per client."""
import logging

from atlas_plus.auth import forwarded_ip


def test_same_host_proxy_naming_loopback_is_not_a_source():
    for v in ("127.0.0.1", "::1", "[::1]", "[::1]:443", "::ffff:127.0.0.1"):
        assert forwarded_ip({"Cf-Connecting-Ip": v}, "127.0.0.1") == "", v
        assert forwarded_ip({"X-Forwarded-For": v}, "127.0.0.1") == "", v


def test_real_forwarded_client_is_still_named():
    assert forwarded_ip({"Cf-Connecting-Ip": "203.0.113.7"}, "127.0.0.1") == "203.0.113.7"
    assert forwarded_ip({"X-Forwarded-For": "203.0.113.7, [::1]"}, "::1") == "203.0.113.7"


def test_listed_non_loopback_proxy_naming_loopback_stays_counted():
    trusted = lambda p: p == "172.17.0.1"  # noqa: E731
    assert forwarded_ip({"Cf-Connecting-Ip": "127.0.0.1"}, "172.17.0.1", trusted=trusted) == "127.0.0.1"


def test_lockout_lines_are_per_client_and_bounded(caplog):
    from atlas_plus.web import app as web

    web._LOCK_WARNED.clear()
    web._LOCK_NAMED.clear()
    with caplog.at_level(logging.WARNING, logger="atlas.web"):
        for i in range(12):
            web._warn_lockout(f"198.51.100.{i}", "127.0.0.1")
            web._warn_lockout(f"198.51.100.{i}", "127.0.0.1")  # the same client again: no second line
    lines = [r.getMessage() for r in caplog.records if "locked out" in r.getMessage() or "not named" in r.getMessage()]
    assert len([x for x in lines if "is locked out" in x]) == web._LOCK_NAMED_PER_PEER
    assert len([x for x in lines if "not named" in x]) == 1
    assert "198.51.100.0 is locked out" in lines[0]


def test_switch_tab_ignores_a_tab_that_is_not_installed():
    from atlas_plus.web import app as web

    page = web._html("")
    assert "if(!document.getElementById('tab-'+name))return;" in page
