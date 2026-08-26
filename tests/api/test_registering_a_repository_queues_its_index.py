"""Registering a repository queues its index, and the answer says which.

`POST /api/repos` used to call `store.upsert(cfg)` and stop. The repository
was registered, nothing cloned it, and every review of it ran on the diff with
the literal string "(no graph context)" where the blast radius should have
been. That is how all 161 Martian Code Review Bench runs went out with the
product's main feature switched off: a script registered 50 forks through this
endpoint, nobody pressed "Index all", and no response, badge or log line ever
said the graph was missing.

Two things are being pinned here, and the second matters as much as the first:

**Registering indexes.** By default, because one person adding one repository
means "make it work". The dedup key is the SAME string "Index all" uses, so a
registration and a button press cannot clone one repository twice into one
directory — asserted by driving both endpoints against one queue rather than
by comparing two source strings.

**The answer is never silent.** `index_status` distinguishes "queued now" from
"already had a graph", from "already in the queue", from "you asked me not
to", from "the queue insert failed". The opt-out exists because bulk
registration is real: the 50 bench forks cost 57.9 GB of clone, and taking the
choice away would replace one defect with another.

Everything below drives the real route through TestClient. The store and the
queue are the only fakes, and the queue fake reproduces the one behaviour that
matters — a live dedup key makes `enqueue` return None.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import current_workspace_id, get_current_user
from src.api.routers import repos as repos_router
from src.config import get_settings
from src.sync.git_providers import parse_repo_url

WS = "ws-tenant-a"
OTHER_WS = "ws-tenant-b"
URL = "https://github.com/acme/billing"
SLUG = parse_repo_url(URL).slug
USER = SimpleNamespace(id="u-1", email="lead@acme.test", is_admin=False)


class FakeStore:
    """The registration half of AutoReviewStore, in memory."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], object] = {}

    def upsert(self, cfg):
        self.rows[(cfg.workspace_id, cfg.repo_slug)] = cfg
        return cfg

    def existing_workspace_binding(self, provider: str, full_name: str) -> str | None:
        for cfg in self.rows.values():
            if cfg.provider == provider and cfg.full_name == full_name:
                return cfg.workspace_id
        return None

    def existing_slug_binding(self, repo_slug: str) -> str | None:
        for ws, slug in self.rows:
            if slug == repo_slug:
                return ws
        return None

    def list_for_workspace(self, workspace_id: str) -> list:
        return [cfg for (ws, _), cfg in self.rows.items() if ws == workspace_id]

    def get_in_workspace(self, workspace_id: str, repo_slug: str):
        return self.rows.get((workspace_id, repo_slug))

    def get(self, user_id: str, repo_slug: str):
        return None


class FakeQueue:
    """`enqueue`, with the one rule that decides this feature.

    The real table has a unique partial index on `(dedup_key) WHERE status IN
    ('pending','running')` and `enqueue` returns None instead of inserting a
    second row. Reproduced rather than stubbed out, because "the second
    registration must not queue a second clone" is precisely that rule.
    """

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.calls: list[dict] = []
        self.live: set[str] = set()
        self.raises = raises

    def enqueue(self, *, kind, payload, dedup_key=None, enqueued_by=None, **kw):
        self.calls.append({
            "kind": kind, "payload": payload,
            "dedup_key": dedup_key, "enqueued_by": enqueued_by, **kw,
        })
        if self.raises is not None:
            raise self.raises
        if dedup_key is not None and dedup_key in self.live:
            return None
        if dedup_key is not None:
            self.live.add(dedup_key)
        return f"job-{len(self.calls)}"


@pytest.fixture
def workspace_dir(tmp_path, monkeypatch):
    """A settings singleton whose graph paths point at an empty tmp dir."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    fake = FakeStore()
    monkeypatch.setattr(repos_router, "get_auto_review_store", lambda: fake)
    return fake


@pytest.fixture
def queue(monkeypatch) -> FakeQueue:
    fake = FakeQueue()
    monkeypatch.setattr("src.sync.queue.enqueue", fake.enqueue)
    return fake


@pytest.fixture
def client(workspace_dir, store, queue) -> TestClient:
    app = FastAPI()
    app.include_router(repos_router.router)
    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[current_workspace_id] = lambda: WS
    return TestClient(app)


def _graph_file(slug: str = SLUG):
    path = get_settings().repo_graph_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("pretend this is a graph", encoding="utf-8")
    return path


def add(client: TestClient, **body):
    return client.post("/api/repos", json={"url": URL, **body})


# ─── the default: registering indexes ────────────────────────────────


def test_registering_queues_exactly_one_index_job(client, queue):
    resp = add(client)

    assert resp.status_code == 201, resp.text
    assert len(queue.calls) == 1, queue.calls
    job = queue.calls[0]
    assert job["kind"] == "index_repo_full"
    assert job["payload"] == {
        "repo_slug": SLUG, "workspace_id": WS, "user_id": USER.id,
    }
    assert job["dedup_key"] == f"index_full:{WS}:{SLUG}"
    assert job["enqueued_by"] == USER.email


def test_the_response_says_the_index_was_queued(client):
    body = add(client).json()

    assert body["index_queued"] is True
    assert body["index_status"] == "queued"
    # The graph does not exist YET — the two fields answer different questions
    # and a caller reading only `indexed` learns nothing about what was started.
    assert body["indexed"] is False


def test_the_job_carries_the_callers_workspace(client, queue):
    """The queue rows are tenant-scoped and the worker resolves credentials
    from the payload. A registration that queued someone else's workspace id
    would clone into the wrong tenant's directory with the wrong token."""
    add(client)
    assert queue.calls[0]["payload"]["workspace_id"] == WS
    assert queue.calls[0]["dedup_key"].split(":")[1] == WS


def test_the_repository_is_still_registered(client, store):
    add(client)
    assert store.get_in_workspace(WS, SLUG) is not None


# ─── the opt-out ─────────────────────────────────────────────────────


def test_the_opt_out_queues_nothing(client, queue, store):
    resp = add(client, index=False)

    assert resp.status_code == 201, resp.text
    assert queue.calls == []
    # Registered all the same — that is the whole point of the flag.
    assert store.get_in_workspace(WS, SLUG) is not None


def test_the_opt_out_says_so(client):
    body = add(client, index=False).json()
    assert body["index_queued"] is False
    assert body["index_status"] == "not_requested"


def test_indexing_is_the_default_when_the_field_is_absent(client, queue):
    """A body that says nothing about indexing must index. The 50 bench forks
    were registered by a client that had never heard of this field."""
    resp = client.post("/api/repos", json={"url": URL, "auto_review": False})
    assert resp.status_code == 201, resp.text
    assert len(queue.calls) == 1
    assert resp.json()["index_status"] == "queued"


# ─── not twice ───────────────────────────────────────────────────────


def test_a_second_registration_does_not_queue_a_second_job(client, queue):
    first = add(client).json()
    second = add(client).json()

    assert first["index_status"] == "queued"
    # The dedup key is held by the job the first call queued, so the queue
    # refused the second — one clone, not two racing into one directory.
    assert second["index_queued"] is False
    assert second["index_status"] == "already_queued"
    assert len(queue.live) == 1


def test_an_already_indexed_repo_is_not_re_queued(client, queue):
    _graph_file()

    body = add(client).json()

    assert queue.calls == [], "a repo with a graph was queued for another clone"
    assert body["index_queued"] is False
    assert body["index_status"] == "already_indexed"
    assert body["indexed"] is True


def test_registration_and_index_all_share_one_dedup_key(client, queue):
    """Not by comparing two strings in two files — by driving both endpoints
    against one queue and asking whether the second one got in.

    If the keys ever drift apart, `index-all` reports queued=1 here and two
    workers clone the same repository into the same directory at once.
    """
    add(client)
    assert len(queue.calls) == 1

    resp = client.post("/api/repos/index-all")

    assert resp.status_code == 202, resp.text
    # Field-by-field, not dict equality. Adding `skipped_repos` — which names
    # WHICH repositories were skipped, because the bare count reads the same
    # whether they are all already indexing or all dead — broke four tests that
    # compared the whole response. A client ignores a field it does not know;
    # a test that fails for one is asserting the shape of the envelope rather
    # than the answer inside it.
    body = resp.json()
    assert (body["queued"], body["skipped"]) == (0, 1)
    assert body["already_indexed"] == []
    assert body["skipped_repos"], "a skipped repo is not named"
    assert queue.calls[1]["dedup_key"] == queue.calls[0]["dedup_key"]


def test_index_all_still_skips_a_repo_that_has_a_graph(client, queue):
    """The other half of "consistent with index_all": both paths read the same
    condition, so neither can start work the other considers unnecessary."""
    add(client, index=False)
    _graph_file()

    resp = client.post("/api/repos/index-all")

    body = resp.json()
    assert (body["queued"], body["skipped"]) == (0, 0)
    assert body["already_indexed"] == [SLUG]
    assert queue.calls == []


# ─── a broken queue must not cost the registration ───────────────────


def test_a_queue_failure_still_registers_the_repository(client, monkeypatch, store):
    monkeypatch.setattr(
        "src.sync.queue.enqueue",
        FakeQueue(raises=RuntimeError("connection refused")).enqueue,
    )

    resp = add(client)

    assert resp.status_code == 201, resp.text
    assert store.get_in_workspace(WS, SLUG) is not None, (
        "the registration was lost because the queue was down"
    )


def test_a_queue_failure_is_reported_not_swallowed(client, monkeypatch):
    monkeypatch.setattr(
        "src.sync.queue.enqueue",
        FakeQueue(raises=RuntimeError("connection refused")).enqueue,
    )

    body = add(client).json()

    assert body["index_queued"] is False
    assert body["index_status"] == "queue_unavailable"


def test_the_repo_appears_in_the_list_after_a_queue_failure(client, monkeypatch):
    monkeypatch.setattr(
        "src.sync.queue.enqueue",
        FakeQueue(raises=RuntimeError("connection refused")).enqueue,
    )
    add(client)

    listed = client.get("/api/repos").json()

    assert [r["slug"] for r in listed] == [SLUG]
    assert listed[0]["indexed"] is False


# ─── silence is reserved for calls that started nothing ──────────────


def test_the_list_claims_nothing_about_indexing(client):
    """GET /api/repos starts no job, so it reports none. `index_status: null`
    is "this call is not about that", which is the honest answer — inventing
    "already_indexed" there would put the original lie back in a new place."""
    add(client, index=False)

    row = client.get("/api/repos").json()[0]

    assert row["index_status"] is None
    assert row["index_queued"] is False


def test_the_auto_review_toggle_claims_nothing_about_indexing(client):
    add(client, index=False)

    body = client.patch(
        f"/api/repos/{SLUG}/auto-review", json={"enabled": True},
    ).json()

    assert body["index_status"] is None
    assert body["index_queued"] is False


# ─── the shared helper, on its own ───────────────────────────────────


def test_the_helper_reports_a_fresh_queue_as_queued(workspace_dir, queue):
    from src.repos.indexing import queue_index_if_needed

    status = queue_index_if_needed(
        "github_acme-other", workspace_id=OTHER_WS, user_id="u-9",
    )
    assert status == "queued"
    assert queue.calls[0]["dedup_key"] == f"index_full:{OTHER_WS}:github_acme-other"


def test_the_helper_never_raises(workspace_dir, monkeypatch):
    """Its callers run after the repository row is already written."""
    from src.repos.indexing import queue_index_if_needed

    def boom(**kw):
        raise RuntimeError("the queue is on fire")

    monkeypatch.setattr("src.sync.queue.enqueue", boom)
    assert queue_index_if_needed(
        SLUG, workspace_id=WS, user_id="u-9",
    ) == "queue_unavailable"


def test_the_helper_answers_for_the_workspace_it_was_given(workspace_dir, queue):
    """Two tenants can hold the same slug string. One tenant's live index must
    not make the other's registration report "already_queued" and start
    nothing."""
    from src.repos.indexing import queue_index_if_needed

    assert queue_index_if_needed(SLUG, workspace_id=WS, user_id="u-1") == "queued"
    assert queue_index_if_needed(SLUG, workspace_id=OTHER_WS, user_id="u-2") == "queued"
    assert len(queue.live) == 2
