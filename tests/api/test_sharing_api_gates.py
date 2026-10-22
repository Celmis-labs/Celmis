"""Who may touch the owner's side of a share: its owner's administrators, the
superadmin, and nobody else.

The list is for the active workspace's administrators. Every route that names ONE
share is gated on the workspace that OWNS the share, whatever workspace the caller
is working in, and answers 404 (not 403) to anybody else: the people of the
workspace the share is given TO included, who must not learn that a share with
that id exists, let alone read what the owner hid from them.

World: `tests/api/sharing_world.py` (B gives A one repository).
"""

from __future__ import annotations

import pytest

from tests.api.sharing_world import add_share

#: The owner's administrators, and the two people the rule lets in everywhere
#: (the master account, and a global admin as for every workspace-admin gate).
ALLOWED = ["admin_b", "su", "gadmin"]
#: Everybody else: B's plain member, A's whole staff, a person in no workspace.
HIDDEN = ["member_b", "admin_a", "owner_a", "member_a", "viewer_a", "both", "loner"]

ONE_SHARE_ROUTES = [
    ("get", "/api/sharing/shares/{id}", None),
    ("get", "/api/sharing/shares/{id}/activity", None),
    ("patch", "/api/sharing/shares/{id}", {}),
    ("post", "/api/sharing/shares/{id}/revoke", None),
]


async def _call(w, who, method, url, body, ws="ws-b"):
    kwargs = {"headers": w.h(who, ws)}
    if body is not None:
        kwargs["json"] = body
    return await getattr(w.client, method)(url, **kwargs)


@pytest.mark.parametrize("method,url,body", ONE_SHARE_ROUTES)
@pytest.mark.parametrize("who", ALLOWED)
async def test_the_owners_administrators_and_the_superadmin_reach_a_share(sw, who, method, url, body):
    share_id = add_share(sw)
    r = await _call(sw, who, method, url.format(id=share_id), body)
    assert r.status_code == 200, (who, method, url, r.text)


@pytest.mark.parametrize("method,url,body", ONE_SHARE_ROUTES)
@pytest.mark.parametrize("who", HIDDEN)
async def test_anybody_else_is_told_there_is_no_such_share(sw, who, method, url, body):
    share_id = add_share(sw)
    for ws in ("ws-a", "ws-b"):
        r = await _call(sw, who, method, url.format(id=share_id), body, ws=ws)
        assert r.status_code == 404, (who, ws, method, url, r.status_code, r.text)
    # Nothing happened: a refused revoke or narrowing left the share alone.
    from tests.api.sharing_world import share_row

    row = share_row(sw, share_id)
    assert row.revoked_at is None and row.deny_globs == []


@pytest.mark.parametrize("method,url,body", ONE_SHARE_ROUTES)
async def test_a_share_that_is_not_theirs_answers_like_one_that_does_not_exist(sw, method, url, body):
    """A 403 would confirm the id is live; the two answers must be the same bytes."""
    share_id = add_share(sw)
    elsewhere = await _call(sw, "admin_a", method, url.format(id=share_id), body, ws="ws-a")
    nowhere = await _call(sw, "admin_a", method,
                          url.format(id="00000000-0000-4000-8000-000000000000"), body, ws="ws-a")
    assert elsewhere.status_code == nowhere.status_code == 404
    assert elsewhere.content == nowhere.content


@pytest.mark.parametrize("method,url,body", ONE_SHARE_ROUTES)
@pytest.mark.parametrize("bad", ["not-an-id", "1' OR '1'='1", "00000000-0000-4000-8000-00000000000G",
                                 "%25", "A" * 300])
async def test_an_id_that_is_not_an_id_is_no_share_for_anybody(sw, method, url, body, bad):
    add_share(sw)
    for who in ("su", "admin_b"):
        r = await _call(sw, who, method, url.format(id=bad), body)
        assert r.status_code == 404, (who, bad, r.text)


@pytest.mark.parametrize("who,expected", [
    ("admin_b", 200), ("su", 200), ("gadmin", 200), ("member_b", 403),
])
async def test_the_list_is_for_the_administrators_of_the_active_workspace(sw, who, expected):
    add_share(sw)
    r = await sw.client.get("/api/sharing/shares", headers=sw.h(who, "ws-b"))
    assert r.status_code == expected, r.text


async def test_a_person_in_no_workspace_is_the_owner_of_their_own_and_it_gives_nothing(sw):
    """`loner` belongs to no workspace, so the first request gives them a personal
    one, and that is the workspace the list is about. B's shares are not in it."""
    add_share(sw)
    r = await sw.client.get("/api/sharing/shares", headers=sw.h("loner", "ws-b"))
    assert r.status_code == 200 and r.json() == []


async def test_the_list_holds_what_the_workspace_gives_and_never_what_it_receives(sw):
    gives = add_share(sw)
    # A gives B something too: B's list must not show it, and A's must.
    from tests.api.rbac_world import A_REPO, A_REPO_FULL

    receives = add_share(sw, slug=A_REPO, full_name=A_REPO_FULL, owner="wsid-a", grantee="wsid-b")
    mine = (await sw.client.get("/api/sharing/shares", headers=sw.h("admin_b", "ws-b"))).json()
    assert [s["share_id"] for s in mine] == [gives]
    theirs = (await sw.client.get("/api/sharing/shares", headers=sw.h("admin_a", "ws-a"))).json()
    assert [s["share_id"] for s in theirs] == [receives]


async def test_the_administrators_of_the_receiving_workspace_get_an_empty_list(sw):
    add_share(sw)
    r = await sw.client.get("/api/sharing/shares", headers=sw.h("admin_a", "ws-a"))
    assert r.status_code == 200 and r.json() == []


async def test_a_caller_who_is_nobody_is_refused_before_anything_else(sw):
    share_id = add_share(sw)
    for url in ("/api/sharing/shares", f"/api/sharing/shares/{share_id}",
                f"/api/sharing/shares/{share_id}/activity"):
        r = await sw.client.get(url, headers={"X-Test-User": "nobody"})
        assert r.status_code == 401, (url, r.status_code)


async def test_an_administrator_of_two_workspaces_is_judged_by_the_owner_not_the_header(sw):
    """`both` is a plain member of A and of B. Naming B in the header does not make
    them B's administrator, and A's header does not matter for a share B owns."""
    share_id = add_share(sw)
    for ws in ("ws-a", "ws-b"):
        r = await sw.client.get(f"/api/sharing/shares/{share_id}", headers=sw.h("both", ws))
        assert r.status_code == 404
