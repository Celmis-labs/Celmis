"""`editor`: a real workspace role, ranked between member and admin, with no
admin powers.

The role list used to be spelled out in four modules. A role added to three
of them is accepted by one route and ranked 0 (below viewer) by another, so
these tests ask every consumer, not just the table.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.users.roles import (
    VALID_WORKSPACE_ROLES,
    WORKSPACE_ADMIN_ROLES,
    WORKSPACE_ROLE_RANK,
    role_rank,
)


def test_the_ranks_are_the_agreed_order():
    assert WORKSPACE_ROLE_RANK == {
        "viewer": 1, "member": 2, "editor": 3, "admin": 4, "owner": 5,
    }
    assert role_rank(None) == 0 and role_rank("nonsense") == 0


def test_editor_is_not_a_workspace_admin_role():
    assert "editor" not in WORKSPACE_ADMIN_ROLES
    assert {"owner", "admin"} == WORKSPACE_ADMIN_ROLES


def test_every_consumer_uses_the_shared_table():
    from src.api.routers import invites, workspaces
    from src.mcp_server import identity

    assert "editor" in invites._VALID_ROLES
    assert "editor" in workspaces._VALID_ROLES
    assert workspaces._ROLE_RANK["editor"] == 3
    assert identity._WS_RANK["editor"] == 3
    assert set(VALID_WORKSPACE_ROLES) == set(WORKSPACE_ROLE_RANK)


class _Session:
    def __init__(self, role: str | None):
        self._role = role

    async def get(self, _model, _key):
        return None if self._role is None else SimpleNamespace(role=self._role)


@pytest.mark.parametrize("role", ["viewer", "member", "editor"])
def test_editor_and_below_cannot_administer_a_workspace(role):
    from src.api.routers.workspaces import _require_ws_admin

    user = SimpleNamespace(id="u1", is_admin=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_require_ws_admin(_Session(role), user, "ws-1"))
    assert exc.value.status_code == 403


@pytest.mark.parametrize("role", ["admin", "owner"])
def test_admin_and_owner_still_can(role):
    from src.api.routers.workspaces import _require_ws_admin

    user = SimpleNamespace(id="u1", is_admin=False)
    asyncio.run(_require_ws_admin(_Session(role), user, "ws-1"))


def test_the_web_copy_matches():
    """web/lib/roles.ts is the UI's copy; a role missing there is a role the
    admin page cannot assign."""
    import re
    from pathlib import Path

    ts = (Path(__file__).resolve().parents[2] / "web" / "lib" / "roles.ts").read_text()
    m = re.search(r"WS_ROLES\s*=\s*\[([^\]]*)\]", ts)
    assert m is not None
    web_roles = re.findall(r'"([a-z]+)"', m.group(1))
    assert web_roles == sorted(WORKSPACE_ROLE_RANK, key=WORKSPACE_ROLE_RANK.__getitem__)
