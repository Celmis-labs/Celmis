"""Switching a person off names that person only if they belong to the receiving workspace.

The owner blocks people by user id and the id is free text, so the blocked list in
the share's detail must not turn into a directory of the installation: the name and
address of a person are read out for somebody in the workspace the share is given
TO (the owner is meant to see those) and for nobody else. Anything else comes back
as the id it was sent with, the same entry whether the id belongs to a stranger, to
the operator account, to one of the owner's own people or to nobody.

World: `tests/api/sharing_world.py` (B gives A one repository).
"""

from __future__ import annotations

import pytest

from tests.api.sharing_world import add_share


async def _block(w, share_id: str, user_id: str):
    return await w.client.patch(f"/api/sharing/shares/{share_id}", json={"block": [user_id]},
                                headers=w.h("admin_b", "ws-b"))


async def _detail(w, share_id: str):
    r = await w.client.get(f"/api/sharing/shares/{share_id}", headers=w.h("admin_b", "ws-b"))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("who", ["gadmin", "loner", "member_b", "admin_b", "su"])
async def test_a_person_outside_the_receiving_workspace_is_listed_by_id_alone(sw, who):
    """`su` is the operator account (a fixed, guessable id); `gadmin` and `loner` are in
    no workspace; `member_b` and `admin_b` are the owner's own people."""
    share_id = add_share(sw)
    uid = sw.uid(who)
    r = await _block(sw, share_id, uid)
    assert r.status_code == 200, r.text
    assert r.json()["blocked"] == [{"user_id": uid, "name": "", "email": ""}]
    assert (await _detail(sw, share_id))["blocked"] == [{"user_id": uid, "name": "", "email": ""}]


async def test_an_id_that_belongs_to_nobody_is_listed_the_same_way(sw):
    """So the entry says nothing about whether the id is a person at all."""
    share_id = add_share(sw)
    r = await _block(sw, share_id, "no-such-person")
    assert r.json()["blocked"] == [{"user_id": "no-such-person", "name": "", "email": ""}]


async def test_a_person_of_the_receiving_workspace_is_named_to_the_owner(sw):
    share_id = add_share(sw)
    r = await _block(sw, share_id, sw.uid("member_a"))
    (entry,) = r.json()["blocked"]
    assert entry["user_id"] == sw.uid("member_a")
    assert entry["email"] == "member-a@acme-corp.io" and entry["name"]


async def test_blocking_still_takes_effect_for_an_id_that_is_not_named(sw):
    """Only what is shown changed: the id is on the list and the audit row keeps it."""
    from tests.api.sharing_world import share_row

    share_id = add_share(sw)
    uid = sw.uid("gadmin")
    await _block(sw, share_id, uid)
    assert share_row(sw, share_id).blocked_user_ids == [uid]
