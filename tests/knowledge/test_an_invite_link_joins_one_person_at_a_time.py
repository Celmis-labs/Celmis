"""Invite links: who can make one, who can use it, and what it can never do.

The contract (docs/CODEBASE.md, "Invite links"): a maintainer makes a link that
names a pack and holds only a hash of its token; the person who opens it must
already belong to the pack's workspace — anyone else, and every token that is
wrong, expired, revoked or used up, gets the same 404; joining adds a `read`
member once, never lowers a maintainer, and uses up one use only when someone
actually joined.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from src.db.models import KnowledgePackInvite, KnowledgePackMember
from src.knowledge import store
from tests.knowledge.pack_rig import A, B

ALL_REFUSALS = (store.InviteNotFound,)


async def pack(world, audience="restricted", who="editor", ws=A):
    async with world.session() as s:
        return (await store.create_pack(s, world.principal(who, ws), ws, slug="p", title="Secret title",
                                        description="Secret text", audience=audience)).pack.id


async def link(world, pid, who="editor", **kw):
    async with world.session() as s:
        view = await store.get_pack(s, world.principal(who), A, pid)
        return await store.create_invite(s, view, **kw)


async def redeem(world, who, token):
    async with world.session() as s:
        return await store.redeem_invite(s, world.users[who], token)


async def refused(world, who, token, op="redeem"):
    async with world.session() as s:
        fn = store.redeem_invite if op == "redeem" else store.preview_invite
        with pytest.raises(store.InviteNotFound) as caught:
            await fn(s, world.users[who], token)
        return caught.value.status, caught.value.message


async def test_a_link_stores_only_the_hash_of_its_token(world):
    pid = await pack(world)
    row, token = await link(world, pid)
    assert len(token) >= 40 and row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in repr(row.__dict__) and row.permission == "read"
    assert row.max_uses == 25 and row.used_count == 0 and row.revoked_at is None
    left = (row.expires_at.replace(tzinfo=UTC) - datetime.now(UTC)).total_seconds()
    assert 6.9 * 86400 < left < 7.1 * 86400


async def test_two_links_never_share_a_token(world):
    pid = await pack(world)
    _a, ta = await link(world, pid)
    _b, tb = await link(world, pid)
    assert ta != tb


@pytest.mark.parametrize("days,uses", [(0, 5), (31, 5), (7, 0), (7, 501), (-1, 1)])
async def test_a_link_has_bounds(world, days, uses):
    pid = await pack(world)
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        with pytest.raises(store.BadInput):
            await store.create_invite(s, view, expires_in_days=days, max_uses=uses)


async def test_only_a_maintainer_makes_lists_or_revokes_links(world):
    pid = await pack(world, audience="workspace")
    row, _ = await link(world, pid)
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("member"), A, pid)
        for call in (store.create_invite(s, view), store.list_invites(s, view),
                     store.revoke_invite(s, view, row.id)):
            with pytest.raises(store.NotMaintainer):
                await call


async def test_a_pack_holds_at_most_twenty_open_links(world, monkeypatch):
    monkeypatch.setattr(store, "MAX_ACTIVE_INVITES", 2)
    pid = await pack(world)
    first, _ = await link(world, pid)
    await link(world, pid)
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        with pytest.raises(store.LimitReached):
            await store.create_invite(s, view)
        await store.revoke_invite(s, view, first.id)
        await store.create_invite(s, view)                    # a revoked one frees its place


async def test_a_member_of_the_workspace_joins_and_then_sees_the_pack(world):
    from src.knowledge.visibility import PackNotAccessible

    pid = await pack(world)
    _row, token = await link(world, pid)
    async with world.session() as s:
        with pytest.raises(PackNotAccessible):
            await store.get_pack(s, world.principal("viewer"), A, pid)
    pack_row, joined = await redeem(world, "viewer", token)
    assert joined and pack_row.id == pid
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("viewer"), A, pid)
        assert not view.can_maintain
        member = await s.get(KnowledgePackMember, (pid, world.uid("viewer")))
        assert member.permission == "read" and member.invite_id is not None
        assert member.granted_by_id == world.uid("editor")


async def test_the_preview_tells_a_member_what_they_are_joining(world):
    pid = await pack(world)
    _row, token = await link(world, pid)
    async with world.session() as s:
        info = await store.preview_invite(s, world.users["viewer"], token)
    assert (info.pack_title, info.pack_description, info.workspace_id, info.already_member) == (
        "Secret title", "Secret text", A, False)
    await redeem(world, "viewer", token)
    async with world.session() as s:
        assert (await store.preview_invite(s, world.users["viewer"], token)).already_member


async def test_joining_twice_costs_one_use_and_changes_nothing(world):
    pid = await pack(world)
    row, token = await link(world, pid)
    assert (await redeem(world, "viewer", token))[1] is True
    assert (await redeem(world, "viewer", token))[1] is False
    async with world.session() as s:
        assert (await s.get(KnowledgePackInvite, row.id)).used_count == 1
    assert await world.count(KnowledgePackMember, pack_id=pid) == 2        # creator + viewer


async def test_a_link_never_lowers_a_maintainer(world):
    pid = await pack(world)
    _row, token = await link(world, pid)
    _pack, joined = await redeem(world, "editor", token)                   # the creator
    assert not joined
    async with world.session() as s:
        assert (await s.get(KnowledgePackMember, (pid, world.uid("editor")))).permission == "maintain"
        assert (await store.get_pack(s, world.principal("editor"), A, pid)).can_maintain


async def test_a_link_runs_out(world):
    pid = await pack(world)
    _row, token = await link(world, pid, max_uses=1)
    await redeem(world, "viewer", token)
    assert await refused(world, "member", token) == (404, "invite not found or expired")
    assert await refused(world, "member", token, "preview") == (404, "invite not found or expired")


async def test_every_bad_token_is_refused_in_the_same_words(world):
    pid = await pack(world)
    live, token = await link(world, pid)
    expired, t_expired = await link(world, pid)
    revoked, t_revoked = await link(world, pid)
    async with world.session() as s:
        (await s.get(KnowledgePackInvite, expired.id)).expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        await store.revoke_invite(s, view, revoked.id)
    outcomes = set()
    for bad in (t_expired, t_revoked, "wrong", "", " ", "x" * 500, token[:-1]):
        outcomes.add(await refused(world, "viewer", bad))
        outcomes.add(await refused(world, "viewer", bad, "preview"))
    # a good token in the hands of someone with no role in the workspace
    outcomes.add(await refused(world, "outsider", token))
    outcomes.add(await refused(world, "admin_b", token))
    outcomes.add(await refused(world, "outsider", token, "preview"))
    assert outcomes == {(404, "invite not found or expired")}
    assert await world.count(KnowledgePackMember, pack_id=pid) == 1        # nobody joined


async def test_a_person_who_left_the_workspace_cannot_use_the_link(world):
    from src.db.models import WorkspaceMember

    pid = await pack(world)
    _row, token = await link(world, pid)
    async with world.session() as s:
        await s.delete(await s.get(WorkspaceMember, (A, world.uid("viewer"))))
        await s.commit()
    assert await refused(world, "viewer", token) == (404, "invite not found or expired")


async def test_a_link_of_a_deleted_pack_is_dead(world):
    pid = await pack(world)
    _row, token = await link(world, pid)
    async with world.session() as s:
        await store.delete_pack(s, await store.get_pack(s, world.principal("editor"), A, pid))
    assert await refused(world, "viewer", token) == (404, "invite not found or expired")


async def test_a_revoked_link_stays_listed_as_revoked(world):
    pid = await pack(world)
    row, _ = await link(world, pid)
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        await store.revoke_invite(s, view, row.id)
        await store.revoke_invite(s, view, row.id)                         # again: nothing changes
        listed = await store.list_invites(s, view)
        assert [(i.id, i.revoked_at is not None) for i in listed] == [(row.id, True)]
        for bad in ("nope", "99999999-9999-4999-8999-999999999999"):
            with pytest.raises(store.InviteNotFound):
                await store.revoke_invite(s, view, bad)


async def test_a_link_of_another_pack_cannot_be_revoked_through_this_one(world):
    pid = await pack(world)
    async with world.session() as s:
        other = (await store.create_pack(s, world.principal("editor"), A, slug="q", title="Q",
                                         audience="workspace")).pack.id
    row, _ = await link(world, other)
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        with pytest.raises(store.InviteNotFound):
            await store.revoke_invite(s, view, row.id)
        assert (await s.get(KnowledgePackInvite, row.id)).revoked_at is None


async def test_the_link_does_not_leak_a_pack_of_another_workspace(world):
    pid = await pack(world, ws=B, who="admin_b")
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("admin_b", B), B, pid)
        _row, token = await store.create_invite(s, view)
    assert await refused(world, "editor", token) == (404, "invite not found or expired")
