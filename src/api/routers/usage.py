"""Usage summary — aggregates cost + tokens from `review_runs` table.

Powers the "Usage" card on /settings so the tech-lead sees at a glance
how much the AI reviewer costs this month.

    GET /api/usage/summary?days=30

Returns totals, plus 30-day daily breakdown for the sparkline.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from src.api.deps import current_workspace_id, get_current_user
from src.api.review_runs import get_review_run_store
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/usage", tags=["usage"])


class DailyUsage(BaseModel):
    date: str            # YYYY-MM-DD (UTC)
    runs: int
    tokens_input: int
    tokens_output: int
    cost_usd: float


class UsageSummary(BaseModel):
    days: int
    total_runs: int
    completed_runs: int
    failed_runs: int
    tokens_input: int
    tokens_output: int
    cost_usd: float
    cost_source_mix: dict[str, int]     # e.g. {"litellm_estimate": 10, "openrouter_actual": 2}
    daily: list[DailyUsage]


@router.get("/summary", response_model=UsageSummary)
def usage_summary(
    days: int = Query(default=30, ge=1, le=365),
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> UsageSummary:
    """Sum tokens + cost from THIS WORKSPACE's review runs over `days`.

    It used to be every workspace's. The three queries below read
    `review_runs` filtered on `started_at` alone, behind plain
    `get_current_user`, so any authenticated account on the installation saw
    the same installation-wide run count, token totals and dollar figure —
    including workspaces it has no membership in. On a deployment with five
    tenants that is a cross-tenant read of commercial data, and the note that
    used to sit here called it intentional.

    `review_runs.workspace_id` has existed since the column was added; nothing
    was ever asking for it.
    """
    store = get_review_run_store()
    since = datetime.now(UTC) - timedelta(days=days)

    with sqlite3.connect(store.db_path) as conn:
        conn.row_factory = sqlite3.Row

        # Overall aggregates.
        overall = conn.execute(
            """
            SELECT
                COUNT(*)                                          AS total_runs,
                SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) AS completed_runs,
                SUM(CASE WHEN status='failed'   THEN 1 ELSE 0 END) AS failed_runs,
                COALESCE(SUM(tokens_input),  0)                    AS tokens_input,
                COALESCE(SUM(tokens_output), 0)                    AS tokens_output,
                COALESCE(SUM(cost_usd),      0)                    AS cost_usd
            FROM review_runs
            WHERE started_at >= ? AND workspace_id = ?
            """,
            (since.isoformat(), workspace_id),
        ).fetchone()

        # Cost source breakdown.
        source_rows = conn.execute(
            """
            SELECT cost_source, COUNT(*) AS n
            FROM review_runs
            WHERE started_at >= ? AND workspace_id = ? AND cost_source IS NOT NULL
            GROUP BY cost_source
            """,
            (since.isoformat(), workspace_id),
        ).fetchall()
        source_mix = {r["cost_source"]: r["n"] for r in source_rows}

        # Daily breakdown for a sparkline.
        # SQLite's `substr(started_at, 1, 10)` extracts YYYY-MM-DD.
        daily_rows = conn.execute(
            """
            SELECT
                substr(started_at, 1, 10) AS date,
                COUNT(*)                   AS runs,
                COALESCE(SUM(tokens_input), 0)  AS tokens_input,
                COALESCE(SUM(tokens_output), 0) AS tokens_output,
                COALESCE(SUM(cost_usd), 0)      AS cost_usd
            FROM review_runs
            WHERE started_at >= ? AND workspace_id = ?
            GROUP BY date
            ORDER BY date
            """,
            (since.isoformat(), workspace_id),
        ).fetchall()

    daily = [
        DailyUsage(
            date=r["date"],
            runs=int(r["runs"]),
            tokens_input=int(r["tokens_input"]),
            tokens_output=int(r["tokens_output"]),
            cost_usd=float(r["cost_usd"]),
        )
        for r in daily_rows
    ]

    return UsageSummary(
        days=days,
        total_runs=int(overall["total_runs"] or 0),
        completed_runs=int(overall["completed_runs"] or 0),
        failed_runs=int(overall["failed_runs"] or 0),
        tokens_input=int(overall["tokens_input"]),
        tokens_output=int(overall["tokens_output"]),
        cost_usd=float(overall["cost_usd"]),
        cost_source_mix=source_mix,
        daily=daily,
    )
