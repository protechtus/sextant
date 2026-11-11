"""Tests for atlas_plus.import_cmd (G-3): structural, provenance-stamped, never-overwrite."""
from __future__ import annotations

import json

import pytest

from atlas_plus import import_cmd


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    mem = tmp_path / "mem"
    mem.mkdir()
    monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(mem))
    return mem


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "topics").mkdir(parents=True)
    (v / ".obsidian").mkdir()
    (v / "note.md").write_text("plain note, no frontmatter", encoding="utf-8")
    (v / "topics" / "deep.md").write_text("---\nid: deep\n---\nkeeps its own frontmatter", encoding="utf-8")
    (v / ".obsidian" / "config.md").write_text("app internals", encoding="utf-8")
    return v


class TestObsidian:
    def test_report_mode_writes_nothing(self, corpus, vault):
        result = import_cmd.import_obsidian(vault, "reference/obsidian", execute=False)
        assert result["planned"] == 2 and result["imported"] == 0
        assert not (corpus / "reference").exists()

    def test_import_preserves_tree_and_adds_frontmatter(self, corpus, vault):
        result = import_cmd.import_obsidian(vault, "reference/obsidian", execute=True)
        assert result["imported"] == 2
        note = (corpus / "reference" / "obsidian" / "note.md").read_text(encoding="utf-8")
        assert note.startswith("---") and "imported_from: obsidian" in note
        deep = (corpus / "reference" / "obsidian" / "topics" / "deep.md").read_text(encoding="utf-8")
        assert deep.startswith("---\nid: deep")  # untouched frontmatter

    def test_obsidian_internals_skipped(self, corpus, vault):
        import_cmd.import_obsidian(vault, "reference/obsidian", execute=True)
        assert not (corpus / "reference" / "obsidian" / ".obsidian").exists()

    def test_never_overwrites(self, corpus, vault):
        target_dir = corpus / "reference" / "obsidian"
        target_dir.mkdir(parents=True)
        (target_dir / "note.md").write_text("precious existing", encoding="utf-8")
        import_cmd.import_obsidian(vault, "reference/obsidian", execute=True)
        assert (target_dir / "note.md").read_text(encoding="utf-8") == "precious existing"
        assert (target_dir / "note_2.md").exists()


def _export(tmp_path, conversations):
    f = tmp_path / "conversations.json"
    f.write_text(json.dumps(conversations), encoding="utf-8")
    return f


def _conv(title, ts, turns):
    mapping = {}
    for i, (role, text) in enumerate(turns):
        mapping[f"n{i}"] = {"message": {
            "author": {"role": role},
            "create_time": ts + i,
            "content": {"parts": [text]},
        }}
    return {"title": title, "create_time": ts, "mapping": mapping}


class TestChatGPT:
    def test_import_writes_dated_markdown(self, corpus, tmp_path):
        export = _export(tmp_path, [
            _conv("Auth design chat", 1735689600, [  # 2025-01-01
                ("user", "how should auth work?"),
                ("assistant", "use bearer tokens"),
            ]),
        ])
        result = import_cmd.import_chatgpt(export, execute=True)
        assert result["imported"] == 1
        files = list((corpus / "reference" / "imports" / "chatgpt").rglob("*.md"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert "imported_from: chatgpt" in text
        assert "## user" in text and "bearer tokens" in text
        assert "2025-01" in str(files[0])

    def test_empty_conversations_skipped(self, corpus, tmp_path):
        export = _export(tmp_path, [
            _conv("Empty", 1735689600, []),
            {"title": "No mapping", "create_time": 1735689600},
        ])
        result = import_cmd.import_chatgpt(export, execute=True)
        assert result["imported"] == 0 and result["skipped_empty"] == 2

    def test_system_messages_dropped(self, corpus, tmp_path):
        export = _export(tmp_path, [
            _conv("Sys", 1735689600, [("system", "you are helpful"), ("user", "hi")]),
        ])
        import_cmd.import_chatgpt(export, execute=True)
        text = next((corpus / "reference" / "imports" / "chatgpt").rglob("*.md")).read_text(encoding="utf-8")
        assert "you are helpful" not in text

    def test_report_mode_counts_without_writing(self, corpus, tmp_path):
        export = _export(tmp_path, [_conv("A", 1735689600, [("user", "x")])])
        result = import_cmd.import_chatgpt(export, execute=False)
        assert result["imported"] == 0 and result["would_import"] == 1  # report mode writes nothing
        assert not (corpus / "reference").exists()

    def test_bad_shape_raises(self, corpus, tmp_path):
        f = tmp_path / "conversations.json"
        f.write_text('{"weird": true}', encoding="utf-8")
        with pytest.raises(ValueError):
            import_cmd.import_chatgpt(f, execute=True)


def _claude_conv(name, created_at, turns):
    return {
        "uuid": "u-1",
        "name": name,
        "created_at": created_at,
        "chat_messages": [{"sender": s, "text": t} for s, t in turns],
    }


class TestClaude:
    def test_import_json(self, corpus, tmp_path):
        f = tmp_path / "conversations.json"
        f.write_text(json.dumps([
            _claude_conv("Launch planning", "2026-03-05T10:00:00Z",
                         [("human", "plan the launch"), ("assistant", "four sittings")]),
        ]), encoding="utf-8")
        result = import_cmd.import_claude(f, execute=True)
        assert result["imported"] == 1
        files = list((corpus / "reference" / "imports" / "claude").rglob("*.md"))
        text = files[0].read_text(encoding="utf-8")
        assert "imported_from: claude" in text and "four sittings" in text
        assert "2026-03" in str(files[0])

    def test_import_zip(self, corpus, tmp_path):
        import zipfile
        z = tmp_path / "claude-export.zip"
        payload = json.dumps([_claude_conv("Zipped", "2026-04-01T00:00:00Z",
                                           [("human", "hi"), ("assistant", "hello")])])
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("data/conversations.json", payload)
        result = import_cmd.import_claude(z, execute=True)
        assert result["imported"] == 1

    def test_empty_skipped_and_report_mode(self, corpus, tmp_path):
        f = tmp_path / "conversations.json"
        f.write_text(json.dumps([_claude_conv("Empty", "2026-04-01T00:00:00Z", [])]), encoding="utf-8")
        result = import_cmd.import_claude(f, execute=False)
        assert result["skipped_empty"] == 1
        assert not (corpus / "reference").exists()

    def test_bad_shape_raises(self, corpus, tmp_path):
        f = tmp_path / "conversations.json"
        f.write_text('{"not": "a list"}', encoding="utf-8")
        with pytest.raises(ValueError):
            import_cmd.import_claude(f, execute=True)


class TestZipHardening:
    """Run 41 hardening: the import zip is the one ingestion surface that read
    attacker-shapeable compressed data without a size bound, and a corrupt zip
    escaped main()'s friendly error path as a raw BadZipFile traceback."""

    def test_decompression_bomb_refused(self, corpus, tmp_path, monkeypatch):
        import zipfile
        monkeypatch.setattr(import_cmd, "_MAX_EXPORT_MEMBER_BYTES", 1024)
        z = tmp_path / "bomb.zip"
        payload = json.dumps([_claude_conv("Big", "2026-04-01T00:00:00Z",
                                           [("human", "x" * 4096)])])
        with zipfile.ZipFile(z, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("conversations.json", payload)
        with pytest.raises(ValueError, match="decompression bomb"):
            import_cmd.import_claude(z, execute=True)
        # refused before any corpus write
        assert not (corpus / "reference").exists()

    def test_under_cap_zip_still_imports(self, corpus, tmp_path):
        import zipfile
        z = tmp_path / "ok.zip"
        payload = json.dumps([_claude_conv("Small", "2026-04-01T00:00:00Z",
                                           [("human", "hi"), ("assistant", "yo")])])
        with zipfile.ZipFile(z, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("conversations.json", payload)
        assert import_cmd.import_claude(z, execute=True)["imported"] == 1

    def test_corrupt_zip_raises_value_error_not_badzipfile(self, corpus, tmp_path):
        z = tmp_path / "corrupt.zip"
        z.write_bytes(b"PK\x03\x04 this is not really a zip archive")
        with pytest.raises(ValueError, match="not a valid zip"):
            import_cmd.import_claude(z, execute=True)


class TestObsidianReimportIdempotent:
    """Edge probe (quality loop run 16): re-running the same vault import
    previously duplicated EVERY note via the collision suffix (2 notes ->
    4 corpus files). Unchanged notes must skip; changed notes still land
    as a new suffixed copy (never overwriting)."""

    def test_unchanged_reimport_skips(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
        from atlas_plus import import_cmd
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "alpha.md").write_text("alpha body", encoding="utf-8")
        (vault / "beta.md").write_text("beta body", encoding="utf-8")
        first = import_cmd.import_obsidian(vault, "reference/obsidian", execute=True)
        assert first["imported"] == 2
        second = import_cmd.import_obsidian(vault, "reference/obsidian", execute=True)
        assert second["imported"] == 0
        assert second["skipped_already_imported"] == 2
        assert sum(1 for _ in (tmp_path / "mem").rglob("*.md")) == 2

    def test_changed_note_lands_as_new_copy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ATLAS_MEMORY_ROOT", str(tmp_path / "mem"))
        from atlas_plus import import_cmd
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "gamma.md"
        note.write_text("version one", encoding="utf-8")
        import_cmd.import_obsidian(vault, "reference/obsidian", execute=True)
        note.write_text("version two", encoding="utf-8")
        result = import_cmd.import_obsidian(vault, "reference/obsidian", execute=True)
        assert result["imported"] == 1  # new suffixed copy, original untouched
        files = sorted(p.name for p in (tmp_path / "mem").rglob("gamma*.md"))
        assert len(files) == 2


class TestZeroPlannedHint:
    """A wrong-but-existing path (vault parent, typo'd sibling dir) reported a
    silent `planned: 0` -- the CLI must say what it looked for and where
    (error-message sweep, quality loop run 19)."""

    def test_empty_dir_prints_hint(self, corpus, tmp_path, monkeypatch, capsys):
        empty = tmp_path / "not-the-vault"
        empty.mkdir()
        monkeypatch.setattr("sys.argv", ["atlas-import", str(empty)])
        import_cmd.main()
        captured = capsys.readouterr()
        assert "Nothing to import" in captured.err
        assert "markdown notes" in captured.err
        assert str(empty) in captured.err
        # And the report-only reminder is NOT shown (there is nothing to re-run).
        assert "Re-run with --yes" not in captured.out

    def test_populated_dir_prints_no_hint(self, corpus, vault, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["atlas-import", str(vault)])
        import_cmd.main()
        captured = capsys.readouterr()
        assert "Nothing to import" not in captured.err
        assert "Re-run with --yes" in captured.out


class TestDryRunFlag:
    """--dry-run is the habitual guess for report mode; it is accepted as a
    no-op alias of the default and contradicts --yes (impatient persona,
    quality loop run 20)."""

    def test_dry_run_is_report_mode(self, corpus, vault, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["atlas-import", str(vault), "--dry-run"])
        import_cmd.main()
        captured = capsys.readouterr()
        assert '"imported": 0' in captured.out
        assert not list(corpus.rglob("*.md"))

    def test_dry_run_with_yes_errors(self, corpus, vault, monkeypatch, capsys):
        monkeypatch.setattr(
            "sys.argv", ["atlas-import", str(vault), "--dry-run", "--yes"])
        with pytest.raises(SystemExit) as exc:
            import_cmd.main()
        assert exc.value.code == 2
        assert "contradict" in capsys.readouterr().err
