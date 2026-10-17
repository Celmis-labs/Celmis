"""Who may see, and who may maintain, a knowledge pack — the one copy of the rule.

A pack lives in ONE workspace and carries an ``audience``:

    workspace    every current member of the workspace
    teams        workspace owners and admins, the teams the pack is granted to,
                 and the people named on it
    restricted   ONLY the teams the pack is granted to and the people named on
                 it — workspace owners/admins do not see it, nor does a global
                 admin; only the superadmin sees everything

The principal is built from live rows (:func:`principal_for`,
:func:`principal_for_sync`):

    su    the superadmin (`is_superadmin`)  — sees and maintains everything
    ga    a global admin that is not the superadmin
    role  the person's role in THIS workspace; None when they are not a CURRENT
          member, and for a role string this build does not know
    teams the ids of teams of THIS workspace the person is in — counted only
          while ``role`` is not None, so a `team_members` row that outlives
          the workspace membership grants nothing

The rule exists twice, on purpose: as pure functions (:func:`can_see`,
:func:`can_maintain`) that a unit test walks exhaustively, and as an SQL
predicate (:func:`visible_clause`, :func:`visible_packs_query`) that every list,
search, count and lookup applies IN THE QUERY, so a hidden pack is never loaded
and then filtered. A test holds the two equal over every combination; the store
also asks the Python predicate of what the SQL returned, so a divergence
fails closed.

Hidden == missing. Every refusal to show a pack is :class:`PackNotAccessible`
whose text is :data:`PACK_NOT_FOUND`, whether the pack does not exist, belongs
to another workspace or is hidden from the caller. Nothing here ever echoes a
pack, team or person name.

Fail closed: a database error, an unknown role or audience, an inactive or
unknown user build a principal that sees nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, NamedTuple

from sqlalchemy import and_, exists, false, or_, select, true
from sqlalchemy.sql.elements import ColumnElement

from src.users.roles import (
    PACK_AUDIENCES,
    PACK_AUTHOR_ROLES,
    PACK_MAINTAINER_ROLES,
    VALID_WORKSPACE_ROLES,
    WORKSPACE_ADMIN_ROLES,
    is_superadmin,
)

logger = logging.getLogger(__name__)

#: The one sentence for a pack that is hidden, missing or in another workspace.
PACK_NOT_FOUND = "pack not found or not accessible"

#: The only grant permission that lets a team or a person maintain a pack.
MAINTAIN = "maintain"


class PackNotAccessible(LookupError):
    """The pack is hidden from the caller, does not exist, or is another
    workspace's. ``str(e)`` is :data:`PACK_NOT_FOUND` whatever it was built
    with, so no caller can leak a name or a reason through it."""

    def __init__(self, *_ignored: object) -> None:
        super().__init__(PACK_NOT_FOUND)

    def __str__(self) -> str:
        return PACK_NOT_FOUND


class TeamGrant(NamedTuple):
    """A team a pack is granted to (the shape :func:`can_see` reads; a
    ``KnowledgePackTeam`` row has the same two attributes)."""

    team_id: str
    permission: str


class MemberGrant(NamedTuple):
    """A person named on a pack (a ``KnowledgePackMember`` row has the same
    ``permission`` attribute)."""

    permission: str


@dataclass(frozen=True)
class PackPrincipal:
    """Who is asking, as far as packs of ONE workspace are concerned."""

    user_id: str
    superadmin: bool
    global_admin: bool
    role: str | None
    team_ids: frozenset[str]


#: Sees nothing, maintains nothing. What every failure builds.
NOBODY = PackPrincipal(
    user_id="", superadmin=False, global_admin=False, role=None, team_ids=frozenset())


# ─── The pure rule ───────────────────────────────────────────────────


def _role(principal: PackPrincipal) -> str | None:
    """The role if this build knows it; an unknown string is no role at all."""
    return principal.role if principal.role in VALID_WORKSPACE_ROLES else None


def _my_grants(principal: PackPrincipal, grants: Iterable[Any]) -> list[Any]:
    """The grants given to a team the principal is in — none at all while they
    hold no role in the workspace."""
    if _role(principal) is None:
        return []
    mine = principal.team_ids
    return [g for g in (grants or ()) if getattr(g, "team_id", None) in mine]


def can_see(
    principal: PackPrincipal,
    pack_row: Any,
    grants: Iterable[Any] = (),
    member_row: Any = None,
) -> bool:
    """May the principal see the pack? ``pack_row`` has ``audience``; ``grants``
    are the pack's team grants (each with ``team_id``); ``member_row`` is the
    principal's own row on the pack, or None."""
    if principal.superadmin:
        return True
    role = _role(principal)
    ga = principal.global_admin
    if role is None and not ga:
        return False
    audience = getattr(pack_row, "audience", None)
    if audience not in PACK_AUDIENCES:
        return False                       # an audience this build does not know
    if audience == "workspace":
        return True
    named = role is not None and (member_row is not None or bool(_my_grants(principal, grants)))
    if audience == "teams":
        return ga or role in WORKSPACE_ADMIN_ROLES or named
    return named                           # restricted


def can_maintain(
    principal: PackPrincipal,
    pack_row: Any,
    grants: Iterable[Any] = (),
    member_row: Any = None,
) -> bool:
    """May the principal change the pack? Only meaningful once :func:`can_see`
    said yes — and it re-asks, so it never answers yes for a hidden pack."""
    if principal.superadmin:
        return True
    if not can_see(principal, pack_row, grants, member_row):
        return False
    role = _role(principal)
    if role in PACK_MAINTAINER_ROLES:      # a viewer reads and never writes, whatever the pack names
        if member_row is not None and getattr(member_row, "permission", None) == MAINTAIN:
            return True
        if any(getattr(g, "permission", None) == MAINTAIN
               for g in _my_grants(principal, grants)):
            return True
    audience = getattr(pack_row, "audience", None)
    return (audience in ("workspace", "teams")
            and (principal.global_admin or role in WORKSPACE_ADMIN_ROLES))


def can_create(principal: PackPrincipal) -> bool:
    """May the principal create a pack in this workspace? The superadmin, a
    global admin, or a member whose role is an author role."""
    if principal.superadmin or principal.global_admin:
        return True
    return _role(principal) in PACK_AUTHOR_ROLES


# ─── The same rule as SQL ────────────────────────────────────────────


def _named(principal: PackPrincipal, permission: str | None = None) -> ColumnElement[bool]:
    """EXISTS: the principal is on the pack by a granted team or by name
    (optionally only with ``permission``). False while they hold no role."""
    from src.db.models import (
        KnowledgePack,
        KnowledgePackMember,
        KnowledgePackTeam,
        Team,
    )

    if _role(principal) is None:
        return false()
    by_name = select(1).where(
        KnowledgePackMember.pack_id == KnowledgePack.id,
        KnowledgePackMember.user_id == principal.user_id,
    )
    if permission is not None:
        by_name = by_name.where(KnowledgePackMember.permission == permission)
    clauses = [exists(by_name)]
    if principal.team_ids:
        # INNER JOIN on Team in the pack's own workspace: a grant to a team
        # that was deleted, or that is another workspace's, matches nothing.
        by_team = (
            select(1)
            .select_from(KnowledgePackTeam)
            .join(Team, Team.id == KnowledgePackTeam.team_id)
            .where(
                KnowledgePackTeam.pack_id == KnowledgePack.id,
                Team.workspace_id == KnowledgePack.workspace_id,
                KnowledgePackTeam.team_id.in_(sorted(principal.team_ids)),
            )
        )
        if permission is not None:
            by_team = by_team.where(KnowledgePackTeam.permission == permission)
        clauses.append(exists(by_team))
    return or_(*clauses)


def visible_clause(principal: PackPrincipal) -> ColumnElement[bool]:
    """The SQL boolean over ``KnowledgePack`` that :func:`can_see` computes.
    Does not scope by workspace — :func:`visible_packs_query` does."""
    from src.db.models import KnowledgePack

    if principal.superadmin:
        return true()
    role = _role(principal)
    if role is None and not principal.global_admin:
        return false()
    audience = KnowledgePack.audience
    admin_view = principal.global_admin or role in WORKSPACE_ADMIN_ROLES
    named = _named(principal)
    return or_(
        audience == "workspace",
        and_(audience == "teams", true() if admin_view else named),
        and_(audience == "restricted", named),
    )


def maintain_clause(principal: PackPrincipal) -> ColumnElement[bool]:
    """The SQL boolean that :func:`can_maintain` computes (always together with
    :func:`visible_clause`)."""
    from src.db.models import KnowledgePack

    if principal.superadmin:
        return true()
    role = _role(principal)
    admin_view = principal.global_admin or role in WORKSPACE_ADMIN_ROLES
    named = _named(principal, MAINTAIN) if role in PACK_MAINTAINER_ROLES else false()
    return and_(
        visible_clause(principal),
        or_(
            named,
            and_(KnowledgePack.audience.in_(("workspace", "teams")),
                 true() if admin_view else false()),
        ),
    )


def visible_packs_query(principal: PackPrincipal, ws_id: str):
    """``SELECT knowledge_packs`` of ``ws_id`` the principal may see. Every list,
    search and lookup starts here, so a hidden pack is never read."""
    from src.db.models import KnowledgePack

    return select(KnowledgePack).where(
        KnowledgePack.workspace_id == ws_id, visible_clause(principal))


# ─── Building the principal ──────────────────────────────────────────


async def principal_for(session, user: Any, ws_id: str) -> PackPrincipal:
    """The principal for ``user`` in workspace ``ws_id``, from live rows.

    Any failure builds :data:`NOBODY`: a database that cannot be read shows no
    pack to anybody.
    """
    from src.db.models import Team, TeamMember, WorkspaceMember

    try:
        if user is None or not getattr(user, "id", None):
            return NOBODY
        if not getattr(user, "is_active", True):
            return NOBODY
        su = is_superadmin(user)
        ga = bool(getattr(user, "is_admin", False)) and not su
        member = await session.get(WorkspaceMember, (ws_id, user.id))
        role = member.role if member is not None and member.role in VALID_WORKSPACE_ROLES \
            else None
        teams: frozenset[str] = frozenset()
        if role is not None:
            rows = await session.scalars(
                select(TeamMember.team_id)
                .join(Team, Team.id == TeamMember.team_id)
                .where(TeamMember.user_id == user.id, Team.workspace_id == ws_id))
            teams = frozenset(rows.all())
        return PackPrincipal(user.id, su, ga, role, teams)
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("pack_principal_failed err=%s", type(exc).__name__)
        return NOBODY


def principal_for_sync(user_id: str, ws_id: str) -> PackPrincipal:
    """:func:`principal_for` for callers without a session (MCP tool bodies).

    Reads the pooled engine of the access resolver and loads the user from the
    user store to ask whether they are the superadmin. An unknown or inactive
    user, a missing workspace and any error build :data:`NOBODY`.
    """
    try:
        if not user_id or not ws_id:
            return NOBODY
        from sqlalchemy.orm import Session

        from src.access.resolver import _sync_engine
        from src.db.models import Team, TeamMember, WorkspaceMember
        from src.users.store import get_user_store

        user = get_user_store().get_by_id(user_id)
        if user is None or not getattr(user, "is_active", True):
            return NOBODY
        su = is_superadmin(user)
        ga = bool(getattr(user, "is_admin", False)) and not su
        with Session(_sync_engine()) as s:
            member = s.get(WorkspaceMember, (ws_id, user_id))
            role = member.role if member is not None and member.role in VALID_WORKSPACE_ROLES \
                else None
            teams: frozenset[str] = frozenset()
            if role is not None:
                teams = frozenset(s.scalars(
                    select(TeamMember.team_id)
                    .join(Team, Team.id == TeamMember.team_id)
                    .where(TeamMember.user_id == user_id, Team.workspace_id == ws_id)
                ).all())
        return PackPrincipal(user_id, su, ga, role, teams)
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("pack_principal_failed err=%s", type(exc).__name__)
        return NOBODY


__all__ = [
    "MAINTAIN",
    "NOBODY",
    "PACK_NOT_FOUND",
    "MemberGrant",
    "PackNotAccessible",
    "PackPrincipal",
    "TeamGrant",
    "can_create",
    "can_maintain",
    "can_see",
    "maintain_clause",
    "principal_for",
    "principal_for_sync",
    "visible_clause",
    "visible_packs_query",
]
