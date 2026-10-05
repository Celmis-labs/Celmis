"""Reviewed pull requests — what Celmis reviewed, and what became of them.

    GET /api/pull-requests — list for the active workspace
    GET /api/pull-requests/stats — the three summary cards above the list
    GET /api/pull-requests/{id}/runs — one PR's review runs, each with its
        ordered stages (the Kodus-style timeline)

Rows come from `review_pull_requests`, upserted after every review run and on
the provider's close/merge webhook (src/review/issues.py). Finding counts are
the PR's tracked issues, by severity.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import false as sa_false
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import current_workspace_id, get_current_user
from src.api.schemas import ReviewRunOut
from src.db.models import (
    RepoReviewPolicy,
    ReviewIssue,
    ReviewPullRequest,
    WorkspaceReviewDefaults,
)
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pull-requests", tags=["pull-requests"])


# ─── Summary cards ────────────────────────────────────────────────────

Bucket = Literal["reviewed_today", "awaiting", "attention"]


class PullRequestStats(BaseModel):
    """The three cards above the list. Every number is computed from rows the
    review pipeline already wrote — no provider call is made per request."""

    #: PRs whose latest review completed today (UTC day).
    reviewed_today: int
    #: Open PRs in auto-review repos, on a targeted base branch, whose latest
    #: review did not complete (skipped or failed).
    awaiting: int
    #: Open PRs whose latest review failed, or that carry open critical/error
    #: findings, or whose latest completed review asked for changes.
    attention: int
    #: Start of the counted day (UTC), ISO 8601.
    day_start: str


@dataclass
class _Buckets:
    reviewed_today: set[str] = field(default_factory=set)
    awaiting: set[str] = field(default_factory=set)
    attention: set[str] = field(default_factory=set)


def _utc_day_start() -> datetime:
    # No workspace timezone exists, so the "day" is the server's UTC day.
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _classify(
    session: AsyncSession, user: User, ws: str, repo: str | None,
) -> _Buckets:
    """Sort the workspace's PR rows into the three cards (sets of row ids).

    Definitions (also the cards' tooltips):

    reviewed_today - the PR's latest run is `complete` or `partial` and
        started today (UTC; no workspace timezone exists). Only the LATEST run
        counts, so a PR reviewed this morning and then skipped (a draft push,
        say) is not counted. Any PR state: one merged after its review still
        was reviewed today.

    awaiting - the PR is open, its repo is registered with auto-review on,
        its base branch is targeted by the repo's effective target_branches
        (same matcher and resolution as the orchestrator's gate: repo policy
        > workspace default > every branch), and its latest review did not
        complete (`skipped` - a draft, say - or `failed`).
        LIMITS: the DB stores the head the last COMPLETED review saw, not the
        provider's current head, so a push nobody has reviewed yet is not
        noticed until its run starts, and a PR Celmis never received an event
        for (opened before the webhook, delivery lost) has no row at all. Both
        need the provider's API, which this endpoint never calls.

    attention - the PR is open and one of: its latest review `failed`; it has
        an open finding of severity critical or error (what "request changes"
        is made of); or its latest completed review's verdict is `changes`.
        A failed PR is therefore in both "awaiting" and "attention".

    Scope: this workspace, the repo filter when given, and only repos the
    caller may read.
    """
    from fastapi import HTTPException

    from src.api import deps
    from src.api.auto_review import get_auto_review_store
    from src.review.branch_patterns import branch_targeted

    where = [ReviewPullRequest.workspace_id == ws,
             ReviewPullRequest.reviews_count > 0]
    if repo:
        where.append(or_(ReviewPullRequest.repo == repo,
                         ReviewPullRequest.repo_slug == repo))
    rows = (await session.execute(
        select(ReviewPullRequest).where(*where))).scalars().all()
    out = _Buckets()
    if not rows:
        return out

    # Readable repos - one permission check per distinct repo.
    cfgs = {(c.provider, c.full_name): c
            for c in await asyncio.to_thread(
                get_auto_review_store().list_for_workspace, ws)}
    verdicts: dict[str, bool] = {}

    async def _may_read(r: ReviewPullRequest) -> bool:
        cfg = cfgs.get((r.provider, r.repo))
        slug = (cfg.repo_slug if cfg else None) or r.repo_slug or r.repo
        if slug not in verdicts:
            try:
                await deps.enforce_repo_permission(slug, user, "read", ws)
                verdicts[slug] = True
            except HTTPException:
                verdicts[slug] = False
        return verdicts[slug]

    rows = [r for r in rows if await _may_read(r)]
    if not rows:
        return out

    # Effective target branches per repo slug, in two queries.
    policies = {slug: tb for slug, tb in (await session.execute(
        select(RepoReviewPolicy.repo_slug, RepoReviewPolicy.target_branches)
    )).all()}
    ws_default = (await session.execute(
        select(WorkspaceReviewDefaults.target_branches).where(
            WorkspaceReviewDefaults.workspace_id == ws))).scalar_one_or_none()

    def _patterns(r: ReviewPullRequest) -> list[str]:
        value = policies.get(r.repo_slug or "")
        if value is None:
            value = ws_default
        return [str(v) for v in value] if isinstance(value, list) else []

    open_rows = [r for r in rows if r.state == "open"]

    # Latest runs of the candidates (SQLite run store, chunked id lookup).
    day = _utc_day_start()
    need_runs = [r.last_run_id for r in rows if r.last_run_id and (
        _aware(r.updated_at) >= day
        or (r.state == "open" and r.last_review_status in ("complete", "partial")))]
    runs: dict = {}
    if need_runs:
        try:
            from src.api.review_runs import get_review_run_store

            runs = await asyncio.to_thread(get_review_run_store().get_many, need_runs)
        except Exception as exc:  # noqa: BLE001 - cards are not worth a 500
            logger.warning("pr_stats_runs_unreadable err=%s", type(exc).__name__)

    for r in rows:
        if r.last_review_status not in ("complete", "partial"):
            continue
        run = runs.get(r.last_run_id or "")
        if run is not None:
            try:
                started = _aware(datetime.fromisoformat(run.started_at))
            except (TypeError, ValueError):
                continue
            if started >= day:
                out.reviewed_today.add(r.id)
        elif _aware(r.updated_at) >= day:
            # Run row gone: the PR row was touched today by a review (a close
            # webhook is the only other writer - rare enough to accept).
            out.reviewed_today.add(r.id)

    if open_rows:
        numbers = {r.number for r in open_rows}
        risky = {(prov, rp, int(n)) for prov, rp, n in (await session.execute(
            select(ReviewIssue.pr_provider, ReviewIssue.pr_repo,
                   ReviewIssue.pr_number).where(
                ReviewIssue.workspace_id == ws,
                ReviewIssue.status == "open",
                ReviewIssue.severity.in_(("critical", "error")),
                ReviewIssue.pr_number.in_(numbers),
            ).distinct()
        )).all()}
        for r in open_rows:
            run = runs.get(r.last_run_id or "")
            if (r.last_review_status == "failed"
                    or (r.provider, r.repo, r.number) in risky
                    or (r.last_review_status in ("complete", "partial")
                        and run is not None and run.verdict == "changes")):
                out.attention.add(r.id)
            cfg = cfgs.get((r.provider, r.repo))
            if (cfg is not None and cfg.enabled
                    and r.last_review_status in ("skipped", "failed")):
                patterns = _patterns(r)
                if (not patterns or not r.base_ref
                        or branch_targeted(r.base_ref, patterns)):
                    out.awaiting.add(r.id)
    return out


@router.get("/stats", response_model=PullRequestStats)
async def pull_request_stats(
    repo: str | None = Query(default=None, max_length=300),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws: str = Depends(current_workspace_id),
) -> PullRequestStats:
    """The page's summary cards; see `_classify` for each definition."""
    b = await _classify(session, user, ws, repo)
    return PullRequestStats(
        reviewed_today=len(b.reviewed_today), awaiting=len(b.awaiting),
        attention=len(b.attention), day_start=_utc_day_start().isoformat(),
    )



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
    bucket: Bucket | None = Query(
        default=None,
        description="Only the PRs counted by one summary card (see /stats)"),
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
    if bucket:
        ids = getattr(await _classify(session, _user, ws, repo), bucket)
        where.append(ReviewPullRequest.id.in_(ids) if ids else sa_false())
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
