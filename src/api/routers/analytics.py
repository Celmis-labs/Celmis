"""Review analytics for the active workspace.

    GET /api/analytics/summary?days=7|30|90

Owner, admin or editor of the workspace (or a global admin) — see
`require_analytics_access`. The arithmetic lives in src/review/analytics.py;
this module only reads the two stores it needs:

  - runs from the SQLite `review_runs` table (time, cost, severity counts);
  - issues from Postgres `review_issues`, each with its PR's state.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import current_workspace_id, require_analytics_access
from src.api.review_runs import get_review_run_store
from src.db.models import ReviewIssue, ReviewPullRequest
from src.db.session import get_async_session
from src.review.analytics import ALLOWED_WINDOWS, summarize
from src.users import User

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

_RUN_COLUMNS = (
    "status", "started_at", "elapsed_seconds", "cost_usd", "findings_count",
    "critical", "error_count", "warning", "info",
)


def _load_runs(workspace_id: str, since: datetime) -> list[dict[str, Any]]:
    """This workspace's run rows since `since`, as dicts. Blocking."""
    store = get_review_run_store()
    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT {', '.join(_RUN_COLUMNS)} FROM review_runs "  # noqa: S608
            "WHERE workspace_id = ? AND started_at >= ?",
            (workspace_id, since.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


async def _load_issues(
    session: AsyncSession, workspace_id: str, since: datetime,
) -> list[dict[str, Any]]:
    rows = (await session.execute(
        select(ReviewIssue).where(
            ReviewIssue.workspace_id == workspace_id,
            or_(ReviewIssue.first_seen_at >= since, ReviewIssue.closed_at >= since),
        )
    )).scalars().all()
    states: dict[tuple[str, str, int], str] = {}
    numbers = {r.pr_number for r in rows}
    if numbers:
        for p in (await session.execute(
            select(ReviewPullRequest).where(
                ReviewPullRequest.workspace_id == workspace_id,
                ReviewPullRequest.number.in_(numbers),
            )
        )).scalars():
            states[(p.provider, p.repo, p.number)] = p.state
    return [
        {
            "status": r.status, "severity": r.severity, "category": r.category,
            "resolution_source": r.resolution_source,
            "first_seen_at": r.first_seen_at, "closed_at": r.closed_at,
            "pr_state": states.get((r.pr_provider, r.pr_repo, r.pr_number)),
        }
        for r in rows
    ]


@router.get("/summary")
async def analytics_summary(
    days: int = Query(default=30),
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(require_analytics_access),
    ws: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    if days not in ALLOWED_WINDOWS:
        raise HTTPException(
            status_code=422,
            detail=f"days must be one of {', '.join(map(str, ALLOWED_WINDOWS))}",
        )
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    runs = await asyncio.to_thread(_load_runs, ws, since)
    issues = await _load_issues(session, ws, since)
    return summarize(runs, issues, days=days, now=now)
