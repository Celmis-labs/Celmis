"""Teams + RBAC (Stage 14).

Endpoints:
    GET    /api/teams                          — list
    POST   /api/teams                          — create (admin)
    DELETE /api/teams/{id}                     — delete (admin)
    GET    /api/teams/{id}/members             — list members
    PUT    /api/teams/{id}/members/{user_id}   — add/update role (admin)
    DELETE /api/teams/{id}/members/{user_id}   — remove (admin)
    GET    /api/teams/{id}/repos               — repos this team can access
    PUT    /api/teams/{id}/repos/{slug}        — grant access (admin)
    DELETE /api/teams/{id}/repos/{slug}        — revoke (admin)
    GET    /api/teams/me                       — my teams + effective permissions

The permission model is intentionally minimal: a user's repo permission is
the *highest* level granted through any team they're in. `is_admin=True`
on the user itself always wins.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import (
    current_workspace_id,
    get_current_user,
    require_workspace_admin,
)
from src.db.models import RepoTeamAccess, Team, TeamMember
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/teams", tags=["teams"])

_VALID_ROLES = {"owner", "admin", "reviewer", "member", "viewer"}
_VALID_PERMS = {"admin", "review", "read"}
_PERM_RANK = {"read": 1, "review": 2, "admin": 3}


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    model_config = ConfigDict(extra="forbid")


class TeamOut(BaseModel):
    id: str
    name: str
    description: str
    member_count: int


class MemberIn(BaseModel):
    role: str = Field(default="member")
    model_config = ConfigDict(extra="forbid")


class MemberOut(BaseModel):
    user_id: str
    role: str


class RepoAccessIn(BaseModel):
    permission: str = Field(default="review")
    model_config = ConfigDict(extra="forbid")


class RepoAccessOut(BaseModel):
    repo_slug: str
    permission: str


class MyTeamsOut(BaseModel):
    teams: list[TeamOut]
    repo_permissions: dict[str, str]   # {repo_slug: highest_perm}


# ─── Teams ────────────────────────────────────────────────────────────


@router.get("", response_model=list[TeamOut])
async def list_teams(
    session: AsyncSession = Depends(get_async_session),
    _user: User = Depends(get_current_user),
    ws_id: str = Depends(current_workspace_id),
) -> list[TeamOut]:
    teams = (await session.scalars(
        select(Team).where(Team.workspace_id == ws_id).order_by(Team.name)
    )).all()
    out: list[TeamOut] = []
    for t in teams:
        cnt = len((await session.scalars(
            select(TeamMember).where(TeamMember.team_id == t.id)
        )).all())
        out.append(TeamOut(id=t.id, name=t.name, description=t.description,
                           member_count=cnt))
    return out


@router.post("", response_model=TeamOut, status_code=201)
async def create_team(
    payload: TeamIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_workspace_admin),
    ws_id: str = Depends(current_workspace_id),
) -> TeamOut:
    row = Team(id=str(uuid.uuid4()), name=payload.name,
               description=payload.description, workspace_id=ws_id)
    session.add(row)
    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        raise HTTPException(status_code=400, detail=f"create failed: {exc}") from exc
    logger.info("team_created id=%s name=%s by=%s", row.id, row.name, user.email)
    return TeamOut(id=row.id, name=row.name, description=row.description,
                   member_count=0)


@router.delete("/{team_id}", status_code=204)
async def delete_team(
    team_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_workspace_admin),
    ws_id: str = Depends(current_workspace_id),
) -> None:
    row = await _team_in_workspace(session, team_id, ws_id)
    await session.delete(row)
    await session.commit()
    logger.info("team_deleted id=%s by=%s", team_id, user.email)


async def _team_in_workspace(session: AsyncSession, team_id: str, ws_id: str) -> Team:
    """The team, if it belongs to the caller's workspace. 404 otherwise.

    EVERY ROUTE BELOW TAKES A `team_id` FROM THE PATH and, until now, none of
    them asked whose team it was. That was survivable only because they all
    required the INSTALLATION's admin — and two of them (`GET
    /{team_id}/members`, `GET /{team_id}/repos`) required nothing at all, so
    any authenticated user could read another tenant's team membership,
    including its user ids, with a plain 200. Reproduced against production.

    404 and not 403: a tenant asking about a team it does not own should not
    learn that the team exists. That is the same choice `_load_owned` makes for
    groups and the chat routes make for chats.
    """
    row = await session.get(Team, team_id)
    if row is None or row.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="team not found")
    return row


# ─── Members ──────────────────────────────────────────────────────────


@router.get("/{team_id}/members", response_model=list[MemberOut])
async def list_members(
    team_id: str,
    session: AsyncSession = Depends(get_async_session),
    ws_id: str = Depends(current_workspace_id),
    _user: User = Depends(get_current_user),
) -> list[MemberOut]:
    await _team_in_workspace(session, team_id, ws_id)
    rows = (await session.scalars(
        select(TeamMember).where(TeamMember.team_id == team_id)
    )).all()
    return [MemberOut(user_id=r.user_id, role=r.role) for r in rows]


@router.put("/{team_id}/members/{user_id}", response_model=MemberOut)
async def upsert_member(
    team_id: str,
    user_id: str,
    payload: MemberIn,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_workspace_admin),
    ws_id: str = Depends(current_workspace_id),
) -> MemberOut:
    if payload.role not in _VALID_ROLES:
        raise HTTPException(status_code=400,
                            detail=f"role must be one of {_VALID_ROLES}")
    await _team_in_workspace(session, team_id, ws_id)
    row = await session.get(TeamMember, (team_id, user_id))
    if row is None:
        row = TeamMember(team_id=team_id, user_id=user_id, role=payload.role)
        session.add(row)
    else:
        row.role = payload.role
    await session.commit()
    logger.info("team_member_upserted team=%s user=%s role=%s by=%s",
                team_id, user_id, payload.role, admin.email)
    return MemberOut(user_id=row.user_id, role=row.role)


@router.delete("/{team_id}/members/{user_id}", status_code=204)
async def remove_member(
    team_id: str,
    user_id: str,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_workspace_admin),
    ws_id: str = Depends(current_workspace_id),
) -> None:
    await _team_in_workspace(session, team_id, ws_id)
    row = await session.get(TeamMember, (team_id, user_id))
    if row is None:
        return
    await session.delete(row)
    await session.commit()
    logger.info("team_member_removed team=%s user=%s by=%s",
                team_id, user_id, admin.email)


# ─── Repo access ──────────────────────────────────────────────────────


@router.get("/{team_id}/repos", response_model=list[RepoAccessOut])
async def list_team_repos(
    team_id: str,
    session: AsyncSession = Depends(get_async_session),
    ws_id: str = Depends(current_workspace_id),
    _user: User = Depends(get_current_user),
) -> list[RepoAccessOut]:
    await _team_in_workspace(session, team_id, ws_id)
    rows = (await session.scalars(
        select(RepoTeamAccess).where(RepoTeamAccess.team_id == team_id)
    )).all()
    return [RepoAccessOut(repo_slug=r.repo_slug, permission=r.permission) for r in rows]


@router.put("/{team_id}/repos/{repo_slug:path}", response_model=RepoAccessOut)
async def grant_repo(
    team_id: str,
    repo_slug: str,
    payload: RepoAccessIn,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_workspace_admin),
    ws_id: str = Depends(current_workspace_id),
) -> RepoAccessOut:
    if payload.permission not in _VALID_PERMS:
        raise HTTPException(status_code=400,
                            detail=f"permission must be one of {_VALID_PERMS}")
    await _team_in_workspace(session, team_id, ws_id)
    row = await session.get(RepoTeamAccess, (repo_slug, team_id))
    if row is None:
        row = RepoTeamAccess(
            repo_slug=repo_slug, team_id=team_id, permission=payload.permission,
        )
        session.add(row)
    else:
        row.permission = payload.permission
    await session.commit()
    logger.info("repo_access_granted repo=%s team=%s perm=%s by=%s",
                repo_slug, team_id, payload.permission, admin.email)
    return RepoAccessOut(repo_slug=row.repo_slug, permission=row.permission)


@router.delete("/{team_id}/repos/{repo_slug:path}", status_code=204)
async def revoke_repo(
    team_id: str,
    repo_slug: str,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_workspace_admin),
    ws_id: str = Depends(current_workspace_id),
) -> None:
    await _team_in_workspace(session, team_id, ws_id)
    await session.execute(sql_delete(RepoTeamAccess).where(
        RepoTeamAccess.repo_slug == repo_slug,
        RepoTeamAccess.team_id == team_id,
    ))
    await session.commit()
    logger.info("repo_access_revoked repo=%s team=%s by=%s",
                repo_slug, team_id, admin.email)


# ─── Self-serve introspection ────────────────────────────────────────


@router.get("/me", response_model=MyTeamsOut)
async def my_teams(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> MyTeamsOut:
    """Return teams the caller is in + effective per-repo permission map."""
    my_memberships = (await session.scalars(
        select(TeamMember).where(TeamMember.user_id == user.id)
    )).all()
    team_ids = [m.team_id for m in my_memberships]
    teams: list[TeamOut] = []
    perms: dict[str, str] = {}
    if team_ids:
        for tid in team_ids:
            t = await session.get(Team, tid)
            if t is None:
                continue
            teams.append(TeamOut(
                id=t.id, name=t.name, description=t.description,
                member_count=0,
            ))
            grants = (await session.scalars(
                select(RepoTeamAccess).where(RepoTeamAccess.team_id == tid)
            )).all()
            for g in grants:
                existing = perms.get(g.repo_slug)
                if existing is None or _PERM_RANK[g.permission] > _PERM_RANK[existing]:
                    perms[g.repo_slug] = g.permission
    return MyTeamsOut(teams=teams, repo_permissions=perms)


__all__ = ["router"]
