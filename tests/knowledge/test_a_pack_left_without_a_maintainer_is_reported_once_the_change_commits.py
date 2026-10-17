"""A restricted pack that loses its last maintainer is reported, and only for real.

The contract (src/knowledge/store.py `release_person`, `erase_user`; docs/CODEBASE.md
"Lifecycle"):

  * the functions return the packs left without a maintainer (id and workspace);
  * the audit row `codebase.pack_orphaned` (target = the pack, no title or name in
    the detail) is written when the surrounding transaction COMMITS — a change
    that is rolled back writes none;
  * a team grant or another named maintainer who is still a member of the
    workspace keeps the pack maintained; a pack that is not restricted never counts;
  * erasing an account reports the packs it orphaned in every workspace, with the
    reason `account_erased`, and nobody as the actor.
"""

from __future__ import annotations

import pytest

import src.security.audit as security_audit
from src.knowledge import store
from tests.knowledge.pack_rig import TEAM_A, A, B, upload


@pytest.fixture
def rows(monkeypatch):
    """The `pack_orphaned` rows only: the rows for the names, tokens and links a
    departure ends are `tests/api/test_every_route_that_changes_a_pack_leaves_an_audit_row.py`'s."""
    captured: list[dict] = []
    monkeypatch.setattr(security_audit, "record_action",
                        lambda **kw: captured.append(kw) if kw["action"] == "codebase.pack_orphaned" else None)
    return captured


async def _pack(world, slug, *, who="editor", audience="restricted", ws=A):
    async with world.session() as s:
        view = await store.create_pack(s, world.principal(who, ws), ws, slug=slug,
                                       title=slug.upper(), audience=audience)
        await store.add_version(s, view, upload(("notes/a.md", "# a\n")), "")
        return view.pack.id


async def test_the_pack_only_the_leaver_maintained_is_returned_and_reported_after_commit(
        world, rows):
    pid = await _pack(world, "vault")
    async with world.session() as s:
        orphans = await store.release_person(
            s, world.uid("editor"), [A], actor="boss@acme-corp.io", actor_id="boss")
        assert [(o.pack_id, o.workspace_id) for o in orphans] == [(pid, A)]
        assert rows == []                                   # nothing before the commit
        await s.commit()
    assert [(r["action"], r["target"], r["workspace_id"], r["actor_id"], r["detail"])
            for r in rows] == [("codebase.pack_orphaned", pid, A, "boss", {"reason": "member_left"})]


async def test_a_change_that_is_rolled_back_reports_nothing(world, rows):
    await _pack(world, "vault")
    async with world.session() as s:
        orphans = await store.release_person(s, world.uid("editor"), [A])
        assert len(orphans) == 1
        await s.rollback()
    assert rows == []
    async with world.session() as s:                        # and the person still holds the pack
        assert len(await store.list_views(s, world.principal("editor"), A)) == 1


async def test_a_second_maintainer_or_a_team_grant_keeps_the_pack_maintained(world, rows):
    by_name = await _pack(world, "by-name")
    by_team = await _pack(world, "by-team")
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, by_name)
        await store.add_member(s, view, "editor2@acme-corp.io", "maintain")
        view = await store.get_pack(s, world.principal("editor"), A, by_team)
        await store.set_teams(s, view, [(TEAM_A, "maintain")])
    async with world.session() as s:
        assert await store.release_person(s, world.uid("editor"), [A]) == []
        await s.commit()
    assert rows == []


async def test_a_pack_that_is_not_restricted_is_never_reported(world, rows):
    await _pack(world, "everyones", audience="workspace")
    await _pack(world, "for-admins", audience="teams")
    async with world.session() as s:
        assert await store.release_person(s, world.uid("editor"), [A]) == []
        await s.commit()
    assert rows == []


async def test_leaving_one_workspace_leaves_the_packs_of_another_alone(world, rows):
    await _pack(world, "vault")
    async with world.session() as s:
        assert await store.release_person(s, world.uid("editor"), [B]) == []
        await s.commit()
    assert rows == []
    assert await world.count(store.KnowledgePackMember, user_id=world.uid("editor")) == 1


async def test_erasing_an_account_reports_the_packs_it_orphaned_without_an_actor(world, rows):
    pid = await _pack(world, "vault")
    async with world.session() as s:
        orphans = await store.erase_user(s, world.uid("editor"))
        assert [o.pack_id for o in orphans] == [pid]
        await s.commit()
    assert [(r["action"], r["target"], r["actor_id"], r["detail"]) for r in rows] == [
        ("codebase.pack_orphaned", pid, None, {"reason": "account_erased"})]
    async with world.session() as s:                        # still there, for the superadmin
        assert len(await store.list_views(s, world.principal("su"), A)) == 1
        assert await store.list_views(s, world.principal("owner"), A) == []


async def test_deleting_an_account_with_a_synchronous_session_reports_the_same_way(world, rows):
    from sqlalchemy.orm import Session

    pid = await _pack(world, "vault")
    with Session(world.sync_engine) as s:
        orphans = store.erase_user_sync(s, world.uid("editor"))
        assert [o.pack_id for o in orphans] == [pid]
        assert rows == []
        s.commit()
    assert [(r["target"], r["detail"]) for r in rows] == [(pid, {"reason": "account_erased"})]
