"""GET /api/repos carries the index bookkeeping, not just "is there a file".

`indexed` on this endpoint is `settings.repo_graph_path(slug).exists()`. That
one bit cannot tell an hour-old graph from a March one, cannot name the
revision it was built from, and reads a repository whose indexing has failed
six times exactly like one nobody has asked to index. `repo_index_state` now
holds all three facts (src/repos/index_state.py writes them from both the full
and the incremental pass) — and until this endpoint read them back, the table
was write-only and no surface in the product could say any of it. That silence
is the third of the three defects that let 161 benchmark reviews go out with
"(no graph context)".

What is pinned here, driving the real route through TestClient against a real
sqlite `repo_index_state` written by the real recorder:

  * a successful index shows up as a revision and a time;
  * a repo whose newest attempt DIED still shows the last good revision, plus
    the error and when it happened — `indexed: false` with an error is
    FAILING, and that is the badge state the bench needed;
  * a repo nothing ever indexed answers null to all five, and so does a
    response that started nothing (POST /api/repos), under the same rule
    `index_status` already follows;
  * the whole page costs ONE database round trip, not one per row;
  * a database this process cannot reach still renders the list.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import current_workspace_id, get_current_user
from src.api.routers import repos as repos_router
from src.config import get_settings
from src.db.models import RepoIndexState
from src.repos.index_state import record_index_failure, record_index_success

WS = "ws-tenant-a"
USER = SimpleNamespace(id="u-1", email="lead@acme.test", is_admin=False)
SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f80911"
OTHER_SHA = "0f1e2d3c4b5a69788796a5b4c3d2e1f009182736"


class FakeStore:
    """The read half of AutoReviewStore — registration is not what is under
    test here, the row it produces is."""

    def __init__(self) -> None:
        self.rows: dict[str, object] = {}

    def register(self, slug: str, full_name: str) -> None:
        self.rows[slug] = SimpleNamespace(
            repo_slug=slug, provider="github", full_name=full_name,
            url=f"https://github.com/{full_name}", workspace_id=WS,
            enabled=False, mode="polling", branch=None,
        )

    def list_for_workspace(self, workspace_id: str) -> list:
        return list(self.rows.values()) if workspace_id == WS else []

    # POST /api/repos needs these three; nothing here exercises the conflicts.
    def upsert(self, cfg):
        self.rows[cfg.repo_slug] = cfg
        return cfg

    def existing_workspace_binding(self, provider: str, full_name: str):
        return None

    def existing_slug_binding(self, repo_slug: str):
        return None


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A temp sqlite `repo_index_state` and a workspace with two repos in it."""
    db = tmp_path / "celmis.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    engine = sa.create_engine(f"sqlite:///{db}")
    RepoIndexState.__table__.create(engine)
    engine.dispose()

    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    get_settings.cache_clear()

    store = FakeStore()
    store.register("github_acme-billing", "acme/billing")
    store.register("github_acme-web", "acme/web")
    monkeypatch.setattr(repos_router, "get_auto_review_store", lambda: store)

    app = FastAPI()
    app.include_router(repos_router.router)
    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[current_workspace_id] = lambda: WS
    yield SimpleNamespace(client=TestClient(app), store=store, tmp=tmp_path)
    get_settings.cache_clear()


def _rows(world) -> dict[str, dict]:
    resp = world.client.get("/api/repos")
    assert resp.status_code == 200, resp.text
    return {r["slug"]: r for r in resp.json()}


# ─── a success is visible ────────────────────────────────────────────


def test_a_repository_that_was_indexed_says_when_and_at_which_revision(world):
    before = datetime.now(UTC)
    record_index_success("github_acme-billing", sha=SHA, files=37, full_rebuild=True)

    row = _rows(world)["github_acme-billing"]

    assert row["last_indexed_sha"] == SHA
    assert row["last_index_error"] is None
    assert row["last_index_error_at"] is None
    at = datetime.fromisoformat(row["last_indexed_at"])
    assert before <= at <= datetime.now(UTC)
    assert datetime.fromisoformat(row["last_full_rebuild_at"]) == at


def test_a_repository_nothing_ever_indexed_answers_null_to_all_of_it(world):
    """Not zero, not "never" — null, the same "this is not recorded" the rest
    of the endpoint uses. Inventing a value here is how the original lie got
    told."""
    record_index_success("github_acme-billing", sha=SHA, files=1, full_rebuild=True)

    row = _rows(world)["github_acme-web"]

    assert row["last_indexed_sha"] is None
    assert row["last_indexed_at"] is None
    assert row["last_full_rebuild_at"] is None
    assert row["last_index_error"] is None
    assert row["last_index_error_at"] is None


# ─── failing is not the same as un-indexed ───────────────────────────


def test_a_repository_whose_newest_attempt_died_says_so_and_keeps_the_good_sha(world):
    """The badge state the benchmark needed. `indexed` is false either way —
    the graph file was never written — but one of these repos is being retried
    and failing, and the other is one nobody has asked to index. Without the
    error they render identically."""
    record_index_success("github_acme-billing", sha=SHA, files=12, full_rebuild=True)
    record_index_failure("github_acme-billing", "clone failed: dangling symlink")

    row = _rows(world)["github_acme-billing"]

    assert row["last_index_error"] == "clone failed: dangling symlink"
    assert row["last_index_error_at"] is not None
    assert row["last_indexed_sha"] == SHA, (
        "the failed attempt overwrote the revision that is actually on disk"
    )
    # And the repo that has never been touched is still silent, so the two
    # states are distinguishable on the wire.
    assert _rows(world)["github_acme-web"]["last_index_error"] is None


def test_a_repository_that_never_succeeded_still_reports_the_failure(world):
    record_index_failure("github_acme-web", "No github token for this workspace")

    row = _rows(world)["github_acme-web"]

    assert row["indexed"] is False
    assert row["last_index_error"] == "No github token for this workspace"
    assert row["last_indexed_sha"] is None


# ─── the cost, and the failure mode ──────────────────────────────────


def test_the_whole_page_costs_one_database_round_trip(world, monkeypatch):
    """Two repos, one query. A per-row read is a connection per repository on
    every poll of a page that refreshes every five seconds while an index
    runs."""
    record_index_success("github_acme-billing", sha=SHA, files=1, full_rebuild=True)
    record_index_success("github_acme-web", sha=OTHER_SHA, files=2, full_rebuild=True)

    import src.repos.index_state as index_state

    real_engine = index_state._engine
    opened: list[int] = []

    def counting_engine():
        opened.append(1)
        return real_engine()

    monkeypatch.setattr(index_state, "_engine", counting_engine)

    rows = _rows(world)

    assert len(rows) == 2
    assert rows["github_acme-billing"]["last_indexed_sha"] == SHA
    assert rows["github_acme-web"]["last_indexed_sha"] == OTHER_SHA
    assert len(opened) == 1, f"one connection per page, not {len(opened)}"


def test_the_list_still_renders_when_the_database_cannot_be_reached(world, monkeypatch):
    """Losing the freshness column is a worse outcome than a 500, but it is
    not a reason for one — the repositories page has to load without a
    database."""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    rows = _rows(world)

    assert sorted(rows) == ["github_acme-billing", "github_acme-web"]
    assert rows["github_acme-billing"]["last_indexed_sha"] is None
    assert rows["github_acme-billing"]["indexed"] is False


# ─── a call answers for what IT did ──────────────────────────────────


def test_registering_claims_nothing_about_a_previous_index(world):
    """POST /api/repos reports what this call started (`index_status`); it is
    not the freshness view and must not answer as if it were, exactly as it
    leaves `index_status` null on the list."""
    record_index_success("github_acme-billing", sha=SHA, files=3, full_rebuild=True)

    body = world.client.post(
        "/api/repos",
        json={"url": "https://github.com/acme/billing", "index": False},
    ).json()

    assert body["last_indexed_sha"] is None
    assert body["last_indexed_at"] is None
    assert body["last_index_error"] is None
    # …while the list, asked the same question, does answer it.
    assert _rows(world)["github_acme-billing"]["last_indexed_sha"] == SHA
