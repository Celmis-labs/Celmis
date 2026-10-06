"""The pull-requests page: Pause and Resume.

Pause holds a PR's automatic reviews until somebody resumes it, whatever the
repository's cadence says. Resume releases it and queues one review that
covers every push skipped meanwhile (`ReviewRequest(resume=True)`); a PR that
is not paused queues nothing. The routes keep the Review button's permission
rule and the workspace boundary, and the list says who is paused and why.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.api.deps import current_workspace_id, get_current_user
from src.api.routers import pull_requests as prs_router
from src.db.models import ReviewIssue, ReviewPullRequest
from src.db.session import get_async_session
from src.review import pr_state
from tests.api.test_issues_and_pull_requests_are_scoped import (
    _jsonb_as_json_on_sqlite,  # noqa: F401 — registers the JSONB→JSON compile
)

WS = "ws-1"
NOW = datetime.now(UTC)


@asynccontextmanager
async def api(monkeypatch, *, allowed: bool = True):
    import src.review.issues as issues_mod

    # The async session the routes read the row from ...
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(ReviewIssue.__table__.create)
        await conn.run_sync(ReviewPullRequest.__table__.create)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        for pr_id, ws, number in (("p7", WS, 7), ("p-other", "ws-2", 7)):
            s.add(ReviewPullRequest(
                id=pr_id, workspace_id=ws, provider="github", repo="acme/api",
                number=number, title="Add cache", author="dana", state="open",
                reviews_count=1, opened_at=NOW, updated_at=NOW))
        await s.commit()
    # ... and the sync engine `pr_state` writes the cadence state with.
    sync = create_engine("sqlite://", poolclass=StaticPool,
                         connect_args={"check_same_thread": False})
    ReviewPullRequest.__table__.create(sync)
    monkeypatch.setattr(issues_mod, "_ENGINE", sync)

    async def _permission(slug, user, min_perm="read", workspace_id=None):
        if not allowed:
            raise HTTPException(status_code=403, detail="no review grant")

    monkeypatch.setattr("src.api.deps.enforce_repo_permission", _permission)
    queued: list[dict] = []

    def _enqueue(provider, repo, number, **kw):
        queued.append({"number": number, **kw})
        return SimpleNamespace(run_id="run-1", status="queued", payload={})

    monkeypatch.setattr("src.review.dispatch.enqueue_review_run", _enqueue)

    app = FastAPI()
    app.include_router(prs_router.router)

    async def _session():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="u-1", email="u@test", is_admin=False)
    app.dependency_overrides[current_workspace_id] = lambda: WS
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.queued, c.sync, c.factory = queued, sync, factory  # type: ignore[attr-defined]
        yield c
    await engine.dispose()
    sync.dispose()


def _state(c, number=7):
    return pr_state.load(WS, "github", "acme/api", number, engine=c.sync)


async def test_pause_holds_the_pr_and_names_who_paused_it(monkeypatch):
    async with api(monkeypatch) as c:
        r = await c.post("/api/pull-requests/p7/pause")
        assert r.status_code == 200, r.text
        assert r.json() == {"pr_id": "p7", "review_paused": True,
                            "run_id": None, "status": None}
        state = _state(c)
        assert state.review_paused and state.paused_reason == "manual"
        assert state.paused_by == "u@test"
        assert c.queued == []


async def test_resume_releases_the_pr_and_queues_one_review_of_what_was_skipped(monkeypatch):
    async with api(monkeypatch) as c:
        await c.post("/api/pull-requests/p7/pause")

        r = await c.post("/api/pull-requests/p7/resume")

        assert r.status_code == 200, r.text
        assert r.json() == {"pr_id": "p7", "review_paused": False,
                            "run_id": "run-1", "status": "queued"}
        assert not _state(c).review_paused
        (job,) = c.queued
        assert job["request"].resume is True and job["request"].explicit
        assert job["workspace_id"] == WS and job["user_id"] == "u-1"


async def test_resuming_a_pr_that_is_not_paused_queues_nothing(monkeypatch):
    async with api(monkeypatch) as c:
        r = await c.post("/api/pull-requests/p7/resume")
        assert r.status_code == 200
        assert r.json()["review_paused"] is False and r.json()["run_id"] is None
        assert c.queued == []


@pytest.mark.parametrize("action", ["pause", "resume"])
async def test_another_workspaces_pr_is_not_found(monkeypatch, action):
    async with api(monkeypatch) as c:
        assert (await c.post(f"/api/pull-requests/p-other/{action}")).status_code == 404
        assert (await c.post(f"/api/pull-requests/nope/{action}")).status_code == 404
        assert c.queued == []


@pytest.mark.parametrize("action", ["pause", "resume"])
async def test_a_caller_who_may_not_review_the_repo_cannot_change_it(monkeypatch, action):
    async with api(monkeypatch, allowed=False) as c:
        assert (await c.post(f"/api/pull-requests/p7/{action}")).status_code == 403
        assert _state(c) is None or not _state(c).review_paused


async def test_the_list_says_who_is_paused_and_why(monkeypatch):
    async with api(monkeypatch) as c:
        async with c.factory() as session:
            row = await session.get(ReviewPullRequest, "p7")
            row.review_paused = True
            row.paused_reason = "auto_pause"
            row.last_reviewed_sha = "abcdef1234"
            await session.commit()

        (item,) = (await c.get("/api/pull-requests")).json()["items"]

        assert item["id"] == "p7"
        assert item["review_paused"] is True
        assert item["paused_reason"] == "auto_pause"
        assert item["last_reviewed_sha"] == "abcdef1234"


async def test_an_unpaused_pr_lists_as_not_paused(monkeypatch):
    async with api(monkeypatch) as c:
        (item,) = (await c.get("/api/pull-requests")).json()["items"]
        assert item["review_paused"] is False and item["paused_reason"] is None
