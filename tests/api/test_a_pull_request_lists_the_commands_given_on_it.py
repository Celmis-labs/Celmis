"""The commands timeline: `GET /api/pull-requests/{id}/commands`.

Scoped like the reviews list: the PR must belong to the caller's workspace and
its repository must be readable by the caller,
the rows are that PR's own, newest first, and nothing in them is a secret
(the comment id and the delivery key stay server-side).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.api.deps import current_workspace_id, get_current_user
from src.api.routers import pull_requests as prs_router
from src.db.models import PRCommandEvent, ReviewIssue, ReviewPullRequest
from src.db.session import get_async_session
from src.review.commands import ledger
from tests.api.test_issues_and_pull_requests_are_scoped import (
    _jsonb_as_json_on_sqlite,  # noqa: F401 — registers the JSONB→JSON compile
)

WS = "ws-1"
NOW = datetime.now(UTC)


@asynccontextmanager
async def api(monkeypatch, *, readable: bool = True):
    sync = create_engine("sqlite://", poolclass=StaticPool,
                         connect_args={"check_same_thread": False})
    PRCommandEvent.__table__.create(sync)
    monkeypatch.setattr(ledger, "_engine", lambda: sync)
    for n, (ws, pr, cid, command, status) in enumerate([
        (WS, 7, "c1", "help", "done"),
        (WS, 7, "c2", "review", "failed"),
        ("ws-2", 7, "c3", "review", "done"),
        (WS, 8, "c4", "help", "done"),
    ]):
        assert ledger.claim(
            ws, "github", "acme/api", pr, cid, command=command,
            force=(command == "review"), actor_id="9", actor_name="dana",
            event_key=f"gh:secret-delivery-{n}", status=status, engine=sync)
    from sqlalchemy.orm import Session
    with Session(sync) as s:
        for row in s.query(PRCommandEvent).all():
            row.created_at = NOW - timedelta(minutes={"c1": 30, "c2": 20}.get(row.comment_id, 10))
        s.commit()

    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(ReviewIssue.__table__.create)
        await conn.run_sync(ReviewPullRequest.__table__.create)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        s.add(ReviewPullRequest(
            id="p7", workspace_id=WS, provider="github", repo="acme/api", number=7,
            title="Add cache", author="dana", state="open", opened_at=NOW, updated_at=NOW))
        s.add(ReviewPullRequest(
            id="p-other", workspace_id="ws-2", provider="github", repo="acme/api",
            number=7, title="Theirs", state="open", opened_at=NOW, updated_at=NOW))
        await s.commit()

    async def perm(slug, user, min_perm="read", workspace_id=None):
        if not readable:
            raise HTTPException(status_code=403, detail="No team is granted access")

    monkeypatch.setattr("src.api.deps.enforce_repo_permission", perm)
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
    sync.dispose()


async def test_a_pr_lists_the_commands_given_on_it_newest_first(monkeypatch):
    async with api(monkeypatch) as c:
        body = (await c.get("/api/pull-requests/p7/commands")).json()
        assert body["pr_id"] == "p7"
        assert [(i["command"], i["status"]) for i in body["items"]] == [
            ("review", "failed"), ("help", "done")]
        assert body["items"][0]["force"] is True
        assert body["items"][0]["actor_name"] == "dana"


async def test_the_timeline_carries_neither_the_comment_id_nor_the_delivery_key(monkeypatch):
    async with api(monkeypatch) as c:
        text = (await c.get("/api/pull-requests/p7/commands")).text
        assert "secret-delivery" not in text
        assert '"comment_id"' not in text and '"event_key"' not in text


async def test_another_workspaces_pr_has_no_command_timeline(monkeypatch):
    async with api(monkeypatch) as c:
        assert (await c.get("/api/pull-requests/p-other/commands")).status_code == 404
        assert (await c.get("/api/pull-requests/missing/commands")).status_code == 404


async def test_the_timeline_only_shows_this_workspaces_rows_for_the_pr(monkeypatch):
    async with api(monkeypatch) as c:
        items = (await c.get("/api/pull-requests/p7/commands?limit=1")).json()["items"]
        assert len(items) == 1
        assert items[0]["command"] == "review"


async def test_a_repository_the_caller_may_not_read_has_no_command_timeline(monkeypatch):
    async with api(monkeypatch, readable=False) as c:
        response = await c.get("/api/pull-requests/p7/commands")
    assert response.status_code == 403
    assert "dana" not in response.text and "review" not in response.text
