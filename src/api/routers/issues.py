"""Review issues — findings followed across a pull request's runs.

    GET   /api/issues          — list, filtered and paged, for the active workspace
    GET   /api/issues/summary  — implementation rate and backlog numbers
    POST  /api/issues/recheck  — recheck the backlog against the target branches
    PATCH /api/issues/{id}     — set the status (member and above)

Rows are written by the review pipeline (src/review/issues.py); this router
only reads them and lets a person close or reopen one. Any member of the
workspace may read; viewers may not change a status.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import current_workspace_id, get_current_user
from src.db.models import ReviewIssue, ReviewPullRequest
from src.db.session import get_async_session
from src.review.issues import AUTO_RESOLUTION_SOURCES, implementation_stats
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
    # The backlog layer: what became of the issue once its PR merged.
    base_ref: str | None = None
    merged_at: datetime | None = None
    close_outcome: str | None = None
    dup_of: str | None = None
    duplicates_count: int = 0
    #: auto | manual | feedback | pr_closed — how it was closed, None if open.
    resolution_kind: str | None = None
    resolution_note: str | None = None
    fixed_by_pr_number: int | None = None
    fixed_by_pr_url: str | None = None
    fixed_by_pr_title: str | None = None
    last_checked_at: datetime | None = None


class IssueList(BaseModel):
    items: list[IssueOut]
    total: int
    limit: int
    offset: int
    #: Per-status totals under the OTHER filters, for the status tabs.
    status_counts: dict[str, int]
    #: Repositories that have issues in this workspace, for the filter.
    repos: list[str]


class IssueSummary(BaseModel):
    days: int
    implemented: int
    unimplemented: int
    dismissed: int
    abandoned: int
    #: implemented / (implemented + unimplemented); None until one is decided.
    implementation_rate: float | None
    #: Open issues of merged PRs: what is still on the branch.
    backlog_open: int
    #: Closed by the resolver inside the window.
    auto_resolved: int
    #: What the last recheck of each branch put back (a revert).
    reopened: int


class RecheckIn(BaseModel):
    repo: str | None = None


class RecheckOut(BaseModel):
    queued: int


class IssuePatch(BaseModel):
    status: Literal["open", "fixed", "dismissed", "resolved"]


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _to_out(
    r: ReviewIssue, pr: ReviewPullRequest | None, *,
    duplicates: int = 0, fixed_by: ReviewPullRequest | None = None,
) -> IssueOut:
    from src.review.issues import resolution_kind

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
        base_ref=r.base_ref, merged_at=r.merged_at, close_outcome=r.close_outcome,
        dup_of=r.dup_of, duplicates_count=duplicates,
        resolution_kind=resolution_kind(r.status, r.resolution_source),
        resolution_note=r.resolution_note,
        fixed_by_pr_number=r.fixed_by_pr_number,
        fixed_by_pr_url=r.fixed_by_pr_url or (fixed_by.url if fixed_by else None),
        fixed_by_pr_title=fixed_by.title if fixed_by else None,
        last_checked_at=r.last_checked_at,
    )


def _resolution_clause(kinds: list[str]):
    """`resolution=auto,manual,…` as a where clause. Unknown words match
    nothing rather than everything."""
    parts = []
    for kind in kinds:
        if kind == "auto":
            parts.append(and_(ReviewIssue.status == "fixed",
                              ReviewIssue.resolution_source.in_(AUTO_RESOLUTION_SOURCES)))
        elif kind in ("manual", "feedback", "pr_closed"):
            parts.append(ReviewIssue.resolution_source == kind)
    return or_(*parts) if parts else ReviewIssue.id.is_(None)


@router.get("", response_model=IssueList)
async def list_issues(
    status: str | None = Query(default=None, description="comma-separated"),
    severity: str | None = Query(default=None, description="comma-separated"),
    category: str | None = Query(default=None, description="comma-separated"),
    repo: str | None = Query(default=None, max_length=300),
    # Repositories to leave out, by slug or by full name. The page never sends
    # it; the agent does, for the repositories the asker's team grants exclude,
    # so the totals and the status counts are the ones the asker may see.
    exclude_repo: list[str] | None = Query(default=None, max_length=100),
    pr: int | None = Query(default=None, ge=0),
    # backlog = issues of PRs that merged; pr = issues of PRs not merged (yet).
    scope: Literal["backlog", "pr"] | None = None,
    resolution: str | None = Query(default=None, description="auto|manual|feedback|pr_closed, comma-separated"),
    outcome: str | None = Query(default=None, description="implemented|unimplemented|dismissed|abandoned, comma-separated"),
    include_duplicates: bool = False,
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
    if exclude_repo:
        base.append(~or_(func.coalesce(ReviewIssue.repo_slug, "").in_(exclude_repo),
                         func.coalesce(ReviewIssue.pr_repo, "").in_(exclude_repo)))
    if pr is not None:
        base.append(ReviewIssue.pr_number == pr)
    if scope == "backlog":
        base.append(ReviewIssue.merged_at.is_not(None))
    elif scope == "pr":
        base.append(ReviewIssue.merged_at.is_(None))
    if resolution_list := _csv(resolution):
        base.append(_resolution_clause(resolution_list))
    if outcome_list := _csv(outcome):
        base.append(ReviewIssue.close_outcome.in_(outcome_list))
    if not include_duplicates and pr is None:
        # A row another PR's run repeated points at the backlog issue; the
        # list shows the one issue, with "also seen in N PRs". Asked for one
        # PR, though, every issue of that PR is its own: hiding the repeats
        # would drop live findings of the very PR the caller named.
        base.append(ReviewIssue.dup_of.is_(None))
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
    keys |= {(r.pr_provider, r.pr_repo, r.fixed_by_pr_number)
             for r in rows if r.fixed_by_pr_number}
    if keys:
        for p in (await session.execute(
            select(ReviewPullRequest).where(
                ReviewPullRequest.workspace_id == ws,
                ReviewPullRequest.number.in_({k[2] for k in keys}),
            )
        )).scalars():
            prs[(p.provider, p.repo, p.number)] = p

    duplicates: dict[str, int] = {}
    if rows:
        for parent, n in (await session.execute(
            select(ReviewIssue.dup_of, func.count()).where(
                ReviewIssue.workspace_id == ws,
                ReviewIssue.dup_of.in_([r.id for r in rows]),
            ).group_by(ReviewIssue.dup_of)
        )).all():
            duplicates[str(parent)] = int(n)

    repos = sorted({
        str(r) for (r,) in (await session.execute(
            select(ReviewIssue.repo_slug).where(ReviewIssue.workspace_id == ws)
            .distinct()
        )).all() if r
    })

    return IssueList(
        items=[_to_out(
            r, prs.get((r.pr_provider, r.pr_repo, r.pr_number)),
            duplicates=duplicates.get(r.id, 0),
            fixed_by=prs.get((r.pr_provider, r.pr_repo, r.fixed_by_pr_number))
            if r.fixed_by_pr_number else None)
            for r in rows],
        total=total, limit=limit, offset=offset,
        status_counts=status_counts, repos=repos,
    )


@router.get("/summary", response_model=IssueSummary)
async def issues_summary(
    repo: str | None = Query(default=None, max_length=300),
    days: int = Query(default=30, ge=1, le=366),
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    ws: str = Depends(current_workspace_id),
) -> IssueSummary:
    """The numbers the page's header strip shows, and the one place the
    metrics read the implementation rate from (`implementation_stats`).

    The rate counts issues of PRs that MERGED inside the window, by the fate
    frozen at the merge; the backlog and the repeat rows are not in it.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    base = [ReviewIssue.workspace_id == ws, ReviewIssue.dup_of.is_(None)]
    if repo:
        base.append(or_(ReviewIssue.repo_slug == repo, ReviewIssue.pr_repo == repo))

    outcomes = (await session.execute(
        select(ReviewIssue.close_outcome, func.count()).where(
            *base, ReviewIssue.merged_at.is_not(None), ReviewIssue.merged_at >= since,
            ReviewIssue.close_outcome.is_not(None),
        ).group_by(ReviewIssue.close_outcome)
    )).all()
    stats = implementation_stats(
        {"close_outcome": o} for o, n in outcomes for _ in range(int(n)))

    backlog_open = int((await session.execute(
        select(func.count()).select_from(ReviewIssue).where(
            *base, ReviewIssue.merged_at.is_not(None), ReviewIssue.status == "open")
    )).scalar() or 0)
    auto_resolved = int((await session.execute(
        select(func.count()).select_from(ReviewIssue).where(
            *base, ReviewIssue.status == "fixed",
            ReviewIssue.resolution_source.in_(AUTO_RESOLUTION_SOURCES),
            ReviewIssue.closed_at >= since)
    )).scalar() or 0)

    from src.db.models import ReviewIssueRecheckState as State

    states = select(State.last_result).where(State.workspace_id == ws)
    if repo:
        states = states.where(State.pr_repo == repo)
    reopened = 0
    for (result,) in (await session.execute(states)).all():
        if isinstance(result, dict):
            with contextlib.suppress(TypeError, ValueError):
                reopened += int(result.get("reopened") or 0)

    return IssueSummary(
        days=days, implemented=stats["implemented"],
        unimplemented=stats["unimplemented"], dismissed=stats["dismissed"],
        abandoned=stats["abandoned"],
        implementation_rate=stats["implementation_rate"],
        backlog_open=backlog_open, auto_resolved=auto_resolved, reopened=reopened,
    )


#: Strong references to the rechecks in flight (a task nobody holds can be
#: collected mid-run).
_RECHECKS: set[asyncio.Task] = set()


@router.post("/recheck", response_model=RecheckOut, status_code=202)
async def recheck_issues(
    payload: RecheckIn | None = None,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws: str = Depends(current_workspace_id),
) -> RecheckOut:
    """Recheck the backlog of the workspace (or one repository) now.

    One recheck per branch that has something to watch, run in the
    background; `queued` is how many branches. A branch whose head has not
    moved since it was last checked costs one request.
    """
    from src.api import deps as deps_module

    if not user.is_admin:
        role = await asyncio.to_thread(deps_module.workspace_role, user.id, ws)
        if role not in deps_module.ISSUE_WRITE_ROLES:
            raise HTTPException(
                status_code=403,
                detail="Rechecking issues requires member or above on this workspace")
    repo = (payload.repo if payload else None) or None
    from src.review.issue_resolver import REVERT_WATCH_DAYS

    watch_since = datetime.now(UTC) - timedelta(days=REVERT_WATCH_DAYS)
    q = select(ReviewIssue.pr_provider, ReviewIssue.pr_repo, ReviewIssue.base_ref).where(
        ReviewIssue.workspace_id == ws, ReviewIssue.merged_at.is_not(None),
        ReviewIssue.base_ref.is_not(None), ReviewIssue.dup_of.is_(None),
        or_(ReviewIssue.status == "open",
            and_(ReviewIssue.status == "fixed",
                 ReviewIssue.resolution_source.in_(AUTO_RESOLUTION_SOURCES),
                 or_(ReviewIssue.closed_at.is_(None),
                     ReviewIssue.closed_at >= watch_since))),
    ).distinct()
    if repo:
        q = q.where(or_(ReviewIssue.repo_slug == repo, ReviewIssue.pr_repo == repo))
    branches = [(str(a), str(b), str(c)) for a, b, c in (await session.execute(q)).all()]

    from src.review.issue_resolver import recheck_backlog

    async def _all() -> None:
        # One branch at a time: each pass can call the model and the provider,
        # and a workspace with many branches must not start them all at once.
        # The provider is the repository's auto-review owner's, as for the
        # sweep and the merge webhook: the asker may have no token of their own.
        for provider, pr_repo, base_ref in branches:
            try:
                await asyncio.to_thread(
                    recheck_backlog, ws, provider, pr_repo, base_ref, reason="manual")
            except Exception as exc:  # noqa: BLE001
                logger.warning("issue_recheck_manual_failed repo=%s err_type=%s",
                               pr_repo, type(exc).__name__)

    if branches:
        task = asyncio.get_running_loop().create_task(_all())
        _RECHECKS.add(task)
        task.add_done_callback(_RECHECKS.discard)
    logger.info("review_issues_recheck ws=%s branches=%d by=%s", ws, len(branches), user.email)
    return RecheckOut(queued=len(branches))


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
