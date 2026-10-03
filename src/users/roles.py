"""Workspace roles — the one copy of the list, its order, and who may grant what.

`WorkspaceMember.role` is free text in the database (no enum, no CHECK), so
the allowed values live here. They used to be spelled out in four modules
(workspaces, invites, deps, the MCP identity resolver); adding a role meant
finding all four, and the one that got missed silently ranked the new role
as 0 — below viewer.

Ranks, lowest to highest:

    viewer 1 < member 2 < editor 3 < admin 4 < owner 5

What each role may do inside its workspace:

  * ``viewer``  — read only.
  * ``member``  — reads, and changes a review issue's status.
  * ``editor``  — the PROMPT EDITOR: agent system prompts, per-repo review
    policies (prompt template, folder rules, per-agent overrides) and
    analytics. No members, invites, teams, LLM keys, git connections, licence.
  * ``admin``   — editor powers plus member/viewer management, LLM keys and
    git connections (`WORKSPACE_ADMIN_ROLES`).
  * ``owner``   — admin, and the one who may delete the workspace.

Who may GRANT a role is a separate question with one answer, `can_change`:

  * owner / admin / editor (`PRIVILEGED_ROLES`) — the SUPERADMIN only:
    granting one, changing a member to or from one, removing one.
  * member / viewer (`DELEGABLE_ROLES`) — the superadmin, or an owner/admin of
    THAT workspace.

The superadmin is the env master account (CELMIS_MASTER_EMAIL +
CELMIS_MASTER_KEY, user id ``master-admin``) and nobody else. Other global
admins (`is_admin`, e.g. from OIDC_ADMIN_ROLE) keep their platform powers —
health, ops, compliance, seeing every workspace — but grant workspace roles
only as far as their own role in that workspace allows.

The web copy is ``web/lib/roles.ts``.
"""

from __future__ import annotations

import os
from typing import Any

WORKSPACE_ROLE_RANK: dict[str, int] = {
    "viewer": 1,
    "member": 2,
    "editor": 3,
    "admin": 4,
    "owner": 5,
}

#: Every role a member or an invite may carry.
VALID_WORKSPACE_ROLES: frozenset[str] = frozenset(WORKSPACE_ROLE_RANK)

#: Roles that administer a workspace (members, invites, settings, keys).
WORKSPACE_ADMIN_ROLES: frozenset[str] = frozenset({"owner", "admin"})

#: Roles that may edit agent prompts and review policies in their workspace.
PROMPT_EDITOR_ROLES: frozenset[str] = frozenset({"owner", "admin", "editor"})

#: Roles only the superadmin may grant, change to or from, or remove.
PRIVILEGED_ROLES: frozenset[str] = frozenset({"owner", "admin", "editor"})

#: Roles a workspace owner/admin may hand out and take back themselves.
DELEGABLE_ROLES: frozenset[str] = VALID_WORKSPACE_ROLES - PRIVILEGED_ROLES

#: Labels a TEAM membership may carry. A team role is a label inside a team —
#: what the team may do to a repository is `RepoTeamAccess.permission`, not
#: this — so it keeps its own extra value, ``reviewer``, on top of the
#: workspace ladder. One table here instead of a second hand-written list in
#: src/api/routers/teams.py, which had already fallen behind (no ``editor``).
TEAM_ROLES: frozenset[str] = VALID_WORKSPACE_ROLES | {"reviewer"}

#: The master account's fixed user id (see src/api/routers/auth.py).
MASTER_ADMIN_ID = "master-admin"


def role_rank(role: str | None) -> int:
    """Rank of `role`; unknown or missing roles rank 0, below viewer."""
    return WORKSPACE_ROLE_RANK.get(role or "", 0)


def master_email() -> str:
    """No in-code default: the master identity exists ONLY when the operator
    explicitly sets CELMIS_MASTER_EMAIL in the env (alongside the key)."""
    return os.environ.get("CELMIS_MASTER_EMAIL", "").strip().lower()


def is_master_email(email: str | None) -> bool:
    """True when ``email`` is the address CELMIS_MASTER_EMAIL names."""
    master = master_email()
    return bool(master and (email or "").strip().lower() == master)


def is_master_identity(user: Any) -> bool:
    """The account IS the master identity: the fixed id, or the master address.

    No flag checks — this is the "never mint a password-reset link for it"
    test, which must hold however the account is currently flagged.
    """
    if user is None:
        return False
    return getattr(user, "id", None) == MASTER_ADMIN_ID or is_master_email(
        getattr(user, "email", None))


def is_superadmin(user: Any) -> bool:
    """The env master account — and nobody else.

    Conditions, each closing a different door:

      * the identity is the master one: the fixed id ``master-admin``, or the
        address CELMIS_MASTER_EMAIL names (a password account the master login
        adopted keeps its own id);
      * matched by ADDRESS, the account carries no Google/OIDC binding. That
        is the rule `_master_login` adopts by — it refuses such an account —
        and the two must agree: otherwise an SSO-bound global admin whose
        address the operator later named as master became superadmin through
        the JWT or personal token it already held, without the key;
      * ``is_admin`` is set. Somebody who signs up with the master address
        before the operator's first master login is NOT the master: only
        `_master_login`, which checked the key, sets the flag on that account
        (and signup refuses the address);
      * the account is active.
    """
    if user is None:
        return False
    if not getattr(user, "is_admin", False) or not getattr(user, "is_active", True):
        return False
    if getattr(user, "id", None) == MASTER_ADMIN_ID:
        return True
    if getattr(user, "has_google", False) or getattr(user, "has_oidc", False):
        return False
    return is_master_email(getattr(user, "email", None))


def grantable_roles(actor: Any, actor_role: str | None) -> frozenset[str]:
    """Roles `actor` may hand out in a workspace where they hold `actor_role`."""
    if is_superadmin(actor):
        return VALID_WORKSPACE_ROLES
    if actor_role in WORKSPACE_ADMIN_ROLES:
        return DELEGABLE_ROLES
    return frozenset()


def can_change(
    actor: Any,
    actor_role: str | None,
    current_role: str | None,
    new_role: str | None,
) -> bool:
    """May `actor` move a membership from `current_role` to `new_role`?

    ``current_role=None`` is adding someone; ``new_role=None`` is removing
    them. `actor_role` is the actor's OWN role in that workspace (None when
    they are not a member). Every path that writes a membership asks this:
    PUT/DELETE members, invite create and accept, the superadmin Users page,
    workspace creation (the creator's owner row), and — as a bound, not a
    write — the workspace reset link.
    """
    if new_role is not None and new_role not in VALID_WORKSPACE_ROLES:
        return False
    if is_superadmin(actor):
        return True
    allowed = grantable_roles(actor, actor_role)
    if not allowed:
        return False
    # Both ends must be grantable: an admin can neither promote to editor nor
    # touch somebody who already is one — that is the "admin demotes the
    # owner" case, refused.
    if current_role is not None and current_role not in allowed:
        return False
    return new_role is None or new_role in allowed
