"""Tests for atlas_plus.sync.manifest -- inventory build and three-way diff."""
from __future__ import annotations

import hashlib

import pytest

from atlas_plus.sync import manifest, policy


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """build_manifest now persists a hash cache under state_dir(); keep every
    test's cache in its own tmp world (never the operator's real .atlas)."""
    monkeypatch.setenv("ATLAS_STATE_DIR", str(tmp_path / "state"))


def _mk(tmp_path, rel, text="x"):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class TestBuildManifest:
    def test_inventories_files_with_hashes(self, tmp_path):
        _mk(tmp_path, "identity/user.md", "hello")
        result = manifest.build_manifest(tmp_path)
        entry = result["files"]["identity/user.md"]
        assert entry["sha256"] == hashlib.sha256(b"hello").hexdigest()
        assert entry["size"] == 5
        assert result["manifest_hash"]

    def test_paths_are_posix(self, tmp_path):
        _mk(tmp_path, "a/b/c.md")
        assert list(manifest.build_manifest(tmp_path)["files"]) == ["a/b/c.md"]

    def test_exclusions_apply(self, tmp_path):
        _mk(tmp_path, "keep.md")
        _mk(tmp_path, "archive/old.md")
        _mk(tmp_path, "versions/keep_2026-01-01_v1.md")
        _mk(tmp_path, ".env", "SECRET=1")
        assert list(manifest.build_manifest(tmp_path)["files"]) == ["keep.md"]

    def test_principal_filtering(self, tmp_path):
        _mk(tmp_path, "reference/sip/a.md")
        _mk(tmp_path, "identity/user.md")
        peer = policy.Principal(name="p", kind="peer", prefixes=("reference/sip/",))
        assert list(manifest.build_manifest(tmp_path, peer)["files"]) == [
            "reference/sip/a.md"
        ]

    def test_missing_root_is_empty(self, tmp_path):
        result = manifest.build_manifest(tmp_path / "nope")
        assert result["files"] == {}

    def test_files_hash_is_order_independent(self):
        a = {"x.md": {"sha256": "1"}, "y.md": {"sha256": "2"}}
        b = {"y.md": {"sha256": "2"}, "x.md": {"sha256": "1"}}
        assert manifest.files_hash(a) == manifest.files_hash(b)

    def test_walk_prunes_excluded_trees_equivalently(self, tmp_path):
        """Pruning at the walk must keep the manifest IDENTICAL to the
        policy-filtered full walk -- excluded trees simply are not entered
        (measured 97% enumeration waste on workspace-shaped corpora,
        quality loop run 35)."""
        mem = tmp_path / "mem"
        _mk(mem, "keep.md")
        _mk(mem, "reference/a.md")
        _mk(mem, "archive/old/x.md")
        _mk(mem, "versions/keep_v1.md")
        _mk(mem, ".git/objects/ab/cd")
        _mk(mem, "sub/archive/deep.md")  # excluded segment at ANY depth
        result = manifest.build_manifest(mem)
        assert set(result["files"]) == {"keep.md", "reference/a.md"}
        # And the walk itself never enumerates the excluded trees.
        enumerated = {rel for rel, _, _ in manifest._iter_files(mem)}
        assert not any(r.startswith(("archive/", "versions/", ".git/")) or "/archive/" in r
                       for r in enumerated)

    def test_files_hash_survives_surrogate_filenames(self):
        """A POSIX filename with invalid UTF-8 decodes to lone surrogates
        (PEP 383); one such name must not crash the whole manifest build
        (hardening, quality loop run 32). Hash stays deterministic."""
        weird = {"notes/bad\udcc3\udca9name.md": {"sha256": "1"}}
        h1 = manifest.files_hash(weird)
        h2 = manifest.files_hash(dict(weird))
        assert h1 == h2 and len(h1) == 64


def _age(path, seconds=10):
    """Back-date a file past the racy margin so it is cache-eligible
    (the racy-entry guard never caches files whose mtime is ~now)."""
    import os as _os
    old = path.stat().st_mtime - seconds
    _os.utime(path, (old, old))
    return path


class TestHashCache:
    """The (mtime_ns, size)-keyed digest cache (measured: 866ms -> stat-only
    for unchanged files at 2,000 files, quality loop run 21). Pure
    optimization: any cache failure must mean re-hash, never wrong hashes."""

    def test_unchanged_file_skips_rehash(self, tmp_path, monkeypatch):
        # Corpus in its own subdir -- in production state_dir (the cache
        # home) is outside the corpus; nesting it inside would make the
        # build hash its own cache file.
        mem = tmp_path / "mem"
        _age(_mk(mem, "a.md", "stable"))
        manifest.build_manifest(mem)  # warm
        calls = []
        real = manifest._sha256_file
        monkeypatch.setattr(manifest, "_sha256_file",
                            lambda p: calls.append(p) or real(p))
        result = manifest.build_manifest(mem)
        assert calls == []
        assert result["files"]["a.md"]["sha256"] == hashlib.sha256(b"stable").hexdigest()

    def test_changed_content_rehashes_correctly(self, tmp_path):
        mem = tmp_path / "mem"
        p = _age(_mk(mem, "a.md", "one"))
        manifest.build_manifest(mem)
        p.write_text("two!", encoding="utf-8")
        result = manifest.build_manifest(mem)
        assert result["files"]["a.md"]["sha256"] == hashlib.sha256(b"two!").hexdigest()

    def test_corrupt_cache_fails_open(self, tmp_path):
        mem = tmp_path / "mem"
        _age(_mk(mem, "a.md", "hello"))
        manifest.build_manifest(mem)
        cache_path = manifest._hash_cache_path(mem)
        cache_path.write_text("{not json", encoding="utf-8")
        result = manifest.build_manifest(mem)
        assert result["files"]["a.md"]["sha256"] == hashlib.sha256(b"hello").hexdigest()

    def test_deleted_files_age_out_of_cache(self, tmp_path):
        mem = tmp_path / "mem"
        p = _age(_mk(mem, "gone.md"))
        manifest.build_manifest(mem)
        p.unlink()
        manifest.build_manifest(mem)
        cache = manifest._load_hash_cache(mem)
        assert "gone.md" not in cache

    def test_principal_build_merges_instead_of_evicting(self, tmp_path):
        mem = tmp_path / "mem"
        _age(_mk(mem, "reference/sip/a.md"))
        _age(_mk(mem, "identity/user.md"))
        manifest.build_manifest(mem)  # full build caches both
        peer = policy.Principal(name="p", kind="peer", prefixes=("reference/sip/",))
        manifest.build_manifest(mem, peer)
        cache = manifest._load_hash_cache(mem)
        assert "identity/user.md" in cache  # restricted view did not evict

    def test_racy_entries_never_cached(self, tmp_path):
        """git's racy-index rule (expert pass 2026-10-05, M3): a file whose
        mtime is within the racy margin of NOW could be rewritten inside
        the same timestamp tick -- caching its digest could pin a stale
        hash forever (silent lost update on sync). Just-written files are
        hashed but not cached; aged files cache normally."""
        mem = tmp_path / "mem"
        _mk(mem, "fresh.md", "just written")
        manifest.build_manifest(mem)
        assert "fresh.md" not in manifest._load_hash_cache(mem)

    def test_aged_entries_cache_and_racy_rewrite_detected(self, tmp_path):
        import os as _os
        mem = tmp_path / "mem"
        p = _mk(mem, "aged.md", "version one")
        old = p.stat().st_mtime - 10
        _os.utime(p, (old, old))  # age the file past the racy margin
        manifest.build_manifest(mem)
        cache = manifest._load_hash_cache(mem)
        assert "aged.md" in cache  # aged file caches normally
        # Same-tick rewrite simulation: new content, SAME mtime+size, but
        # mtime now within the racy margin -> cache must NOT be trusted.
        p.write_text("version 2w0", encoding="utf-8")  # same size
        import time as _t
        now = _t.time()
        _os.utime(p, (now, now))
        out = manifest.build_manifest(mem)
        assert out["files"]["aged.md"]["sha256"] == hashlib.sha256(b"version 2w0").hexdigest()

    def test_cache_io_never_raises_systemexit(self, tmp_path, monkeypatch):
        """state_dir() can SystemExit on an unfilled placeholder; a cache
        problem may cost speed, never a hub API call (finding 8)."""
        mem = tmp_path / "mem"
        _mk(mem, "a.md", "hello")
        monkeypatch.setenv("ATLAS_STATE_DIR", "<FILL_ME_IN>")
        result = manifest.build_manifest(mem)  # must not raise
        assert "a.md" in result["files"]

    def test_distinct_roots_use_distinct_caches(self, tmp_path):
        r1, r2 = tmp_path / "r1", tmp_path / "r2"
        _age(_mk(r1, "a.md", "one"))
        _age(_mk(r2, "b.md", "two"))
        manifest.build_manifest(r1)
        manifest.build_manifest(r2)
        assert manifest._hash_cache_path(r1) != manifest._hash_cache_path(r2)
        assert "a.md" in manifest._load_hash_cache(r1)
        assert "b.md" in manifest._load_hash_cache(r2)


def _f(sha):
    return {"sha256": sha}


class TestDiff3:
    def test_in_sync(self):
        plan = manifest.diff3({"a": _f("1")}, {"a": _f("1")}, {"a": _f("1")})
        assert plan.clean and plan.in_sync == 1

    def test_remote_changed_pulls(self):
        plan = manifest.diff3({"a": _f("1")}, {"a": _f("2")}, {"a": _f("1")})
        assert plan.to_pull == ["a"] and not plan.conflicts

    def test_local_changed_pushes(self):
        plan = manifest.diff3({"a": _f("2")}, {"a": _f("1")}, {"a": _f("1")})
        assert plan.to_push == ["a"] and not plan.conflicts

    def test_both_changed_conflicts(self):
        plan = manifest.diff3({"a": _f("2")}, {"a": _f("3")}, {"a": _f("1")})
        assert plan.conflicts == ["a"]

    def test_both_changed_identically_in_sync(self):
        plan = manifest.diff3({"a": _f("2")}, {"a": _f("2")}, {"a": _f("1")})
        assert plan.clean and plan.in_sync == 1

    def test_new_local_pushes_without_base(self):
        plan = manifest.diff3({"a": _f("1")}, {}, None)
        assert plan.to_push == ["a"]

    def test_new_remote_pulls_without_base(self):
        plan = manifest.diff3({}, {"a": _f("1")}, None)
        assert plan.to_pull == ["a"]

    def test_both_new_different_conflicts(self):
        plan = manifest.diff3({"a": _f("1")}, {"a": _f("2")}, None)
        assert plan.conflicts == ["a"]

    def test_remote_delete_reported_not_pulled(self):
        plan = manifest.diff3({"a": _f("1")}, {}, {"a": _f("1")})
        assert plan.remote_deleted == ["a"]
        assert not plan.to_pull and not plan.to_push

    def test_local_delete_reported_not_pushed(self):
        plan = manifest.diff3({}, {"a": _f("1")}, {"a": _f("1")})
        assert plan.local_deleted == ["a"]

    def test_delete_vs_edit_is_conflict(self):
        plan = manifest.diff3({}, {"a": _f("2")}, {"a": _f("1")})
        assert plan.conflicts == ["a"]

    def test_dropped_on_both_sides_is_silent(self):
        plan = manifest.diff3({}, {}, {"a": _f("1")})
        assert plan.clean and plan.in_sync == 0

    def test_summary_mentions_counts(self):
        plan = manifest.diff3({"a": _f("1")}, {}, None)
        assert "push 1" in plan.summary()
