"""User directory (Stage 22) — read-only enumeration for member pickers.

Team / workspace / access-rule editors need to resolve human-friendly emails
instead of asking admins to paste raw user UUIDs. This exposes the minimal
directory an admin needs; it is **not** a user-management CRUD surface.

The one write here is the admin-issued password-reset link (Stage 23). It lives
on this router rather than `/api/auth` because it is an *administrative* act
behind `require_admin`, not something an anonymous caller may trigger.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, get_users, require_admin
from src.db.models import WorkspaceMember
from src.db.session import get_async_session
from src.users import User, UserStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["users"])


class UserDirectoryEntry(BaseModel):
    id: str
    email: str
    name: str
    is_admin: bool
    is_active: bool


class ResetLinkOut(BaseModel):
    url: str
    expires_at: datetime
    email: str


class DirectoryEntry(BaseModel):
    id: str
    email: str
    name: str


def build_reset_link(target: User) -> tuple[str, datetime]:
    """Mint a reset link for `target`. Shared by the global-admin endpoint
    below and the workspace-admin endpoint in workspaces.py."""
    from src.api.routers.auth import _reset_url, issue_reset_token
    raw, expires_at = issue_reset_token(target.id)
    return _reset_url(raw), expires_at


#: Accounts the platform owns rather than a person: the seeded local user and
#: the master-key recovery account. Neither is ever a workspace member, and
#: offering them in a picker only invites someone to try.
PLATFORM_USER_IDS = frozenset({"default", "master-admin"})


async def visible_user_ids(session: AsyncSession, caller: User) -> set[str]:
    """Users the caller already shares a workspace with, plus the caller.

    The boundary for every people-picker in the product. A tenant may name
    people it already works with; reaching anyone else goes through an
    invitation, which the other side has to accept.
    """
    own = (await session.scalars(
        select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == caller.id)
    )).all()
    if not own:
        return {caller.id}
    peers = (await session.scalars(
        select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id.in_(own))
    )).all()
    return set(peers) | {caller.id}


@router.get("/directory", response_model=list[DirectoryEntry])
async def users_directory(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
    users: UserStore = Depends(get_users),
) -> list[DirectoryEntry]:
    """People the caller already shares a workspace with.

    This used to answer with every active account on the installation, to any
    signed-in caller. That is a tenant roster: sign up, open the member picker,
    read every customer's email address. Worse, the ids it handed out were
    accepted verbatim by PUT /workspaces/{id}/members/{user_id}, so a stranger
    could be pulled into an attacker's own workspace and then handed a
    password-reset link through the endpoint next door.

    Adding someone the caller has never worked with is what invitations are
    for — the other side accepts, rather than being enrolled silently.

    Global admins keep the installation-wide view; that is their job, and
    /api/users already gives them the same list with more fields.
    """
    allowed = None if user.is_admin else await visible_user_ids(session, user)
    return [
        DirectoryEntry(id=u.id, email=u.email, name=u.name or "")
        for u in users.list(active_only=True)
        if u.id not in PLATFORM_USER_IDS
        and (allowed is None or u.id in allowed)
    ]


@router.get("", response_model=list[UserDirectoryEntry])
async def list_users(
    include_inactive: bool = False,
    _admin: User = Depends(require_admin),
    users: UserStore = Depends(get_users),
) -> list[UserDirectoryEntry]:
    rows = users.list(active_only=not include_inactive)
    return [
        UserDirectoryEntry(
            id=u.id, email=u.email, name=u.name or "",
            is_admin=u.is_admin, is_active=u.is_active,
        )
        for u in rows
    ]


@router.post("/{user_id}/reset-link", response_model=ResetLinkOut)
async def create_reset_link(
    user_id: str,
    admin: User = Depends(require_admin),
    users: UserStore = Depends(get_users),
) -> ResetLinkOut:
    """Mint a password-reset link for a user and hand it to the admin.

    This is the mailer-less delivery path (Metabase and GitLab both work this
    way): with no SMTP configured, someone trusted has to carry the link. An
    admin already holds every git and LLM credential in the workspace, so this
    grants no privilege they lacked — unlike returning the token to whoever
    typed an email address into the public /forgot-password form.

    The link is short-lived and single-use. Pass it over a private channel.
    """
    from src.api.routers.auth import _reset_url, issue_reset_token

    target = users.get_by_id(user_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.id == "master-admin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The master account authenticates only via CELMIS_MASTER_KEY",
        )

    raw, expires_at = issue_reset_token(target.id)
    logger.info(
        "password_reset_link_issued by=%s for=%s", admin.email, target.email,
    )
    return ResetLinkOut(url=_reset_url(raw), expires_at=expires_at, email=target.email)
