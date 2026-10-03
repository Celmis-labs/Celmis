"""Writing a workspace membership — the one path every route goes through.

Members can be added, re-roled or removed from five places: PUT/DELETE
/api/workspaces/{id}/members, an invite created for an existing account, an
invite accepted, and the superadmin's Users page. Each used to write the row
itself, and each had its own idea of who was allowed to — so "an admin can
demote the owner" was true on one route and would have stayed true on another
after a fix to the first.

Now every one of them calls `change_membership`, which asks the single rule
in src/users/roles.py (`can_change`), writes the row, and records the change
in the audit log (actor, user, workspace, old → new).
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Workspace, WorkspaceMember
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
                "Only the superadmin can grant, change or remove owner, admin "
                "or editor; workspace owners and admins manage members and "
                "viewers."
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
    if await session.get(Workspace, ws_id) is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    granting = authority if authority is not None else actor
    row = await session.get(WorkspaceMember, (ws_id, user_id))
    old = row.role if row is not None else None
    if old == new_role:
        return old, new_role
    refuse_unless_can_change(
        granting, await actor_role(session, granting, ws_id), old, new_role)

    if new_role is None:
        if row is not None:
            await session.delete(row)
    elif row is None:
        session.add(WorkspaceMember(workspace_id=ws_id, user_id=user_id, role=new_role))
    else:
        row.role = new_role
    await session.commit()

    from src.security.audit import record_action

    record_action(
        action="workspace.member_role_changed",
        actor=actor.email, actor_id=actor.id, workspace_id=ws_id,
        target=user_id, ip=ip,
        detail={
            "old_role": old, "new_role": new_role, "via": via,
            **({"granted_by": granting.email} if granting is not actor else {}),
        },
    )
    logger.info(
        "workspace_member_changed ws=%s user=%s %s->%s via=%s by=%s",
        ws_id, user_id, old, new_role, via, actor.email,
    )
    return old, new_role


__all__ = ["actor_role", "change_membership", "refuse_unless_can_change"]
