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
    #: Open PRs (read from the provider's cached listing) on a targeted base
    #: branch with no completed review at their current head.
    awaiting: int
    #: True when some repo's listing failed or timed out: `awaiting` is then a
    #: lower bound ("at least N") and `missing_repos` names what is left out.
    partial: bool = False
    missing_repos: list[str] = Field(default_factory=list)
    #: Open PRs whose latest review failed, or that carry open critical/error
    #: findings, or whose latest completed review asked for changes.
    attention: int
    #: Start of the counted day (UTC), ISO 8601.
    day_start: str


@dataclass
class _Buckets:
    reviewed_today: set[str] = field(default_factory=set)
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

    awaiting - NOT computed here: see `_awaiting` (provider listing, not DB).

    attention - the PR is open and one of: its latest review `failed`; it has
        an open finding of severity critical or error (what "request changes"
        is made of); or its latest completed review's verdict is `changes`.

    Scope: this workspace, the repo filter when given, and only repos the
    caller may read.
    """
    from fastapi import HTTPException

    from src.api import deps
    from src.api.auto_review import get_auto_review_store

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
    return out


@dataclass
class _AwaitingPR:
    provider: str
    repo: str
    slug: str
    number: int
    title: str
    author: str
    url: str
    base: str | None
    created_at: str | None
    updated_at: str | None
    row_id: str | None  # the PR's DB row, when it has one


@dataclass
class _Awaiting:
    items: list[_AwaitingPR] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


#: Wall-clock budget for all provider listings of one request, and how many
#: run at once. The listings come from the ~5 min cache (`cached_open_pulls`),
#: so a warm page costs no provider call at all.
AWAITING_BUDGET_S = 4.0
AWAITING_WORKERS = 6


async def _awaiting(
    session: AsyncSession, user: User, ws: str, repo: str | None,
) -> _Awaiting:
    """Open PRs still waiting for a review, from the providers' listings.

    A PR is awaiting when ALL hold:
      * it is open in a repo registered in this workspace that the caller may
        read (the repo filter narrows this) - whatever the auto-review mode,
        so manual repos count;
      * its base branch is targeted by the repo's effective target_branches
        (repo policy > workspace default > every branch; the orchestrator's
        matcher);
      * it is not a draft, unless the repo's effective run_on_drafts is true;
      * no complete/partial review exists at its CURRENT head: the provider's
        head sha differs from the head the latest completed review stored
        (`review_pull_requests.head_sha`), or there is no such review. When
        the provider gives no sha, any completed review counts as reviewed.

    The listings are fetched concurrently (bounded pool) under an overall
    budget. A repo that fails or misses the budget is left out and named in
    `missing`, so the number is a lower bound, never a guess.
    """
    from fastapi import HTTPException

    from src.api import deps
    from src.api.auto_review import get_auto_review_store
    from src.review.branch_patterns import branch_targeted

    out = _Awaiting()
    unique: dict[tuple[str, str], object] = {}
    for c in await asyncio.to_thread(get_auto_review_store().list_for_workspace, ws):
        if repo and repo not in (c.full_name, c.repo_slug):
            continue
        unique.setdefault((c.provider, c.full_name), c)
    cfgs = []
    for c in unique.values():
        try:
            await deps.enforce_repo_permission(c.repo_slug, user, "read", ws)
        except HTTPException:
            continue
        cfgs.append(c)
    if not cfgs:
        return out

    from concurrent.futures import ThreadPoolExecutor

    from src.api.routers.repos import _open_listing, _repo_credential

    def _fetch(cfg):
        secret, email, gitlab = _repo_credential(cfg, user)
        return _open_listing(cfg, secret, email, branch=None, gitlab=gitlab)

    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=AWAITING_WORKERS,
                              thread_name_prefix="pr-awaiting")
    try:
        futs = [(c, loop.run_in_executor(pool, _fetch, c)) for c in cfgs]
        await asyncio.wait([f for _, f in futs], timeout=AWAITING_BUDGET_S)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    listings = []
    for c, fut in futs:
        if fut.done() and not fut.cancelled() and fut.exception() is None:
            listings.append((c, fut.result()))
        else:
            if fut.done() and not fut.cancelled():
                logger.warning("pr_awaiting_listing_failed repo=%s err=%s",
                               c.full_name, type(fut.exception()).__name__)
            out.missing.append(c.full_name)
    out.missing.sort()
    if not listings:
        return out

    # Effective settings per repo, and what the DB knows of each PR's review.
    policies = {slug: (tb, rd) for slug, tb, rd in (await session.execute(
        select(RepoReviewPolicy.repo_slug, RepoReviewPolicy.target_branches,
               RepoReviewPolicy.run_on_drafts)
    )).all()}
    ws_tb, ws_rd = (await session.execute(
        select(WorkspaceReviewDefaults.target_branches,
               WorkspaceReviewDefaults.run_on_drafts).where(
            WorkspaceReviewDefaults.workspace_id == ws))).one_or_none() or (None, None)
    known = {(r.provider, r.repo, r.number): r for r in (await session.execute(
        select(ReviewPullRequest).where(
            ReviewPullRequest.workspace_id == ws,
            ReviewPullRequest.reviews_count > 0))).scalars().all()}

    for c, listing in listings:
        try:
            from src.sync.git_providers import parse_repo_url
            slug = parse_repo_url(f"{c.provider}:{c.full_name}").slug
        except Exception:  # noqa: BLE001
            slug = c.repo_slug
        pol_tb, pol_rd = policies.get(slug, policies.get(c.repo_slug, (None, None)))
        tb = pol_tb if pol_tb is not None else ws_tb
        patterns = [str(v) for v in tb] if isinstance(tb, list) else []
        rd = pol_rd if pol_rd is not None else ws_rd
        drafts_ok = bool(rd)
        for p in listing.items:
            if p.draft and not drafts_ok:
                continue
            if patterns and p.target_branch and not branch_targeted(
                    p.target_branch, patterns):
                continue
            row = known.get((c.provider, c.full_name, p.number))
            done = row is not None and row.last_review_status in ("complete", "partial")
            if p.head_sha:
                reviewed_here = (row is not None and bool(row.head_sha)
                                 and row.head_sha == p.head_sha and done)
            else:
                reviewed_here = done
            if reviewed_here:
                continue
            out.items.append(_AwaitingPR(
                provider=c.provider, repo=c.full_name, slug=c.repo_slug,
                number=p.number, title=p.title, author=p.author, url=p.url,
                base=p.target_branch, created_at=p.created_at,
                updated_at=p.updated_at, row_id=row.id if row else None))
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
    aw = await _awaiting(session, user, ws, repo)
    return PullRequestStats(
        reviewed_today=len(b.reviewed_today), awaiting=len(aw.items),
        partial=bool(aw.missing), missing_repos=aw.missing,
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
    if bucket == "awaiting":
        return await _awaiting_list(session, _user, ws, repo, state, review_status,
                                    q, limit, offset)
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


def _parse_ts(value: str | None) -> datetime:
    try:
        return _aware(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return datetime.now(UTC)


async def _awaiting_list(
    session: AsyncSession, user: User, ws: str, repo: str | None,
    state: str | None, review_status: str | None, q: str | None,
    limit: int, offset: int,
) -> PullRequestList:
    """The list under the "Awaiting review" card: exactly the PRs `_awaiting`
    counts. Most have no DB row, so they are synthetic items (id
    "awaiting:<provider>:<repo>#<n>", status "awaiting", no reviews yet);
    the page renders them without the review timeline. A `state` other than
    open or any `review_status` filter matches none of them."""
    aw = await _awaiting(session, user, ws, repo)
    items = aw.items
    if (state and state != "open") or review_status:
        items = []
    if q and q.strip():
        term = q.strip().lstrip("#").lower()
        items = [i for i in items if term in i.title.lower()
                 or term in i.author.lower()
                 or (term.isdigit() and i.number == int(term))]
    items = sorted(items, key=lambda i: _parse_ts(i.updated_at), reverse=True)
    page = items[offset:offset + limit]
    out = [PullRequestOut(
        id=i.row_id or f"awaiting:{i.provider}:{i.repo}#{i.number}",
        provider=i.provider, repo=i.repo, repo_slug=i.slug, number=i.number,
        title=i.title, author=i.author or None, url=i.url or None,
        head_ref=None, base_ref=i.base, state="open", head_sha=None,
        last_review_status="awaiting", last_run_id=None,
        reviews_count=0 if not i.row_id else 1,
        opened_at=_parse_ts(i.created_at), updated_at=_parse_ts(i.updated_at),
        closed_at=None,
    ) for i in page]
    return PullRequestList(items=out, total=len(items), limit=limit, offset=offset,
                           repos=sorted({i.repo for i in aw.items}))


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
    from src.api.deps import can_see_review_cost

    show_cost = await asyncio.to_thread(can_see_review_cost, user, ws)
    return PullRequestRuns(
        pr_id=pr_id,
        items=[_run_to_out(r, with_adjustments=False, with_stages=True,
                           show_cost=show_cost) for r in runs],
    )
