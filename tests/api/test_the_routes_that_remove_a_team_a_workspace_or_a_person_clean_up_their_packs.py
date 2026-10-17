"""Deleting a team, a workspace or an account leaves no pack row that names it.

The contract (docs/CODEBASE.md, "What deleting things does"):

  * deleting a team removes the team's grants on packs — and with them the
    access they gave — while the packs stay;
  * deleting a workspace removes its packs, versions, files, invites, grants and
    named people, and touches no other workspace's;
  * erasing or deleting a person removes the packs they were NAMED on and the ids
    that recorded who made or granted what; the packs, their versions and files
    stay, because they are the workspace's, not the person's;
  * a person's data export lists the packs they are named on.
"""

from __future__ import annotations

from sqlalchemy import create_engine, func, select

from src.db.models import (
    KnowledgeItem,
    KnowledgePack,
    KnowledgePackInvite,
    KnowledgePackMember,
    KnowledgePackTeam,
    KnowledgePackVersion,
)
from tests.api.codebase_support import call, seeded
from tests.api.rbac_world import B_PACK_ID, world

CHILDREN = (KnowledgeItem, KnowledgePackVersion, KnowledgePackInvite, KnowledgePackMember,
            KnowledgePackTeam)


async def _count(w, model, **where) -> int:
    async with w.factory() as s:
        q = select(func.count()).select_from(model)
        for key, value in where.items():
            q = q.where(getattr(model, key) == value)
        return int(await s.scalar(q) or 0)


async def test_deleting_a_team_takes_its_grants_and_the_access_they_gave(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        ids = await seeded(w)
        team = w.ids["team_a"]
        assert await _count(w, KnowledgePackTeam, team_id=team) == 2
        assert (await call(w, "owner_a", "GET", f"/api/codebase/packs/{ids['prg']}")).status_code == 200

        gone = await call(w, "admin_a", "DELETE", f"/api/teams/{team}")
        assert gone.status_code == 204, gone.text

        assert await _count(w, KnowledgePackTeam, team_id=team) == 0
        assert await _count(w, KnowledgePack) >= 6                       # the packs stay
        assert (await call(w, "owner_a", "GET", f"/api/codebase/packs/{ids['prg']}")).status_code == 404
        assert (await call(w, "owner_a", "GET", f"/api/codebase/packs/{ids['ptg']}")).status_code == 200


async def test_deleting_a_team_of_another_workspace_removes_nothing(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await seeded(w)
        before = await _count(w, KnowledgePackTeam)
        r = await call(w, "admin_a", "DELETE", f"/api/teams/{w.ids['team_b']}")
        assert r.status_code == 404
        assert await _count(w, KnowledgePackTeam) == before


async def test_deleting_a_workspace_takes_every_row_of_its_packs_and_only_those(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await seeded(w)
        a, b = w.ws["ws-a"], w.ws["ws-b"]
        assert await _count(w, KnowledgePack, workspace_id=b) == 1
        a_packs = await _count(w, KnowledgePack, workspace_id=a)
        a_items = await _count(w, KnowledgeItem, workspace_id=a)

        r = await call(w, "su", "DELETE", f"/api/workspaces/{b}")
        assert r.status_code == 204, r.text

        assert await _count(w, KnowledgePack, workspace_id=b) == 0
        for model in (KnowledgeItem, KnowledgePackVersion, KnowledgePackInvite):
            assert await _count(w, model, workspace_id=b) == 0, model.__name__
        for model in (KnowledgePackMember, KnowledgePackTeam):
            assert await _count(w, model, pack_id=B_PACK_ID) == 0, model.__name__
        assert await _count(w, KnowledgePack, workspace_id=a) == a_packs
        assert await _count(w, KnowledgeItem, workspace_id=a) == a_items


async def _named_and_made(w) -> dict[str, str]:
    ids = await seeded(w)                                   # editor_a made all five
    for who in ("member_a", "viewer_a"):
        r = await call(w, "editor_a", "POST", f"/api/codebase/packs/{ids['pr']}/members",
                       json={"email": w.users[who].email, "permission": "read"})
        assert r.status_code == 200, r.text
    r = await call(w, "editor_a", "POST", f"/api/codebase/packs/{ids['pr']}/members",
                   json={"email": w.users["admin_a"].email, "permission": "maintain"})
    assert r.status_code == 200
    return ids


async def test_the_export_lists_the_packs_a_person_is_named_on(tmp_path, monkeypatch):
    from src.api.routers import gdpr

    async with world(tmp_path, monkeypatch, extra_routers=(gdpr.router,)) as w:
        ids = await _named_and_made(w)
        r = await w.client.get(f"/api/gdpr/export/{w.uid('member_a')}", headers=w.h("gadmin"))
        assert r.status_code == 200, r.text
        assert r.json()["knowledge_pack_memberships"] == [{
            "pack_id": ids["pr"], "workspace_id": w.ws["ws-a"], "slug": "pr", "permission": "read"}]
        other = await w.client.get(f"/api/gdpr/export/{w.uid('viewer_a')}", headers=w.h("gadmin"))
        assert [m["pack_id"] for m in other.json()["knowledge_pack_memberships"]] == [ids["pr"]]
        none = await w.client.get(f"/api/gdpr/export/{w.uid('owner_a')}", headers=w.h("gadmin"))
        assert none.json()["knowledge_pack_memberships"] == []


async def test_erasing_a_person_unnames_them_and_forgets_who_made_what(tmp_path, monkeypatch):
    from src.api.routers import gdpr

    async with world(tmp_path, monkeypatch, extra_routers=(gdpr.router,)) as w:
        ids = await _named_and_made(w)
        me = w.uid("editor_a")
        assert await _count(w, KnowledgePack, created_by_id=me) == 5
        assert await _count(w, KnowledgePackMember, user_id=me) >= 1       # named on what they made
        versions = await _count(w, KnowledgePackVersion)

        r = await w.client.delete(f"/api/gdpr/user/{me}", headers=w.h("gadmin"))
        assert r.status_code == 200, r.text

        assert await _count(w, KnowledgePackMember, user_id=me) == 0
        assert await _count(w, KnowledgePack, created_by_id=me) == 0
        assert await _count(w, KnowledgePackVersion, created_by_id=me) == 0
        assert await _count(w, KnowledgePackMember, granted_by_id=me) == 0
        assert await _count(w, KnowledgePack, workspace_id=w.ws["ws-a"]) == 5     # the packs stay
        assert await _count(w, KnowledgePackVersion) == versions
        # and nobody else's name was touched
        assert await _count(w, KnowledgePackMember, user_id=w.uid("member_a")) == 1
        got = await call(w, "member_a", "GET", f"/api/codebase/packs/{ids['pr']}")
        assert got.status_code == 200


async def test_deleting_an_account_unnames_it_and_drops_its_personal_workspace_packs(tmp_path, monkeypatch):
    from src.api.routers import auth

    async with world(tmp_path, monkeypatch, extra_routers=(auth.router,)) as w:
        await _named_and_made(w)
        import src.llm.budget as budget

        sync_engine = create_engine(f"sqlite:///{tmp_path / 'celmis.db'}")
        monkeypatch.setattr(budget, "_ENGINE", sync_engine)

        # a pack in the leaver's personal workspace
        from src.api.workspace_provision import personal_slug
        from src.db.models import Workspace, WorkspaceMember

        me = w.uid("member_a")
        async with w.factory() as s:
            personal = Workspace(id="wsid-personal", name="Mine", slug=personal_slug(me), description="")
            s.add(personal)
            s.add(WorkspaceMember(workspace_id=personal.id, user_id=me, role="owner"))
            await s.commit()
        made = await call(w, "member_a", "POST", "/api/codebase/packs", personal.slug,
                          json={"slug": "mine", "title": "Mine"})
        assert made.status_code == 201, made.text
        assert await _count(w, KnowledgePack, workspace_id="wsid-personal") == 1

        r = await w.client.delete("/api/auth/me", headers=w.h("member_a"))
        assert r.status_code == 204, r.text

        assert await _count(w, KnowledgePack, workspace_id="wsid-personal") == 0
        assert await _count(w, KnowledgePackMember, user_id=me) == 0
        assert await _count(w, KnowledgePack, workspace_id=w.ws["ws-a"]) == 5     # others' packs stay
        assert await _count(w, KnowledgePackMember, user_id=w.uid("viewer_a")) == 1
        sync_engine.dispose()
