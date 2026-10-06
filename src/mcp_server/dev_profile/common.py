"""Helpers every dev tool shares: scope, graph access, repository arguments."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

from src.mcp_server.dev_profile import access, emit
from src.mcp_server.dev_profile.access import DevScope, RepoNotAccessible
from src.mcp_server.dev_profile.freshness import RepoFresh, read_freshness

logger = logging.getLogger(__name__)

#: Graph stores are opened per call (FalkorDBLite is one process per file), so
#: fan-out over repositories is bounded.
POOL_SIZE = 8
#: Most repositories one call fans out to. A token that reaches hundreds of
#: repositories must name one (`repo=`) to search more than this many.
MAX_REPOS_PER_CALL = 40
RESPONSE_FORMATS = ("concise", "detailed")


def begin() -> DevScope:
    """Per-call preamble: the token must be a dev token; returns the scope."""
    access.require_dev_scope()
    return access.mcp_scope()


def fresh_entries(slugs: list[str]) -> list[RepoFresh]:
    """Freshness entries for `slugs`, in order, skipping any with no revision."""
    info = read_freshness(list(dict.fromkeys(slugs)))
    return [info[s] for s in dict.fromkeys(slugs) if s in info]


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}
LOCK_TIMEOUT_S = 60.0


def _slug_lock(slug: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(slug, threading.Lock())


@contextmanager
def open_store(slug: str) -> Iterator[Any]:
    """The repository's graph, for reading.

    FalkorDBLite takes about 3.5 seconds to shut its server down, and every
    graph tool in this server pays that on every call. Here the shutdown runs
    on a background thread AFTER the answer is ready, so the caller does not
    wait for it; a per-repository lock held until it finishes keeps the next
    open of the same file from racing a server that is still going down. The
    lock is not re-entrant on purpose: open a repository once per tool call
    and pass the store down.
    """
    from src.config import get_settings
    from src.indexing.graph.graph_store import make_graph_store

    lock = _slug_lock(slug)
    if not lock.acquire(timeout=LOCK_TIMEOUT_S):
        raise TimeoutError(f"graph of {slug} is busy")
    try:
        store = make_graph_store(get_settings().repo_graph_path(slug))
    except BaseException:
        lock.release()
        raise

    def _close() -> None:
        try:
            store.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("dev_graph_close_failed repo=%s err=%s", slug, exc)
        finally:
            lock.release()

    try:
        yield store
    finally:
        threading.Thread(target=_close, name=f"dev-graph-close-{slug}", daemon=True).start()


def expand(store: Any, symbol: str, direction: str, depth: int,
           max_nodes: int = 100) -> list[dict]:
    """Callers or callees of `symbol` (a name or a graph id) as graph rows.

    The logic of `tools.find_callers` / `find_callees`, run on a store the
    caller already holds instead of opening a second one.
    """
    from src.mcp_server import tools as legacy

    query = (legacy._query_incoming_callers if direction == "callers"
             else legacy._query_outgoing_callees)
    seen: set[str] = set()
    rows: list[dict] = []
    for target in legacy._resolve_targets(store, symbol):
        for row in query(store, target, depth, max_nodes):
            key = str(row.get("id"))
            if key not in seen:
                seen.add(key)
                rows.append(row)
        if len(rows) >= max_nodes:
            return rows[:max_nodes]
    return rows


def map_repos[T](slugs: list[str], fn: Callable[[str], T]) -> dict[str, T]:
    """Run `fn(slug)` per repository on a bounded pool. A failing repository
    is logged and left out: one broken graph must not blank the answer. The
    fan-out is capped at `MAX_REPOS_PER_CALL`."""
    if len(slugs) > MAX_REPOS_PER_CALL:
        logger.info("dev_fanout_capped asked=%d cap=%d", len(slugs), MAX_REPOS_PER_CALL)
        slugs = slugs[:MAX_REPOS_PER_CALL]
    if len(slugs) <= 1:
        out: dict[str, T] = {}
        for s in slugs:
            try:
                out[s] = fn(s)
            except Exception as exc:  # noqa: BLE001
                logger.warning("dev_repo_failed repo=%s err=%s", s, exc)
        return out
    results: dict[str, T] = {}
    with ThreadPoolExecutor(max_workers=min(POOL_SIZE, len(slugs))) as pool:
        futures = {s: pool.submit(fn, s) for s in slugs}
        for s, fut in futures.items():
            try:
                results[s] = fut.result()
            except Exception as exc:  # noqa: BLE001
                logger.warning("dev_repo_failed repo=%s err=%s", s, exc)
    return results


def repo_error(scope: DevScope, name: str) -> str:
    """The one answer for 'no such repo / not yours / ambiguous'."""
    sim = scope.similar(name)
    hint = f"similar: {', '.join(sim)}" if sim else ""
    return emit.error(None, f'repo "{name}": {access.NOT_ACCESSIBLE}', hint,
                      "call repos to list the ones you can read")


def clamp(value: int, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = lo
    return max(lo, min(hi, v))


def norm_format(value: str) -> bool:
    """True for `detailed`."""
    return (value or "concise").strip().lower() == "detailed"


def pick_repos(scope: DevScope, repo: str, *, need: str = "metadata",
               ) -> tuple[list[str], str | None]:
    """(slugs, error). With `repo`: that one; without: every readable one."""
    if repo and repo.strip():
        try:
            return [scope.resolve(repo, need=need)], None  # type: ignore[arg-type]
        except RepoNotAccessible:
            return [], repo_error(scope, repo)
    return scope.slugs(need=need), None  # type: ignore[arg-type]
