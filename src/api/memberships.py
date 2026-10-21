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

Four pieces of state hang off a membership and are kept true here, in the
same transaction, so no route can forget them:

  * a REMOVAL revokes the live email-bound invites addressed to that person
    for that workspace. Otherwise a stale invite would put them straight back
    — silently, at their next verified Google/SSO sign-in.
  * a REMOVAL also takes the person out of the teams OF THAT WORKSPACE and ends
    what they held on its knowledge packs (their names on packs, the publish
    tokens and the invite links they made). Otherwise the rights would lie
    dormant and come back, unasked, the day the person is added again.
  * a REMOVAL also revokes the MCP tokens the person holds for THAT workspace
    (every kind: issued by an admin, minted by themselves, an OAuth grant). The
    membership check already refuses such a token while the person is out, but
    the row stayed live, so adding them back, even as a viewer, would have
    brought the very same token back to life. A role CHANGE revokes nothing:
    a token never reads the role. The revoked row records why
    (``revoked_by = membership_removed:<who>``) and the audit log gets one
    ``mcp.token_revoked_on_removal`` row per workspace. This process forgets the
    cached rows at once; another worker's cache catches up within
    ``token_store.CACHE_TTL_SECONDS`` (30 s), and until then it is the
    membership check that refuses the token, on every call and uncached, for
    everybody but a global admin (who needs no membership).
  * a GRANT to a team workspace (not the person's own personal one) closes
    their pending access request as approved, listing what was granted.
    Otherwise an admin who adds somebody from /admin/users leaves the request
    pending forever, the requester's page polling for a decision that never
    comes. The approval route itself (via="access_request") decides its own
    row and is left alone.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.db.models import (
    AccessRequest,
    Team,
    TeamMember,
    Workspace,
    WorkspaceInvite,
    WorkspaceMember,
)
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
    await _leave_teams_on_removal(session, user_id=user_id, planned=planned)
    await _release_packs_on_removal(session, actor=actor, user_id=user_id, planned=planned)
    revoked_tokens = await _revoke_tokens_on_removal(
        session, actor=actor, user_id=user_id, planned=planned)
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
    record_token_revocations(
        revoked_tokens, actor=actor.email, actor_id=actor.id, user_id=user_id,
        via=via, ip=ip)
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


def _live_invites_query(email: str, ws_ids: Sequence[str]):
    """The email-bound invites of ``ws_ids`` that could still be used."""
    return select(WorkspaceInvite).where(
        WorkspaceInvite.workspace_id.in_(list(ws_ids)),
        func.lower(WorkspaceInvite.email) == email,
        WorkspaceInvite.revoked.is_(False),
        WorkspaceInvite.used_count < WorkspaceInvite.max_uses,
    )


def _team_rows_statement(user_id: str, ws_ids: Sequence[str]):
    """Delete a person's rows in the teams OF ``ws_ids`` (and of no other workspace)."""
    return delete(TeamMember).where(
        TeamMember.user_id == user_id,
        TeamMember.team_id.in_(select(Team.id).where(Team.workspace_id.in_(list(ws_ids)))))


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
    rows = (await session.scalars(_live_invites_query(email, removed))).all()
    out: dict[str, list[str]] = {}
    for inv in rows:
        inv.revoked = True
        out.setdefault(inv.workspace_id, []).append(inv.id)
    return out


async def _leave_teams_on_removal(
    session: AsyncSession, *, user_id: str,
    planned: list[tuple[str, WorkspaceMember | None, str | None, str | None]],
) -> None:
    """Take a removed member out of the teams of the workspaces they left, and
    out of those only. A team is a subset of its workspace's people; a row left
    behind would hand the team's grants (repositories, packs) back to the person
    the day they are added to the workspace again."""
    removed = [ws_id for ws_id, _row, old, new in planned if old is not None and new is None]
    if not removed:
        return
    await session.execute(_team_rows_statement(user_id, removed))


def release_account_sync(session: Session, *, user_id: str, email: str) -> dict[str, list[str]]:
    """What a person holds in every workspace they belong to, ended - for the
    deletion of their account, which has a synchronous session and no actor to
    ask. It is the removal rules of :func:`change_memberships` applied to every
    workspace at once: they leave the teams of those workspaces and the live invites
    made out to their address are revoked, so nothing outlives the account that a
    person added later under the same address would pick up. The membership rows
    themselves, and the packs (see ``pack_store.erase_user_sync``), are the
    caller's. Returns ``{workspace_id: [invite ids]}`` for the audit."""
    ws_ids = list(session.scalars(
        select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user_id)).all())
    if not ws_ids:
        return {}
    session.execute(_team_rows_statement(user_id, ws_ids))
    address = (email or "").strip().lower()
    revoked: dict[str, list[str]] = {}
    if address:
        for inv in session.scalars(_live_invites_query(address, ws_ids)).all():
            inv.revoked = True
            revoked.setdefault(inv.workspace_id, []).append(inv.id)
    return revoked


def revoke_account_tokens_sync(session: Session, *, user_id: str) -> dict[str, list[str]]:
    """Every MCP token a person holds, in every workspace, revoked in the
    caller's transaction - for the deletion of their account (a synchronous
    session, no actor), the counterpart of what a removal does for one
    workspace. The rows are kept as a record (``revoked_by = account_deleted:<id>``).
    Returns ``{workspace_id: [token ids]}`` for :func:`record_token_revocations`."""
    from src.mcp_server import token_store

    return token_store.revoke_held(session, by=f"account_deleted:{user_id}", user_id=user_id)


def revoke_workspace_tokens_sync(
    session: Session, *, by: str, workspace_ids: list[str],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Every MCP token bound to workspaces that are about to be deleted, whoever
    holds it, revoked in the caller's transaction: the person tokens (``pat``,
    ``cli``, ``self``, ``oauth_grant``, internal) and the project tokens
    (``cmcp_``), which have no person and so are ended by nothing else.
    Returns ``(person tokens, project tokens)``, each ``{workspace_id: [token ids]}``,
    for :func:`record_token_revocations`. Call it BEFORE the workspace row is deleted."""
    from src.mcp_server import project_tokens, token_store

    return (
        token_store.revoke_held(session, by=by, workspace_ids=workspace_ids),
        project_tokens.revoke_held(session, workspace_ids=workspace_ids),
    )


def merge_revocations(*parts: dict[str, list[str]]) -> dict[str, list[str]]:
    """``{workspace_id: [token ids]}`` dicts joined, an id listed once."""
    out: dict[str, list[str]] = {}
    for part in parts:
        for ws_id, ids in part.items():
            merged = out.setdefault(ws_id, [])
            merged.extend(i for i in ids if i not in merged)
    return out


async def _release_packs_on_removal(
    session: AsyncSession, *, actor: User, user_id: str,
    planned: list[tuple[str, WorkspaceMember | None, str | None, str | None]],
) -> None:
    """End what a removed member holds on the knowledge packs of the workspaces
    they left: their names on packs go and the publish tokens and invite links
    they made are revoked, so that adding them back later — even as a viewer — does not bring any of it
    back to life. A restricted pack left without a maintainer is kept as it is
    and reported in the audit log (`codebase.pack_orphaned`); see
    `src.knowledge.store.release_person`."""
    removed = [ws_id for ws_id, _row, old, new in planned if old is not None and new is None]
    if not removed:
        return
    from src.knowledge import store as pack_store

    await pack_store.release_person(
        session, user_id, removed, actor=actor.email, actor_id=actor.id)


async def _revoke_tokens_on_removal(
    session: AsyncSession, *, actor: User, user_id: str,
    planned: list[tuple[str, WorkspaceMember | None, str | None, str | None]],
) -> dict[str, list[str]]:
    """Stage `revoked_at` on the MCP tokens a removed member holds for the
    workspaces they left, and for those only. Returns ``{workspace_id: [token
    ids]}``; :func:`record_token_revocations` audits them and forgets the cached
    rows once the transaction has committed."""
    removed = [ws_id for ws_id, _row, old, new in planned if old is not None and new is None]
    if not removed:
        return {}
    from src.mcp_server import token_store

    return await session.run_sync(lambda s: token_store.revoke_held(
        s, by=f"membership_removed:{actor.id}", user_id=user_id, workspace_ids=removed))


def record_token_revocations(
    revoked: dict[str, list[str]], *, actor: str, actor_id: str, user_id: str | None,
    via: str, ip: str | None = None, project_revoked: dict[str, list[str]] | None = None,
) -> None:
    """After the commit that revoked ``revoked`` (person tokens) and
    ``project_revoked`` (project tokens): drop the person rows from this
    process's token cache and write one audit row per workspace, with the
    project token ids under their own key. Never raises."""
    project_revoked = project_revoked or {}
    if not revoked and not project_revoked:
        return
    try:
        from src.mcp_server import token_store
        from src.security.audit import record_action

        for ws_id in dict.fromkeys([*revoked, *project_revoked]):
            token_ids = revoked.get(ws_id, [])
            for token_id in token_ids:
                token_store.invalidate(token_id)
            detail: dict = {"token_ids": token_ids, "via": via}
            if project_revoked.get(ws_id):
                detail["project_token_ids"] = project_revoked[ws_id]
            record_action(
                action="mcp.token_revoked_on_removal", actor=actor, actor_id=actor_id,
                workspace_id=ws_id, target=user_id or ws_id, ip=ip, detail=detail,
            )
    except Exception as exc:  # noqa: BLE001 - the bookkeeping never undoes a removal
        logger.warning("mcp_token_revoke_audit_failed err=%s", type(exc).__name__)


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
    "actor_role", "change_membership", "change_memberships", "merge_revocations",
    "record_token_revocations", "refuse_unless_can_change", "release_account_sync",
    "revoke_account_tokens_sync", "revoke_workspace_tokens_sync",
]
