"""Users and their workspace roles — the superadmin's page.

    GET    /api/admin/users?q=                              — search accounts
    GET    /api/admin/workspaces                            — every workspace (for the picker)
    GET    /api/admin/users/{user_id}/memberships           — one person's workspaces + roles
    PUT    /api/admin/users/{user_id}/memberships/{ws_id}   — add / change {role}
    DELETE /api/admin/users/{user_id}/memberships/{ws_id}   — remove

One person can be admin of several workspaces and editor of several others —
memberships were always per workspace; what was missing was a place that shows
them per PERSON. Superadmin only (`require_superadmin`), and every write goes
through `change_membership`, the same rule and audit row as PUT
/api/workspaces/{id}/members — this page has no rule of its own to drift.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import client_ip, get_users, require_superadmin
from src.api.memberships import change_membership
from src.api.routers.users import PLATFORM_USER_IDS
from src.db.models import Workspace, WorkspaceMember
from src.db.session import get_async_session
from src.users import User, UserStore

router = APIRouter(prefix="/api/admin", tags=["admin-users"])

_MAX_RESULTS = 200


class AdminUserOut(BaseModel):
    id: str
    email: str
    name: str
    is_admin: bool
    is_active: bool
    memberships: int


class AdminWorkspaceOut(BaseModel):
    id: str
    name: str
    slug: str


class MembershipOut(BaseModel):
    workspace_id: str
    workspace_name: str
    workspace_slug: str
    role: str


class MembershipIn(BaseModel):
    role: str = Field(min_length=1, max_length=32)
    model_config = ConfigDict(extra="forbid")


def _target(users: UserStore, user_id: str) -> User:
    if user_id in PLATFORM_USER_IDS:
        raise HTTPException(status_code=404, detail="User not found")
    u = users.get_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="User not found")
    return u


@router.get("/users", response_model=list[AdminUserOut])
async def search_users(
    q: str = Query(default="", max_length=200),
    session: AsyncSession = Depends(get_async_session),
    _su: User = Depends(require_superadmin),
    users: UserStore = Depends(get_users),
) -> list[AdminUserOut]:
    needle = q.strip().lower()
    rows = [
        u for u in users.list(active_only=False)
        if u.id not in PLATFORM_USER_IDS
        and (not needle or needle in u.email.lower() or needle in (u.name or "").lower())
    ]
    rows.sort(key=lambda u: u.email.lower())
    rows = rows[:_MAX_RESULTS]
    counts: dict[str, int] = {}
    if rows:
        for m in (await session.scalars(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id.in_([u.id for u in rows]))
        )).all():
            counts[m.user_id] = counts.get(m.user_id, 0) + 1
    return [
        AdminUserOut(
            id=u.id, email=u.email, name=u.name or "", is_admin=u.is_admin,
            is_active=u.is_active, memberships=counts.get(u.id, 0),
        )
        for u in rows
    ]


@router.get("/workspaces", response_model=list[AdminWorkspaceOut])
async def all_workspaces(
    session: AsyncSession = Depends(get_async_session),
    _su: User = Depends(require_superadmin),
) -> list[AdminWorkspaceOut]:
    rows = (await session.scalars(select(Workspace).order_by(Workspace.name))).all()
    return [AdminWorkspaceOut(id=w.id, name=w.name, slug=w.slug) for w in rows]


async def _memberships(session: AsyncSession, user_id: str) -> list[MembershipOut]:
    rows = (await session.execute(
        select(WorkspaceMember, Workspace)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .where(WorkspaceMember.user_id == user_id)
        .order_by(Workspace.name)
    )).all()
    return [
        MembershipOut(workspace_id=w.id, workspace_name=w.name,
                      workspace_slug=w.slug, role=m.role)
        for m, w in rows
    ]


@router.get("/users/{user_id}/memberships", response_model=list[MembershipOut])
async def user_memberships(
    user_id: str,
    session: AsyncSession = Depends(get_async_session),
    _su: User = Depends(require_superadmin),
    users: UserStore = Depends(get_users),
) -> list[MembershipOut]:
    _target(users, user_id)
    return await _memberships(session, user_id)


@router.put("/users/{user_id}/memberships/{ws_id}", response_model=list[MembershipOut])
async def set_membership(
    user_id: str,
    ws_id: str,
    payload: MembershipIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    su: User = Depends(require_superadmin),
    users: UserStore = Depends(get_users),
) -> list[MembershipOut]:
    target = _target(users, user_id)
    if not target.is_active:
        raise HTTPException(status_code=409, detail="Account is deactivated")
    await change_membership(
        session, actor=su, ws_id=ws_id, user_id=user_id,
        new_role=payload.role, via="admin_users", ip=client_ip(request),
    )
    return await _memberships(session, user_id)


@router.delete("/users/{user_id}/memberships/{ws_id}", response_model=list[MembershipOut])
async def remove_membership(
    user_id: str,
    ws_id: str,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    su: User = Depends(require_superadmin),
    users: UserStore = Depends(get_users),
) -> list[MembershipOut]:
    _target(users, user_id)
    await change_membership(
        session, actor=su, ws_id=ws_id, user_id=user_id,
        new_role=None, via="admin_users", ip=client_ip(request),
    )
    return await _memberships(session, user_id)


__all__ = ["router"]
