"""Review settings overview — the workspace layer and every repository, at once.

Endpoint (the caller's ACTIVE workspace only):
    GET /api/review-settings/overview — members read

What a Kodus-style settings screen opens with: the workspace defaults (how
many settings they set) and a row per repository the caller may read, with
"Overridden N" — how many workspace-defaultable settings that repository's
policy overrides — and how its most recent review went.

Cheap by construction: one read of the defaults row, one of the workspace's
policy rows, one grouped read of the reviewed-PR rows, and the repository
grant check per repository (the rule /api/review-policies/overrides-summary
applies). The per-field detail stays on /api/review-defaults and
/api/review-policies/{slug}; nothing here is a second copy of it — the
override count is `review_defaults.overridden_fields`, the function the
per-field counts on /api/review-defaults use.

Scoping, twice, as for the review-policy routes: to the ACTIVE workspace
(another tenant's rows are never read — every query filters on it), and to
the repositories the caller's team grants let them read. A caller who is not
a member of the workspace (only reachable for the shared single-tenant
default) gets 403.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import current_workspace_id, get_current_user, is_workspace_admin
from src.api.schemas import (
    ReviewSettingsOverview,
    ReviewSettingsRepoSummary,
    ReviewSettingsWorkspaceSummary,
)
from src.db.models import RepoReviewPolicy, ReviewPullRequest, WorkspaceReviewDefaults
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/review-settings", tags=["review-settings"])


async def _last_reviews(
    session: AsyncSession, ws_id: str,
) -> dict[str, tuple[str | None, datetime | None]]:
    """repo key → (status, time) of its most recently reviewed PR.

    Keyed by BOTH spellings a PR row carries (`repo_slug`, the local slug,
    and `repo`, the provider's owner/name) so a repository registered either
    way finds its row. `updated_at` of a reviewed PR row is the time its last
    review was recorded (a close/merge webhook may move it later too — the
    status is still the last review's). Optional: an unreadable table costs
    the two columns, not the page.
    """
    reviewed = and_(ReviewPullRequest.workspace_id == ws_id,
                    ReviewPullRequest.reviews_count > 0)
    latest = (
        select(ReviewPullRequest.repo, func.max(ReviewPullRequest.updated_at).label("at"))
        .where(reviewed)
        .group_by(ReviewPullRequest.repo)
        .subquery()
    )
    try:
        rows = (await session.execute(
            select(ReviewPullRequest.repo, ReviewPullRequest.repo_slug,
                   ReviewPullRequest.last_review_status, ReviewPullRequest.updated_at)
            .join(latest, and_(ReviewPullRequest.repo == latest.c.repo,
                               ReviewPullRequest.updated_at == latest.c.at))
            .where(reviewed)
        )).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("review_settings_last_reviews_unavailable ws=%s err=%s", ws_id, exc)
        await session.rollback()
        return {}
    out: dict[str, tuple[str | None, datetime | None]] = {}
    for repo, slug, status, at in rows:
        for key in {repo, slug} - {None, ""}:
            prev = out.get(key)
            if prev is None or (at and (prev[1] is None or at > prev[1])):
                out[key] = (status, at)
    return out


@router.get("/overview", response_model=ReviewSettingsOverview)
async def review_settings_overview(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> ReviewSettingsOverview:
    """The active workspace's defaults and every readable repository: what
    each overrides and how its last review went."""
    from src.api.auto_review import get_auto_review_store
    from src.api.deps import enforce_repo_permission
    from src.api.routers.review_defaults import _require_member, overridden_fields
    from src.review.review_defaults import INHERITABLE_FIELDS, defaults_from_row

    await _require_member(user, ws_id)

    defaults_row = None
    try:
        defaults_row = await session.get(WorkspaceReviewDefaults, ws_id)
    except Exception as exc:  # noqa: BLE001 — a database behind the migration
        logger.warning("review_settings_defaults_unavailable ws=%s err=%s", ws_id, exc)
        await session.rollback()
    defaults = defaults_from_row(defaults_row) or {}
    set_fields = [n for n in INHERITABLE_FIELDS if defaults.get(n) is not None]

    policies: dict[str, Any] = {
        row.repo_slug: row for row in (await session.scalars(
            select(RepoReviewPolicy).where(RepoReviewPolicy.workspace_id == ws_id)
        )).all()
    }
    last = await _last_reviews(session, ws_id)

    configs = await asyncio.to_thread(
        get_auto_review_store().list_for_workspace, ws_id)
    repos: list[ReviewSettingsRepoSummary] = []
    seen: set[str] = set()
    for cfg in configs:
        # Rows are keyed (user, slug): one repository registered by two
        # members is one repository here.
        if cfg.repo_slug in seen:
            continue
        seen.add(cfg.repo_slug)
        try:
            await enforce_repo_permission(cfg.repo_slug, user, "read", ws_id)
        except HTTPException:
            continue
        policy = policies.get(cfg.repo_slug) or policies.get(cfg.full_name)
        fields = overridden_fields(policy) if policy is not None else []
        status, at = last.get(cfg.repo_slug) or last.get(cfg.full_name) or (None, None)
        repos.append(ReviewSettingsRepoSummary(
            repo_slug=cfg.repo_slug,
            full_name=cfg.full_name,
            provider=cfg.provider,
            has_policy=policy is not None,
            review_enabled=bool(policy.enabled) if policy is not None else True,
            overridden_count=len(fields),
            overridden_fields=fields,
            last_review_status=status,
            last_review_at=at,
        ))

    return ReviewSettingsOverview(
        workspace=ReviewSettingsWorkspaceSummary(
            workspace_id=ws_id,
            set_count=len(set_fields),
            set_fields=set_fields,
            can_edit=bool(await asyncio.to_thread(is_workspace_admin, user, ws_id)),
            updated_by=getattr(defaults_row, "updated_by", None),
            updated_at=getattr(defaults_row, "updated_at", None),
        ),
        repositories=repos,
    )
