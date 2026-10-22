# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""The picker of the "Share a repository" dialog: `GET /api/sharing-grants/workspaces`.

The administrator of the owner workspace is usually not a member of the workspace that
should receive the share, so no route they could already call lists it. This one does,
for the people who may make a share from the active workspace, and only where an
installation's workspaces are one organisation (`single_tenant`). Under `multi_tenant`
workspaces may belong to different organisations, and a list of them would tell one
organisation which others exist: the route answers as a path that is not there, to
everybody and before asking who they are, and the dialog keeps its text field.

Left out of the list, always: the workspace the share would come from, and the built-in
`default` workspace (which cannot be a side of a share).
"""

from __future__ import annotations

import pytest

from src.ee import license as lic
from tests.api.sharing_world import WS_A, WS_B
from tests.ee.sharing_support import GRANTS, create, relicense

LISTING = f"{GRANTS}/workspaces"
WS_C = "wsid-c"

#: Whoever may make a share from workspace B: its owner and admins, the master account and
#: a global administrator (who passes every workspace-admin gate).
ALLOWED = ["admin_b", "su", "gadmin"]
#: Below administrator in B, and in no workspace at all: refused before anything is listed.
REFUSED = ["member_b"]


def _only_in(monkeypatch, mode: str) -> None:
    from src import deployment
    from src.config import get_settings

    monkeypatch.setenv("CELMIS_DEPLOYMENT_MODE", mode)
    get_settings.cache_clear()
    deployment.reset_mode_cache()


@pytest.fixture
def single_tenant(gw, monkeypatch):
    """The sharing world, in the mode where an installation's workspaces are one organisation,
    with two more workspaces: a third, ordinary one, and the built-in `default` one."""
    from sqlalchemy.orm import Session

    from src import deployment
    from src.db.models import Workspace

    _only_in(monkeypatch, "single_tenant")
    with Session(gw.engine) as s:
        s.add(Workspace(id=WS_C, name="Charlie", slug="ws-c", description=""))
        s.add(Workspace(id="default", name="Default", slug="default", description=""))
        s.commit()
    yield gw
    deployment.reset_mode_cache()


@pytest.fixture
def multi_tenant(gw, monkeypatch):
    from sqlalchemy.orm import Session

    from src import deployment
    from src.db.models import Workspace

    _only_in(monkeypatch, "multi_tenant")
    with Session(gw.engine) as s:
        s.add(Workspace(id=WS_C, name="Charlie", slug="ws-c", description=""))
        s.commit()
    yield gw
    deployment.reset_mode_cache()


async def _list(w, *, who: str = "admin_b", ws: str | None = "ws-b"):
    return await w.client.get(LISTING, headers=w.h(who, ws))


# ─── single_tenant: the list ─────────────────────────────────────────


@pytest.mark.parametrize("who", ALLOWED)
async def test_the_owners_administrators_the_master_account_and_a_global_admin_get_the_list(
        single_tenant, who):
    r = await _list(single_tenant, who=who)

    assert r.status_code == 200, (who, r.text)
    assert [w["id"] for w in r.json()] == [WS_A, WS_C]


async def test_the_list_names_each_workspace_by_id_slug_and_display_name_and_nothing_else(
        single_tenant):
    r = await _list(single_tenant)

    assert r.json() == [
        {"id": WS_A, "slug": "ws-a", "name": "Alpha"},
        {"id": WS_C, "slug": "ws-c", "name": "Charlie"},
    ]


async def test_it_lists_workspaces_the_caller_is_not_a_member_of(single_tenant):
    """The reason the route exists: B's administrator belongs to B alone."""
    assert await single_tenant.role("admin_b", "ws-b") == "admin"
    assert await single_tenant.role("admin_b", "ws-a") is None

    r = await _list(single_tenant)

    assert WS_A in [w["id"] for w in r.json()]


async def test_the_workspace_a_share_would_come_from_is_not_in_the_list(single_tenant):
    for who, ws, own in (("admin_b", "ws-b", WS_B), ("admin_a", "ws-a", WS_A)):
        ids = [w["id"] for w in (await _list(single_tenant, who=who, ws=ws)).json()]
        assert own not in ids, (who, ids)


async def test_the_built_in_default_workspace_is_never_in_the_list(single_tenant):
    for who in ALLOWED:
        ids = [w["id"] for w in (await _list(single_tenant, who=who)).json()]
        assert "default" not in ids, (who, ids)


async def test_the_built_in_default_workspace_that_gives_no_share_has_nothing_to_pick_from(
        single_tenant):
    r = await _list(single_tenant, who="su", ws="default")

    assert r.status_code == 200 and r.json() == []


async def test_the_list_follows_the_active_workspace_not_the_one_a_header_claims(single_tenant):
    """A's administrator who claims B is pinned to A, as every workspace-admin route does."""
    r = await _list(single_tenant, who="admin_a", ws="ws-b")

    assert r.status_code == 200
    assert [w["id"] for w in r.json()] == [WS_B, WS_C]


async def test_the_list_is_ordered_by_display_name_whatever_the_ids_are(single_tenant):
    from sqlalchemy.orm import Session

    from src.db.models import Workspace

    with Session(single_tenant.engine) as s:
        s.add(Workspace(id="zzz-first", name="aardvark", slug="ws-first", description=""))
        s.commit()

    r = await _list(single_tenant)

    assert [w["name"] for w in r.json()] == ["aardvark", "Alpha", "Charlie"]


async def test_a_workspace_from_the_list_can_be_named_by_its_id_to_make_a_share(single_tenant):
    picked = (await _list(single_tenant)).json()[0]

    r = await create(single_tenant, workspace=picked["id"])

    assert r.status_code == 201, r.text
    assert r.json()["grantee_workspace_id"] == picked["id"]


# ─── single_tenant: who may ask ──────────────────────────────────────


@pytest.mark.parametrize("who", REFUSED)
async def test_a_plain_member_of_the_workspace_is_refused(single_tenant, who):
    r = await _list(single_tenant, who=who)

    assert r.status_code == 403
    assert "ws-a" not in r.text and "Alpha" not in r.text


@pytest.mark.parametrize("who", ["editor_a", "member_a", "viewer_a"])
async def test_nobody_below_administrator_in_their_own_workspace_is_listed_anything(
        single_tenant, who):
    for ws in ("ws-a", "ws-b"):
        r = await _list(single_tenant, who=who, ws=ws)
        assert r.status_code == 403, (who, ws, r.text)


async def test_a_person_who_is_nobody_is_refused_before_anything_is_listed(single_tenant):
    r = await single_tenant.client.get(LISTING, headers={"X-Test-User": "nobody"})

    assert r.status_code == 401


# ─── multi_tenant: the route is not there ────────────────────────────


async def _unknown_path(w, headers: dict[str, str]):
    return await w.client.get(f"{GRANTS}/workspaces/no-such-path", headers=headers)


@pytest.mark.parametrize("who", ALLOWED + REFUSED + ["admin_a", "loner"])
async def test_under_multi_tenant_everybody_gets_the_answer_of_a_path_that_is_not_there(
        multi_tenant, who):
    headers = multi_tenant.h(who, "ws-b")

    r = await _list(multi_tenant, who=who)
    gone = await _unknown_path(multi_tenant, headers)

    assert r.status_code == 404 and gone.status_code == 404
    assert r.content == gone.content, "a different answer would say the route exists"
    for hidden in ("Alpha", "Charlie", "ws-a", "ws-c", WS_A, WS_C):
        assert hidden not in r.text


async def test_under_multi_tenant_the_route_does_not_ask_who_is_calling(multi_tenant):
    """Not even a missing identity is told apart: a path that is not there answers the same
    to a stranger as to an administrator."""
    stranger = await multi_tenant.client.get(LISTING)
    gone = await multi_tenant.client.get(f"{GRANTS}/workspaces/no-such-path")

    assert stranger.status_code == 404
    assert stranger.content == gone.content


async def test_making_a_share_by_typing_a_slug_is_unchanged_under_multi_tenant(multi_tenant):
    """The text field of the dialog is what is left, and the create route takes what it
    always took."""
    r = await create(multi_tenant, workspace="ws-a")

    assert r.status_code == 201, r.text


async def test_under_multi_tenant_the_mode_decides_not_the_number_of_workspaces(multi_tenant):
    """Three workspaces exist, as in single_tenant."""
    assert (await _list(multi_tenant)).status_code == 404


# ─── the licence ─────────────────────────────────────────────────────


async def test_without_the_licence_claim_the_list_is_not_mounted(single_tenant):
    relicense(single_tenant, ())

    r = await _list(single_tenant)

    assert r.status_code == 404


async def test_a_licence_that_lapses_while_the_process_runs_stops_the_list_too(
        single_tenant, monkeypatch):
    assert (await _list(single_tenant)).status_code == 200

    monkeypatch.setattr(lic.License, "expired", lambda self, now=None: True)

    r = await _list(single_tenant)
    assert r.status_code == 403 and "expired" in r.json()["detail"]
