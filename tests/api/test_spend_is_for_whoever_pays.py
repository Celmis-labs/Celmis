"""/api/spend/* — what a workspace spent, and its budget, are read by the
workspace's owner and admins (and global admins), nobody below.

Same rule as the agent (`get_spend`, `get_budget`), MCP and the Usage page.
`/api/usage/summary` (the member's own activity) is a different surface and is
not covered here.
"""

from __future__ import annotations

import pytest

from tests.api.rbac_world import world

PATHS = ["/api/spend/summary", "/api/spend/daily", "/api/spend/budget"]


def _extra():
    from src.api.routers import spend

    return (spend.router,)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("who", ["viewer_a", "member_a", "editor_a"])
async def test_below_admin_is_refused(tmp_path, monkeypatch, who, path):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        r = await w.client.get(path, headers=w.h(who, "ws-a"))
        assert r.status_code == 403, (who, path, r.text)


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("who", ["admin_a", "owner_a", "gadmin"])
async def test_owner_admin_and_global_admin_are_not_refused(tmp_path, monkeypatch, who, path):
    async with world(tmp_path, monkeypatch, extra_routers=_extra()) as w:
        r = await w.client.get(path, headers=w.h(who, "ws-a"))
        assert r.status_code != 403, (who, path, r.text)
