"""Access requests — "I have no team workspace yet, please let me in".

A person who signs in (password, Google or company SSO) owns a personal
workspace and nothing else until somebody adds them. An invite needs an
inviter who already knows them; this is the other direction: the person asks,
in general, and the SUPERADMIN decides.

Requester (any signed-in account):

    POST   /api/access-requests       {comment?}  — ask (one pending at a time)
    GET    /api/access-requests/me                — eligibility + latest request
    DELETE /api/access-requests/me                — cancel the pending one

Superadmin only (`require_superadmin` — not a workspace admin, not another
global admin):

    GET    /api/admin/access-requests?status=     — list, pending first
    POST   /api/admin/access-requests/{id}/approve {grants: [{workspace_id, role}]}
    POST   /api/admin/access-requests/{id}/reject  {reason}

What the requester is shown is deliberately thin. The request is not addressed
to a workspace, and nothing the requester can read names one — not while it
is pending, not when it is rejected. Only an APPROVED request lists the
workspaces it granted, which the requester is now a member of anyway.

An approval writes every grant through `change_memberships`
(src/api/memberships.py) — the one membership writer, its rule and its audit
row, with via="access_request" — in ONE transaction together with the request
row: one grant that cannot be applied and none of them are. Deciding a
request that is no longer pending is 409, so a double click or two
superadmin tabs cannot approve and then reject the same request.

Email: when SMTP is configured (src/notifications/mailer.py, the mailer the
invites and password resets already use) the requester is told of a decision
and the superadmin of a new request, best effort. Without it the requester
sees the decision in-app: on /access-request and as a toast on the next visit.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import client_ip, get_current_user, get_users, require_superadmin
from src.api.memberships import change_memberships
from src.api.workspace_provision import personal_slug
from src.db.models import AccessRequest, Workspace, WorkspaceMember
from src.db.session import get_async_session
from src.users import User, UserStore
from src.users.roles import VALID_WORKSPACE_ROLES, is_superadmin

logger = logging.getLogger(__name__)

router = APIRouter(tags=["access-requests"])

COMMENT_MAX = 1000
REASON_MAX = 1000
STATUSES = ("pending", "approved", "rejected", "cancelled")


# ─── Who counts as "no team access" ──────────────────────────────────


def is_personal_slug_of(slug: str, user_id: str) -> bool:
    return slug == personal_slug(user_id)


async def team_workspace_ids(session: AsyncSession, user_id: str) -> list[str]:
    """Workspaces `user_id` belongs to, other than their own personal one."""
    rows = (await session.execute(
        select(WorkspaceMember.workspace_id, Workspace.slug)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .where(WorkspaceMember.user_id == user_id)
    )).all()
    return [ws_id for ws_id, slug in rows if not is_personal_slug_of(slug, user_id)]


async def users_with_team_access(session: AsyncSession, user_ids: list[str]) -> set[str]:
    """Of `user_ids`, those with at least one non-personal membership."""
    if not user_ids:
        return set()
    rows = (await session.execute(
        select(WorkspaceMember.user_id, Workspace.slug)
        .join(Workspace, Workspace.id == WorkspaceMember.workspace_id)
        .where(WorkspaceMember.user_id.in_(user_ids))
    )).all()
    return {uid for uid, slug in rows if not is_personal_slug_of(slug, uid)}


def sign_in_methods(user: User) -> list[str]:
    """How this account can sign in: any of password / google / oidc."""
    out: list[str] = []
    if user.has_password:
        out.append("password")
    if user.has_google:
        out.append("google")
    if user.has_oidc:
        out.append("oidc")
    return out or [str(getattr(user.auth_method, "value", user.auth_method))]


def _personal_owner(users: UserStore, slug: str) -> str | None:
    """The user id whose personal workspace `slug` is, if it is one."""
    if not slug.startswith("u-"):
        return None
    candidate = slug[2:]
    return candidate if users.get_by_id(candidate) is not None else None


# ─── Schemas ─────────────────────────────────────────────────────────


class AccessRequestIn(BaseModel):
    comment: str = Field(default="", max_length=COMMENT_MAX)
    model_config = ConfigDict(extra="forbid")


class GrantIn(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=32)
    model_config = ConfigDict(extra="forbid")


class ApproveIn(BaseModel):
    grants: list[GrantIn] = Field(min_length=1, max_length=100)
    model_config = ConfigDict(extra="forbid")


class RejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=REASON_MAX)
    model_config = ConfigDict(extra="forbid")

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("a reason is required")
        return v.strip()


class GrantOut(BaseModel):
    workspace_id: str
    workspace_name: str
    workspace_slug: str = ""
    role: str


class MyRequestOut(BaseModel):
    """What the REQUESTER sees. No workspace appears here unless approved."""

    id: str
    status: str
    comment: str
    created_at: str
    decided_at: str | None = None
    #: The superadmin's reason — on a rejected request only.
    decision_note: str | None = None
    #: The workspaces granted — on an approved request only.
    grants: list[GrantOut] = []


class MyAccessOut(BaseModel):
    #: True when the account has no workspace but its personal one — the
    #: banner and the request form are offered then.
    eligible: bool
    has_team_access: bool
    request: MyRequestOut | None = None


class AdminRequestOut(BaseModel):
    id: str
    user_id: str
    email: str
    name: str
    sign_in_methods: list[str]
    user_active: bool
    comment: str
    status: str
    created_at: str
    updated_at: str | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    decision_note: str | None = None
    grants: list[GrantOut] = []


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _grants_out(row: AccessRequest) -> list[GrantOut]:
    return [
        GrantOut(
            workspace_id=str(g.get("workspace_id", "")),
            workspace_name=str(g.get("workspace_name", "")),
            workspace_slug=str(g.get("workspace_slug", "")),
            role=str(g.get("role", "")),
        )
        for g in (row.grants or []) if isinstance(g, dict)
    ]


def _mine(row: AccessRequest) -> MyRequestOut:
    return MyRequestOut(
        id=row.id, status=row.status, comment=row.comment or "",
        created_at=_iso(row.created_at) or "",
        decided_at=_iso(row.decided_at),
        decision_note=row.decision_note if row.status == "rejected" else None,
        grants=_grants_out(row) if row.status == "approved" else [],
    )


def _audit(action: str, *, actor: User, row: AccessRequest, ip: str | None,
           detail: dict | None = None) -> None:
    from src.security.audit import record_action

    record_action(
        action=action, actor=actor.email, actor_id=actor.id,
        target=row.user_id, ip=ip,
        detail={"request_id": row.id, **(detail or {})},
    )


def _mail(to: str, subject: str, body: str) -> None:
    """Best effort through the mailer invites already use; silent without SMTP."""
    try:
        from src.notifications.mailer import mailer_configured, send_email_background

        if to and mailer_configured():
            send_email_background(to, subject, body)
    except Exception as exc:  # noqa: BLE001
        logger.warning("access_request_email_failed err=%s", exc)


async def _pending_of(session: AsyncSession, user_id: str) -> AccessRequest | None:
    return (await session.scalars(
        select(AccessRequest).where(
            AccessRequest.user_id == user_id, AccessRequest.status == "pending")
    )).first()


async def _latest_of(session: AsyncSession, user_id: str) -> AccessRequest | None:
    return (await session.scalars(
        select(AccessRequest)
        .where(AccessRequest.user_id == user_id)
        .order_by(AccessRequest.created_at.desc())
        .limit(1)
    )).first()


# ─── Requester ───────────────────────────────────────────────────────


@router.get("/api/access-requests/me", response_model=MyAccessOut)
async def my_access_request(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> MyAccessOut:
    has_team = bool(await team_workspace_ids(session, user.id))
    latest = await _latest_of(session, user.id)
    return MyAccessOut(
        eligible=not has_team and not is_superadmin(user),
        has_team_access=has_team,
        request=_mine(latest) if latest is not None else None,
    )


@router.post("/api/access-requests", response_model=MyRequestOut, status_code=201)
async def create_access_request(
    payload: AccessRequestIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> MyRequestOut:
    if is_superadmin(user):
        raise HTTPException(status_code=409, detail="The superadmin grants access, not asks for it")
    if await team_workspace_ids(session, user.id):
        raise HTTPException(status_code=409, detail="You already have access to a team workspace")
    if await _pending_of(session, user.id) is not None:
        raise HTTPException(status_code=409, detail="You already have a pending access request")
    row = AccessRequest(
        id=str(uuid.uuid4()), user_id=user.id, email=user.email,
        comment=payload.comment.strip(), status="pending", grants=[],
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as exc:
        # Two submits raced past the check above; the partial unique index
        # (one pending per user) let exactly one of them in.
        await session.rollback()
        raise HTTPException(
            status_code=409, detail="You already have a pending access request") from exc
    await session.refresh(row)
    ip = client_ip(request)
    _audit("access_request.created", actor=user, row=row, ip=ip,
           detail={"comment_length": len(row.comment)})
    logger.info("access_request_created id=%s user=%s", row.id, user.email)
    from src.users.roles import master_email

    _mail(
        master_email(), "Celmis: new access request",
        f"{user.email} asked for access to team workspaces.\n\n"
        + (f"Comment: {row.comment}\n\n" if row.comment else "")
        + "Review it under Administration → Access requests.",
    )
    return _mine(row)


@router.delete("/api/access-requests/me", status_code=204)
async def cancel_access_request(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> None:
    row = await _pending_of(session, user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="No pending access request")
    row.status = "cancelled"
    row.updated_at = datetime.now(UTC)
    await session.commit()
    _audit("access_request.cancelled", actor=user, row=row, ip=client_ip(request))
    logger.info("access_request_cancelled id=%s user=%s", row.id, user.email)


# ─── Superadmin ──────────────────────────────────────────────────────


@router.get("/api/admin/access-requests", response_model=list[AdminRequestOut])
async def list_access_requests(
    status: Literal["all", "pending", "approved", "rejected", "cancelled"] = Query(default="all"),
    session: AsyncSession = Depends(get_async_session),
    _su: User = Depends(require_superadmin),
    users: UserStore = Depends(get_users),
) -> list[AdminRequestOut]:
    q = select(AccessRequest)
    if status != "all":
        q = q.where(AccessRequest.status == status)
    q = q.order_by(
        case((AccessRequest.status == "pending", 0), else_=1),
        AccessRequest.created_at.desc(),
    ).limit(500)
    rows = (await session.scalars(q)).all()
    return [_admin_out(r, users.get_by_id(r.user_id)) for r in rows]


async def _load_for_decision(session: AsyncSession, request_id: str) -> AccessRequest:
    row = (await session.scalars(
        select(AccessRequest).where(AccessRequest.id == request_id).with_for_update()
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Access request not found")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail=f"This request is already {row.status}")
    return row


@router.post("/api/admin/access-requests/{request_id}/approve", response_model=AdminRequestOut)
async def approve_access_request(
    request_id: str,
    payload: ApproveIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    su: User = Depends(require_superadmin),
    users: UserStore = Depends(get_users),
) -> AdminRequestOut:
    row = await _load_for_decision(session, request_id)
    target = users.get_by_id(row.user_id)
    if target is None or not target.is_active:
        raise HTTPException(status_code=409, detail="The requesting account no longer exists or is deactivated")

    ws_ids = [g.workspace_id for g in payload.grants]
    if len(set(ws_ids)) != len(ws_ids):
        raise HTTPException(status_code=422, detail="Each workspace may appear only once")
    resolved: list[tuple[Workspace, str]] = []
    for g in payload.grants:
        if g.role not in VALID_WORKSPACE_ROLES:
            raise HTTPException(
                status_code=422, detail=f"role must be one of {sorted(VALID_WORKSPACE_ROLES)}")
        ws = await session.get(Workspace, g.workspace_id)
        if ws is None:
            raise HTTPException(status_code=422, detail=f"Workspace {g.workspace_id} does not exist")
        if _personal_owner(users, ws.slug) is not None:
            raise HTTPException(
                status_code=422,
                detail=f"{ws.name} is a personal workspace; grant a team workspace instead")
        resolved.append((ws, g.role))

    # Staged first, committed by `change_memberships` together with the
    # membership rows — the request is approved exactly when the grants are.
    now = datetime.now(UTC)
    row.status = "approved"
    row.decided_by = su.email
    row.decided_at = now
    row.updated_at = now
    row.decision_note = None
    row.grants = [
        {"workspace_id": ws.id, "workspace_name": ws.name,
         "workspace_slug": ws.slug, "role": role}
        for ws, role in resolved
    ]
    ip = client_ip(request)
    await change_memberships(
        session, actor=su, user_id=row.user_id,
        changes=[(ws.id, role) for ws, role in resolved],
        via="access_request", ip=ip, extra_detail={"access_request_id": row.id},
    )
    _audit("access_request.approved", actor=su, row=row, ip=ip,
           detail={"grants": [{"workspace_id": ws.id, "role": role} for ws, role in resolved]})
    logger.info("access_request_approved id=%s user=%s grants=%d by=%s",
                row.id, row.email, len(resolved), su.email)
    _mail(
        target.email, "Celmis: your access request was approved",
        "You now have access to:\n"
        + "".join(f"  - {ws.name} ({role})\n" for ws, role in resolved)
        + "\nSign in to Celmis and pick the workspace in the switcher.",
    )
    return _admin_out(row, target)


@router.post("/api/admin/access-requests/{request_id}/reject", response_model=AdminRequestOut)
async def reject_access_request(
    request_id: str,
    payload: RejectIn,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    su: User = Depends(require_superadmin),
    users: UserStore = Depends(get_users),
) -> AdminRequestOut:
    row = await _load_for_decision(session, request_id)
    now = datetime.now(UTC)
    row.status = "rejected"
    row.decided_by = su.email
    row.decided_at = now
    row.updated_at = now
    row.decision_note = payload.reason
    row.grants = []
    await session.commit()
    _audit("access_request.rejected", actor=su, row=row, ip=client_ip(request),
           detail={"reason_length": len(payload.reason)})
    logger.info("access_request_rejected id=%s user=%s by=%s", row.id, row.email, su.email)
    target = users.get_by_id(row.user_id)
    if target is not None:
        _mail(
            target.email, "Celmis: your access request was declined",
            f"Reason: {payload.reason}\n\nYou can send a new request from Celmis.",
        )
    return _admin_out(row, target)


def _admin_out(row: AccessRequest, u: User | None) -> AdminRequestOut:
    return AdminRequestOut(
        id=row.id, user_id=row.user_id, email=u.email if u else row.email,
        name=(u.name or "") if u else "",
        sign_in_methods=sign_in_methods(u) if u else [],
        user_active=bool(u and u.is_active),
        comment=row.comment or "", status=row.status,
        created_at=_iso(row.created_at) or "", updated_at=_iso(row.updated_at),
        decided_by=row.decided_by, decided_at=_iso(row.decided_at),
        decision_note=row.decision_note, grants=_grants_out(row),
    )


__all__ = [
    "router", "sign_in_methods", "team_workspace_ids", "users_with_team_access",
]
