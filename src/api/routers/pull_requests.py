"""Reviewed pull requests — what Celmis reviewed, and what became of them.

    GET /api/pull-requests — list for the active workspace

Rows come from `review_pull_requests`, upserted after every review run and on
the provider's close/merge webhook (src/review/issues.py). Finding counts are
the PR's tracked issues, by severity.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import current_workspace_id, get_current_user
from src.db.models import ReviewIssue, ReviewPullRequest
from src.db.session import get_async_session
from src.users import User

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

    items = []
    for r in rows:
        key = (r.provider, r.repo, r.number)
        sev = counts.get(key, {})
        items.append(PullRequestOut(
            id=r.id, provider=r.provider, repo=r.repo, repo_slug=r.repo_slug,
            number=r.number, title=r.title, author=r.author, url=r.url,
            head_ref=r.head_ref, base_ref=r.base_ref, state=r.state,
            head_sha=r.head_sha, last_review_status=r.last_review_status,
            last_run_id=r.last_run_id, reviews_count=r.reviews_count,
            issues_total=sum(sev.values()), issues_open=open_counts.get(key, 0),
            by_severity={s: sev.get(s, 0)
                         for s in ("critical", "error", "warning", "info")},
            opened_at=r.opened_at, updated_at=r.updated_at, closed_at=r.closed_at,
        ))
    return PullRequestList(items=items, total=total, limit=limit, offset=offset,
                           repos=repos)
