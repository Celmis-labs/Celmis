"""The owner's side of repository sharing: see a share, narrow it, end it.

A workspace (the owner) can share ONE of its repositories with another workspace
(the receiver); the receiver's superadmin then puts the share on a developer token
(``mcp_tokens``). This module is what the OWNER does with what it gave. Creating
and extending a share is the Enterprise feature ``sharing`` and lives in
``src/ee``; everything here is free in every build, and nothing here imports
``src.ee``: the owner can always see, narrow and end what they gave.

  GET   /api/sharing/shares                       what this workspace shares out
  GET   /api/sharing/shares/{id}                  one share and who holds a token for it
  GET   /api/sharing/shares/{id}/activity         who read it, when (names and emails)
  PATCH /api/sharing/shares/{id}                  narrow only (409 for anything wider)
  POST  /api/sharing/shares/{id}/revoke           end it, from the next call on

The list is for the workspace's administrators. Every route about ONE share names
the share in its path and is gated on the share's OWNER (``require_share_admin``):
for anybody else, including the administrators of the receiving workspace, the
share does not exist (404, not 403).

The activity view is a fixed projection: when, which tool, how it ended, and who.
Never the repository's paths, the token, the arguments or the sizes; the owner
sees the receiving workspace's people because they are the ones reading the
owner's code, which is said to the owner when the share is made. Each change here
is written to the owner's audit trail (``sharing.*``).

``list_shares``, ``share_detail`` and the models are public so the Enterprise router
(``/api/sharing-grants``) answers with the same shapes; it borrows ``_session``,
``_iso`` and ``_jsonable`` as well, so there is one way to open a session and to write a
time in an audit row.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from src.access import shares as repo_shares
from src.api.deps import (
    client_ip,
    current_workspace_id,
    get_users,
    require_share_admin,
    require_workspace_admin,
)
from src.users import User, UserStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sharing", tags=["sharing"])

#: Most shares one list returns, newest first. A workspace shares a handful.
MAX_LISTED = 200
#: Longest window and most rows of the activity view.
MAX_ACTIVITY_DAYS = 365
MAX_ACTIVITY_ROWS = 1000


# ─── models ──────────────────────────────────────────────────────────


class ShareSummaryOut(BaseModel):
    share_id: str
    slug: str
    full_name: str = ""
    grantee_workspace_id: str
    grantee_name: str = ""
    #: active | expired | revoked
    status: str
    created_at: str | None = None
    expires_at: str
    allow_ask: bool
    max_people: int
    #: How many path patterns the owner hides in it (the patterns are in the detail).
    deny_count: int
    #: People who hold a live token for it now.
    holders: int
    last_used_at: str | None = None
    revoked_at: str | None = None
    revoke_reason: str | None = None


class SharePersonOut(BaseModel):
    user_id: str
    name: str = ""
    email: str = ""


class ShareHolderOut(SharePersonOut):
    #: How many live tokens of theirs carry the share.
    tokens: int
    last_used_at: str | None = None


class ShareDetailOut(ShareSummaryOut):
    deny_globs: list[str] = []
    #: People the owner switched off. They keep their tokens; the share no longer
    #: answers for them.
    blocked: list[SharePersonOut] = []
    people: list[ShareHolderOut] = []
    created_by: str = ""


class ShareActivityOut(BaseModel):
    """One read of the share. This is the whole projection: a field added here is a
    field the owner can see about somebody else's workspace."""

    ts: str
    tool: str
    status: str
    user_id: str
    name: str = ""
    email: str = ""


class ShareNarrowIn(BaseModel):
    """What a share can be changed to: less. Anything else is refused as unknown
    (422), so that a client does not think it widened or replaced something."""

    model_config = ConfigDict(extra="forbid")

    #: More path patterns to hide (added to the ones already hidden).
    add_deny: list[str] = Field(default_factory=list, max_length=repo_shares.MAX_DENY_GLOBS)
    #: An earlier end. A later one is a widening.
    expires_at: datetime | None = None
    #: Only ``false`` narrows; ``true`` on a share without questions is a widening.
    allow_ask: bool | None = None
    #: Fewer people; never fewer than hold a token for it now.
    max_people: int | None = None
    #: People (user ids) to switch off.
    block: list[str] = Field(default_factory=list, max_length=repo_shares.MAX_BLOCKED)


# ─── helpers ─────────────────────────────────────────────────────────


def _session():  # noqa: ANN202
    from sqlalchemy.orm import Session

    from src.access.resolver import _sync_engine

    return Session(_sync_engine())


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    value = _utc(value)
    return value.isoformat() if value is not None else None


def _usage(share_id: str, grantee_workspace_id: str):  # noqa: ANN202
    """The call-log condition for 'this call read through this share'. A share id is
    a UUID (checked by the caller), so it cannot carry a wildcard into the LIKE; the
    receiving workspace is in the condition as well, so a row of another workspace
    that names the id would not be counted. Same technique as pack usage."""
    from sqlalchemy import Text, and_, cast

    from src.db.models import McpCallLog

    return and_(
        McpCallLog.workspace_id == grantee_workspace_id,
        McpCallLog.shares.is_not(None),
        cast(McpCallLog.shares, Text).like(f'%"{share_id}"%'),
    )


def _person(users: UserStore, user_id: str) -> SharePersonOut:
    found = users.get_by_id(user_id) if user_id else None
    return SharePersonOut(
        user_id=user_id,
        name=(getattr(found, "name", "") or "") if found else "",
        email=(getattr(found, "email", "") or "") if found else "")


def _members(session, workspace_id: str, user_ids) -> set[str]:  # noqa: ANN001
    """Which of ``user_ids`` belong to ``workspace_id``.

    The owner switches people off by user id and may type any string, so the name
    and address of a person are read out only for somebody in the receiving
    workspace. Looked up for anybody else, the blocked list would be a directory of
    the installation (the fixed id of the operator account, an id seen elsewhere)."""
    from sqlalchemy import select

    from src.db.models import WorkspaceMember

    wanted = sorted({str(u) for u in user_ids if u})
    if not wanted:
        return set()
    return set(session.execute(
        select(WorkspaceMember.user_id).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id.in_(wanted))
    ).scalars().all())


def _summary(session, row, *, names: dict[str, str]) -> ShareSummaryOut:  # noqa: ANN001
    from sqlalchemy import func, select

    from src.db.models import McpCallLog

    last = session.execute(
        select(func.max(McpCallLog.ts)).where(_usage(row.id, row.grantee_workspace_id))
    ).scalar()
    return ShareSummaryOut(
        share_id=row.id, slug=row.repo_slug, full_name=row.repo_full_name or "",
        grantee_workspace_id=row.grantee_workspace_id,
        grantee_name=names.get(row.grantee_workspace_id, ""),
        status=repo_shares.status_of(row), created_at=_iso(row.created_at),
        expires_at=_iso(row.expires_at) or "", allow_ask=bool(row.allow_ask),
        max_people=int(row.max_people or repo_shares.DEFAULT_PEOPLE),
        deny_count=len(row.deny_globs) if isinstance(row.deny_globs, list) else 0,
        holders=len(repo_shares.holders(session, row.id)), last_used_at=_iso(last),
        revoked_at=_iso(row.revoked_at), revoke_reason=row.revoke_reason or None)


def _workspace_names(session, ids) -> dict[str, str]:  # noqa: ANN001
    from sqlalchemy import select

    from src.db.models import Workspace

    wanted = sorted({i for i in ids if i})
    if not wanted:
        return {}
    return {w.id: w.name for w in session.execute(
        select(Workspace).where(Workspace.id.in_(wanted))).scalars().all()}


def list_shares(session, owner_workspace_id: str) -> list[ShareSummaryOut]:  # noqa: ANN001
    """The shares `owner_workspace_id` gives, newest first, every status."""
    from sqlalchemy import select

    from src.db.models import RepoShare

    rows = session.execute(
        select(RepoShare).where(RepoShare.owner_workspace_id == owner_workspace_id)
        .order_by(RepoShare.created_at.desc()).limit(MAX_LISTED)
    ).scalars().all()
    names = _workspace_names(session, [r.grantee_workspace_id for r in rows])
    return [_summary(session, r, names=names) for r in rows]


def share_detail(session, share_id: str, users: UserStore) -> ShareDetailOut:  # noqa: ANN001
    """One share with the people who hold a token for it and the people switched off.

    Raises 404 for an id that is not a share; the caller has already decided that
    the person may see it (``require_share_admin``)."""
    from sqlalchemy import func, select

    from src.db.models import McpCallLog, RepoShare

    row = session.get(RepoShare, share_id) if repo_shares.is_share_id(share_id) else None
    if row is None:
        raise HTTPException(status_code=404, detail="No such share.")
    names = _workspace_names(session, [row.grantee_workspace_id])
    base = _summary(session, row, names=names)
    last_by_person = dict(session.execute(
        select(McpCallLog.user_id, func.max(McpCallLog.ts))
        .where(_usage(row.id, row.grantee_workspace_id)).group_by(McpCallLog.user_id)
    ).all())
    people = [
        ShareHolderOut(**_person(users, uid).model_dump(), tokens=len(tokens),
                       last_used_at=_iso(last_by_person.get(uid)))
        for uid, tokens in sorted(repo_shares.holders(session, row.id).items())]
    # By the name the owner reads, not by the id they never see.
    people.sort(key=lambda p: ((p.name or p.email or p.user_id).casefold(), p.user_id))
    blocked_ids = [str(u) for u in (row.blocked_user_ids
                                    if isinstance(row.blocked_user_ids, list) else [])]
    known = _members(session, row.grantee_workspace_id, blocked_ids)
    return ShareDetailOut(
        **base.model_dump(),
        deny_globs=[str(g) for g in row.deny_globs] if isinstance(row.deny_globs, list) else [],
        blocked=[_person(users, u) if u in known else SharePersonOut(user_id=u)
                 for u in blocked_ids],
        people=people, created_by=row.created_by or "")


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


# ─── routes ──────────────────────────────────────────────────────────


@router.get("/shares", response_model=list[ShareSummaryOut])
def list_my_shares(
    workspace_id: str = Depends(current_workspace_id),
    _admin: User = Depends(require_workspace_admin),
) -> list[ShareSummaryOut]:
    """What the active workspace shares out. Shares it RECEIVES are not here: the
    receiving workspace's administrators are not told about them, and the
    superadmin reads them where tokens are issued."""
    with _session() as s:
        return list_shares(s, workspace_id)


@router.get("/shares/{share_id}", response_model=ShareDetailOut)
def get_share(
    share_id: str,
    _admin: User = Depends(require_share_admin),
    users: UserStore = Depends(get_users),
) -> ShareDetailOut:
    with _session() as s:
        return share_detail(s, share_id, users)


@router.get("/shares/{share_id}/activity", response_model=list[ShareActivityOut])
def share_activity(
    share_id: str,
    days: int = Query(default=30, ge=1, le=MAX_ACTIVITY_DAYS),
    limit: int = Query(default=200, ge=1, le=MAX_ACTIVITY_ROWS),
    _admin: User = Depends(require_share_admin),
    users: UserStore = Depends(get_users),
) -> list[ShareActivityOut]:
    """Who read the share, newest first: `ts`, `tool`, `status`, the person. The
    call log keeps what the call touched by id, so the share is found by its id in
    the receiving workspace's rows, and nothing else of the row is read out."""
    from sqlalchemy import select

    from src.db.models import McpCallLog, RepoShare

    with _session() as s:
        share = s.get(RepoShare, share_id)
        if share is None:  # cannot happen past the gate; keeps the query honest
            raise HTTPException(status_code=404, detail="No such share.")
        since = datetime.now(UTC) - timedelta(days=days)
        created = _utc(share.created_at)
        if created is not None and created > since:
            since = created  # nothing before the share existed can name it
        rows = s.execute(
            select(McpCallLog.ts, McpCallLog.tool, McpCallLog.status, McpCallLog.user_id)
            .where(_usage(share.id, share.grantee_workspace_id), McpCallLog.ts >= since)
            .order_by(McpCallLog.ts.desc()).limit(limit)
        ).all()
    people: dict[str, SharePersonOut] = {}
    out: list[ShareActivityOut] = []
    for ts, tool, status, user_id in rows:
        who = people.setdefault(user_id, _person(users, user_id))
        out.append(ShareActivityOut(
            ts=_iso(ts) or "", tool=tool, status=status, user_id=user_id,
            name=who.name, email=who.email))
    return out


@router.patch("/shares/{share_id}", response_model=ShareDetailOut)
def narrow_share(
    share_id: str,
    payload: ShareNarrowIn,
    request: Request,
    user: User = Depends(require_share_admin),
    users: UserStore = Depends(get_users),
) -> ShareDetailOut:
    """Give less than the share gives now. Takes effect on the receivers' next call:
    the rows are read on every call, so there is no token to reissue."""
    from src.db.models import RepoShare

    with _session() as s:
        try:
            changes = repo_shares.narrow(
                s, share_id, add_deny=payload.add_deny, expires_at=payload.expires_at,
                allow_ask=payload.allow_ask, max_people=payload.max_people,
                block=payload.block)
        except repo_shares.WideningError as exc:
            s.rollback()
            raise HTTPException(status_code=409, detail=repo_shares.WIDENING) from exc
        except repo_shares.ShareError as exc:
            s.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        s.commit()
        row = s.get(RepoShare, share_id)
        detail = share_detail(s, share_id, users)
        owner, ident = row.owner_workspace_id, row.id
    if changes:
        repo_shares.audit_owner(
            "sharing.narrowed", owner_workspace_id=owner, share_id=ident,
            actor=user.email, actor_id=user.id, ip=client_ip(request),
            detail={"changes": _jsonable(changes)})
    return detail


@router.post("/shares/{share_id}/revoke", response_model=ShareDetailOut)
def revoke_share(
    share_id: str,
    request: Request,
    user: User = Depends(require_share_admin),
    users: UserStore = Depends(get_users),
) -> ShareDetailOut:
    """End the share. The receivers' next call finds it ended; their tokens are not
    touched (the share id on them simply stops counting), and registering the
    repository again later does not bring it back. Ending a share that has ended
    is not an error and writes nothing."""
    from src.api.deps import workspace_role
    from src.db.models import RepoShare
    from src.users.roles import WORKSPACE_ADMIN_ROLES

    with _session() as s:
        row = s.get(RepoShare, share_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No such share.")
        owner = row.owner_workspace_id
        # Somebody who is not an administrator of the owner but passed the gate is
        # the platform side, and the trail says so.
        mine = workspace_role(user.id, owner) in WORKSPACE_ADMIN_ROLES
        reason = "owner" if mine else "superadmin"
        held = len(repo_shares.holders(s, row.id))
        ended = repo_shares.revoke_share(s, share_id, by=user.id, reason=reason)
        s.commit()
        detail = share_detail(s, share_id, users)
    if ended:
        repo_shares.audit_owner(
            "sharing.revoked", owner_workspace_id=owner, share_id=share_id,
            actor=user.email, actor_id=user.id, ip=client_ip(request),
            detail={"reason": reason, "holders": held})
    return detail


__all__ = [
    "ShareActivityOut", "ShareDetailOut", "ShareHolderOut", "ShareNarrowIn",
    "SharePersonOut", "ShareSummaryOut", "list_shares", "router", "share_detail",
]
