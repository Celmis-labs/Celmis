"""Tests для RepoIndexCache — TTL + ref-count + cold tier load."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.review.lifecycle import (
    CacheLoadError,
    RepoIndexCache,
)
from src.review.settings import ReviewSettings
from src.review.storage import LocalSnapshotBackend


@pytest.fixture
def fake_db(tmp_path) -> Path:
    db = tmp_path / "graph.fdblite"
    db.write_bytes(b"FALKORDB" + b"\x00" * 1024)
    return db


@pytest.fixture
def cache(tmp_path) -> RepoIndexCache:
    """Build cache з isolated local cache dir + small TTL для testing."""
    cache_dir = tmp_path / "cache"
    snap_root = tmp_path / "snapshots"
    settings = ReviewSettings(
        local_cache_dir=cache_dir,
        hot_cache_size=3,
        hot_ttl_seconds=2,
    )
    settings.ensure_directories()
    backend = LocalSnapshotBackend(root=snap_root)
    return RepoIndexCache(settings=settings, backend=backend)


# ─── Cache miss / load ──────────────────────────────────────────


class TestColdLoad:
    def test_load_from_snapshot(self, cache, fake_db, monkeypatch) -> None:
        # Use snapshot backend
        cache.backend.upload_snapshot("repo-1", "sha-1", fake_db)

        # Force settings.repo_graph_path() to non-existent file (so cache не
        # use existing graph)
        monkeypatch.setattr(
            "src.config.get_settings",
            lambda: type("S", (), {
                "repo_graph_path": lambda self, slug: Path("/nonexistent")
            })(),
        )

        with cache.session("repo-1") as (path, sha):
            assert path.exists()
            assert sha == "sha-1"

    def test_load_miss_raises_without_callback(self, cache, monkeypatch) -> None:
        monkeypatch.setattr(
            "src.config.get_settings",
            lambda: type("S", (), {
                "repo_graph_path": lambda self, slug: Path("/nonexistent")
            })(),
        )
        with pytest.raises(CacheLoadError), cache.session("ghost-repo"):
            pass

    def test_on_load_miss_callback(self, tmp_path, monkeypatch, fake_db) -> None:
        cache_dir = tmp_path / "cache"
        settings = ReviewSettings(
            local_cache_dir=cache_dir,
            hot_cache_size=3,
            hot_ttl_seconds=10,
        )
        settings.ensure_directories()
        backend = LocalSnapshotBackend(root=tmp_path / "snap")

        rebuild_calls = []

        def _rebuild(slug: str, target: Path) -> str | None:
            rebuild_calls.append(slug)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(fake_db.read_bytes())
            return "rebuilt-sha"

        monkeypatch.setattr(
            "src.config.get_settings",
            lambda: type("S", (), {
                "repo_graph_path": lambda self, slug: Path("/nonexistent")
            })(),
        )

        cache = RepoIndexCache(
            settings=settings, backend=backend, on_load_miss=_rebuild,
        )
        with cache.session("missing-repo") as (path, sha):
            assert path.exists()
            assert sha == "rebuilt-sha"

        assert rebuild_calls == ["missing-repo"]


# ─── Reference counting ────────────────────────────────────────


class TestRefCount:
    def test_concurrent_sessions_share_entry(self, cache, fake_db, monkeypatch) -> None:
        cache.backend.upload_snapshot("shared-repo", "sha1", fake_db)
        monkeypatch.setattr(
            "src.config.get_settings",
            lambda: type("S", (), {
                "repo_graph_path": lambda self, slug: Path("/nonexistent")
            })(),
        )

        # Дві concurrent sessions — share same cache entry
        with (
            cache.session("shared-repo") as (path1, _),
            cache.session("shared-repo") as (path2, _),
        ):
            assert path1 == path2  # same warmed file

        stats = cache.stats()
        # Після обох exit refcount = 0
        for entry in stats["entries"]:
            if entry["slug"] == "shared-repo":
                assert entry["refcount"] == 0

    def test_release_after_session(self, cache, fake_db, monkeypatch) -> None:
        cache.backend.upload_snapshot("r", "sha", fake_db)
        monkeypatch.setattr(
            "src.config.get_settings",
            lambda: type("S", (), {
                "repo_graph_path": lambda self, slug: Path("/nonexistent")
            })(),
        )

        with cache.session("r"):
            pass
        # Refcount has decremented
        for entry in cache.stats()["entries"]:
            if entry["slug"] == "r":
                assert entry["refcount"] == 0


# ─── TTL eviction ──────────────────────────────────────────────


class TestTTL:
    def test_entry_expires_after_ttl(self, cache, fake_db, monkeypatch) -> None:
        cache.backend.upload_snapshot("temp-repo", "sha1", fake_db)
        monkeypatch.setattr(
            "src.config.get_settings",
            lambda: type("S", (), {
                "repo_graph_path": lambda self, slug: Path("/nonexistent")
            })(),
        )

        with cache.session("temp-repo"):
            assert cache.stats()["size"] == 1

        # Wait > TTL (cache fixture has TTL=2s)
        time.sleep(2.5)
        cache._evict_expired()
        assert cache.stats()["size"] == 0


# ─── Manual eviction + janitor ─────────────────────────────────


class TestEvictionAndJanitor:
    def test_manual_evict(self, cache, fake_db, monkeypatch) -> None:
        cache.backend.upload_snapshot("r", "sha", fake_db)
        monkeypatch.setattr(
            "src.config.get_settings",
            lambda: type("S", (), {
                "repo_graph_path": lambda self, slug: Path("/nonexistent")
            })(),
        )
        with cache.session("r"):
            pass
        assert cache.evict("r") is True
        assert cache.stats()["size"] == 0

    def test_evict_nonexistent_returns_false(self, cache) -> None:
        assert cache.evict("never-existed") is False

    def test_janitor_start_stop(self, cache) -> None:
        cache.start_janitor(interval_seconds=1)
        assert cache._janitor_thread is not None
        assert cache._janitor_thread.is_alive()
        cache.stop_janitor()
        assert cache._janitor_thread is None

    def test_existing_graph_used_first(
        self, cache, fake_db, tmp_path, monkeypatch,
    ) -> None:
        """Якщо settings.repo_graph_path() existsує → use, не snapshot."""
        existing_graph = tmp_path / "existing.fdblite"
        existing_graph.write_bytes(b"EXISTING")

        monkeypatch.setattr(
            "src.config.get_settings",
            lambda: type("S", (), {
                "repo_graph_path": lambda self, slug: existing_graph
            })(),
        )

        with cache.session("any-slug") as (path, sha):
            assert path == existing_graph
            assert sha == "current"
