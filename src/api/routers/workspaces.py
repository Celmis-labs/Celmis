"""Workspaces — multi-tenancy boundary (Stage 19).

Every logically-shared entity (projects, review policies, notification
channels, deprecations, teams) carries a `workspace_id`. A user can
belong to N workspaces via `workspace_members`. Their "active"
workspace comes from:

  1. Header `X-Workspace: <slug>` on the request, OR
  2. First membership by role rank (owner > admin > member > viewer).

Endpoints:
    GET    /api/workspaces               — my workspaces + active hint
    POST   /api/workspaces               — create a shared workspace (superadmin)
    DELETE /api/workspaces/{id}          — delete (owner or superadmin)
    GET    /api/workspaces/{id}/members
    PUT    /api/workspaces/{id}/members/{user_id}
    DELETE /api/workspaces/{id}/members/{user_id}

Who may write a membership is `can_change` in src/users/roles.py, applied by
src/api/memberships.py for every route that writes one: owner/admin/editor are
the superadmin's to grant, change and remove; an owner/admin of the workspace
manages members and viewers.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import client_ip, get_current_user, require_superadmin
from src.api.memberships import actor_role, change_membership, refuse_unless_can_change
from src.db.models import Workspace, WorkspaceMember
from src.db.session import get_async_session
from src.users import User
from src.users.roles import (
    VALID_WORKSPACE_ROLES,
    WORKSPACE_ADMIN_ROLES,
    WORKSPACE_ROLE_RANK,
    is_superadmin,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])

_VALID_ROLES = VALID_WORKSPACE_ROLES
_ROLE_RANK = WORKSPACE_ROLE_RANK
_SLUG_PAT = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = Field(default="", max_length=2000)
    model_config = ConfigDict(extra="forbid")


class WorkspaceOut(BaseModel):
    id: str
    name: str
    slug: str
    description: str
    role: str | None = None  # caller's role, set on GET /me


class MemberIn(BaseModel):
    role: str = Field(default="member")
    model_config = ConfigDict(extra="forbid")


class MemberOut(BaseModel):
    user_id: str
    role: str
    email: str = ""
    name: str = ""


class MyWorkspacesOut(BaseModel):
    workspaces: list[WorkspaceOut]
    active_id: str | None


# ─── Per-workspace RBAC helpers ─────────────────────────────────────


async def _require_ws_admin(session: AsyncSession, user: User, ws_id: str) -> None:
    """Caller must own/administer THIS workspace (path id), or be a global
    admin. Owners manage their own workspace without a platform admin."""
    if user.is_admin:
        return
    m = await session.get(WorkspaceMember, (ws_id, user.id))
    if m is None or m.role not in WORKSPACE_ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Requires owner/admin on this workspace")


async def _require_ws_member(session: AsyncSession, user: User, ws_id: str) -> None:
    """Caller must be a member of THIS workspace (or global admin) — otherwise
    listing its members would disclose another tenant's roster."""
    if user.is_admin:
        return
    if await session.get(WorkspaceMember, (ws_id, user.id)) is None:
        raise HTTPException(status_code=403, detail="Not a member of this workspace")


# ─── Active-workspace resolution ────────────────────────────────────


async def resolve_active_workspace(
    request_header: str | None,
    user: User,
    session: AsyncSession,
) -> str | None:
    """Header wins if present + user is a member. Otherwise fallback to
    best-rank membership. Returns workspace_id or None (unbound)."""
    if request_header:
        slug = request_header.strip().lower()
        ws = (await session.scalars(
            select(Workspace).where(Workspace.slug == slug)
        )).first()
        if ws:
            m = await session.get(WorkspaceMember, (ws.id, user.id))
            if m or user.is_admin:
                return ws.id
    memberships = (await session.scalars(
        select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)
    )).all()
    if not memberships:
        return None
    memberships.sort(key=lambda m: -_ROLE_RANK.get(m.role, 0))
    return memberships[0].workspace_id


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("", response_model=MyWorkspacesOut)
async def list_my_workspaces(
    request: Request,
    x_workspace: str | None = Header(default=None),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> MyWorkspacesOut:
    memberships = (await session.scalars(
        select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)
    )).all()
    role_by_ws = {m.workspace_id: m.role for m in memberships}
    if role_by_ws:
        ws_rows = (await session.scalars(
            select(Workspace).where(Workspace.id.in_(list(role_by_ws.keys())))
        )).all()
    elif user.is_admin:
        # Global admins see everything even without membership.
        ws_rows = (await session.scalars(select(Workspace))).all()
    else:
        ws_rows = []
    out = [
        WorkspaceOut(
            id=w.id, name=w.name, slug=w.slug,
            description=w.description, role=role_by_ws.get(w.id),
        )
        for w in sorted(ws_rows, key=lambda x: x.name)
    ]
    # The frontend switcher persists the active workspace as an `x-workspace`
    # cookie (SSE flows can't set headers), so honor the cookie as a fallback —
    # otherwise active_id ignores a just-switched workspace and the UI snaps
    # back to the best-rank membership.
    hint = x_workspace or request.cookies.get("x-workspace")
    active = await resolve_active_workspace(hint, user, session)
    return MyWorkspacesOut(workspaces=out, active_id=active)


@router.post("", response_model=WorkspaceOut, status_code=201)
async def create_workspace(
    payload: WorkspaceIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_superadmin),
) -> WorkspaceOut:
    # A SHARED workspace is the superadmin's to create: its creator becomes
    # its owner, and owner is a role only the superadmin hands out — letting
    # anybody create one was letting anybody grant themselves "owner".
    # Everybody still gets a personal workspace of their own, provisioned at
    # signup/login (src/api/workspace_provision.py), not through this route.
    if not _SLUG_PAT.match(payload.slug):
        raise HTTPException(status_code=400, detail="invalid slug")
    ws = Workspace(
        id=str(uuid.uuid4()), name=payload.name, slug=payload.slug,
        description=payload.description, created_by=user.email,
    )
    session.add(ws)
    # Creator becomes owner.
    session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role="owner"))
    try:
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        raise HTTPException(status_code=400, detail=f"create failed: {exc}") from exc
    logger.info("workspace_created id=%s slug=%s by=%s", ws.id, ws.slug, user.email)
    return WorkspaceOut(
        id=ws.id, name=ws.name, slug=ws.slug,
        description=ws.description, role="owner",
    )


@router.delete("/{ws_id}", status_code=204)
async def delete_workspace(
    ws_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> None:
    # The owner, or the superadmin. An admin may not: deleting the workspace
    # takes the owner's work with it, a bigger act than demoting them, which
    # an admin is not allowed either.
    if not is_superadmin(user) and await actor_role(session, user, ws_id) != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can delete this workspace")
    ws = await session.get(Workspace, ws_id)
    if ws is None:
        return
    if ws.slug == "default":
        raise HTTPException(status_code=400, detail="cannot delete default workspace")
    await session.delete(ws)
    await session.commit()
    logger.info("workspace_deleted id=%s by=%s", ws_id, user.email)


@router.get("/{ws_id}/members", response_model=list[MemberOut])
async def list_members(
    ws_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> list[MemberOut]:
    await _require_ws_member(session, user, ws_id)
    rows = (await session.scalars(
        select(WorkspaceMember).where(WorkspaceMember.workspace_id == ws_id)
    )).all()
    # Users live in the SQLite UserStore, not Postgres — enrich in Python.
    from src.users.store import get_user_store
    ustore = get_user_store()
    out: list[MemberOut] = []
    for r in rows:
        u = ustore.get_by_id(r.user_id)
        out.append(MemberOut(
            user_id=r.user_id, role=r.role,
            email=(u.email if u else ""), name=((u.name or "") if u else ""),
        ))
    return out


@router.post("/{ws_id}/members/{user_id}/reset-link")
async def member_reset_link(
    ws_id: str,
    user_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Password-reset link for a member of THIS workspace — workspace
    owner/admin power (the global-admin variant lives in /api/users).
    A tenant admin must never mint a takeover link for a global admin."""
    await _require_ws_admin(session, user, ws_id)
    member = await session.get(WorkspaceMember, (ws_id, user_id))
    if member is None:
        raise HTTPException(status_code=404, detail="Not a member of this workspace")
    # A reset link is a takeover of the account it is minted for, so it is
    # bounded by the same rule as changing that member: a workspace admin may
    # mint one for a member or viewer, never for another admin, an editor or
    # the owner — otherwise "cannot demote the owner" was one click from
    # "can become the owner".
    if user_id != user.id:
        refuse_unless_can_change(
            user, await actor_role(session, user, ws_id), member.role, member.role)
    from src.users.store import get_user_store
    target = get_user_store().get_by_id(user_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=404, detail="User not found")
    if target.is_admin and not user.is_admin:
        raise HTTPException(
            status_code=403, detail="Cannot reset a global admin's password")
    from src.api.routers.users import build_reset_link
    link, expires_at = build_reset_link(target)
    logger.info("password_reset_link_issued by=%s for=%s ws=%s",
                user.email, target.email, ws_id)
    return {"url": link, "expires_at": expires_at}


@router.put("/{ws_id}/members/{user_id}", response_model=MemberOut)
async def upsert_member(
    ws_id: str, user_id: str, payload: MemberIn, request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_user),
) -> MemberOut:
    superadmin = is_superadmin(admin)
    if not superadmin:
        await _require_ws_admin(session, admin, ws_id)
    if payload.role not in _VALID_ROLES:
        raise HTTPException(status_code=400,
                            detail=f"role must be one of {sorted(_VALID_ROLES)}")
    if await session.get(Workspace, ws_id) is None:
        raise HTTPException(status_code=404, detail="workspace not found")

    # Enrolment is not a way to reach a stranger. This endpoint used to write
    # whatever id it was given — no existence check, no consent — and the
    # reset-link endpoint below then treats membership as authority to mint a
    # password-reset link for that account. Together that was a full takeover
    # of any non-admin user on the installation, from a fresh signup.
    #
    # So an id may only name someone the caller already shares a workspace
    # with. Everyone else joins by accepting an invitation.
    row = await session.get(WorkspaceMember, (ws_id, user_id))
    if row is None:
        from src.api.routers.users import PLATFORM_USER_IDS, visible_user_ids
        from src.users.store import get_user_store
        target = get_user_store().get_by_id(user_id)
        if target is None or not target.is_active or user_id in PLATFORM_USER_IDS:
            raise HTTPException(status_code=404, detail="User not found")
        if not admin.is_admin and user_id not in await visible_user_ids(session, admin):
            raise HTTPException(
                status_code=403,
                detail="Invite this person by email or link — they have to accept.",
            )
    _old, new = await change_membership(
        session, actor=admin, ws_id=ws_id, user_id=user_id,
        new_role=payload.role, via="workspace_members", ip=client_ip(request),
    )
    return MemberOut(user_id=user_id, role=new or payload.role)


@router.delete("/{ws_id}/members/{user_id}", status_code=204)
async def remove_member(
    ws_id: str, user_id: str, request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(get_current_user),
) -> None:
    if not is_superadmin(admin):
        await _require_ws_admin(session, admin, ws_id)
    row = await session.get(WorkspaceMember, (ws_id, user_id))
    if row is None:
        return
    await change_membership(
        session, actor=admin, ws_id=ws_id, user_id=user_id,
        new_role=None, via="workspace_members", ip=client_ip(request),
    )


__all__ = ["router", "resolve_active_workspace"]
