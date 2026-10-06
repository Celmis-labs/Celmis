# Celmis Enterprise Edition. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Productivity metrics for the active workspace.

    GET  /api/analytics/productivity/overview     KPIs, series, size histogram, deployments
    GET  /api/analytics/productivity/prs          the slowest or largest merged PRs
    GET  /api/analytics/productivity/developers   merged work and reviews per person
    GET  /api/analytics/productivity/filters      what the filter bar can offer
    GET  /api/analytics/productivity/sync         per-repository sync status
    POST /api/analytics/productivity/sync/run     queue a sync (a full re-read starts over)
    GET  /api/analytics/productivity/sync/estimate   what a backfill would cost
    GET  PUT /api/analytics/productivity/settings workspace default or one repository's override

Mounted by `src.ee.analytics.router` under the SAME licence feature
("analytics"), so one licence line covers both pages. Every route is for owner
and admin (`require_workspace_admin`, the rule that already guards spend and
review cost): the developer table and the sync settings are not for editors,
members or viewers, on the server and in the nav. The tables and the sync
that fills them are AGPL (src/productivity); this module only reads them,
plus the two writes that belong to a settings page (save, queue a sync).

Everything is scoped by the caller's workspace. A repository named in a
request must be one of the workspace's own (the auto-review store), so one
tenant cannot queue or configure another's.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import (
    current_workspace_id,
    require_workspace_admin,
)
from src.db.models import (
    ProductivityDeployment,
    ProductivityPrEvent,
    ProductivityPullRequest,
    ProductivityRepoSettings,
    ProductivitySyncState,
    ReviewIssue,
)
from src.db.session import get_async_session
from src.ee.analytics import productivity as metrics
from src.productivity import classify
from src.productivity import settings as settings_mod
from src.users import User

logger = logging.getLogger(__name__)

# The full prefix is spelled here, not inherited: `include_router` into the
# analytics router does not carry that router's own prefix down.
router = APIRouter(prefix="/api/analytics/productivity", tags=["productivity"])

#: What a request may read in one go; a workspace with more rows than this in
#: two windows gets the newest and `truncated: true`.
_ROW_CAP = 50_000

#: A repository name that cannot exist: what an empty group filters down to.
_NO_REPO = "::no-such-repository::"

_PR_COLUMNS = (
    "id", "provider", "repo", "number", "title", "url", "author_key", "author_name",
    "state", "target_branch", "created_at", "first_commit_at", "first_review_at",
    "merged_at", "closed_at", "additions", "deletions", "files_changed", "kind",
    "prod_deployed_at", "deploy_link", "detail_state",
)
_DEPLOYMENT_COLUMNS = (
    "id", "provider", "repo", "branch", "deployed_at", "source", "is_failure",
    "recovered_at",
)


# ─── what is asked for ───────────────────────────────────────────────

@dataclass(frozen=True)
class Filters:
    repos: frozenset[str] = frozenset()      # lower-cased full names; empty = all
    author: str | None = None
    target: str | None = None


def _window(days: int) -> int:
    if days not in metrics.ALLOWED_WINDOWS:
        raise HTTPException(
            status_code=422,
            detail=f"days must be one of {', '.join(map(str, metrics.ALLOWED_WINDOWS))}",
        )
    return days


def _workspace_repos(workspace_id: str) -> list[dict[str, Any]]:
    """The repositories this workspace has connected, each once. Blocking.

    Auto-review rows are keyed (user, repository), so one repository registered
    by two members comes back twice; it is one repository here, read with the
    registrant that sorts first, so the choice is the same on every call.
    """
    from src.api.auto_review import get_auto_review_store

    rows = sorted(
        ({"provider": c.provider, "repo": c.full_name, "repo_slug": c.repo_slug,
          "user_id": getattr(c, "user_id", None) or "default"}
         for c in get_auto_review_store().list_for_workspace(workspace_id)),
        key=lambda r: (r["provider"], r["repo"], r["user_id"]),
    )
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        key = (r["provider"], r["repo"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _group_repos(workspace_id: str, group: str) -> frozenset[str] | None:
    """Lower-cased full names of a repository group, or None when there is none. Blocking."""
    from src.groups.manager import get_group_manager
    from src.sync.git_providers import parse_repo_url

    for _path, g in get_group_manager().iter_groups(workspace_id):
        if g.name != group:
            continue
        names: set[str] = set()
        for ident in g.repos:
            try:
                parsed = parse_repo_url(ident)
            except ValueError:
                continue
            names.add(f"{parsed.owner}/{parsed.name}".lower())
        return frozenset(names)
    return None


async def _filters(
    workspace_id: str, repo: list[str] | None, group: str | None,
    author: str | None, target: str | None,
) -> Filters:
    repos = {r.strip().lower() for r in (repo or []) if r.strip()}
    if group:
        named = await asyncio.to_thread(_group_repos, workspace_id, group)
        if named is None:
            raise HTTPException(status_code=404, detail="Unknown repository group")
        # A group AND a repo list both narrow: the result is what they share.
        repos = (repos & named) if repos else set(named)
        if not repos:
            repos = {_NO_REPO}  # an empty group matches nothing, rather than everything
    return Filters(frozenset(repos), (author or None), (target or None))


# ─── reading ─────────────────────────────────────────────────────────

async def _settings_by_repo(session: AsyncSession, workspace_id: str):
    """`cfg(provider, repo)` -> the resolved settings, from the rows of this workspace."""
    rows = (await session.execute(
        select(ProductivityRepoSettings).where(ProductivityRepoSettings.workspace_id == workspace_id)
    )).scalars().all()
    found = {
        (r.provider, r.repo): {n: getattr(r, n) for n in settings_mod.SETTING_NAMES}
        for r in rows
    }

    def cfg(provider: str, repo: str):
        return settings_mod.resolve(
            workspace_row=found.get(("", "")), repo_row=found.get((provider, repo)))

    return cfg


def _scope(model: Any, workspace_id: str, f: Filters) -> list[Any]:
    clauses = [model.workspace_id == workspace_id]
    if f.repos:
        clauses.append(func.lower(model.repo).in_(f.repos))
    return clauses


async def _load(
    session: AsyncSession, workspace_id: str, f: Filters, since: datetime, now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Pull requests and deployments touching [since, now], filtered, as dicts.

    Ignored authors' PRs (bots) are dropped here, with each repository's own
    list: they would otherwise be most of the "fastest PRs" and none of the
    team's work.
    """
    cfg = await _settings_by_repo(session, workspace_id)
    pr = ProductivityPullRequest
    clauses = _scope(pr, workspace_id, f)
    if f.author:
        clauses.append(pr.author_key == f.author)
    if f.target:
        clauses.append(pr.target_branch == f.target)
    touched = or_(*(and_(col >= since, col < now) for col in
                    (pr.created_at, pr.merged_at, pr.closed_at, pr.prod_deployed_at)))
    rows = (await session.execute(
        select(*(getattr(pr, c) for c in _PR_COLUMNS)).where(*clauses, touched)
        .order_by(pr.updated_on.desc().nulls_last()).limit(_ROW_CAP)
    )).mappings().all()
    prs = []
    for r in rows:
        d = dict(r)
        settings = cfg(d["provider"], d["repo"])
        if classify.is_ignored_actor(d.get("author_key"), d.get("author_name"),
                                     settings.ignored_authors):
            continue
        prs.append(d)

    dep = ProductivityDeployment
    dclauses = _scope(dep, workspace_id, f)
    if f.target and await _is_deployed_branch(session, workspace_id, f.target):
        dclauses.append(dep.branch == f.target)
    drows = (await session.execute(
        select(*(getattr(dep, c) for c in _DEPLOYMENT_COLUMNS))
        .where(*dclauses, dep.deployed_at >= since, dep.deployed_at < now)
        .order_by(dep.deployed_at.desc()).limit(_ROW_CAP)
    )).mappings().all()
    deployments = []
    for r in drows:
        d = dict(r)
        d["settle_days"] = cfg(d["provider"], d["repo"]).failure_window_days
        deployments.append(d)
    return prs, deployments, len(rows) >= _ROW_CAP or len(drows) >= _ROW_CAP


async def _is_deployed_branch(session: AsyncSession, workspace_id: str, branch: str) -> bool:
    """Whether a deployment was ever recorded against `branch`.

    Deployments are recorded only against production branches, while the target
    filter offers every branch pull requests aim at. Narrowing deployments by
    a branch they never land on would zero every delivery figure while lead
    time, which is read from pull requests, still showed numbers; so the
    filter narrows deployments only when it names a branch they do land on.
    """
    dep = ProductivityDeployment
    found = (await session.execute(
        select(dep.id).where(dep.workspace_id == workspace_id, dep.branch == branch).limit(1)
    )).first()
    return found is not None


async def _covered_from(session: AsyncSession, workspace_id: str, f: Filters) -> datetime | None:
    """The date from which every repository in scope has been synced, or None when unknown.

    It is the LATEST backfill start among them: a period that begins before it
    is missing at least one repository's history.
    """
    st = ProductivitySyncState
    clauses = [st.workspace_id == workspace_id]
    if f.repos:
        clauses.append(func.lower(st.repo).in_(f.repos))
    rows = (await session.execute(
        select(st.backfill_from).where(*clauses)
    )).scalars().all()
    if not rows:
        return None
    if any(r is None for r in rows):
        return None  # a repository whose first sync has not started says nothing
    return max(r if r.tzinfo else r.replace(tzinfo=UTC) for r in rows)


def _outcome_column():
    """`review_issues.close_outcome`, or None in a build whose ledger has not got it."""
    return getattr(ReviewIssue, "close_outcome", None)


async def _load_implementation(
    session: AsyncSession, workspace_id: str, f: Filters, since: datetime, now: datetime,
) -> list[dict[str, Any]] | None:
    """Review issues with their frozen outcome, or None while the ledger has no such column.

    The outcome (`close_outcome`) and the rate over it belong to the issues
    backlog; a build without them has nothing to report, and says so.
    """
    outcome = _outcome_column()
    if outcome is None or metrics.implementation_stats_or_none() is None:
        return None
    clauses = [ReviewIssue.workspace_id == workspace_id,
               ReviewIssue.first_seen_at >= since, ReviewIssue.first_seen_at < now]
    if f.repos:
        clauses.append(func.lower(ReviewIssue.pr_repo).in_(f.repos))
    rows = (await session.execute(
        select(outcome.label("close_outcome"), ReviewIssue.first_seen_at).where(*clauses)
        .limit(_ROW_CAP)
    )).mappings().all()
    return [dict(r) for r in rows]


# ─── the read endpoints ──────────────────────────────────────────────

@router.get("/overview")
async def productivity_overview(
    days: int = Query(default=metrics.DEFAULT_WINDOW),
    bucket: str = Query(default="week"),
    repo: list[str] | None = Query(default=None),
    group: str | None = Query(default=None),
    author: str | None = Query(default=None),
    target: str | None = Query(default=None),
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(require_workspace_admin),
    ws: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    _window(days)
    if bucket not in metrics.BUCKETS:
        raise HTTPException(status_code=422, detail=f"bucket must be one of {', '.join(metrics.BUCKETS)}")
    f = await _filters(ws, repo, group, author, target)
    now = datetime.now(UTC)
    start = now - timedelta(days=days)
    previous = start - timedelta(days=days)
    prs, deployments, truncated = await _load(session, ws, f, previous, now)
    ledger = await _load_implementation(session, ws, f, previous, now)
    implementation = (
        metrics.implementation_figures(ledger, start, now, bucket, previous_start=previous)
        if ledger is not None else {"available": False}
    )
    out = metrics.overview(prs, deployments, days=days, bucket=bucket, now=now,
                           implementation=implementation,
                           covered_from=await _covered_from(session, ws, f))
    out["truncated"] = truncated
    # The review ledger knows repositories, not people or branches.
    out["implementation_scope"] = "repository"
    return out


@router.get("/prs")
async def productivity_prs(
    days: int = Query(default=metrics.DEFAULT_WINDOW),
    sort: str = Query(default="cycle"),
    limit: int = Query(default=50, ge=1, le=200),
    repo: list[str] | None = Query(default=None),
    group: str | None = Query(default=None),
    author: str | None = Query(default=None),
    target: str | None = Query(default=None),
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(require_workspace_admin),
    ws: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    _window(days)
    if sort not in metrics.SORTS:
        raise HTTPException(status_code=422, detail=f"sort must be one of {', '.join(metrics.SORTS)}")
    f = await _filters(ws, repo, group, author, target)
    now = datetime.now(UTC)
    prs, _deployments, truncated = await _load(session, ws, f, now - timedelta(days=days), now)
    return {"days": days, "sort": sort, "truncated": truncated,
            "prs": metrics.pr_rows(prs, days=days, sort=sort, limit=limit, now=now)}


@router.get("/developers")
async def productivity_developers(
    days: int = Query(default=metrics.DEFAULT_WINDOW),
    repo: list[str] | None = Query(default=None),
    group: str | None = Query(default=None),
    target: str | None = Query(default=None),
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(require_workspace_admin),
    ws: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    _window(days)
    f = await _filters(ws, repo, group, None, target)
    now = datetime.now(UTC)
    start = now - timedelta(days=days)
    prs, _deployments, truncated = await _load(session, ws, f, start, now)
    cfg = await _settings_by_repo(session, ws)

    pr = ProductivityPullRequest
    ev = ProductivityPrEvent
    clauses = [ev.workspace_id == ws, ev.at >= start, ev.at < now,
               ev.is_bot.is_(False), ev.is_author.is_(False)]
    if f.repos:
        clauses.append(func.lower(pr.repo).in_(f.repos))
    if f.target:
        clauses.append(pr.target_branch == f.target)
    rows = (await session.execute(
        select(ev.actor_key, ev.actor_name, ev.kind, ev.pr_id, ev.at, ev.is_bot, ev.is_author,
               pr.provider, pr.repo)
        .join(pr, pr.id == ev.pr_id).where(*clauses).limit(_ROW_CAP)
    )).mappings().all()
    events = [
        dict(r) for r in rows
        if not classify.is_ignored_actor(r["actor_key"], r["actor_name"],
                                         cfg(r["provider"], r["repo"]).ignored_authors)
    ]
    return {"days": days, "truncated": truncated or len(rows) >= _ROW_CAP,
            "min_sample": metrics.MIN_SAMPLE,
            "developers": metrics.developers(prs, events, days=days, now=now)}


@router.get("/filters")
async def productivity_filters(
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(require_workspace_admin),
    ws: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """What the filter bar offers: repositories with data, groups, authors, target branches."""
    pr = ProductivityPullRequest
    since = datetime.now(UTC) - timedelta(days=max(metrics.ALLOWED_WINDOWS) * 2)
    base = (pr.workspace_id == ws, pr.created_at >= since)
    repos = (await session.execute(
        select(pr.provider, pr.repo).where(*base).distinct().order_by(pr.repo).limit(500)
    )).all()
    # Per repository, because each one has its own ignored-authors list: an
    # account ignored everywhere is not offered (picking it would show nothing,
    # as `_load` drops its PRs), one ignored in some repositories still is.
    cfg = await _settings_by_repo(session, ws)
    author_rows = (await session.execute(
        select(pr.author_key, func.max(pr.author_name), pr.provider, pr.repo)
        .where(*base, pr.author_key.is_not(None))
        .group_by(pr.author_key, pr.provider, pr.repo).limit(_ROW_CAP)
    )).all()
    offered: dict[str, str | None] = {}
    for key, name, provider, repo_name in author_rows:
        if classify.is_ignored_actor(key, name, cfg(provider, repo_name).ignored_authors):
            continue
        if offered.get(key) is None:
            offered[key] = name
    authors = sorted(offered.items(), key=lambda kv: ((kv[1] or kv[0]).lower(), kv[0]))[:500]
    targets = (await session.execute(
        select(pr.target_branch).where(*base, pr.target_branch.is_not(None))
        .distinct().order_by(pr.target_branch).limit(200)
    )).scalars().all()

    def _group_names() -> list[str]:
        from src.groups.manager import get_group_manager

        return sorted({g.name for _p, g in get_group_manager().iter_groups(ws)})

    try:
        groups = await asyncio.to_thread(_group_names)
    except Exception as exc:  # noqa: BLE001 — a broken group file must not blank the filter bar
        logger.warning("productivity_groups_unreadable err=%s", exc)
        groups = []
    return {
        "repos": [{"provider": p, "repo": r} for p, r in repos],
        "groups": groups,
        "authors": [{"key": k, "name": n or k} for k, n in authors],
        "targets": list(targets),
    }


# ─── sync status and control ─────────────────────────────────────────

async def _known_repo(ws: str, provider: str, repo: str) -> dict[str, Any]:
    """The workspace's own repository, or 404."""
    for r in await asyncio.to_thread(_workspace_repos, ws):
        if r["provider"] == provider and r["repo"] == repo:
            return r
    raise HTTPException(status_code=404, detail="Unknown repository for this workspace")


@router.get("/sync")
async def productivity_sync(
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(require_workspace_admin),
    ws: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Every connected repository with its settings state and sync progress."""
    from src.productivity.sync import sync_status

    cfg = await _settings_by_repo(session, ws)
    repos = await asyncio.to_thread(_workspace_repos, ws)
    status = {(s["provider"], s["repo"]): s for s in await asyncio.to_thread(sync_status, ws)}
    items = []
    seen = set()
    for r in repos:
        key = (r["provider"], r["repo"])
        seen.add(key)
        resolved = cfg(*key)
        items.append({"provider": r["provider"], "repo": r["repo"],
                      "enabled": resolved.enabled, "backfill_days": resolved.backfill_days,
                      "status": status.get(key)})
    for key, st in status.items():  # data from a repository no longer connected
        if key not in seen:
            items.append({"provider": key[0], "repo": key[1], "enabled": False,
                          "backfill_days": cfg(*key).backfill_days, "status": st})
    now = datetime.now(UTC)
    limited = [
        i["status"]["rate_limited_until"] for i in items
        if i["status"] and i["status"]["rate_limited_until"]
        and datetime.fromisoformat(i["status"]["rate_limited_until"]) > now
    ]
    return {
        "repos": items,
        "any_enabled": any(i["enabled"] for i in items),
        "backfilling": any(i["enabled"] and (not i["status"] or not i["status"]["backfill_done"])
                           for i in items),
        "rate_limited_until": max(limited) if limited else None,
    }


class SyncRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=40)
    repo: str = Field(min_length=1, max_length=300)
    #: Forget the watermark and read the whole backfill window again.
    full: bool = False


@router.post("/sync/run")
async def productivity_sync_run(
    body: SyncRunIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_workspace_admin),
    ws: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Queue a sync now. A full re-read costs API calls, so it asks for no more than the page does."""
    from src.productivity.sync import enqueue_sync

    known = await _known_repo(ws, body.provider, body.repo)
    if not (await _settings_by_repo(session, ws))(body.provider, body.repo).enabled:
        raise HTTPException(status_code=409, detail="Productivity sync is not enabled for this repository")
    job = await asyncio.to_thread(
        enqueue_sync, ws, body.provider, body.repo, user_id=known["user_id"],
        repo_slug=known["repo_slug"], full=body.full)
    return {"queued": bool(job), "job_id": job,
            "detail": None if job else "A sync of this repository is already queued or running"}


@router.get("/sync/estimate")
async def productivity_sync_estimate(
    provider: str = Query(min_length=1, max_length=40),
    repo: str = Query(min_length=1, max_length=300),
    backfill_days: int | None = Query(default=None, ge=1, le=settings_mod.BACKFILL_DAYS_MAX),
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(require_workspace_admin),
    ws: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """How many requests, and how long, reading `backfill_days` of history would take.

    One cheap call to the provider (a page of one, for its total). Asked
    before switching a repository on, so a 180-day backfill is a choice.
    """
    known = await _known_repo(ws, provider, repo)
    resolved = (await _settings_by_repo(session, ws))(provider, repo)
    days = backfill_days or resolved.backfill_days
    try:
        return {"days": days, "rate_per_hour": resolved.rate_per_hour,
                **await asyncio.to_thread(_estimate, ws, known, days, resolved.rate_per_hour)}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        from src.productivity.providers.base import ProviderError
        from src.productivity.ratelimit import RateLimited

        if isinstance(exc, RateLimited):
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        if isinstance(exc, ProviderError):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        logger.warning("productivity_estimate_failed repo=%s err=%s", repo, type(exc).__name__)
        raise HTTPException(status_code=502, detail="The provider could not be asked") from exc


def _estimate(ws: str, known: dict[str, Any], days: int, rate: int) -> dict[str, Any]:
    from src.productivity.sync import build_provider, estimate_backfill

    provider = build_provider(ws, known["provider"], known["repo"], user_id=known["user_id"],
                              rate_per_hour=rate)
    return estimate_backfill(provider, days, rate_per_hour=rate)


# ─── settings ────────────────────────────────────────────────────────

@router.get("/settings")
async def productivity_settings(
    provider: str | None = Query(default=None, max_length=40),
    repo: str | None = Query(default=None, max_length=300),
    _user: User = Depends(require_workspace_admin),
    ws: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """The layers of one scope: the workspace default (no `repo`) or a repository.

    `workspace` and `repo` hold only the values SET at that layer, `resolved`
    what the repository runs with, `builtin` the defaults — so the page can
    say where each value comes from.
    """
    provider, repo = await _scope_of(ws, provider, repo)
    out = await asyncio.to_thread(settings_mod.load_layers, ws, provider, repo)
    out["limits"] = {
        "backfill_days_max": settings_mod.BACKFILL_DAYS_MAX,
        "rate_per_hour": [settings_mod.RATE_PER_HOUR_MIN, settings_mod.RATE_PER_HOUR_MAX],
        "deploy_sources": list(settings_mod.DEPLOY_SOURCES),
    }
    return out


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str | None = Field(default=None, max_length=40)
    repo: str | None = Field(default=None, max_length=300)
    #: name -> value; null clears the field back to inheriting.
    changes: dict[str, Any] = Field(default_factory=dict)


@router.put("/settings")
async def productivity_save_settings(
    body: SettingsIn,
    _user: User = Depends(require_workspace_admin),
    ws: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Save values into one layer. Owner and admin. Switching a repository on queues its first sync."""
    from src.productivity.sync import enqueue_sync

    unknown = sorted(set(body.changes) - set(settings_mod.SETTING_NAMES))
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown setting: {', '.join(unknown)}")
    provider, repo = await _scope_of(ws, body.provider, body.repo)
    try:
        await asyncio.to_thread(settings_mod.save, ws, provider, repo, body.changes)
    except settings_mod.SettingsError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    queued = 0
    if body.changes.get("enabled") is True:
        for r in await asyncio.to_thread(_workspace_repos, ws):
            if provider and (r["provider"], r["repo"]) != (provider, repo):
                continue
            try:
                resolved = await asyncio.to_thread(settings_mod.load, ws, r["provider"], r["repo"])
                if resolved.enabled and await asyncio.to_thread(
                    enqueue_sync, ws, r["provider"], r["repo"], user_id=r["user_id"],
                    repo_slug=r["repo_slug"],
                ):
                    queued += 1
            except Exception as exc:  # noqa: BLE001 — the setting is saved; the tick will pick it up
                logger.warning("productivity_first_sync_not_queued repo=%s err=%s", r["repo"], exc)
    out = await asyncio.to_thread(settings_mod.load_layers, ws, provider, repo)
    out["queued"] = queued
    return out


async def _scope_of(ws: str, provider: str | None, repo: str | None) -> tuple[str, str]:
    """('', '') for the workspace default; otherwise a repository of THIS workspace."""
    if not provider and not repo:
        return "", ""
    if not provider or not repo:
        raise HTTPException(status_code=422, detail="Give both provider and repo, or neither")
    await _known_repo(ws, provider, repo)
    return provider, repo
