"""RepoIndexCache — TTL-based warm tier for on-demand PR review.

Stage 17.3 (May 2026).

Tiers (per research):
    Hot RAM   — TTLCache (LRU + idle TTL=10min) — N concurrent open graph DBs
    Warm SSD  — local snapshot files (decompress at need)
    Cold S3   — optional, via SnapshotBackend

Concurrency:
    - Per-repo RLock — two queries do not conflict
    - Reference counting — eviction skips if the handle is in use
    - Janitor thread cleans expired entries + closes file handles

API contract:
    cache.acquire(repo_slug) → (graph_path, refcount_token)
    cache.release(token)     → decrement refcount + maybe evict
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from cachetools import TTLCache

from src.review.settings import ReviewSettings, get_review_settings
from src.review.storage import SnapshotBackend, get_snapshot_backend

logger = logging.getLogger(__name__)


@dataclass
class _CachedRepo:
    """One in-cache repo entry."""

    repo_slug: str
    local_path: Path  # path to the warm graph file (.fdblite)
    commit_sha: str
    loaded_at: float
    refcount: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)


@dataclass(frozen=True)
class CacheToken:
    """Returned to the caller — must be passed back to release()."""

    repo_slug: str
    commit_sha: str


class RepoIndexCache:
    """Thread-safe warm cache with ref-counted eviction + janitor."""

    def __init__(
        self,
        settings: ReviewSettings | None = None,
        backend: SnapshotBackend | None = None,
        *,
        on_load_miss: Callable[[str, Path], str | None] | None = None,
    ) -> None:
        """
        Args:
            on_load_miss: callable(repo_slug, target_path) → commit_sha or None.
                Called on a cold cache miss (no local file) — the caller can
                rebuild the snapshot from scratch (for example re-index the
                repo). If None — only the snapshot backend is used.
        """
        self.settings = settings or get_review_settings()
        self.settings.ensure_directories()
        self.backend = backend or get_snapshot_backend(self.settings)
        self._cache: TTLCache[str, _CachedRepo] = TTLCache(
            maxsize=self.settings.hot_cache_size,
            ttl=self.settings.hot_ttl_seconds,
        )
        self._cache_lock = threading.Lock()
        self._on_load_miss = on_load_miss
        self._janitor_stop = threading.Event()
        self._janitor_thread: threading.Thread | None = None

    # ─── Janitor ────────────────────────────────────────────────

    def start_janitor(self, interval_seconds: int = 60) -> None:
        """Start a background thread that cleans expired cache entries."""
        if self._janitor_thread and self._janitor_thread.is_alive():
            return
        self._janitor_stop.clear()

        def _run():
            while not self._janitor_stop.wait(interval_seconds):
                try:
                    self._evict_expired()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("janitor_cycle_failed: %s", exc)

        thread = threading.Thread(target=_run, daemon=True, name="review-janitor")
        thread.start()
        self._janitor_thread = thread
        logger.info("janitor_started interval=%ds", interval_seconds)

    def stop_janitor(self) -> None:
        self._janitor_stop.set()
        if self._janitor_thread:
            self._janitor_thread.join(timeout=5)
            self._janitor_thread = None

    def _evict_expired(self) -> None:
        """Trigger TTLCache expire + cleanup zero-refcount entries."""
        with self._cache_lock:
            # cachetools.TTLCache cleans up expired keys on access — explicit expire()
            # is also OK
            self._cache.expire()

    # ─── Acquire / release ──────────────────────────────────────

    @contextmanager
    def session(self, repo_slug: str):
        """Context manager — acquire + auto-release.

        Usage:
            with cache.session("github_org-repo") as (graph_path, sha):
                store = make_graph_store(graph_path)
                ...
        """
        token, graph_path, sha = self._acquire(repo_slug)
        try:
            yield graph_path, sha
        finally:
            self._release(token)

    def _acquire(self, repo_slug: str) -> tuple[CacheToken, Path, str]:
        """Get warm graph_path. Cache hit → reuse; miss → load."""
        with self._cache_lock:
            entry = self._cache.get(repo_slug)
            if entry is not None:
                with entry.lock:
                    entry.refcount += 1
                token = CacheToken(repo_slug=repo_slug, commit_sha=entry.commit_sha)
                return token, entry.local_path, entry.commit_sha

        # Cache miss — load (outside cache lock)
        local_path, commit_sha = self._load_repo(repo_slug)

        # Insert into the cache
        with self._cache_lock:
            entry = self._cache.get(repo_slug)
            if entry is None:
                entry = _CachedRepo(
                    repo_slug=repo_slug,
                    local_path=local_path,
                    commit_sha=commit_sha,
                    loaded_at=time.time(),
                )
                self._cache[repo_slug] = entry
            with entry.lock:
                entry.refcount += 1
            token = CacheToken(repo_slug=repo_slug, commit_sha=entry.commit_sha)
            return token, entry.local_path, entry.commit_sha

    def _release(self, token: CacheToken) -> None:
        with self._cache_lock:
            entry = self._cache.get(token.repo_slug)
            if entry is None:
                return
            with entry.lock:
                entry.refcount = max(0, entry.refcount - 1)

    def _load_repo(self, repo_slug: str) -> tuple[Path, str]:
        """Resolve the graph file for repo_slug.

        Priority:
            1. Existing per-repo graph in settings.repo_graph_path() (Phase 5)
            2. Snapshot backend (LocalSnapshotBackend or S3)
            3. on_load_miss callback (the caller can rebuild) — optional
        """
        # Try existing per-repo graph (Phase 5)
        # Deferred import so that monkeypatching in tests works
        from src.config import get_settings as _get_main_settings
        main_settings = _get_main_settings()
        existing_graph = main_settings.repo_graph_path(repo_slug)
        if existing_graph.exists():
            logger.debug(
                "cache_load_existing_graph repo=%s path=%s",
                repo_slug, existing_graph,
            )
            return existing_graph, "current"

        # Try snapshot backend
        target_path = (
            self.settings.local_cache_dir / repo_slug / "graph.fdblite"
        )
        meta = self.backend.download_latest(repo_slug, target_path)
        if meta is not None:
            return target_path, meta.commit_sha

        # On-load-miss callback (optional rebuild)
        if self._on_load_miss is not None:
            commit_sha = self._on_load_miss(repo_slug, target_path)
            if commit_sha and target_path.exists():
                return target_path, commit_sha

        raise CacheLoadError(
            f"Cannot load repo '{repo_slug}': no existing graph, no snapshot, "
            f"and no rebuild callback."
        )

    # ─── Diagnostics ────────────────────────────────────────────

    def stats(self) -> dict:
        """Cache statistics — for monitoring."""
        with self._cache_lock:
            entries = []
            for slug, entry in list(self._cache.items()):
                entries.append({
                    "slug": slug,
                    "commit_sha": entry.commit_sha,
                    "refcount": entry.refcount,
                    "loaded_at": entry.loaded_at,
                    "age_seconds": int(time.time() - entry.loaded_at),
                })
            return {
                "size": len(self._cache),
                "maxsize": self._cache.maxsize,
                "ttl_seconds": self._cache.ttl,
                "entries": entries,
            }

    def evict(self, repo_slug: str) -> bool:
        """Manual eviction — for admin / testing."""
        with self._cache_lock:
            return self._cache.pop(repo_slug, None) is not None


class CacheLoadError(RuntimeError):
    """Cannot load repo — neither existing graph, snapshot, nor rebuild."""


# ─── Singleton ──────────────────────────────────────────────────


_default_cache: RepoIndexCache | None = None


def get_repo_index_cache() -> RepoIndexCache:
    global _default_cache
    if _default_cache is None:
        _default_cache = RepoIndexCache()
    return _default_cache
