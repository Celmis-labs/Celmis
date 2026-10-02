"""Workspace roles — the one copy of the list and its order.

`WorkspaceMember.role` is free text in the database (no enum, no CHECK), so
the allowed values live here. They used to be spelled out in four modules
(workspaces, invites, deps, the MCP identity resolver); adding a role meant
finding all four, and the one that got missed silently ranked the new role
as 0 — below viewer.

Ranks, lowest to highest:

    viewer 1 < member 2 < editor 3 < admin 4 < owner 5

``editor`` can see workspace analytics but holds NO workspace-admin powers:
`require_workspace_admin` and every member/invite/settings route still
require owner or admin (`WORKSPACE_ADMIN_ROLES`).

The web copy is ``web/lib/roles.ts``.
"""

from __future__ import annotations

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


def role_rank(role: str | None) -> int:
    """Rank of `role`; unknown or missing roles rank 0, below viewer."""
    return WORKSPACE_ROLE_RANK.get(role or "", 0)
