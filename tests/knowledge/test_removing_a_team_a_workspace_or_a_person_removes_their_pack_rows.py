"""What is cleaned up when the things a pack refers to go away, and what is counted.

The contract (docs/CODEBASE.md, "Lifecycle"): deleting a team removes its pack
grants and nobody else's; deleting a workspace removes every pack in it with
every child row and leaves other workspaces whole; erasing a person removes
their names on packs and the ids that recorded who made or granted what, but
keeps the packs; the cleanup helpers never commit (the caller's transaction
does). What a pack's usage counts is `test_usage_counts_calls_and_skill_opens_...`.
"""

from __future__ import annotations

from sqlalchemy import select

from src.db.models import (
    KnowledgeItem,
    KnowledgePack,
    KnowledgePackInvite,
    KnowledgePackMember,
    KnowledgePackTeam,
    KnowledgePackVersion,
)
from src.knowledge import store
from tests.knowledge.pack_rig import TEAM_A, TEAM_B, A, B, upload

TABLES = (KnowledgePack, KnowledgePackTeam, KnowledgePackMember, KnowledgePackVersion,
          KnowledgeItem, KnowledgePackInvite)


async def pack(world, slug, who="editor", ws=A, **kw):
    async with world.session() as s:
        view = await store.create_pack(s, world.principal(who, ws), ws, slug=slug,
                                       title=slug.title(), **kw)
        await store.add_version(s, view, upload(("notes/a.md", f"# {slug}\n")))
        await store.create_invite(s, view)
        return view.pack.id


async def counts(world):
    return {m.__tablename__: await world.count(m) for m in TABLES}


async def test_deleting_a_team_removes_only_its_grants(world):
    one = await pack(world, "one", teams=[(TEAM_A, "read")])
    two = await pack(world, "two", who="admin_b", ws=B, audience="workspace", teams=[(TEAM_B, "read")])
    async with world.session() as s:
        await store.delete_team_grants(s, TEAM_A)
        await s.commit()
    assert await world.count(KnowledgePackTeam, pack_id=one) == 0
    assert await world.count(KnowledgePackTeam, pack_id=two) == 1
    assert await world.count(KnowledgePack) == 2                         # the packs stay


async def test_deleting_a_workspace_removes_its_packs_and_every_child_row(world):
    await pack(world, "one", teams=[(TEAM_A, "read")])
    await pack(world, "two", audience="workspace")
    keep = await pack(world, "keep", who="admin_b", ws=B, audience="workspace",
                      teams=[(TEAM_B, "read")])
    before = await counts(world)
    assert all(before.values())
    async with world.session() as s:
        await store.delete_workspace_packs(s, A)
        await s.commit()
    assert await counts(world) == {
        "knowledge_packs": 1, "knowledge_pack_teams": 1, "knowledge_pack_members": 1,
        "knowledge_pack_versions": 1, "knowledge_items": 2, "knowledge_pack_invites": 1}
    assert await world.count(KnowledgePack, workspace_id=A) == 0
    assert await world.count(KnowledgePack, id=keep) == 1
    for model in (KnowledgePackVersion, KnowledgeItem, KnowledgePackInvite):
        assert await world.count(model, workspace_id=A) == 0
    assert await world.count(KnowledgePackTeam, pack_id=keep) == 1
    assert await world.count(KnowledgePackMember, pack_id=keep) == 1


async def test_the_cleanup_waits_for_the_callers_commit(world):
    await pack(world, "one", audience="workspace")
    async with world.session() as s:
        await store.delete_workspace_packs(s, A)
        await s.rollback()
    assert await world.count(KnowledgePack) == 1 and await world.count(KnowledgeItem) > 0


async def test_erasing_a_person_removes_their_names_and_authorship_but_not_the_packs(world):
    one = await pack(world, "one", who="editor", audience="workspace")
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, one)
        await store.add_member(s, view, "member@acme-corp.io", "read")
        await store.add_member(s, view, "viewer@acme-corp.io", "read")
    uid = world.uid("editor")
    async with world.session() as s:
        listed = await store.memberships_of(s, uid)
        assert [(m["slug"], m["permission"]) for m in listed] == [("one", "maintain")]
        await store.erase_user(s, uid)
        await s.commit()
    assert await world.count(KnowledgePackMember, user_id=uid) == 0
    assert await world.count(KnowledgePackMember, pack_id=one) == 2     # the others stay
    async with world.session() as s:
        assert await store.memberships_of(s, uid) == []
        row = await s.get(KnowledgePack, one)
        assert row is not None and row.created_by_id is None
        for model, column in ((KnowledgePackVersion, "created_by_id"),
                              (KnowledgePackInvite, "created_by_id"),
                              (KnowledgePackMember, "granted_by_id")):
            values = (await s.scalars(select(getattr(model, column)))).all()
            assert uid not in values, model
    assert await world.count(KnowledgeItem) > 0 and await world.count(KnowledgePackVersion) == 1


async def test_erasing_a_person_with_no_packs_is_a_no_op(world):
    await pack(world, "one", audience="workspace")
    before = await counts(world)
    async with world.session() as s:
        await store.erase_user(s, world.uid("outsider"))
        await s.commit()
    assert await counts(world) == before
