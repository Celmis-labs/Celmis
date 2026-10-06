"""The last requirements check is stored on the pull request and served to the
people who may read that repository: another workspace's PR is a 404, an
unreadable repository a 403, and nothing but the curated rows ever leaves
(no token, no raw Jira answer, no link that is not https)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from src.api.deps import current_workspace_id, get_current_user
from src.api.routers import pull_requests as prs_router
from src.db.models import ReviewIssue, ReviewPullRequest
from src.db.session import get_async_session
from src.review import issues
from src.review.task_context.checklist import Requirement
from tests.api.test_issues_and_pull_requests_are_scoped import (
    _jsonb_as_json_on_sqlite,  # noqa: F401 — registers the JSONB→JSON compile
)

WS = "ws-1"
NOW = datetime.now(UTC)
ROWS = [
    {"key": "PROJ-1", "id": "AC1", "text": "Offcuts under 10 mm are dropped", "verdict": "met",
     "evidence": "src/export.py:1"},
    {"key": "PROJ-1", "id": "AC2", "text": "A report is written", "verdict": "missing", "evidence": ""},
]
TASKS = [
    {"key": "PROJ-1", "url": "https://celmis.example.com/browse/PROJ-1", "summary": "Export",
     "status": "In Progress", "issue_type": "Story", "token": "sk-should-never-leave"},
    {"key": "PROJ-2", "url": "javascript:alert(1)", "summary": "Odd link"},
]


@asynccontextmanager
async def api(*, readable: bool = True, with_rows: bool = True, monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool,
                                 connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.run_sync(ReviewIssue.__table__.create)
        await conn.run_sync(ReviewPullRequest.__table__.create)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        for pid, ws in (("p1", WS), ("p-other", "ws-2")):
            s.add(ReviewPullRequest(
                id=pid, workspace_id=ws, provider="github", repo="acme/shop", number=1,
                title="t", state="open", reviews_count=1, opened_at=NOW, updated_at=NOW,
                task_refs=TASKS if with_rows else None,
                requirements_check=ROWS if with_rows else None))
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


async def test_a_reader_gets_the_tasks_and_one_row_per_criterion(monkeypatch):
    async with api(monkeypatch=monkeypatch) as c:
        body = (await c.get("/api/pull-requests/p1/requirements")).json()
    assert [t["key"] for t in body["tasks"]] == ["PROJ-1", "PROJ-2"]
    assert [(r["id"], r["verdict"]) for r in body["requirements"]] == [("AC1", "met"), ("AC2", "missing")]
    assert body["requirements"][0]["evidence"] == "src/export.py:1"


async def test_only_an_https_link_is_served_and_no_other_field_leaves(monkeypatch):
    async with api(monkeypatch=monkeypatch) as c:
        text = (await c.get("/api/pull-requests/p1/requirements")).text
        body = (await c.get("/api/pull-requests/p1/requirements")).json()
    assert body["tasks"][0]["url"] == "https://celmis.example.com/browse/PROJ-1"
    assert body["tasks"][1]["url"] == ""
    assert "sk-should-never-leave" not in text and "javascript:" not in text


async def test_a_pr_that_read_no_task_has_empty_lists(monkeypatch):
    async with api(with_rows=False, monkeypatch=monkeypatch) as c:
        body = (await c.get("/api/pull-requests/p1/requirements")).json()
    assert body["tasks"] == [] and body["requirements"] == []


async def test_another_workspaces_pr_is_not_found(monkeypatch):
    async with api(monkeypatch=monkeypatch) as c:
        assert (await c.get("/api/pull-requests/p-other/requirements")).status_code == 404
        assert (await c.get("/api/pull-requests/nope/requirements")).status_code == 404


async def test_a_repository_the_caller_may_not_read_is_forbidden(monkeypatch):
    async with api(readable=False, monkeypatch=monkeypatch) as c:
        response = await c.get("/api/pull-requests/p1/requirements")
    assert response.status_code == 403 and "Offcuts" not in response.text


# ─── persistence ─────────────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    ReviewIssue.__table__.create(eng)
    ReviewPullRequest.__table__.create(eng)
    return eng


def _record(engine, *, requirements, task_status="ok", status="skipped"):
    pr = SimpleNamespace(provider="github", repo="acme/shop", number=1, title="t", author="a",
                         url=None, head_ref="f", base_ref="develop", state="open", head_sha="h1",
                         local_slug="acme/shop")
    task = SimpleNamespace(status=task_status, task_refs=lambda: [{"key": "PROJ-1"}])
    batch = SimpleNamespace(task_context=task, requirements=requirements, issues=[], findings=[],
                            file_hashes={})
    issues._record(batch, pr, run_id="r1", workspace_id=WS, status=status, engine=engine)


def _stored(engine):
    with Session(engine) as s:
        return s.query(ReviewPullRequest).one().requirements_check


def test_a_review_stores_its_requirement_rows(engine):
    _record(engine, requirements=[Requirement.from_dict(r) for r in ROWS])
    assert [r["id"] for r in _stored(engine)] == ["AC1", "AC2"]


def test_a_run_that_reviewed_nothing_keeps_the_stored_rows(engine):
    _record(engine, requirements=[Requirement.from_dict(r) for r in ROWS])
    _record(engine, requirements=None)
    assert len(_stored(engine)) == 2


def test_a_pr_that_names_no_task_any_more_loses_its_rows(engine):
    _record(engine, requirements=[Requirement.from_dict(r) for r in ROWS])
    _record(engine, requirements=None, task_status="no_key")
    assert _stored(engine) is None


def test_a_review_that_read_the_task_but_checked_nothing_clears_the_old_rows(engine):
    _record(engine, requirements=[Requirement.from_dict(r) for r in ROWS], status="complete")
    _record(engine, requirements=None, status="complete")
    assert _stored(engine) is None


def test_a_jira_failure_in_a_review_keeps_the_stored_rows(engine):
    _record(engine, requirements=[Requirement.from_dict(r) for r in ROWS], status="complete")
    _record(engine, requirements=None, task_status="error", status="complete")
    assert len(_stored(engine)) == 2
