""""Already indexed" means a graph file, and a leftover directory is not one.

`queue_index_if_needed` refuses to queue when the repository already has a
graph, and it has to read that the same way GET /api/repos reads `indexed`:
`settings.repo_graph_path(slug).exists()` — the graph FILE. The enclosing
directory (`repo_data_path`) is not the same question. A sarif run, a failed
index, or a purge that took the graph and left the folder all leave that
directory behind, and a check on it answers "already_indexed" for a repository
that has no graph at all.

That answer is the exact silence this wave exists to remove, in its most
convincing form: registration reports a state, the state is a lie, the list
right next to it says `indexed: false`, and nothing ever queues a clone. Every
other test here is satisfied by either spelling — the graph file's parent
directory is created along with it — so the two are separated on purpose.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import current_workspace_id, get_current_user
from src.api.routers import repos as repos_router
from src.config import get_settings
from src.repos.indexing import queue_index_if_needed
from src.sync.git_providers import parse_repo_url

WS = "ws-tenant-a"
URL = "https://github.com/acme/billing"
SLUG = parse_repo_url(URL).slug
USER = SimpleNamespace(id="u-1", email="lead@acme.test", is_admin=False)


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], object] = {}

    def upsert(self, cfg):
        self.rows[(cfg.workspace_id, cfg.repo_slug)] = cfg
        return cfg

    def existing_workspace_binding(self, provider, full_name):
        return None

    def existing_slug_binding(self, repo_slug):
        return None

    def list_for_workspace(self, workspace_id):
        return [cfg for (ws, _), cfg in self.rows.items() if ws == workspace_id]

    def get_in_workspace(self, workspace_id, repo_slug):
        return self.rows.get((workspace_id, repo_slug))

    def get(self, user_id, repo_slug):
        return None


class FakeQueue:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def enqueue(self, *, kind, payload, dedup_key=None, enqueued_by=None, **kw):
        self.calls.append({"kind": kind, "dedup_key": dedup_key})
        return f"job-{len(self.calls)}"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    store, queue = FakeStore(), FakeQueue()
    monkeypatch.setattr(repos_router, "get_auto_review_store", lambda: store)
    monkeypatch.setattr("src.sync.queue.enqueue", queue.enqueue)
    app = FastAPI()
    app.include_router(repos_router.router)
    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[current_workspace_id] = lambda: WS
    yield SimpleNamespace(client=TestClient(app), queue=queue, store=store)
    get_settings.cache_clear()


def _data_dir_without_a_graph():
    """What a purge, a sarif run or a half-finished index leaves behind."""
    settings = get_settings()
    data = settings.repo_data_path(SLUG)
    data.mkdir(parents=True, exist_ok=True)
    (data / "scan.sarif").write_text("{}", encoding="utf-8")
    assert not settings.repo_graph_path(SLUG).exists()
    return data


def test_a_leftover_data_directory_does_not_count_as_an_index(world):
    _data_dir_without_a_graph()

    status = queue_index_if_needed(SLUG, workspace_id=WS, user_id=USER.id)

    assert status == "queued", (
        "a directory with no graph in it was reported as an existing index"
    )
    assert len(world.queue.calls) == 1


def test_registering_such_a_repository_still_clones_it(world):
    _data_dir_without_a_graph()

    body = world.client.post("/api/repos", json={"url": URL}).json()

    assert body["index_status"] == "queued"
    assert body["index_queued"] is True
    assert len(world.queue.calls) == 1


def test_the_answer_cannot_disagree_with_what_the_list_reports(world):
    """`indexed` on the list is the graph file. If registration ever decides
    "already_indexed" on a weaker condition, the two sit side by side on the
    same screen saying opposite things — and nothing clones the repository."""
    _data_dir_without_a_graph()

    added = world.client.post("/api/repos", json={"url": URL}).json()
    listed = world.client.get("/api/repos").json()[0]

    assert listed["indexed"] is False
    assert added["index_status"] != "already_indexed"


def test_a_real_graph_file_still_stops_the_clone(world):
    """The other half — otherwise "never trust the directory" would just mean
    "always clone", and pressing add on an indexed repo would re-clone it."""
    graph = get_settings().repo_graph_path(SLUG)
    graph.parent.mkdir(parents=True, exist_ok=True)
    graph.write_text("pretend this is a graph", encoding="utf-8")

    status = queue_index_if_needed(SLUG, workspace_id=WS, user_id=USER.id)

    assert status == "already_indexed"
    assert world.queue.calls == []
