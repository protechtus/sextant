"""Sweep 20 agentic wave (AG69, AG71, AG72): functional tests that run the fixed paths."""
from __future__ import annotations

import io
import json
import sys

import pytest


class _Stdin:
    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)

    def isatty(self):
        return False

    def read(self):
        return self.buffer.getvalue().decode("utf-8", "replace")


def _payload(monkeypatch, data: bytes):
    from atlas_plus.summarizer import hook_entry
    hook_entry._PAYLOAD.update(seen=False, empty=False, garbled=False, transcript_file="", refused="")
    hook_entry._SESSION.pop("id", None)
    monkeypatch.setattr(sys, "stdin", _Stdin(data))
    return hook_entry._transcript_root_from_payload(), hook_entry


def test_hook_payload_bytes_accent_utf16_root_and_garbage(tmp_path, monkeypatch):
    t = tmp_path / ".claude" / "projects" / "E--caf\u00e9-Jos\u00e9" / "s.jsonl"
    t.parent.mkdir(parents=True)
    t.write_text("", encoding="utf-8")
    body = json.dumps({"session_id": "a1", "transcript_path": str(t)}, ensure_ascii=False)
    root, he = _payload(monkeypatch, body.encode("utf-8"))                     # UTF-8 bytes with an accent
    assert root.endswith("projects") and he._PAYLOAD["transcript_file"] and not he._PAYLOAD["refused"]
    root, he = _payload(monkeypatch, body.encode("utf-16"))                    # PowerShell Out-File: BOM + UTF-16
    assert root.endswith("projects") and not he._PAYLOAD["garbled"]
    root, he = _payload(monkeypatch, json.dumps({"session_id": "a2", "transcript_path": "E:/x.jsonl"}).encode())
    assert root == "" and he._PAYLOAD["refused"]                                # a drive-root path is refused, never a scan
    root, he = _payload(monkeypatch, b"\xff\x00{not json")                    # undecodable: refused, never a whole-day run
    assert root == "" and he._PAYLOAD["seen"] and he._PAYLOAD["garbled"]


def test_facts_failure_records_the_real_line_count(tmp_path, monkeypatch):
    from atlas_plus.curation import suggest_facts as sf

    monkeypatch.setattr(sf.atlas_paths, "state_dir", lambda: tmp_path)

    def boom(*a, **k):
        sf._FACTS_N_LINES["c9"] = 42
        raise RuntimeError("junk reply")

    import inspect
    boom.__signature__ = inspect.signature(sf._suggest_facts_run)  # the wrapper binds the real parameters
    monkeypatch.setattr(sf, "_suggest_facts_run", boom)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            sf.suggest_facts(conversation_id="c9", dry_run=False)
    assert json.loads(sf._facts_failure_file("c9").read_text(encoding="utf-8")) == {"lines": 42, "count": 3}
    assert sf._facts_gave_up("c9", 42)


def test_absurd_pid_is_a_dead_owner(tmp_path):
    from atlas_plus.summarizer.renderer import _lock_owner_dead
    lock = tmp_path / "write.lock"
    lock.write_text("99999999999999999999", encoding="ascii")
    assert _lock_owner_dead(lock) is True


def test_verify_names_a_broken_cert_file(tmp_path):
    from atlas_plus.roundtrip import _cert_file_problem
    assert _cert_file_problem(str(tmp_path / "nope.crt")) == "does not exist"
    assert _cert_file_problem(str(tmp_path)) == "is a directory"
    (tmp_path / "empty.crt").write_bytes(b"")
    assert _cert_file_problem(str(tmp_path / "empty.crt")) == "is empty"
    (tmp_path / "junk.crt").write_bytes(b"garbage")
    assert _cert_file_problem(str(tmp_path / "junk.crt")) == "holds no PEM certificate"
    (tmp_path / "ok.crt").write_bytes(b"-----BEGIN " + b"CERTIFICATE-----\nMIIB\n-----END " + b"CERTIFICATE-----\n")
    assert _cert_file_problem(str(tmp_path / "ok.crt")) == ""


def test_piped_question_marks_at_word_edges():
    from atlas_plus.cli import _stdin_looks_mangled
    assert _stdin_looks_mangled("?lan is the project name")
    assert _stdin_looks_mangled("?")
    assert _stdin_looks_mangled("caf? na?ve")
    assert not _stdin_looks_mangled("is it done? yes")


def test_device_names_are_validated(tmp_path):
    from atlas_plus.sync import devices
    reg = tmp_path / "devices.json"
    for bad in ("", " ", "nl\nname", "tab\tname", "../x", "a\\b", "C:x", "x" * 65, " lead"):
        with pytest.raises(ValueError):
            devices.pair(bad, path=reg)
    devices.pair("laptop", path=reg)
    with pytest.raises(ValueError):
        devices.pair("LAPTOP", path=reg)


def test_reset_state_recognises_a_stock_install(tmp_path, monkeypatch):
    from atlas_plus import uninstall_cmd as u
    state = tmp_path / "state"
    state.mkdir()
    (state / u.STATE_MARKER).write_text("", encoding="utf-8")
    (state / "logs").mkdir()
    (state / "search.sqlite").write_bytes(b"x")
    monkeypatch.setattr(u.atlas_paths, "state_dir", lambda: state)
    monkeypatch.setattr(u, "validate_destructive_target", lambda *a, **k: "")
    out = u.clear_derived_state(yes=True)
    assert out.startswith("cleared derived") and "search.sqlite" in out and not (state / "search.sqlite").exists()
    assert (state / "logs").is_dir()                                            # logs are not derived: kept


@pytest.mark.parametrize("text", [
    "the root password on the box is Xy9kQ2vLm7",
    "das Passwort des Servers lautet De4!kQ9wLm",
    "\u0631\u0645\u0632 \u0639\u0628\u0648\u0631 \u0633\u0631\u0648\u0631 Fa6$kL3mNq \u0627\u0633\u062a",
    "\u05d4\u05b7\u05e1\u05bc\u05b4\u05d9\u05e1\u05b0\u05de\u05b8\u05d4 \u05d4\u05b4\u05d9\u05d0 He7#mK2wQp",
    "\u043f\u0430\u0440\u043e\u043b\u044c \u043d\u043e\u0432\u044b\u0439: Ru3$kL8mPq",
    "kata sandi server adalah Id6#tR4kLm",
    "\u5bc6\u7801\u662fXy9kQ2vLm7",
])
def test_keyword_line_pass_masks_qualified_prose(text):
    from atlas_plus.redaction import redact
    out = redact(text)
    assert "[REDACTED]" in out and not any(v in out for v in ("Xy9kQ2vLm7", "De4!kQ9wLm", "Fa6$kL3mNq", "He7#mK2wQp", "Ru3$kL8mPq", "Id6#tR4kLm"))


@pytest.mark.parametrize("text", [
    "the password policy requires twelve characters",
    "reset your password at the portal, see RFC9110",
    "getPassword2() returns the stored value",
    "password hashing uses argon2id v1.3",
    "commit 3a4a879 changed the password page",
])
def test_keyword_line_pass_leaves_prose_alone(text):
    from atlas_plus.redaction import redact
    assert redact(text) == text


def test_turkish_i_folds_for_search():
    from atlas_plus.mcp.memory_server.fts import _recompose
    assert _recompose("Diyarbak\u0131r").lower() == _recompose("D\u0130YARBAKIR").lower() == "diyarbakir"
