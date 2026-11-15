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


# ---------------------------------------------------------------- MN62 (manual wave 20, locales)
_S = "Xk9#" + "mQ2vLp7z"


@pytest.mark.parametrize("text", [
    "\u0130STANBUL VPN \u015e\u0130FRES\u0130 " + _S,                       # uppercase Turkish
    "\u015eifr\u0259: " + _S,                                                  # Azeri
    "Bak\u0131 ofisinin parolu " + _S + " idi.",
    "\uff8a\uff9f\uff7d\uff9c\uff70\uff84\uff9e " + _S,                   # half-width katakana
    "\uff50\uff41\uff53\uff53\uff57\uff4f\uff52\uff44\u3000" + _S,     # full-width word
    __import__("unicodedata").normalize("NFD", "\ube44\ubc00\ubc88\ud638 ") + _S,  # NFD Hangul
    "\u0643\u064e\u0644\u0650\u0645\u064e\u0629\u064f \u0627\u0644\u0645\u0640\u0640\u0640\u0631\u0648\u0631 \u0644\u0644\u062e\u0627\u062f\u0645 \u0647\u064a " + _S,  # tatweel
    "Min adgangskode er " + _S,                                                 # ASCII Danish
    "Mitt losenord ar " + _S,
    "password is below\n" + _S,                                                # the value on the next line
    "\u041f\u0430\u0440\u043e\u043b\u044c \u0441\u0435\u0440\u0432\u0435\u0440\u0430:\n" + _S,
])
def test_mn62_leaks_are_masked(text):
    from atlas_plus.redaction import redact
    assert _S not in redact(text) and "Xk9" not in redact(text)


@pytest.mark.parametrize("text", [
    "I forgot my password for Windows11 again.",
    "Reset the password on the iPhone15 and the Office365 account.",
    "Elfelejtettem a jelszavam az iPhone15-on.",
    "A jelszava Windows11 alatt lejart.",
    "\u015eifre s\u0131f\u0131rlama ba\u011flant\u0131s\u0131 g\u00f6nderildi.",
    "Yeni \u015fifre politikas\u0131 yar\u0131n ba\u015fl\u0131yor.",
    "\u015eifre: de\u011fi\u015ftirildi",
])
def test_mn62_ordinary_sentences_stay(text):
    from atlas_plus.redaction import redact
    assert redact(text) == text


def test_mn62_search_folds_greek_sharp_s_schwa_and_facts_normalise():
    import unicodedata
    from atlas_plus.mcp.memory_server.fts import _recompose, _bigram_fallback
    from atlas_plus.curation.duplicates import _norm_fact
    assert _recompose("\u039a\u03b1\u03bb\u03b7\u03bc\u03ad\u03c1\u03b1").lower() == _recompose("\u039a\u0391\u039b\u0397\u039c\u0395\u03a1\u0391").lower()
    assert _recompose("STRA\u1e9eE").lower() == "strasse"
    assert _recompose("G\u0259nc\u0259") == "Gence"
    q = _bigram_fallback("\u0e17\u0e33\u0e19\u0e49\u0e33\u0e41\u0e02\u0e47\u0e07")
    assert "\u0e4d" not in q                                                   # recomposed sara am, like the index
    v = "Vi\u1ec7t Nam l\u00e0 n\u01b0\u1edbc"
    assert _norm_fact(v) == _norm_fact(unicodedata.normalize("NFD", v))
    assert _norm_fact("\u0130stanbul ofisi") == _norm_fact("istanbul ofisi")


# ---------------------------------------------------------------- MN63 (Stop hook day eight)
def test_bom_day_log_entry_is_replaced_not_duplicated():
    from atlas_plus.summarizer.renderer import remove_session_entry
    day = "\ufeff## 09:00 work\n<!-- session:s-long turns:188 -->\nold body\n<!-- end session:s-long -->\n\n## note\nmine\n"
    out = remove_session_entry(day, "<!-- session:s-long")
    assert "turns:188" not in out and "old body" not in out and "mine" in out


def test_nul_in_session_id_and_wrong_encoding_payload(monkeypatch, tmp_path):
    t = tmp_path / ".claude" / "projects" / "p" / "s.jsonl"
    t.parent.mkdir(parents=True)
    t.write_text("", encoding="utf-8")
    body = json.dumps({"session_id": "ab\u0000cd", "transcript_path": str(t)})
    root, he = _payload(monkeypatch, body.encode("utf-8"))
    assert he._SESSION["id"] == "abcd" and root.endswith("projects")
    root, he = _payload(monkeypatch, json.dumps({"session_id": "x", "transcript_path": "C:/Users/Jos\u00e9/a/b.jsonl"}, ensure_ascii=False).encode("cp1252"))
    assert root == "" and he._PAYLOAD["garbled"] and he._PAYLOAD["encoding"]


def test_facts_done_marker_is_per_workspace(tmp_path, monkeypatch):
    from atlas_plus.curation import suggest_facts as sf
    monkeypatch.setattr(sf.atlas_paths, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(sf.atlas_paths, "memory_root", lambda: tmp_path / "ws2")
    a = sf._session_done_marker("s1")
    monkeypatch.setattr(sf.atlas_paths, "memory_root", lambda: tmp_path / "ws3")
    b = sf._session_done_marker("s1")
    assert a != b and a.parent == b.parent


# ---------------------------------------------------------------- MN65, MN66
def test_search_on_a_huge_note_is_fast_and_has_a_snippet(tmp_path, monkeypatch):
    import time
    from atlas_plus.mcp.memory_server import fts
    mem = tmp_path / "mem"
    (mem / "reference").mkdir(parents=True)
    (mem / "reference" / "log.md").write_text("the server logged error code abc\n" * 30000, encoding="utf-8")  # ~1M chars
    (mem / "reference" / "small.md").write_text("one error here", encoding="utf-8")
    monkeypatch.setattr(fts, "MEMORY_ROOT", mem)
    monkeypatch.setattr(fts, "FTS_DB", tmp_path / "state" / "fts.sqlite")
    fts.rebuild(force=True)
    t0 = time.perf_counter()
    res = fts.search("error", limit=3)
    assert time.perf_counter() - t0 < 4.0, "snippet() over every hit of a 1M-char note took minutes (MN66)"
    big = [r for r in res if r["path"].endswith("log.md")]
    assert big and "[error]" in big[0]["snippet"]


def test_reset_all_refuses_before_wiping_when_state_would_refuse(tmp_path, monkeypatch):
    from atlas_plus import uninstall_cmd as u
    state = tmp_path / "state"
    state.mkdir()
    (state / u.STATE_MARKER).write_text("", encoding="utf-8")                    # marker, no Sextant state at all
    monkeypatch.setattr(u.atlas_paths, "state_dir", lambda: state)
    monkeypatch.setattr(u, "validate_destructive_target", lambda *a, **k: "")
    assert u.derived_state_refusal().startswith("REFUSED")
    (state / "logs").mkdir()
    (state / "logs" / "activity.ndjson").write_text("{}\n", encoding="utf-8")   # what Sextant itself wrote
    assert u.derived_state_refusal() is None                                     # a second reset is "nothing to clear", not REFUSED


def test_sync_names_and_inbox_folders(tmp_path):
    from atlas_plus.sync import devices, apply
    reg = tmp_path / "devices.json"
    for bad in ("lap\u200btop", "\u202eevil", ".", "..", "con", "NUL", "COM1", "laptop.", "a*b", "<x>", "%APPDATA%"):
        with pytest.raises(ValueError):
            devices.pair(bad, path=reg)
    devices.pair("\u00e9mile", path=reg)
    with pytest.raises(ValueError):
        devices.pair("e\u0301mile", path=reg)                                  # the NFD spelling is the same device
    root = tmp_path / "hub"
    root.mkdir()
    a = apply.inbox(root, "x.", "reference/a.md", b"same")
    b = apply.inbox(root, "x", "reference/a.md", b"same")
    assert a.parent != b.parent                                                 # `x.` and `x` no longer share an inbox (MN65)


def test_bom_on_a_piped_confirmation_path_and_crlf_dry_run(tmp_path, monkeypatch):
    from atlas_plus import import_cmd
    mem = tmp_path / "mem"
    mem.mkdir()
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "crlf.md").write_bytes(b"ab\r\n" * 300)                          # 1,200 raw, 900 after LF normalisation
    monkeypatch.setattr(import_cmd, "MAX_IMPORT_CHARS", 1000)
    dry = import_cmd.import_obsidian(vault, "reference/obsidian", execute=False)
    real = import_cmd.import_obsidian(vault, "reference/obsidian", execute=True)
    assert dry["truncated"] == [] and real["truncated"] == []                   # the dry run and the write agree


def test_sextant_dash_h_is_help():
    from typer.testing import CliRunner
    from atlas_plus import cli
    r = CliRunner().invoke(cli.app, ["-h"])
    assert r.exit_code == 0 and "Usage" in r.output

