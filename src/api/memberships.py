"""Writing a workspace membership — the one path every route goes through.

Members can be added, re-roled or removed from six places: PUT/DELETE
/api/workspaces/{id}/members, an invite created for an existing account, an
invite accepted, the superadmin's Users page, and the owner row of a newly
created workspace — and an approved access request, which writes several at
once (`change_memberships`, all or nothing). Each used to write the row
itself, and each had its own idea of who was allowed to — so "an admin can
demote the owner" was true on one route and would have stayed true on another
after a fix to the first.

Now every one of them calls `change_membership`, which asks the single rule
in src/users/roles.py (`can_change`), writes the row, and records the change
in the audit log (actor, user, workspace, old → new).

Two pieces of state hang off a membership and are kept true here, in the
same transaction, so no route can forget them:

  * a REMOVAL revokes the live email-bound invites addressed to that person
    for that workspace. Otherwise a stale invite would put them straight back
    — silently, at their next verified Google/SSO sign-in.
  * a GRANT to a team workspace (not the person's own personal one) closes
    their pending access request as approved, listing what was granted.
    Otherwise an admin who adds somebody from /admin/users leaves the request
    pending forever, the requester's page polling for a decision that never
    comes. The approval route itself (via="access_request") decides its own
    row and is left alone.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import AccessRequest, Workspace, WorkspaceInvite, WorkspaceMember
from src.users import User
from src.users.roles import VALID_WORKSPACE_ROLES, can_change

logger = logging.getLogger(__name__)


async def actor_role(session: AsyncSession, actor: User, ws_id: str) -> str | None:
    """`actor`'s own role in `ws_id`, or None when they are not a member."""
    m = await session.get(WorkspaceMember, (ws_id, actor.id))
    return m.role if m is not None else None


def refuse_unless_can_change(
    actor: User, role_of_actor: str | None,
    current_role: str | None, new_role: str | None,
) -> None:
    """422 for a role that does not exist, 403 for one the actor may not give
    (or a member the actor may not touch)."""
    if new_role is not None and new_role not in VALID_WORKSPACE_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"role must be one of {sorted(VALID_WORKSPACE_ROLES)}",
        )
    if not can_change(actor, role_of_actor, current_role, new_role):
        raise HTTPException(
            status_code=403,
            detail=(
                "Only the superadmin can grant, change or remove owner; the "
                "workspace owner manages admins and editors; owners and admins "
                "manage members and viewers."
            ),
        )


async def change_membership(
    session: AsyncSession,
    *,
    actor: User,
    ws_id: str,
    user_id: str,
    new_role: str | None,
    via: str,
    ip: str | None = None,
    authority: User | None = None,
) -> tuple[str | None, str | None]:
    """Move `user_id` in `ws_id` to `new_role` (None removes). Returns (old, new).

    `authority` is whose right to grant is checked — the actor themselves,
    except when an invite is accepted: there the person clicking is the
    actor, and the right is the inviter's, re-checked at the moment of use.
    Commits. Raises 404 for an unknown workspace, 403/422 per
    `refuse_unless_can_change`.
    """
    [(_, old, new)] = await change_memberships(
        session, actor=actor, user_id=user_id, changes=[(ws_id, new_role)],
        via=via, ip=ip, authority=authority,
    )
    return old, new


async def change_memberships(
    session: AsyncSession,
    *,
    actor: User,
    user_id: str,
    changes: list[tuple[str, str | None]],
    via: str,
    ip: str | None = None,
    authority: User | None = None,
    extra_detail: dict | None = None,
) -> list[tuple[str, str | None, str | None]]:
    """Several (workspace, role) changes for ONE person, all or nothing.

    Every change is checked (workspace exists, `can_change`) BEFORE any row is
    written, and the rows are written in one commit — so an approval that
    names five workspaces and one it may not grant writes none of them. The
    audit rows are recorded after the commit, one per membership that actually
    changed, exactly as `change_membership` records a single one (it is this
    function with one item). Anything the caller staged on `session` before
    the call is committed in the same transaction.
    """
    granting = authority if authority is not None else actor
    planned: list[tuple[str, WorkspaceMember | None, str | None, str | None]] = []
    workspaces: dict[str, Workspace] = {}
    for ws_id, new_role in changes:
        ws = await session.get(Workspace, ws_id)
        if ws is None:
            raise HTTPException(status_code=404, detail="workspace not found")
        workspaces[ws_id] = ws
        row = await session.get(WorkspaceMember, (ws_id, user_id))
        old = row.role if row is not None else None
        if old != new_role:
            refuse_unless_can_change(
                granting, await actor_role(session, granting, ws_id), old, new_role)
        planned.append((ws_id, row, old, new_role))

    for ws_id, row, old, new_role in planned:
        if old == new_role:
            continue
        if new_role is None:
            if row is not None:
                await session.delete(row)
        elif row is None:
            session.add(WorkspaceMember(workspace_id=ws_id, user_id=user_id, role=new_role))
        else:
            row.role = new_role
    revoked = await _revoke_invites_on_removal(session, user_id=user_id, planned=planned)
    resolved = None
    if via != "access_request":
        resolved = await _resolve_pending_request(
            session, actor=actor, user_id=user_id, planned=planned,
            workspaces=workspaces, via=via)
    await session.commit()

    from src.security.audit import record_action

    for ws_id, invite_ids in revoked.items():
        record_action(
            action="invite.revoked_on_removal", actor=actor.email, actor_id=actor.id,
            workspace_id=ws_id, target=user_id, ip=ip,
            detail={"invite_ids": invite_ids, "via": via},
        )
    if resolved is not None:
        record_action(
            action="access_request.resolved_by_grant", actor=actor.email,
            actor_id=actor.id, target=user_id, ip=ip,
            detail={"request_id": resolved.id, "via": via,
                    "grants": [{"workspace_id": g["workspace_id"], "role": g["role"]}
                               for g in resolved.grants]},
        )

    for ws_id, _row, old, new_role in planned:
        if old == new_role:
            continue
        record_action(
            action="workspace.member_role_changed",
            actor=actor.email, actor_id=actor.id, workspace_id=ws_id,
            target=user_id, ip=ip,
            detail={
                "old_role": old, "new_role": new_role, "via": via,
                **({"granted_by": granting.email} if granting is not actor else {}),
                **(extra_detail or {}),
            },
        )
        logger.info(
            "workspace_member_changed ws=%s user=%s %s->%s via=%s by=%s",
            ws_id, user_id, old, new_role, via, actor.email,
        )
    return [(ws_id, old, new_role) for ws_id, _row, old, new_role in planned]


async def _revoke_invites_on_removal(
    session: AsyncSession, *, user_id: str,
    planned: list[tuple[str, WorkspaceMember | None, str | None, str | None]],
) -> dict[str, list[str]]:
    """Stage `revoked=True` on the live email-bound invites for a removed
    member's address in that workspace. Returns {workspace_id: [invite ids]}."""
    removed = [ws_id for ws_id, _row, old, new in planned if old is not None and new is None]
    if not removed:
        return {}
    try:
        from src.users.store import get_user_store

        target = get_user_store().get_by_id(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("invite_revoke_on_removal_lookup_failed user=%s err=%s", user_id, exc)
        return {}
    email = ((target.email if target else "") or "").strip().lower()
    if not email:
        return {}
    rows = (await session.scalars(
        select(WorkspaceInvite).where(
            WorkspaceInvite.workspace_id.in_(removed),
            func.lower(WorkspaceInvite.email) == email,
            WorkspaceInvite.revoked.is_(False),
            WorkspaceInvite.used_count < WorkspaceInvite.max_uses,
        )
    )).all()
    out: dict[str, list[str]] = {}
    for inv in rows:
        inv.revoked = True
        out.setdefault(inv.workspace_id, []).append(inv.id)
    return out


async def _resolve_pending_request(
    session: AsyncSession, *, actor: User, user_id: str,
    planned: list[tuple[str, WorkspaceMember | None, str | None, str | None]],
    workspaces: dict[str, Workspace], via: str,
) -> AccessRequest | None:
    """Stage the user's pending access request as approved when this change
    gives them a team workspace they were not in. Returns the row, or None."""
    from src.api.workspace_provision import personal_slug

    granted = [
        (workspaces[ws_id], new) for ws_id, _row, old, new in planned
        if old is None and new is not None
        and workspaces[ws_id].slug != personal_slug(user_id)
    ]
    if not granted:
        return None
    req = (await session.scalars(
        select(AccessRequest).where(
            AccessRequest.user_id == user_id, AccessRequest.status == "pending",
        ).with_for_update()
    )).first()
    # Re-checked in Python: the row may already be in this session's identity
    # map with a decision staged on it.
    if req is None or req.status != "pending":
        return None
    now = datetime.now(UTC)
    req.status = "approved"
    req.decided_by = actor.email
    req.decided_at = now
    req.updated_at = now
    req.decision_note = f"Granted directly ({via})"
    req.grants = [
        {"workspace_id": ws.id, "workspace_name": ws.name,
         "workspace_slug": ws.slug, "role": role}
        for ws, role in granted
    ]
    return req


__all__ = [
    "actor_role", "change_membership", "change_memberships", "refuse_unless_can_change",
]
