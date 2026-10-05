"""Review issues — findings followed across a pull request's runs.

    GET   /api/issues        — list, filtered and paged, for the active workspace
    PATCH /api/issues/{id}   — set the status (member and above)

Rows are written by the review pipeline (src/review/issues.py); this router
only reads them and lets a person close or reopen one. Any member of the
workspace may read; viewers may not change a status.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import current_workspace_id, get_current_user
from src.db.models import ReviewIssue, ReviewPullRequest
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/issues", tags=["issues"])

_STATUSES = ("open", "fixed", "dismissed", "resolved")
_SEVERITY_ORDER = case(
    (ReviewIssue.severity == "critical", 0),
    (ReviewIssue.severity == "error", 1),
    (ReviewIssue.severity == "warning", 2),
    else_=3,
)


class IssueOut(BaseModel):
    id: str
    status: str
    severity: str
    category: str
    title: str
    body: str
    suggestion: str | None
    repo_slug: str
    file_path: str
    line: int | None
    agent: str | None
    rule_id: str | None
    resolution_source: str | None
    pr_provider: str
    pr_repo: str
    pr_number: int
    pr_url: str | None
    pr_title: str | None = None
    pr_state: str | None = None
    occurrences: int
    first_seen_sha: str | None
    last_seen_sha: str | None
    fixed_in_sha: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    closed_at: datetime | None


class IssueList(BaseModel):
    items: list[IssueOut]
    total: int
    limit: int
    offset: int
    #: Per-status totals under the OTHER filters, for the status tabs.
    status_counts: dict[str, int]
    #: Repositories that have issues in this workspace, for the filter.
    repos: list[str]


class IssuePatch(BaseModel):
    status: Literal["open", "fixed", "dismissed", "resolved"]


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _to_out(r: ReviewIssue, pr: ReviewPullRequest | None) -> IssueOut:
    return IssueOut(
        id=r.id, status=r.status, severity=r.severity, category=r.category,
        title=r.title, body=r.body, suggestion=r.suggestion,
        repo_slug=r.repo_slug, file_path=r.file_path, line=r.line,
        agent=r.agent, rule_id=r.rule_id,
        resolution_source=r.resolution_source,
        pr_provider=r.pr_provider, pr_repo=r.pr_repo, pr_number=r.pr_number,
        pr_url=r.pr_url or (pr.url if pr else None),
        pr_title=pr.title if pr else None, pr_state=pr.state if pr else None,
        occurrences=r.occurrences, first_seen_sha=r.first_seen_sha,
        last_seen_sha=r.last_seen_sha, fixed_in_sha=r.fixed_in_sha,
        first_seen_at=r.first_seen_at, last_seen_at=r.last_seen_at,
        closed_at=r.closed_at,
    )


@router.get("", response_model=IssueList)
async def list_issues(
    status: str | None = Query(default=None, description="comma-separated"),
    severity: str | None = Query(default=None, description="comma-separated"),
    category: str | None = Query(default=None, description="comma-separated"),
    repo: str | None = Query(default=None, max_length=300),
    pr: int | None = Query(default=None, ge=0),
    q: str | None = Query(default=None, max_length=200),
    sort: Literal["newest", "oldest", "severity", "last_seen"] = "newest",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    ws: str = Depends(current_workspace_id),
) -> IssueList:
    base = [ReviewIssue.workspace_id == ws]
    if severity_list := _csv(severity):
        base.append(ReviewIssue.severity.in_(severity_list))
    if category_list := _csv(category):
        base.append(ReviewIssue.category.in_(category_list))
    if repo:
        base.append(or_(ReviewIssue.repo_slug == repo, ReviewIssue.pr_repo == repo))
    if pr is not None:
        base.append(ReviewIssue.pr_number == pr)
    if q and q.strip():
        like = f"%{q.strip().lower()}%"
        base.append(or_(
            func.lower(ReviewIssue.title).like(like),
            func.lower(ReviewIssue.file_path).like(like),
        ))

    status_counts = {s: 0 for s in _STATUSES}
    for st, n in (await session.execute(
        select(ReviewIssue.status, func.count()).where(*base)
        .group_by(ReviewIssue.status)
    )).all():
        status_counts[str(st)] = int(n)

    where = list(base)
    if status_list := _csv(status):
        where.append(ReviewIssue.status.in_(status_list))

    total = int((await session.execute(
        select(func.count()).select_from(ReviewIssue).where(*where)
    )).scalar() or 0)

    order = {
        "newest": (ReviewIssue.first_seen_at.desc(),),
        "oldest": (ReviewIssue.first_seen_at.asc(),),
        "severity": (_SEVERITY_ORDER.asc(), ReviewIssue.first_seen_at.desc()),
        "last_seen": (ReviewIssue.last_seen_at.desc(),),
    }[sort]
    rows = (await session.execute(
        select(ReviewIssue).where(*where).order_by(*order, ReviewIssue.id)
        .limit(limit).offset(offset)
    )).scalars().all()

    prs: dict[tuple[str, str, int], ReviewPullRequest] = {}
    keys = {(r.pr_provider, r.pr_repo, r.pr_number) for r in rows}
    if keys:
        for p in (await session.execute(
            select(ReviewPullRequest).where(
                ReviewPullRequest.workspace_id == ws,
                ReviewPullRequest.number.in_({k[2] for k in keys}),
            )
        )).scalars():
            prs[(p.provider, p.repo, p.number)] = p

    repos = sorted({
        str(r) for (r,) in (await session.execute(
            select(ReviewIssue.repo_slug).where(ReviewIssue.workspace_id == ws)
            .distinct()
        )).all() if r
    })

    return IssueList(
        items=[_to_out(r, prs.get((r.pr_provider, r.pr_repo, r.pr_number)))
               for r in rows],
        total=total, limit=limit, offset=offset,
        status_counts=status_counts, repos=repos,
    )


@router.patch("/{issue_id}", response_model=IssueOut)
async def set_issue_status(
    issue_id: str,
    payload: IssuePatch,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws: str = Depends(current_workspace_id),
) -> IssueOut:
    # The role check, the tenant check and the close/reopen bookkeeping are
    # one function: the agent changes an issue through it too.
    from src.automation.actions_reviews import apply_issue_status

    row, pr = await apply_issue_status(session, user, ws, issue_id, payload.status)
    return _to_out(row, pr)
