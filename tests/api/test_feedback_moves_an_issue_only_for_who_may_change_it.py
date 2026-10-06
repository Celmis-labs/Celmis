"""Feedback on a finding reaches its issue — for members, on any run of the PR.

PUT /api/feedback/run/{id} with file_path + title also dismisses the PR's
issue, and DELETE reopens it. PATCH /api/issues refuses a viewer; the feedback
route used to do the same change for one, unchecked. A viewer's verdict is
still recorded — it just stops there.

And the issue is found by the run's PR, not by run id: an issue keeps only
its first and latest run, so feedback given on a run in between matched
nothing and the run page and the Issues page disagreed.

Runs over sqlite with the real models; `workspace_role` and the run store are
replaced.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import src.api.review_runs as runs_mod
import src.review.issues as issues_mod
from src.api import deps as deps_module
from src.api.deps import current_workspace_id, get_current_user
from src.api.routers import feedback as feedback_router
from src.db.models import FindingFeedback, ReviewIssue
from src.db.session import get_async_session
from src.review.issues import fingerprint


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


WS = "ws-1"
TITLE = "Unchecked return"


class _Runs:
    """r1..r3 are runs of PR #7; r9 is a run of another workspace."""

    def get(self, run_id: str):
        return None  # no stored run body: the repository gate has nothing to ask

    def pr_of(self, run_id: str):
        if run_id == "r9":
            return ("ws-2", "github", "acme/api", 7)
        if run_id in ("r1", "r2", "r3"):
            return (WS, "github", "acme/api", 7)
        return None


@pytest.fixture
def ledger(monkeypatch):
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    ReviewIssue.__table__.create(eng)
    now = datetime.now(UTC)
    with Session(eng) as s:
        s.add(ReviewIssue(
            id="i1", workspace_id=WS, repo_slug="github_acme-api",
            fingerprint=fingerprint("defect.ret", "src/a.py", TITLE),
            file_path="src/a.py", agent="defect", rule_id="defect.ret",
            category="bug", severity="error", title=TITLE, body="",
            status="open", pr_provider="github", pr_repo="acme/api", pr_number=7,
            first_run_id="r1", last_run_id="r3", occurrences=3,
            first_seen_at=now, last_seen_at=now,
        ))
        s.commit()
    monkeypatch.setattr(issues_mod, "_engine", lambda: eng)
    monkeypatch.setattr(runs_mod, "get_review_run_store", lambda: _Runs())
    yield eng
    eng.dispose()


def _status(eng) -> str:
    with Session(eng) as s:
        return s.execute(select(ReviewIssue.status)).scalar_one()


@asynccontextmanager
async def api(*, role: str | None, monkeypatch, is_admin: bool = False):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(FindingFeedback.__table__.create)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(deps_module, "workspace_role", lambda uid, ws: role)

    app = FastAPI()
    app.include_router(feedback_router.router)

    async def _session():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_async_session] = _session
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="u-1", email="u@test", is_admin=is_admin)
    app.dependency_overrides[current_workspace_id] = lambda: WS
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    await engine.dispose()


def _body(state: str = "dismissed") -> dict:
    return {"finding_key": "k-1234", "state": state, "file_path": "src/a.py",
            "title": TITLE, "rule_id": "defect.ret"}


_CLEAR = "/api/feedback/run/r2/k-1234?file_path=src/a.py&title=Unchecked%20return" \
         "&rule_id=defect.ret"


async def test_a_viewer_records_a_verdict_but_cannot_close_the_issue(
    ledger, monkeypatch,
) -> None:
    async with api(role="viewer", monkeypatch=monkeypatch) as c:
        r = await c.put("/api/feedback/run/r3", json=_body())
        assert r.status_code == 200, r.text
        assert [f["state"] for f in (await c.get("/api/feedback/run/r3")).json()] == [
            "dismissed"]
    assert _status(ledger) == "open"


async def test_a_viewer_cannot_reopen_one_either(ledger, monkeypatch) -> None:
    async with api(role="member", monkeypatch=monkeypatch) as c:
        await c.put("/api/feedback/run/r2", json=_body())
    assert _status(ledger) == "dismissed"
    async with api(role="viewer", monkeypatch=monkeypatch) as c:
        assert (await c.delete(_CLEAR)).status_code == 204
    assert _status(ledger) == "dismissed"


async def test_a_member_moves_it_from_a_run_between_first_and_latest(
    ledger, monkeypatch,
) -> None:
    async with api(role="member", monkeypatch=monkeypatch) as c:
        assert (await c.put("/api/feedback/run/r2", json=_body())).status_code == 200
        assert _status(ledger) == "dismissed"
        assert (await c.delete(_CLEAR)).status_code == 204
        assert _status(ledger) == "open"


async def test_a_global_admin_needs_no_workspace_role(ledger, monkeypatch) -> None:
    async with api(role=None, is_admin=True, monkeypatch=monkeypatch) as c:
        await c.put("/api/feedback/run/r2", json=_body())
    assert _status(ledger) == "dismissed"


async def test_a_run_of_another_workspace_does_not_lend_its_pr(ledger, monkeypatch) -> None:
    async with api(role="member", monkeypatch=monkeypatch) as c:
        await c.put("/api/feedback/run/r9", json=_body())
    assert _status(ledger) == "open"
