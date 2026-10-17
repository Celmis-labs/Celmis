"""The life of a pack in the store: who may make it, change it, remove it.

The contract (docs/CODEBASE.md, "Packs"): editors and above create packs, the
creator always maintains the pack they made, a slug clash with a pack the
caller cannot see is answered exactly like one they can, nobody locks
themselves out of a pack by changing who it is for, and deleting a pack removes
every row it owned — nothing relies on a database cascade.
"""

from __future__ import annotations

import pytest

from src.db.models import (
    KnowledgeItem,
    KnowledgePack,
    KnowledgePackInvite,
    KnowledgePackMember,
    KnowledgePackTeam,
    KnowledgePackVersion,
)
from src.knowledge import store
from src.knowledge.visibility import PACK_NOT_FOUND, PackNotAccessible
from tests.knowledge.pack_rig import TEAM_A, TEAM_B, A, B, upload

CHILDREN = (KnowledgeItem, KnowledgePackVersion, KnowledgePackInvite, KnowledgePackMember,
            KnowledgePackTeam)


async def make(world, who="editor", slug="billing", ws=A, **kw):
    async with world.session() as s:
        return (await store.create_pack(
            s, world.principal(who, ws), ws, slug=slug, title=kw.pop("title", slug.title()), **kw)
                ).pack.id


# ─── Making ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("who", ["su", "ga", "owner", "admin", "editor"])
async def test_these_people_may_create_a_pack(world, who):
    assert await make(world, who)


@pytest.mark.parametrize("who", ["member", "viewer", "outsider"])
async def test_these_people_may_not(world, who):
    async with world.session() as s:
        with pytest.raises(store.NotAuthor):
            await store.create_pack(s, world.principal(who), A, slug="x", title="X")
    assert await world.count(KnowledgePack) == 0


async def test_the_creator_always_maintains_what_they_made(world):
    pid = await make(world, "editor", audience="restricted")
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        assert view.can_maintain
        row = await s.get(KnowledgePackMember, (pid, world.uid("editor")))
        assert row.permission == "maintain"
        # nobody else sees a restricted pack they are not named on
        for who in ("owner", "admin", "member", "ga"):
            with pytest.raises(PackNotAccessible):
                await store.get_pack(s, world.principal(who), A, pid)
        assert (await store.get_pack(s, world.principal("su"), A, pid)).can_maintain


async def test_a_new_pack_is_for_teams_by_default(world):
    pid = await make(world)
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("admin"), A, pid)
        assert view.pack.audience == "teams" and view.can_maintain
        with pytest.raises(PackNotAccessible):
            await store.get_pack(s, world.principal("member"), A, pid)


@pytest.mark.parametrize("slug", ["", "bill ing", "-x", "x-", "a--b", "a/b", "x" * 65, "é"])
async def test_a_slug_must_be_plain(world, slug):
    async with world.session() as s:
        with pytest.raises(store.BadInput):
            await store.create_pack(s, world.principal("editor"), A, slug=slug, title="T")


async def test_a_title_and_audience_are_checked(world):
    async with world.session() as s:
        p = world.principal("editor")
        for kw in ({"title": ""}, {"title": "x" * 201}, {"description": "d" * 2001},
                   {"audience": "everyone"}):
            args = {"slug": "ok", "title": "T", **kw}
            with pytest.raises(store.BadInput):
                await store.create_pack(s, p, A, **args)


async def test_a_slug_clash_reads_the_same_whether_or_not_the_pack_is_visible(world):
    await make(world, "editor", "taken", audience="restricted")      # editor2 cannot see it
    await make(world, "editor", "shown", audience="workspace")
    async with world.session() as s:
        p = world.principal("editor2")
        for slug in ("taken", "shown"):
            with pytest.raises(store.SlugTaken) as caught:
                await store.create_pack(s, p, A, slug=slug, title="T")
            assert caught.value.status == 409 and caught.value.message == "slug is not available"


async def test_the_same_slug_may_live_in_two_workspaces(world):
    assert await make(world, "editor", "shared")
    assert await make(world, "admin_b", "shared", ws=B)


async def test_a_workspace_holds_at_most_two_hundred_packs(world, monkeypatch):
    monkeypatch.setattr(store, "MAX_PACKS_PER_WORKSPACE", 2)
    await make(world, slug="one")
    await make(world, slug="two")
    async with world.session() as s:
        with pytest.raises(store.LimitReached):
            await store.create_pack(s, world.principal("editor"), A, slug="three", title="T")


async def test_team_grants_at_creation_must_be_teams_of_this_workspace(world):
    ok = await make(world, "editor", "granted", teams=[(TEAM_A, "read")])
    async with world.session() as s:
        assert await s.get(KnowledgePackTeam, (ok, TEAM_A)) is not None
        with pytest.raises(store.TeamNotFound):
            await store.create_pack(s, world.principal("editor"), A, slug="foreign", title="T",
                                    teams=[(TEAM_B, "read")])
        with pytest.raises(store.TeamNotFound):
            await store.create_pack(s, world.principal("editor"), A, slug="nope", title="T",
                                    teams=[("no-such-team", "read")])
        with pytest.raises(store.BadInput):
            await store.create_pack(s, world.principal("editor"), A, slug="perm", title="T",
                                    teams=[(TEAM_A, "write")])
    assert await world.count(KnowledgePack) == 1


async def test_a_global_admin_without_a_role_cannot_lock_themselves_out_of_a_restricted_pack(world):
    async with world.session() as s:
        with pytest.raises(store.LockOut):
            await store.create_pack(s, world.principal("ga"), A, slug="x", title="X",
                                    audience="restricted")
    assert await world.count(KnowledgePack) == 0
    assert await world.count(KnowledgePackMember) == 0
    assert await make(world, "ga", "visible", audience="teams")       # others are fine


# ─── Seeing and listing ──────────────────────────────────────────────


async def test_a_list_holds_exactly_the_visible_packs_by_title(world):
    await make(world, "editor", "zeta", title="Zeta", audience="workspace")
    await make(world, "editor", "alpha", title="alpha", audience="workspace")
    await make(world, "editor", "secret", title="Secret", audience="restricted")
    await make(world, "editor", "team", title="Team", audience="teams",
               teams=[(TEAM_A, "read")])
    await make(world, "admin_b", "other", title="Other", audience="workspace", ws=B)
    async with world.session() as s:
        def slugs(infos):
            return [i.slug for i in infos]

        async def listing(who):
            return slugs(await store.describe(s, await store.list_views(s, world.principal(who), A)))

        assert await listing("member") == ["alpha", "team", "zeta"]       # in team ta via TEAM_A
        assert await listing("viewer") == ["alpha", "zeta"]
        assert await listing("admin") == ["alpha", "team", "zeta"]        # admins see teams, not restricted
        assert await listing("editor") == ["alpha", "secret", "team", "zeta"]
        assert await listing("su") == ["alpha", "secret", "team", "zeta"]
        assert await listing("outsider") == []
        assert await listing("admin_b") == []                              # B's admin, asking about A


async def test_a_pack_of_another_workspace_is_missing(world):
    pid = await make(world, "admin_b", "theirs", ws=B, audience="workspace")
    async with world.session() as s:
        with pytest.raises(PackNotAccessible):
            await store.get_pack(s, world.principal("owner"), A, pid)
        with pytest.raises(PackNotAccessible):
            await store.get_pack(s, world.principal("su"), A, pid)


@pytest.mark.parametrize("pack_id", ["", "x", "not-a-uuid", "1" * 40, "../../etc",
                                     "11111111-1111-4111-8111-11111111111", "%", "null"])
async def test_a_malformed_id_is_missing_not_an_error(world, pack_id):
    async with world.session() as s:
        with pytest.raises(PackNotAccessible) as caught:
            await store.get_pack(s, world.principal("su"), A, pack_id)
        assert str(caught.value) == PACK_NOT_FOUND


async def test_hidden_missing_and_foreign_packs_raise_the_same_error(world):
    hidden = await make(world, "editor", "hidden", audience="restricted")
    foreign = await make(world, "admin_b", "foreign", ws=B, audience="workspace")
    missing = "99999999-9999-4999-8999-999999999999"
    seen = set()
    async with world.session() as s:
        for pid in (hidden, foreign, missing, "garbage"):
            with pytest.raises(PackNotAccessible) as caught:
                await store.get_pack(s, world.principal("member"), A, pid)
            seen.add((type(caught.value), str(caught.value), caught.value.args))
    assert len(seen) == 1


async def test_only_a_maintainer_is_shown_the_teams_a_pack_is_granted_to(world):
    pid = await make(world, "editor", "t", audience="workspace", teams=[(TEAM_A, "read")])
    async with world.session() as s:
        mine = await store.describe(s, [await store.get_pack(s, world.principal("editor"), A, pid)])
        theirs = await store.describe(s, [await store.get_pack(s, world.principal("member"), A, pid)])
    assert [(t.team_id, t.name, t.permission) for t in mine[0].teams] == [(TEAM_A, "ta", "read")]
    assert mine[0].can_maintain and theirs[0].teams == [] and not theirs[0].can_maintain


async def test_a_grant_to_a_team_that_is_gone_shows_and_grants_nothing(world):
    from src.db.models import Team

    pid = await make(world, "editor", "t", audience="teams", teams=[(TEAM_A, "maintain")])
    async with world.session() as s:
        await s.delete(await s.get(Team, TEAM_A))
        await s.commit()
    async with world.session() as s:
        with pytest.raises(PackNotAccessible):
            await store.get_pack(s, world.principal("member"), A, pid)
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        assert view.grants == []


# ─── Changing ────────────────────────────────────────────────────────


async def test_a_maintainer_edits_the_fields_and_is_told_which_changed(world):
    pid = await make(world, "editor", "p", audience="workspace")
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        changed = await store.update_pack(s, view, title="New", description="d")
        assert changed == ["title", "description"]
        assert await store.update_pack(s, view, title="New") == []
    async with world.session() as s:
        row = await s.get(KnowledgePack, pid)
        assert (row.title, row.description) == ("New", "d")


async def test_someone_who_only_sees_a_pack_may_not_change_it(world):
    pid = await make(world, "editor", "p", audience="workspace")
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("member"), A, pid)
        assert not view.can_maintain
        for call in (store.update_pack(s, view, title="x"), store.delete_pack(s, view),
                     store.set_teams(s, view, []), store.list_members(s, view),
                     store.list_invites(s, view), store.usage(s, view),
                     store.list_tokens(s, view)):
            with pytest.raises(store.NotMaintainer) as caught:
                await call
            assert caught.value.status == 403
    assert await world.count(KnowledgePack) == 1


async def test_a_workspace_admin_maintains_a_teams_pack_and_a_stranger_does_not_see_it(world):
    teams = await make(world, "editor", "t", audience="teams")
    async with world.session() as s:
        assert (await store.get_pack(s, world.principal("admin"), A, teams)).can_maintain
        assert (await store.get_pack(s, world.principal("owner"), A, teams)).can_maintain
        assert (await store.get_pack(s, world.principal("ga"), A, teams)).can_maintain
        with pytest.raises(PackNotAccessible):
            await store.get_pack(s, world.principal("editor2"), A, teams)


async def test_a_slug_is_kept_in_lower_case(world):
    async with world.session() as s:
        view = await store.create_pack(s, world.principal("editor"), A, slug="Billing", title="T")
        assert view.pack.slug == "billing"
        with pytest.raises(store.SlugTaken):
            await store.create_pack(s, world.principal("editor"), A, slug="BILLING", title="T")


async def test_switching_to_restricted_keeps_an_admin_who_would_lose_the_pack(world):
    pid = await make(world, "editor", "p", audience="teams")
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("admin"), A, pid)
        assert await store.update_pack(s, view, audience="restricted") == ["audience"]
    async with world.session() as s:
        row = await s.get(KnowledgePackMember, (pid, world.uid("admin")))
        assert row is not None and row.permission == "maintain"
        assert (await store.get_pack(s, world.principal("admin"), A, pid)).can_maintain
        with pytest.raises(PackNotAccessible):
            await store.get_pack(s, world.principal("owner"), A, pid)       # not named


async def test_a_global_admin_without_a_role_may_not_restrict_a_pack(world):
    pid = await make(world, "editor", "p", audience="teams")
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("ga"), A, pid)
        with pytest.raises(store.LockOut):
            await store.update_pack(s, view, audience="restricted")
    async with world.session() as s:
        assert (await s.get(KnowledgePack, pid)).audience == "teams"
        assert await world.count(KnowledgePackMember, user_id=world.uid("ga")) == 0


async def test_replacing_the_teams_replaces_every_grant(world):
    pid = await make(world, "editor", "p", audience="restricted", teams=[(TEAM_A, "read")])
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        await store.set_teams(s, view, [])
    assert await world.count(KnowledgePackTeam, pack_id=pid) == 0
    async with world.session() as s:
        with pytest.raises(PackNotAccessible):
            await store.get_pack(s, world.principal("member"), A, pid)


async def test_dropping_your_own_team_grant_keeps_you_a_maintainer(world):
    pid = await make(world, "editor2", "p", audience="restricted", teams=[(TEAM_A, "maintain")])
    async with world.session() as s:
        session_member = await s.get(KnowledgePackMember, (pid, world.uid("editor2")))
        await s.delete(session_member)             # named only through the team now
        await s.commit()
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor2"), A, pid)
        assert view.can_maintain
        await store.set_teams(s, view, [])
    async with world.session() as s:
        assert (await store.get_pack(s, world.principal("editor2"), A, pid)).can_maintain


async def test_a_team_of_another_workspace_cannot_be_granted(world):
    pid = await make(world, "editor", "p")
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        with pytest.raises(store.TeamNotFound):
            await store.set_teams(s, view, [(TEAM_B, "read")])
        with pytest.raises(store.BadInput):
            await store.set_teams(s, view, [(TEAM_A, "root")])
    assert await world.count(KnowledgePackTeam) == 0


# ─── Removing ────────────────────────────────────────────────────────


async def test_deleting_a_pack_removes_every_row_it_owned(world):
    pid = await make(world, "editor", "p", audience="workspace", teams=[(TEAM_A, "read")])
    keep = await make(world, "editor", "keep", audience="workspace", teams=[(TEAM_A, "read")])
    async with world.session() as s:
        for pack in (pid, keep):
            view = await store.get_pack(s, world.principal("editor"), A, pack)
            await store.add_version(s, view, upload(("notes/a.md", "# a\n")))
            await store.create_invite(s, view)
    before = {m: await world.count(m, pack_id=pid) for m in CHILDREN}
    assert all(before.values())
    async with world.session() as s:
        await store.delete_pack(s, await store.get_pack(s, world.principal("editor"), A, pid))
    assert {m: await world.count(m, pack_id=pid) for m in CHILDREN} == {m: 0 for m in CHILDREN}
    assert await world.count(KnowledgePack, id=pid) == 0
    assert all([await world.count(m, pack_id=keep) for m in CHILDREN])      # the other is whole


async def test_explicit_members_are_added_by_email_and_only_from_this_workspace(world):
    pid = await make(world, "editor", "p", audience="restricted")
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        row = await store.add_member(s, view, "Member@Acme-Corp.io", "read")
        assert row.user_id == world.uid("member") and row.permission == "read"
        again = await store.add_member(s, view, "member@acme-corp.io", "maintain")
        assert again.permission == "maintain"
        for email in ("admin-b@acme-corp.io", "outsider@acme-corp.io", "nobody@acme-corp.io",
                      "", "member"):
            with pytest.raises(store.PersonNotFound):
                await store.add_member(s, view, email, "read")
        with pytest.raises(store.BadInput):
            await store.add_member(s, view, "viewer@acme-corp.io", "owner")
    async with world.session() as s:
        assert (await store.get_pack(s, world.principal("member"), A, pid)).can_maintain


async def test_a_person_who_left_the_workspace_cannot_be_added_and_sees_nothing(world):
    from src.db.models import WorkspaceMember

    pid = await make(world, "editor", "p", audience="restricted")
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        await store.add_member(s, view, "member@acme-corp.io", "read")
        await s.delete(await s.get(WorkspaceMember, (A, world.uid("member"))))
        await s.commit()
        with pytest.raises(store.PersonNotFound):
            await store.add_member(s, view, "member@acme-corp.io", "read")
        with pytest.raises(PackNotAccessible):
            await store.get_pack(s, world.principal("member"), A, pid)


async def test_a_member_can_be_removed_and_a_stranger_cannot(world):
    pid = await make(world, "editor", "p", audience="restricted")
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        await store.add_member(s, view, "member@acme-corp.io", "read")
        await store.remove_member(s, view, world.uid("member"))
        with pytest.raises(store.MemberNotFound):
            await store.remove_member(s, view, world.uid("member"))
        listed = await store.list_members(s, view)
    assert [m.user_id for m in listed] == [world.uid("editor")]


async def test_a_pack_names_at_most_five_hundred_people(world, monkeypatch):
    monkeypatch.setattr(store, "MAX_MEMBERS", 2)
    pid = await make(world, "editor", "p", audience="workspace")
    async with world.session() as s:
        view = await store.get_pack(s, world.principal("editor"), A, pid)
        await store.add_member(s, view, "member@acme-corp.io", "read")
        with pytest.raises(store.LimitReached):
            await store.add_member(s, view, "viewer@acme-corp.io", "read")
        await store.add_member(s, view, "member@acme-corp.io", "maintain")     # editing is fine
