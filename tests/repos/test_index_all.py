"""Bulk indexing: one implementation, graphs only, queued.

Three things this must not become, each of which was a real possibility while
writing it:

  * A second copy of the indexing logic. The route did it inline; the queue
    handler could not reuse it (no request, no `Depends`, no HTTPException), so
    the obvious move was to copy the body — and the copy is the one that misses
    the next credential fix.
  * A vault generator. Indexing builds the graph. Vault generation costs model
    calls per repo and is a separate, explicitly-chosen action; a bulk button
    that quietly triggered it would bill a workspace for nine repos of prose
    nobody asked for.
  * Synchronous. Nine repos at 5-60s each is well past any request's patience.
"""

from __future__ import annotations

import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_route_and_the_worker_call_the_same_function():
    """A copy drifts. The button and "index all" must fail and succeed
    identically, including the workspace-first credential lookup."""
    route = (ROOT / "src" / "api" / "routers" / "repos.py").read_text()
    handler = (ROOT / "src" / "sync" / "handlers.py").read_text()
    assert "from src.repos.indexing import" in route
    assert "index_repo_sync" in route
    assert "from src.repos.indexing import index_repo_sync" in handler


def test_indexing_never_generates_a_vault():
    """The whole point of the button as asked for: graphs only."""
    source = (ROOT / "src" / "repos" / "indexing.py").read_text()
    for forbidden in ("generate_vault", "GenerationOrchestrator", "generate_notes"):
        assert forbidden not in source, (
            f"{forbidden} in the index path — indexing must not trigger "
            f"documentation generation"
        )


def test_index_all_is_queued_not_synchronous():
    from src.api.routers import repos

    source = inspect.getsource(repos.index_all)
    assert "enqueue(" in source
    assert "KIND_INDEX_REPO_FULL" in source


def test_the_full_index_kind_is_distinct_from_the_incremental_one():
    """`KIND_INDEX_REPO` runs the INCREMENTAL pass, which returns
    'skipped: clone missing' for a repo nobody has cloned — exactly the case
    "Index all" exists to serve. Reusing it would make the button a no-op on a
    fresh workspace."""
    from src.sync import queue

    assert queue.KIND_INDEX_REPO_FULL != queue.KIND_INDEX_REPO

    incremental = (ROOT / "src" / "sync" / "incremental.py").read_text()
    assert '"reason": "clone missing"' in incremental, (
        "the incremental indexer stopped skipping un-cloned repos — if it now "
        "clones, this whole distinction can go away"
    )


def test_the_handler_is_registered():
    """An unregistered kind sits in the queue forever with no error."""
    worker = (ROOT / "src" / "sync" / "worker.py").read_text()
    assert "register(jq.KIND_INDEX_REPO_FULL, h.handle_index_repo_full)" in worker


class _FakeQueue:
    """`enqueue` with the one rule that decides this: the real `sync_jobs`
    table has a unique partial index on `(dedup_key) WHERE status IN
    ('pending','running')` and `enqueue` answers None instead of inserting."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.live: set[str] = set()

    def enqueue(self, *, kind, payload, dedup_key=None, enqueued_by=None, **kw):
        self.calls.append({"kind": kind, "payload": payload, "dedup_key": dedup_key})
        if dedup_key in self.live:
            return None
        self.live.add(dedup_key)
        return f"job-{len(self.calls)}"


def _index_all_client(monkeypatch, slugs: list[str], workspace_id: str):
    """The real route, a real workspace dependency, fake store and queue."""
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.deps import current_workspace_id, get_current_user
    from src.api.routers import repos as repos_router

    configs = [SimpleNamespace(repo_slug=slug, workspace_id=workspace_id)
               for slug in slugs]
    monkeypatch.setattr(repos_router, "get_auto_review_store", lambda: SimpleNamespace(
        list_for_workspace=lambda ws: configs if ws == workspace_id else [],
    ))
    app = FastAPI()
    app.include_router(repos_router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="u-1", email="lead@acme.test", is_admin=False)
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    return TestClient(app)


def test_pressing_the_button_twice_cannot_clone_twice(tmp_path, monkeypatch):
    """Driven, not read.

    This used to grep `index_all` for the literal "index_full:", which stopped
    meaning anything the moment the key moved into a shared helper so that
    registering a repository could use the same one. What matters was never
    the spelling — it is that the second press adds nothing, and that the
    first press queues every repo rather than letting one repo's key block the
    other eight.
    """
    from src.config import get_settings

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    queue = _FakeQueue()
    monkeypatch.setattr("src.sync.queue.enqueue", queue.enqueue)
    client = _index_all_client(
        monkeypatch, ["github_acme-billing", "github_acme-web"], "ws-a")
    try:
        first = client.post("/api/repos/index-all").json()
        second = client.post("/api/repos/index-all").json()
    finally:
        get_settings.cache_clear()

    # Per repo, not per workspace: a workspace-wide key would have let the
    # first repo's job swallow the second repo's.
    # Field-by-field, not dict equality. Adding `skipped_repos` — which names
    # WHICH repositories were skipped, because the bare count reads the same
    # whether they are all already indexing or all dead — broke four tests that
    # compared the whole response. A client ignores a field it does not know;
    # a test that fails for one is asserting the shape of the envelope rather
    # than the answer inside it.
    assert (first["queued"], first["skipped"]) == (2, 0)
    assert first["already_indexed"] == []
    assert (second["queued"], second["skipped"]) == (0, 2)
    assert second["already_indexed"] == []
    assert len(second["skipped_repos"]) == 2, "the skipped ones are not named"
    assert len(queue.live) == 2


def test_the_key_is_the_one_registering_a_repo_uses(tmp_path, monkeypatch):
    """Registration queues an index too (POST /api/repos). If the two paths
    ever spell the key differently, both jobs are accepted and two workers
    clone one repository into one directory at the same time."""
    from src.config import get_settings
    from src.repos.indexing import index_dedup_key

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    queue = _FakeQueue()
    monkeypatch.setattr("src.sync.queue.enqueue", queue.enqueue)
    # Pre-load the key the registration path would produce, then press the
    # button: the queue must refuse it.
    queue.live.add(index_dedup_key("github_acme-billing", "ws-a"))
    client = _index_all_client(monkeypatch, ["github_acme-billing"], "ws-a")
    try:
        out = client.post("/api/repos/index-all").json()
    finally:
        get_settings.cache_clear()

    assert (out["queued"], out["skipped"]) == (0, 1)
    assert out["already_indexed"] == []
    assert len(out["skipped_repos"]) == 1


def test_an_already_indexed_repo_is_skipped_unless_forced():
    source = inspect.getsource(
        __import__("src.api.routers.repos", fromlist=["repos"]).index_all)
    assert "if not force and" in source
    assert "repo_graph_path" in source


def test_the_verb_carries_its_own_tenant():
    """No ambient request context — the worker has none, and a function that
    guesses the workspace is how one tenant's bulk action reaches another's
    repositories."""
    from src.repos.indexing import index_repo_sync

    params = inspect.signature(index_repo_sync).parameters
    assert "workspace_id" in params
    assert "user_id" in params
    assert params["workspace_id"].kind is inspect.Parameter.KEYWORD_ONLY
