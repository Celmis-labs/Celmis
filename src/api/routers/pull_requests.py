"""Reviewed pull requests — what Celmis reviewed, and what became of them.

    GET /api/pull-requests — list for the active workspace
    GET /api/pull-requests/{id}/runs — one PR's review runs, each with its
        ordered stages (the Kodus-style timeline)

Rows come from `review_pull_requests`, upserted after every review run and on
the provider's close/merge webhook (src/review/issues.py). Finding counts are
the PR's tracked issues, by severity.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import current_workspace_id, get_current_user
from src.api.schemas import ReviewRunOut
from src.db.models import ReviewIssue, ReviewPullRequest
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pull-requests", tags=["pull-requests"])


class PullRequestOut(BaseModel):
    id: str
    provider: str
    repo: str
    repo_slug: str | None
    number: int
    title: str
    author: str | None
    url: str | None
    head_ref: str | None
    base_ref: str | None
    state: str
    head_sha: str | None
    #: complete | partial | skipped | failed — the last review's outcome
    last_review_status: str | None
    #: Why the last review ended the way it did — "Skipped — Branch mismatch:
    #: target branch 'master' does not match configured patterns ['main']".
    #: Read from the run store; null when the run predates stages and its
    #: outcome needs no explanation, or the run row is gone.
    last_review_reason: str | None = None
    last_run_id: str | None
    reviews_count: int
    issues_total: int = 0
    issues_open: int = 0
    by_severity: dict[str, int] = Field(default_factory=dict)
    opened_at: datetime
    updated_at: datetime
    closed_at: datetime | None


class PullRequestList(BaseModel):
    items: list[PullRequestOut]
    total: int
    limit: int
    offset: int
    repos: list[str]


@router.get("", response_model=PullRequestList)
async def list_pull_requests(
    q: str | None = Query(default=None, max_length=200),
    repo: str | None = Query(default=None, max_length=300),
    state: Literal["open", "merged", "closed"] | None = None,
    review_status: Literal["complete", "partial", "skipped", "failed"] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    ws: str = Depends(current_workspace_id),
) -> PullRequestList:
    # A close/merge webhook writes a row for any PR of a bound repository —
    # one never reviewed included (a skipped draft, one opened before the
    # install) — so the state of a review still running when it closed is not
    # lost. This page is what Celmis REVIEWED: such rows stay out of it.
    reviewed = ReviewPullRequest.reviews_count > 0
    where = [ReviewPullRequest.workspace_id == ws, reviewed]
    if repo:
        where.append(or_(ReviewPullRequest.repo == repo,
                         ReviewPullRequest.repo_slug == repo))
    if state:
        where.append(ReviewPullRequest.state == state)
    if review_status:
        where.append(ReviewPullRequest.last_review_status == review_status)
    if q and q.strip():
        term = q.strip().lstrip("#")
        like = f"%{term.lower()}%"
        conds = [
            func.lower(ReviewPullRequest.title).like(like),
            func.lower(func.coalesce(ReviewPullRequest.author, "")).like(like),
        ]
        if term.isdigit():
            conds.append(ReviewPullRequest.number == int(term))
        where.append(or_(*conds))

    total = int((await session.execute(
        select(func.count()).select_from(ReviewPullRequest).where(*where)
    )).scalar() or 0)
    rows = (await session.execute(
        select(ReviewPullRequest).where(*where)
        .order_by(ReviewPullRequest.updated_at.desc(), ReviewPullRequest.id)
        .limit(limit).offset(offset)
    )).scalars().all()

    counts: dict[tuple[str, str, int], dict[str, int]] = {}
    open_counts: dict[tuple[str, str, int], int] = {}
    if rows:
        numbers = {r.number for r in rows}
        for prov, rp, num, sev, st, n in (await session.execute(
            select(
                ReviewIssue.pr_provider, ReviewIssue.pr_repo, ReviewIssue.pr_number,
                ReviewIssue.severity, ReviewIssue.status, func.count(),
            ).where(
                ReviewIssue.workspace_id == ws,
                ReviewIssue.pr_number.in_(numbers),
            ).group_by(
                ReviewIssue.pr_provider, ReviewIssue.pr_repo, ReviewIssue.pr_number,
                ReviewIssue.severity, ReviewIssue.status,
            )
        )).all():
            key = (prov, rp, int(num))
            bucket = counts.setdefault(key, {})
            bucket[sev] = bucket.get(sev, 0) + int(n)
            if st == "open":
                open_counts[key] = open_counts.get(key, 0) + int(n)

    repos = sorted({
        str(r) for (r,) in (await session.execute(
            select(ReviewPullRequest.repo).where(
                ReviewPullRequest.workspace_id == ws, reviewed)
            .distinct()
        )).all() if r
    })

    reasons: dict[str, str | None] = {}
    run_ids = [r.last_run_id for r in rows if r.last_run_id]
    if run_ids:
        try:
            from src.api.review_runs import get_review_run_store

            runs = await asyncio.to_thread(get_review_run_store().get_many, run_ids)
            reasons = {rid: run.status_reason for rid, run in runs.items()}
        except Exception as exc:  # noqa: BLE001 — the list outranks the reason
            logger.warning("pr_list_reasons_unreadable err=%s", type(exc).__name__)

    items = []
    for r in rows:
        key = (r.provider, r.repo, r.number)
        sev = counts.get(key, {})
        items.append(PullRequestOut(
            id=r.id, provider=r.provider, repo=r.repo, repo_slug=r.repo_slug,
            number=r.number, title=r.title, author=r.author, url=r.url,
            head_ref=r.head_ref, base_ref=r.base_ref, state=r.state,
            head_sha=r.head_sha, last_review_status=r.last_review_status,
            last_review_reason=reasons.get(r.last_run_id or ""),
            last_run_id=r.last_run_id, reviews_count=r.reviews_count,
            issues_total=sum(sev.values()), issues_open=open_counts.get(key, 0),
            by_severity={s: sev.get(s, 0)
                         for s in ("critical", "error", "warning", "info")},
            opened_at=r.opened_at, updated_at=r.updated_at, closed_at=r.closed_at,
        ))
    return PullRequestList(items=items, total=total, limit=limit, offset=offset,
                           repos=repos)


class PullRequestRuns(BaseModel):
    pr_id: str
    #: Newest first, each with `stages`.
    items: list[ReviewRunOut]


@router.get("/{pr_id}/runs", response_model=PullRequestRuns)
async def pull_request_runs(
    pr_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws: str = Depends(current_workspace_id),
) -> PullRequestRuns:
    """Every recorded review of one PR, newest first, with its stages.

    Scoped like the list: a PR of another workspace is a 404, never a 403,
    so an id cannot be probed for existence.
    """
    row = (await session.execute(
        select(ReviewPullRequest).where(
            ReviewPullRequest.id == pr_id, ReviewPullRequest.workspace_id == ws)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Pull request not found")
    from src.api.review_runs import get_review_run_store
    from src.api.routers.reviews import _run_to_out

    runs = await asyncio.to_thread(
        get_review_run_store().list_for_pr, ws, row.provider, row.repo,
        int(row.number), user_id=user.id, limit=limit,
    )
    return PullRequestRuns(
        pr_id=pr_id,
        items=[_run_to_out(r, with_adjustments=False, with_stages=True) for r in runs],
    )
