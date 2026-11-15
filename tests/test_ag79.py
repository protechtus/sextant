"""AG79 (sweep 22): expert review of the sweep 21 fold.

Every test runs the fixed path and checks the outcome; none pins source text.
"""
from __future__ import annotations

import shutil
import types

import pytest


# ---------------------------------------------------------------- value-slot redaction: the gap rule
@pytest.mark.parametrize("line", [
    "The password for the staging database is Summer2024",
    "the password for the production database server is Natsu2024",
    "the wifi password, which I set yesterday, is Natsu2024",
    "das Passwort für den Staging-Server ist Natsu2024",
])
def test_a_long_qualifier_still_binds_the_cue(line):
    from atlas_plus.redaction import redact
    out = redact(line, env={})
    assert "Natsu2024" not in out and "Summer2024" not in out and "[REDACTED]" in out


@pytest.mark.parametrize("line", [
    "Password hashing in this release moved to Argon2id",
    "The password hashing algorithm we chose is Argon2id",
    "Reset the password on Windows11 before the upgrade",
])
def test_names_beside_a_password_word_still_stay(line):
    from atlas_plus.redaction import redact
    assert redact(line, env={}) == line


# ---------------------------------------------------------------- the carry rule: siblings of MN68
@pytest.mark.parametrize("text", [
    "USER: password is  # see next line\nNatsu2024",
    "password is:\n- Natsu2024",
    "password is:\n> Natsu2024",
    "the password is:\n1. Natsu2024",
])
def test_a_carried_value_is_masked_behind_a_comment_or_a_list_mark(text):
    from atlas_plus.redaction import redact
    out = redact(text, env={})
    assert "Natsu2024" not in out and "[REDACTED]" in out


def test_a_list_after_a_password_line_without_a_cue_keeps_names():
    from atlas_plus.redaction import redact
    text = "Change the password\n- Installation2"
    assert redact(text, env={}) == text


# ---------------------------------------------------------------- the facts prompt redacts across turns
def test_the_facts_prompt_masks_a_value_on_the_next_turn(monkeypatch):
    from atlas_plus.summarizer import loader
    from atlas_plus.curation import suggest_facts as sf
    T = types.SimpleNamespace
    monkeypatch.setattr(loader, "load_turns", lambda **kw: [
        T(role="assistant", content="What is the new database password?"),
        T(role="user", content="the password is"),
        T(role="user", content="Natsu2024"),
        T(role="assistant", content="Saved. Next we install Python3.11."),
    ])
    text = sf._load_transcript_text(conversation_id="x")
    assert "Natsu2024" not in text
    assert text.splitlines()[2] == "USER: [REDACTED]"
    assert text.splitlines()[0] == "ASSISTANT: What is the new database password?"
    assert "Python3.11" in text


# ---------------------------------------------------------------- the hub generation: a restored hub that caught up
def _hub_two_devices(tmp_path, monkeypatch):
    hub, laptop, phone = tmp_path / "hub", tmp_path / "laptop", tmp_path / "phone"
    for d in (hub, laptop, phone):
        (d / "reference").mkdir(parents=True)
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(hub))
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ATLAS_SYNC_MANIFEST_TTL", "0")
    from fastapi.testclient import TestClient
    from atlas_plus.sync import api, devices
    from atlas_plus.sync.client import SyncClient
    _, t1 = devices.pair("laptop")
    _, t2 = devices.pair("phone")
    app = api.create_app()
    lc = SyncClient(token=t1, http_client=TestClient(app, raise_server_exceptions=False))
    pc = SyncClient(token=t2, http_client=TestClient(app, raise_server_exceptions=False))
    return hub, laptop, phone, lc, pc


@pytest.mark.parametrize("restore_marks", [True, False])
def test_a_restored_hub_that_caught_up_does_not_roll_a_device_back(tmp_path, monkeypatch, restore_marks):
    from atlas_plus.sync import store
    hub, laptop, phone, lc, pc = _hub_two_devices(tmp_path, monkeypatch)
    (laptop / "reference" / "a.md").write_text("v1", encoding="utf-8")
    lc.sync_once(laptop)
    shutil.copytree(hub, tmp_path / "bk" / "hub")
    shutil.copytree(store.sync_dir(), tmp_path / "bk" / "sync")
    (laptop / "reference" / "a.md").write_text("v2 newer", encoding="utf-8")
    assert lc.sync_once(laptop).pushed == ["reference/a.md"]
    shutil.rmtree(hub)
    shutil.copytree(tmp_path / "bk" / "hub", hub)
    names = ["generation", "devices.json"] + (["generation-marks"] if restore_marks else [])
    for name in names:
        shutil.copy2(tmp_path / "bk" / "sync" / name, store.sync_dir() / name)
    (phone / "reference" / "p.md").write_text("from phone", encoding="utf-8")
    assert pc.sync_once(phone).pushed == ["reference/p.md"]   # the count is back where the laptop last saw it
    r = lc.sync_once(laptop)
    assert (laptop / "reference" / "a.md").read_text(encoding="utf-8") == "v2 newer"
    assert "reference/a.md" not in r.pulled
    assert any("restored from a backup" in e for e in r.errors)


def test_an_ordinary_second_device_push_is_not_a_rollback(tmp_path, monkeypatch):
    hub, laptop, phone, lc, pc = _hub_two_devices(tmp_path, monkeypatch)
    (laptop / "reference" / "a.md").write_text("v1", encoding="utf-8")
    lc.sync_once(laptop)
    pc.sync_once(phone)
    (phone / "reference" / "a.md").write_text("v2 from phone", encoding="utf-8")
    assert pc.sync_once(phone).pushed == ["reference/a.md"]
    r = lc.sync_once(laptop)
    assert r.pulled == ["reference/a.md"] and not r.errors
    assert (laptop / "reference" / "a.md").read_text(encoding="utf-8") == "v2 from phone"


# ---------------------------------------------------------------- push batching: a batch fails half-way
def test_a_failed_second_batch_keeps_the_first_batch_reported_as_pushed(tmp_path, monkeypatch):
    import httpx
    hub, laptop, _phone, lc, _pc = _hub_two_devices(tmp_path, monkeypatch)
    real = lc._post
    calls = {"push": 0}

    def flaky(path, body):
        if path.endswith("/push"):
            calls["push"] += 1
            if calls["push"] == 2:
                return httpx.Response(502, text="bad gateway")
        return real(path, body)

    monkeypatch.setattr(lc, "_post", flaky)
    for i in range(620):
        (laptop / "reference" / f"n{i:04d}.md").write_text(f"note {i}", encoding="utf-8")
    r = lc.sync_once(laptop)
    assert len(list((hub / "reference").glob("*.md"))) == 500
    assert len(r.pushed) == 500
    assert len(r.errors) == 120 and all("502" in e for e in r.errors)
    monkeypatch.setattr(lc, "_post", real)
    r2 = lc.sync_once(laptop)
    assert len(r2.pushed) == 120 and not r2.errors and not r2.conflicts


def test_a_token_refused_on_the_second_batch_is_one_sentence(tmp_path, monkeypatch):
    import httpx
    hub, laptop, _phone, lc, _pc = _hub_two_devices(tmp_path, monkeypatch)
    real = lc._post
    calls = {"push": 0}

    def refused(path, body):
        if path.endswith("/push"):
            calls["push"] += 1
            if calls["push"] >= 2:
                return httpx.Response(401, json={"detail": "unauthorized"})
        return real(path, body)

    monkeypatch.setattr(lc, "_post", refused)
    for i in range(620):
        (laptop / "reference" / f"n{i:04d}.md").write_text(f"note {i}", encoding="utf-8")
    r = lc.sync_once(laptop)
    assert len(r.pushed) == 500
    assert len(r.errors) == 1 and "rejected this device's token" in r.errors[0]
