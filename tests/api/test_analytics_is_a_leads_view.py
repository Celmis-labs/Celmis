"""Issues, pull requests and analytics — who may read what, and the numbers.

/api/analytics/summary reports cost and how often the team merged what the
reviewer flagged; it is for owner, admin and editor of the workspace (or a
global admin). A member or a viewer gets 403. /api/issues and
/api/pull-requests are readable by any member; changing an issue's status
needs member or above, so a viewer cannot.

The router runs over sqlite with the real models; `workspace_role` is
replaced so no Postgres is needed, and the SQLite run store is replaced by
seeded rows.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from src.api import deps as deps_module
from src.api.deps import current_workspace_id, get_current_user
from src.api.routers import analytics as analytics_router
from src.api.routers import issues as issues_router
from src.api.routers import pull_requests as prs_router
from src.db.models import ReviewIssue, ReviewPullRequest
from src.db.session import get_async_session
from src.review.analytics import percentile, summarize


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:  # pragma: no cover
    return "JSON"


WS = "ws-1"
NOW = datetime.now(UTC)


def _ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


RUNS = [
    {"status": "complete", "started_at": _ago(1).isoformat(), "elapsed_seconds": 60.0,
     "cost_usd": 0.20, "findings_count": 3, "critical": 1, "error_count": 1,
     "warning": 1, "info": 0},
    {"status": "complete", "started_at": _ago(2).isoformat(), "elapsed_seconds": 120.0,
     "cost_usd": 0.40, "findings_count": 2, "critical": 0, "error_count": 0,
     "warning": 1, "info": 1},
    {"status": "partial", "started_at": _ago(3).isoformat(), "elapsed_seconds": 300.0,
     "cost_usd": None, "findings_count": 1, "critical": 0, "error_count": 1,
     "warning": 0, "info": 0},
    {"status": "failed", "started_at": _ago(3).isoformat(), "elapsed_seconds": 2.0,
     "cost_usd": None, "findings_count": 0, "critical": 0, "error_count": 0,
     "warning": 0, "info": 0},
    # Outside a 7-day window.
    {"status": "complete", "started_at": _ago(20).isoformat(), "elapsed_seconds": 90.0,
     "cost_usd": 1.0, "findings_count": 9, "critical": 9, "error_count": 0,
     "warning": 0, "info": 0},
]


def _issue(i: int, *, status="open", source=None, category="bug", severity="error",
           pr=7, first=1.0, closed=None) -> dict:
    return {
        "id": f"i{i}", "workspace_id": WS, "repo_slug": "github_acme-api",
        "fingerprint": f"fp{i}", "file_path": f"src/f{i}.py", "line": i,
        "agent": "defect", "rule_id": "defect.x", "category": category,
        "severity": severity, "title": f"Issue {i}", "body": "", "suggestion": None,
        "status": status, "resolution_source": source, "pr_provider": "github",
        "pr_repo": "acme/api", "pr_number": pr, "pr_url": None,
        "first_run_id": "r1", "last_run_id": "r1", "occurrences": 1,
        "first_seen_at": _ago(first), "last_seen_at": _ago(first),
        "closed_at": _ago(closed) if closed is not None else None,
    }


ISSUES = [
    _issue(1, status="fixed", source="auto_next_commit", closed=0.5),
    _issue(2, status="fixed", source="auto_next_commit", closed=0.5, category="security",
           severity="critical"),
    _issue(3, status="open", pr=8, category="performance", severity="warning"),
    _issue(4, status="open", pr=8),
    _issue(5, status="dismissed", source="manual", closed=0.2, category="style",
           severity="info"),
    _issue(6, status="open", pr=9),
]
PRS = [
    {"id": "p7", "workspace_id": WS, "provider": "github", "repo": "acme/api",
     "number": 7, "title": "Add cache layer", "author": "dana", "state": "open",
     "reviews_count": 3, "last_review_status": "complete", "head_ref": "feat/cache",
     "opened_at": _ago(2), "updated_at": _ago(0.5)},
    {"id": "p8", "workspace_id": WS, "provider": "github", "repo": "acme/api",
     "number": 8, "title": "Fix login", "author": "lee", "state": "merged",
     "reviews_count": 1, "last_review_status": "partial",
     "opened_at": _ago(3), "updated_at": _ago(1)},
    {"id": "p9", "workspace_id": WS, "provider": "github", "repo": "acme/web",
     "number": 9, "title": "Bump deps", "author": "dana", "state": "open",
     "reviews_count": 1, "last_review_status": "failed",
     "opened_at": _ago(3), "updated_at": _ago(2)},
]


# ─── the arithmetic ──────────────────────────────────────────────────


def test_percentile_is_nearest_rank() -> None:
    assert percentile([], 50) is None
    assert percentile([60.0, 120.0, 300.0], 50) == 120.0
    assert percentile([60.0, 120.0, 300.0], 90) == 300.0


def test_summary_over_seeded_rows() -> None:
    issues = [
        {**i, "pr_state": next(p["state"] for p in PRS if p["number"] == i["pr_number"])}
        for i in ISSUES
    ]
    out = summarize(RUNS, issues, days=7, now=NOW)

    assert out["reviews"]["total"] == 4
    assert out["reviews"]["by_status"] == {
        "complete": 2, "partial": 1, "skipped": 0, "failed": 1}
    rt = out["review_time_seconds"]
    assert rt["avg"] == 160.0 and rt["p50"] == 120.0 and rt["p90"] == 300.0
    assert rt["samples"] == 3, "a failed run's 2 seconds is not a review time"
    cost = out["cost_usd"]
    assert cost["total"] == pytest.approx(0.60)
    assert cost["avg_per_review"] == pytest.approx(0.30)
    assert cost["runs_with_unknown_cost"] == 1, "NULL is unknown, not $0"
    assert out["cost_basis"] == "review_runs.cost_usd"
    assert out["findings_by_severity"] == {
        "critical": 1, "error": 2, "warning": 2, "info": 1}
    assert out["issues_by_category"]["security"] == 1
    assert out["issues_by_category"]["bug"] == 3
    oc = out["outcomes"]
    assert oc["found"] == 6
    assert oc["fixed_in_next_commits"] == 2
    assert oc["open_on_merged_prs"] == 2
    assert oc["still_open"] == 3
    assert oc["fix_rate_pct"] == 40.0, "2 fixed of 5 not dismissed"
    assert out["issues"]["fixed"] == 2 and out["issues"]["dismissed"] == 1
    assert len(out["daily"]) == 8, "every day of the window, zeros included"
    assert sum(d["reviews"] for d in out["daily"]) == 4


# ─── the routers ─────────────────────────────────────────────────────


@asynccontextmanager
async def api(*, role: str | None, is_admin: bool = False, monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(ReviewIssue.__table__.create)
        await conn.run_sync(ReviewPullRequest.__table__.create)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        for row in ISSUES:
            s.add(ReviewIssue(**row))
        for row in PRS:
            s.add(ReviewPullRequest(**row))
        # Another workspace's issue, which nothing here may see.
        s.add(ReviewIssue(**{**_issue(99), "id": "other", "workspace_id": "ws-2"}))
        await s.commit()

    monkeypatch.setattr(deps_module, "workspace_role", lambda uid, ws: role)
    monkeypatch.setattr(analytics_router, "_load_runs", lambda ws, since: list(RUNS))

    app = FastAPI()
    for r in (analytics_router, issues_router, prs_router):
        app.include_router(r.router)

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


@pytest.mark.parametrize(("role", "admin", "code"), [
    ("viewer", False, 403),
    ("member", False, 403),
    (None, False, 403),
    ("editor", False, 200),
    ("admin", False, 200),
    ("owner", False, 200),
    (None, True, 200),
])
async def test_analytics_access(role, admin, code, monkeypatch) -> None:
    async with api(role=role, is_admin=admin, monkeypatch=monkeypatch) as c:
        r = await c.get("/api/analytics/summary?days=7")
        assert r.status_code == code, r.text
        if code == 200:
            body = r.json()
            assert body["reviews"]["total"] == 4
            assert body["outcomes"]["found"] == 6, "another workspace leaked in"


async def test_analytics_refuses_an_unknown_window(monkeypatch) -> None:
    async with api(role="owner", monkeypatch=monkeypatch) as c:
        assert (await c.get("/api/analytics/summary?days=12")).status_code == 422


async def test_issues_list_filters_and_scopes(monkeypatch) -> None:
    async with api(role="viewer", monkeypatch=monkeypatch) as c:
        body = (await c.get("/api/issues")).json()
        assert body["total"] == 6
        assert body["status_counts"] == {
            "open": 3, "fixed": 2, "dismissed": 1, "resolved": 0}
        assert all(i["id"] != "other" for i in body["items"])

        body = (await c.get("/api/issues?status=open&sort=severity")).json()
        assert [i["id"] for i in body["items"]] == ["i4", "i6", "i3"]
        assert body["items"][0]["pr_state"] == "merged"

        body = (await c.get("/api/issues?category=security,performance")).json()
        assert {i["id"] for i in body["items"]} == {"i2", "i3"}
        body = (await c.get("/api/issues?pr=8")).json()
        assert {i["id"] for i in body["items"]} == {"i3", "i4"}


async def test_a_viewer_cannot_change_an_issue_and_a_member_can(monkeypatch) -> None:
    async with api(role="viewer", monkeypatch=monkeypatch) as c:
        r = await c.patch("/api/issues/i3", json={"status": "dismissed"})
        assert r.status_code == 403
    async with api(role="member", monkeypatch=monkeypatch) as c:
        r = await c.patch("/api/issues/i3", json={"status": "dismissed"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "dismissed" and body["resolution_source"] == "manual"
        r = await c.patch("/api/issues/i3", json={"status": "open"})
        assert r.json()["closed_at"] is None
        assert (await c.patch("/api/issues/other", json={"status": "open"})).status_code == 404
        assert (await c.patch("/api/issues/i3", json={"status": "gone"})).status_code == 422


async def test_pull_requests_list(monkeypatch) -> None:
    async with api(role="viewer", monkeypatch=monkeypatch) as c:
        body = (await c.get("/api/pull-requests")).json()
        assert [p["number"] for p in body["items"]] == [7, 8, 9]
        p8 = body["items"][1]
        assert p8["state"] == "merged" and p8["issues_open"] == 2
        assert p8["by_severity"]["warning"] == 1 and p8["issues_total"] == 2
        assert body["repos"] == ["acme/api", "acme/web"]

        body = (await c.get("/api/pull-requests?q=dana")).json()
        assert [p["number"] for p in body["items"]] == [7, 9]
        body = (await c.get("/api/pull-requests?q=%238")).json()
        assert [p["number"] for p in body["items"]] == [8]
        body = (await c.get("/api/pull-requests?state=merged")).json()
        assert body["total"] == 1
