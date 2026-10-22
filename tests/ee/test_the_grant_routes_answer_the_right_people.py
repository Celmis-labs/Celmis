# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Who may make a share and who may give more of one: the owner's administrators.

Creating is gated on the ACTIVE workspace's administrators, the way every workspace-admin
route is (a plain member is 403). Extending names a share and is gated on the workspace
that OWNS it, whatever workspace the caller is working in: for anybody else, the
receiving workspace's people included, the share does not exist (404, never 403), and the
answer is the same bytes as for an id that was never a share.

The licence is a second gate, tested in tests/ee/test_sharing_is_enterprise.py. These
tests run with the claim granted.
"""

from __future__ import annotations

import pytest

from tests.api.rbac_world import A_REPO
from tests.api.sharing_world import WS_A, WS_B, add_share, share_row
from tests.ee.sharing_support import GRANTS, create, extend

#: The owner's administrators, the master account, and a global admin (who passes every
#: workspace-admin gate).
ALLOWED = ["admin_b", "su", "gadmin"]
#: Everybody else: B's plain member, all of A's staff, a person in no workspace.
HIDDEN = ["member_b", "admin_a", "owner_a", "member_a", "viewer_a", "both", "loner"]


async def _rows(w) -> list:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.db.models import RepoShare

    with Session(w.engine) as s:
        return list(s.execute(select(RepoShare)).scalars().all())


# ─── creating ────────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ALLOWED)
async def test_the_owners_administrators_the_superadmin_and_a_global_admin_can_make_a_share(gw, who):
    r = await create(gw, who=who, ws="ws-b")

    assert r.status_code == 201, (who, r.text)
    assert share_row(gw, r.json()["share_id"]).owner_workspace_id == WS_B


async def test_a_plain_member_of_the_owner_cannot_make_a_share(gw):
    r = await create(gw, who="member_b")

    assert r.status_code == 403
    assert await _rows(gw) == []


@pytest.mark.parametrize("who,expected", [
    ("admin_a", 404), ("owner_a", 404), ("loner", 404),
    ("editor_a", 403), ("member_a", 403), ("viewer_a", 403),
])
@pytest.mark.parametrize("ws", ["ws-a", "ws-b"])
async def test_nobody_outside_the_owner_can_share_the_owners_repository(gw, who, expected, ws):
    """The header names a workspace the caller is not in: they are pinned to their own,
    where the repository is not registered (an administrator there reaches the
    repository check and does not find it), and below administrator the gate stops
    them first. Never a share, whichever workspace the request claims to work in."""
    r = await create(gw, who=who, ws=ws)

    assert r.status_code == expected, (who, ws, r.status_code, r.text)
    assert await _rows(gw) == []


async def test_the_receiving_workspaces_administrator_cannot_pull_a_repository_to_themselves(gw):
    r = await create(gw, who="admin_a", ws="ws-a", workspace="ws-a")

    assert r.status_code == 404 and r.json()["detail"] == "No such repository."
    assert await _rows(gw) == []


async def test_an_administrator_of_a_workspace_shares_only_what_that_workspace_registered(gw):
    """B's administrator names A's repository: it is not in B's registry."""
    r = await create(gw, repo_slug=A_REPO)

    assert r.status_code == 404 and await _rows(gw) == []


async def test_a_person_who_is_nobody_is_refused_before_anything_else(gw):
    r = await gw.client.post(GRANTS, json={"repo_slug": "x", "workspace": "ws-a"},
                             headers={"X-Test-User": "nobody"})

    assert r.status_code == 401


# ─── extending ───────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ALLOWED)
async def test_the_owners_administrators_and_the_superadmin_can_extend_a_share(gw, who):
    share_id = add_share(gw)

    r = await extend(gw, share_id, {"max_people": 6}, who=who)

    assert r.status_code == 200, (who, r.text)
    assert share_row(gw, share_id).max_people == 6


@pytest.mark.parametrize("who", HIDDEN)
async def test_anybody_else_is_told_there_is_no_such_share(gw, who):
    share_id = add_share(gw)
    for ws in ("ws-a", "ws-b"):
        r = await extend(gw, share_id, {"max_people": 6}, who=who, ws=ws)
        assert r.status_code == 404, (who, ws, r.status_code, r.text)
    assert share_row(gw, share_id).max_people == 5, "a refused extension changed the share"


async def test_a_share_that_is_not_theirs_answers_like_one_that_does_not_exist(gw):
    """A 403 would confirm that the id is live; the two answers must be the same bytes."""
    share_id = add_share(gw)
    body = {"max_people": 6}

    elsewhere = await extend(gw, share_id, body, who="admin_a", ws="ws-a")
    nowhere = await extend(gw, "00000000-0000-4000-8000-000000000000", body,
                           who="admin_a", ws="ws-a")

    assert elsewhere.status_code == nowhere.status_code == 404
    assert elsewhere.content == nowhere.content


@pytest.mark.parametrize("bad", ["not-an-id", "1' OR '1'='1", "00000000-0000-4000-8000-00000000000G",
                                 "%25", "A" * 300])
async def test_an_id_that_is_not_an_id_is_no_share_for_anybody(gw, bad):
    add_share(gw)
    for who in ("su", "admin_b"):
        r = await extend(gw, bad, {"max_people": 6}, who=who)
        assert r.status_code == 404, (who, bad, r.text)


async def test_a_share_of_another_pair_is_the_owners_to_extend_and_nobody_elses(gw):
    """A gives B something: A's administrator extends it, B's cannot."""
    from tests.api.rbac_world import A_REPO_FULL

    share_id = add_share(gw, slug=A_REPO, full_name=A_REPO_FULL, owner=WS_A, grantee=WS_B)

    assert (await extend(gw, share_id, {"max_people": 6}, who="admin_b", ws="ws-b")).status_code == 404
    assert (await extend(gw, share_id, {"max_people": 6}, who="admin_a", ws="ws-a")).status_code == 200


async def test_the_gate_is_judged_by_the_owner_not_by_the_header(gw):
    """`both` is a plain member of A and of B. Naming B in the header does not make them
    B's administrator."""
    share_id = add_share(gw)

    for ws in ("ws-a", "ws-b"):
        assert (await extend(gw, share_id, {"max_people": 6}, who="both", ws=ws)).status_code == 404


async def test_a_person_who_is_nobody_cannot_extend(gw):
    share_id = add_share(gw)

    r = await gw.client.post(f"{GRANTS}/{share_id}/extend", json={"max_people": 6},
                             headers={"X-Test-User": "nobody"})

    assert r.status_code == 401
