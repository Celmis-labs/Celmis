"""The pull-requests page: each PR says why its last review ended that way,
and lists its reviews with their stages.

"Skipped" alone is a verdict where the reader needed an explanation — a
branch that is not configured for review and a draft are different things to
do something about. The reason is read from the run store by the PR's
`last_run_id`; the runs of a PR are matched by its coordinates (and by
`pr_ref` for runs that failed before the PR was fetched), scoped to the
workspace, newest first, each with its stages.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.api.deps import current_workspace_id, get_current_user
from src.api.review_runs import ReviewRun, ReviewRunStore
from src.api.routers import pull_requests as prs_router
from src.db.models import ReviewIssue, ReviewPullRequest
from src.db.session import get_async_session
from tests.api.test_issues_and_pull_requests_are_scoped import (
    _jsonb_as_json_on_sqlite,  # noqa: F401 — registers the JSONB→JSON compile
)

WS = "ws-1"
NOW = datetime.now(UTC)
MISMATCH = ("Skipped — Branch mismatch: target branch 'master' does not match "
            "configured patterns ['main']")


def _stages(reason: str, status: str = "skipped") -> list[dict]:
    return [
        {"key": "received", "name": "Review started", "status": "success",
         "started_at": NOW.isoformat(), "duration_ms": 0, "reason": "Triggered by a GitHub webhook delivery."},
        {"key": "gate_target_branch", "name": "Validate target branch",
         "status": status, "started_at": NOW.isoformat(), "duration_ms": 3,
         "reason": reason.removeprefix("Skipped — ")},
        {"key": "finished", "name": "Finished", "status": status,
         "started_at": NOW.isoformat(), "duration_ms": 198000, "reason": reason},
    ]


@asynccontextmanager
async def api(tmp_path, monkeypatch):
    store = ReviewRunStore(tmp_path / "runs.db")
    monkeypatch.setattr("src.api.review_runs._default_store", store)
    store.insert(ReviewRun(
        id="r1", user_id="u-1", pr_ref="github:acme/api#7", workspace_id=WS,
        status="complete", started_at=(NOW - timedelta(days=3)).isoformat(),
        pr_provider="github", pr_repo="acme/api", pr_number=7,
    ))
    store.insert(ReviewRun(
        id="r2", user_id="u-1", pr_ref="github:acme/api#7", workspace_id=WS,
        status="skipped", started_at=(NOW - timedelta(days=1)).isoformat(),
        pr_provider="github", pr_repo="acme/api", pr_number=7,
        stages=_stages(MISMATCH),
    ))
    # Failed before the PR was fetched: matched by its reference alone.
    store.insert(ReviewRun(
        id="r0", user_id="u-1", pr_ref="github:acme/api#7", workspace_id=WS,
        status="failed", error_message="boom", started_at=(NOW - timedelta(days=5)).isoformat(),
    ))
    store.insert(ReviewRun(
        id="x", user_id="u-9", pr_ref="github:acme/api#7", workspace_id="ws-2",
        status="complete", pr_provider="github", pr_repo="acme/api", pr_number=7,
    ))

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(ReviewIssue.__table__.create)
        await conn.run_sync(ReviewPullRequest.__table__.create)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add(ReviewPullRequest(
            id="p7", workspace_id=WS, provider="github", repo="acme/api", number=7,
            title="Add cache", author="dana", state="open", reviews_count=3,
            last_review_status="skipped", last_run_id="r2", base_ref="master",
            head_ref="feat/cache", opened_at=NOW, updated_at=NOW))
        s.add(ReviewPullRequest(
            id="p-other", workspace_id="ws-2", provider="github", repo="acme/api",
            number=7, title="Theirs", state="open", reviews_count=1,
            opened_at=NOW, updated_at=NOW))
        await s.commit()

    monkeypatch.setattr("src.api.deps.is_workspace_admin", lambda _u, _ws: False)
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
        yield c
    await engine.dispose()


async def test_the_row_says_why_the_last_review_was_skipped(tmp_path, monkeypatch):
    async with api(tmp_path, monkeypatch) as c:
        body = (await c.get("/api/pull-requests")).json()
        (p7,) = body["items"]
        assert p7["last_review_status"] == "skipped"
        assert p7["last_review_reason"] == MISMATCH

        skipped = (await c.get("/api/pull-requests?review_status=skipped")).json()
        assert [p["id"] for p in skipped["items"]] == ["p7"]
        assert (await c.get("/api/pull-requests?review_status=failed")).json()["total"] == 0


async def test_a_pr_lists_its_reviews_with_their_stages(tmp_path, monkeypatch):
    async with api(tmp_path, monkeypatch) as c:
        body = (await c.get("/api/pull-requests/p7/runs")).json()
        assert [r["id"] for r in body["items"]] == ["r2", "r1", "r0"]
        newest = body["items"][0]
        assert newest["status"] == "skipped"
        assert newest["status_reason"] == MISMATCH
        assert [s["key"] for s in newest["stages"]] == [
            "received", "gate_target_branch", "finished"]
        assert newest["stages"][1]["status"] == "skipped"
        assert "configured patterns ['main']" in newest["stages"][1]["reason"]
        # Recorded before stages existed: null, never [] — and a failure still
        # says what it was.
        assert body["items"][1]["stages"] is None
        assert body["items"][2]["status_reason"] == "boom"


async def test_another_workspaces_pr_is_not_found(tmp_path, monkeypatch):
    async with api(tmp_path, monkeypatch) as c:
        assert (await c.get("/api/pull-requests/p-other/runs")).status_code == 404
        assert (await c.get("/api/pull-requests/nope/runs")).status_code == 404
