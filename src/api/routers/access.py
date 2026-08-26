"""Fine-grained research-access rules (Stage 22).

Governs what a team may *learn* about a repo through Q&A / graph / vector
search — down to individual paths. The same rules are enforced across UI, REST
and MCP (see ``src/access/resolver.py``).

Endpoints:
    GET    /api/access/rules[?repo_slug=&team_id=]   — list rules (auth)
    PUT    /api/access/rules                         — upsert rule (admin)
    DELETE /api/access/rules/{rule_id}               — delete rule (admin)
    GET    /api/access/my[?repo_slug=]               — caller's effective access
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.access import resolve_access
from src.api.deps import (
    current_workspace_id,
    get_current_user,
    require_workspace_admin,
)
from src.db.models import RepoAccessRule, Team
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/access", tags=["access"])

_VALID_VISIBILITY = {"none", "metadata", "code"}


class AccessRuleIn(BaseModel):
    team_id: str = Field(min_length=1)
    repo_slug: str = Field(min_length=1, max_length=300)
    visibility: str = Field(default="code")
    allow_globs: list[str] = Field(default_factory=list, max_length=100)
    deny_globs: list[str] = Field(default_factory=list, max_length=100)
    sensitivity_tags: list[str] = Field(default_factory=list, max_length=50)
    note: str = Field(default="", max_length=2000)
    model_config = ConfigDict(extra="forbid")


class AccessRuleOut(BaseModel):
    id: str
    workspace_id: str
    team_id: str
    team_name: str | None
    repo_slug: str
    visibility: str
    allow_globs: list[str]
    deny_globs: list[str]
    sensitivity_tags: list[str]
    note: str


class MyAccessItem(BaseModel):
    repo_slug: str
    visibility: str
    researchable: bool
    code_visible: bool
    open_default: bool
    allow_globs: list[str]
    deny_globs: list[str]
    sensitivity_tags: list[str]


# ─── Rules CRUD ───────────────────────────────────────────────────────


def _to_out(rule: RepoAccessRule, team_name: str | None) -> AccessRuleOut:
    return AccessRuleOut(
        id=rule.id,
        workspace_id=rule.workspace_id,
        team_id=rule.team_id,
        team_name=team_name,
        repo_slug=rule.repo_slug,
        visibility=rule.visibility,
        allow_globs=list(rule.allow_globs or []),
        deny_globs=list(rule.deny_globs or []),
        sensitivity_tags=list(rule.sensitivity_tags or []),
        note=rule.note or "",
    )


@router.get("/rules", response_model=list[AccessRuleOut])
async def list_rules(
    repo_slug: str | None = None,
    team_id: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> list[AccessRuleOut]:
    stmt = select(RepoAccessRule).where(RepoAccessRule.workspace_id == ws_id)
    if repo_slug:
        stmt = stmt.where(RepoAccessRule.repo_slug == repo_slug)
    if team_id:
        stmt = stmt.where(RepoAccessRule.team_id == team_id)
    rules = (await session.scalars(stmt.order_by(RepoAccessRule.repo_slug))).all()
    # team-name lookup for display
    team_ids = {r.team_id for r in rules}
    names: dict[str, str] = {}
    if team_ids:
        for t in (await session.scalars(
            select(Team).where(Team.id.in_(team_ids))
        )).all():
            names[t.id] = t.name
    return [_to_out(r, names.get(r.team_id)) for r in rules]


@router.put("/rules", response_model=AccessRuleOut)
async def upsert_rule(
    payload: AccessRuleIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_workspace_admin),
    ws_id: str = Depends(current_workspace_id),
) -> AccessRuleOut:
    if payload.visibility not in _VALID_VISIBILITY:
        raise HTTPException(
            status_code=422,
            detail=f"visibility must be one of {sorted(_VALID_VISIBILITY)}",
        )
    team = await session.get(Team, payload.team_id)
    if team is None or team.workspace_id != ws_id:
        raise HTTPException(
            status_code=404, detail="team not found in this workspace",
        )
    existing = (await session.scalars(
        select(RepoAccessRule).where(
            RepoAccessRule.workspace_id == ws_id,
            RepoAccessRule.team_id == payload.team_id,
            RepoAccessRule.repo_slug == payload.repo_slug,
        )
    )).first()
    if existing is None:
        existing = RepoAccessRule(
            id=str(uuid.uuid4()),
            workspace_id=ws_id,
            team_id=payload.team_id,
            repo_slug=payload.repo_slug,
            created_by=user.id,
        )
        session.add(existing)
    existing.visibility = payload.visibility
    existing.allow_globs = payload.allow_globs
    existing.deny_globs = payload.deny_globs
    existing.sensitivity_tags = payload.sensitivity_tags
    existing.note = payload.note
    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        raise HTTPException(status_code=400, detail=f"upsert failed: {exc}") from exc
    await session.refresh(existing)
    logger.info(
        "access_rule_upsert repo=%s team=%s vis=%s deny=%d by=%s",
        payload.repo_slug, payload.team_id, payload.visibility,
        len(payload.deny_globs), user.email,
    )
    return _to_out(existing, team.name)


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_workspace_admin),
    ws_id: str = Depends(current_workspace_id),
) -> None:
    row = await session.get(RepoAccessRule, rule_id)
    if row is None or row.workspace_id != ws_id:
        return
    await session.delete(row)
    await session.commit()
    logger.info("access_rule_deleted id=%s by=%s", rule_id, user.email)


# ─── Effective access for the caller (parity: UI + agents) ────────────


@router.get("/my", response_model=list[MyAccessItem])
async def my_access(
    repo_slug: str | None = None,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> list[MyAccessItem]:
    """Caller's effective research access. With ``repo_slug`` → that repo;
    otherwise every repo that has a rule configured in the workspace."""
    if repo_slug:
        repos = [repo_slug]
    else:
        repos = list((await session.scalars(
            select(RepoAccessRule.repo_slug)
            .where(RepoAccessRule.workspace_id == ws_id)
            .distinct()
        )).all())
    if not repos:
        return []
    access = resolve_access(
        user_id=user.id, is_admin=user.is_admin,
        workspace_id=ws_id, repos=repos,
    )
    return [
        MyAccessItem(**access[r].to_dict())  # to_dict keys match the schema
        for r in repos
    ]
