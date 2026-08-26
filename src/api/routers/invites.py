"""Workspace invitations (Stage 23).

Two ways to get someone into a workspace, both admin-only:

  * **By email** — if the account already exists it is added straight away;
    otherwise an email-bound invite is created that only that address can
    redeem after signing up.
  * **By link** — an open, multi-use, expiring token anyone can redeem.

Only the SHA-256 hash of a token is stored; the raw value is shown once at
creation. Redeeming is done by an authenticated user (sign up first, then
accept), which keeps the flow simple and avoids a second account-creation path.

    GET    /api/invites                 — list active invites (admin)
    POST   /api/invites                 — create email/link invite (admin)
    DELETE /api/invites/{invite_id}     — revoke (admin)
    GET    /api/invites/preview/{token} — what does this link grant?
    POST   /api/invites/accept          — redeem as the logged-in user
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import (
    current_workspace_id,
    get_current_user,
    get_users,
    require_workspace_admin,
)
from src.db.models import Workspace, WorkspaceInvite, WorkspaceMember
from src.db.session import get_async_session
from src.users import User, UserStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/invites", tags=["invites"])

_VALID_ROLES = {"owner", "admin", "member", "viewer"}
DEFAULT_TTL_DAYS = 14


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _expiry_for(payload: InviteIn) -> datetime:
    """When this invite stops working. Most specific wins.

    An emailed invite ignores `never_expires`: it is a standing credential
    sitting in a mailbox, and the one thing that bounds it is the clock.
    A link is handed over deliberately and its uses are counted, so a
    non-expiring one is a decision someone can actually make.
    """
    if payload.never_expires and not payload.email:
        return NEVER_EXPIRES
    if payload.ttl_minutes is not None:
        return datetime.now(UTC) + timedelta(minutes=payload.ttl_minutes)
    return datetime.now(UTC) + timedelta(days=payload.ttl_days)


#: "Never expires", stored rather than modelled as NULL. `expires_at` is NOT
#: NULL in the database, and widening it would mean an Alembic revision plus a
#: guard at every read — for a value that behaves exactly like a date far
#: enough away. The validity check (`expires_at <= now`) keeps working as-is.
NEVER_EXPIRES = datetime(9999, 12, 31, tzinfo=UTC)


class InviteIn(BaseModel):
    # Omit `email` for an open link invite.
    email: EmailStr | None = None
    role: str = Field(default="member")
    ttl_days: int = Field(default=DEFAULT_TTL_DAYS, ge=1, le=90)
    # A link handed over in a call is useful for minutes, not weeks — so the
    # short end is expressible. Minutes take precedence over ttl_days when set;
    # ttl_days stays the default so existing callers are unaffected.
    ttl_minutes: int | None = Field(default=None, ge=1, le=129_600)
    never_expires: bool = False
    max_uses: int = Field(default=1, ge=1, le=500)


class InviteOut(BaseModel):
    id: str
    workspace_id: str
    email: str | None
    role: str
    max_uses: int
    used_count: int
    expires_at: str
    revoked: bool
    created_by: str | None


class InviteCreated(InviteOut):
    # Present only in the creation response — never stored, never re-shown.
    token: str | None = None
    invite_url: str | None = None
    added_directly: bool = False


class InvitePreview(BaseModel):
    workspace_id: str
    workspace_name: str
    role: str
    email_bound: bool
    valid: bool
    detail: str = ""


def _to_out(row: WorkspaceInvite) -> InviteOut:
    return InviteOut(
        id=row.id, workspace_id=row.workspace_id, email=row.email, role=row.role,
        max_uses=row.max_uses, used_count=row.used_count,
        expires_at=row.expires_at.isoformat(), revoked=row.revoked,
        created_by=row.created_by,
    )


@router.get("", response_model=list[InviteOut])
async def list_invites(
    session: AsyncSession = Depends(get_async_session),
    _admin: User = Depends(require_workspace_admin),
    ws: str = Depends(current_workspace_id),
) -> list[InviteOut]:
    rows = (await session.scalars(
        select(WorkspaceInvite)
        .where(WorkspaceInvite.workspace_id == ws)
        .order_by(WorkspaceInvite.created_at.desc())
    )).all()
    return [_to_out(r) for r in rows]


@router.post("", response_model=InviteCreated, status_code=201)
async def create_invite(
    payload: InviteIn,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_workspace_admin),
    users: UserStore = Depends(get_users),
    ws: str = Depends(current_workspace_id),
) -> InviteCreated:
    if payload.role not in _VALID_ROLES:
        raise HTTPException(status_code=422, detail=f"role must be one of {sorted(_VALID_ROLES)}")

    # Existing account + email invite → just add them, no token round-trip.
    if payload.email:
        existing = users.get_by_email(str(payload.email))
        if existing is not None:
            member = await session.get(WorkspaceMember, (ws, existing.id))
            if member is None:
                session.add(WorkspaceMember(
                    workspace_id=ws, user_id=existing.id, role=payload.role,
                ))
            else:
                member.role = payload.role
            await session.commit()
            logger.info(
                "invite_direct_add ws=%s user=%s role=%s by=%s",
                ws, existing.email, payload.role, admin.email,
            )
            return InviteCreated(
                id="direct", workspace_id=ws, email=str(payload.email),
                role=payload.role, max_uses=1, used_count=1,
                expires_at=datetime.now(UTC).isoformat(),
                revoked=False, created_by=admin.email, added_directly=True,
            )

    raw = secrets.token_urlsafe(32)
    row = WorkspaceInvite(
        id=str(uuid.uuid4()),
        workspace_id=ws,
        token_hash=_hash(raw),
        email=str(payload.email) if payload.email else None,
        role=payload.role,
        max_uses=1 if payload.email else payload.max_uses,
        expires_at=_expiry_for(payload),
        created_by=admin.email,
    )
    session.add(row)
    await session.commit()
    logger.info(
        "invite_created ws=%s email=%s role=%s uses=%d by=%s",
        ws, payload.email or "(link)", payload.role, row.max_uses, admin.email,
    )
    # Email-bound invite + SMTP configured → deliver the link directly. The
    # token is still returned to the admin below, so manual sharing keeps
    # working as the fallback when there is no mailer.
    if payload.email:
        try:
            from src.notifications.mailer import (
                absolute_url,
                mailer_configured,
                send_email_background,
            )
            if mailer_configured():
                ws_row = await session.get(Workspace, ws)
                ws_name = ws_row.name if ws_row is not None else ws
                send_email_background(
                    str(payload.email),
                    f"You're invited to {ws_name} on Celmis",
                    f"{admin.email} invited you to join the workspace "
                    f"\"{ws_name}\" as {payload.role}.\n\n"
                    f"Accept the invitation:\n{absolute_url(f'/invite/{raw}')}\n\n"
                    "If you don't have an account yet, you can sign up on that page "
                    "with this email address.",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("invite_email_failed err=%s", exc)
    out = InviteCreated(**_to_out(row).model_dump())
    out.token = raw
    out.invite_url = f"/invite/{raw}"
    return out


@router.delete("/{invite_id}", status_code=204)
async def revoke_invite(
    invite_id: str,
    session: AsyncSession = Depends(get_async_session),
    admin: User = Depends(require_workspace_admin),
    ws: str = Depends(current_workspace_id),
) -> None:
    row = await session.get(WorkspaceInvite, invite_id)
    if row is None or row.workspace_id != ws:
        return
    row.revoked = True
    await session.commit()
    logger.info("invite_revoked id=%s by=%s", invite_id, admin.email)


async def _load_valid(session: AsyncSession, token: str) -> WorkspaceInvite | None:
    row = (await session.scalars(
        select(WorkspaceInvite).where(WorkspaceInvite.token_hash == _hash(token))
    )).first()
    if row is None or row.revoked:
        return None
    if row.expires_at <= datetime.now(UTC):
        return None
    if row.used_count >= row.max_uses:
        return None
    return row


@router.get("/preview/{token}", response_model=InvitePreview)
async def preview(
    token: str,
    session: AsyncSession = Depends(get_async_session),
) -> InvitePreview:
    """Unauthenticated — lets the invite landing page say what it grants."""
    row = await _load_valid(session, token)
    if row is None:
        return InvitePreview(
            workspace_id="", workspace_name="", role="", email_bound=False,
            valid=False, detail="This invite is invalid, expired or already used.",
        )
    ws = await session.get(Workspace, row.workspace_id)
    return InvitePreview(
        workspace_id=row.workspace_id,
        workspace_name=ws.name if ws else row.workspace_id,
        role=row.role, email_bound=bool(row.email), valid=True,
    )


class AcceptIn(BaseModel):
    token: str = Field(min_length=16, max_length=256)


@router.post("/accept")
async def accept(
    payload: AcceptIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> dict:
    row = await _load_valid(session, payload.token)
    if row is None:
        raise HTTPException(status_code=400, detail="Invite is invalid, expired or already used")
    if row.email and row.email.lower() != user.email.lower():
        raise HTTPException(
            status_code=403,
            detail="This invite was issued for a different email address",
        )

    member = await session.get(WorkspaceMember, (row.workspace_id, user.id))
    if member is None:
        session.add(WorkspaceMember(
            workspace_id=row.workspace_id, user_id=user.id, role=row.role,
        ))
    else:
        member.role = row.role
    row.used_count += 1
    await session.commit()
    logger.info(
        "invite_accepted ws=%s user=%s role=%s", row.workspace_id, user.email, row.role,
    )
    ws = await session.get(Workspace, row.workspace_id)
    return {
        "ok": True,
        "workspace_id": row.workspace_id,
        "workspace_slug": ws.slug if ws else row.workspace_id,
        "role": row.role,
    }
