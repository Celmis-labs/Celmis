"""Leaving a workspace also ends the person's teams there and the invite links they made.

The contract (src/api/memberships.py, src/knowledge/store.py `release_person`,
docs/CODEBASE.md "What deleting things does"):

  * removing a member — by an admin or by the person leaving — deletes their
    TeamMember rows for the teams OF THAT WORKSPACE, in the same transaction, and
    only those: a team of another workspace they belong to is not touched, and
    nobody else leaves a team;
  * so adding them back later, in any role, does not give them the team's grants
    again (a pack granted to the team stays closed to them);
  * the pack invite links they made in that workspace are revoked (the rows stay
    as a record), so a link they handed out stops adding people; links other
    people made, and links they made in another workspace, keep working;
  * erasing an account revokes the invite links it made, in every workspace,
    before the ids that name the maker are cleared.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from src.db.models import KnowledgePackInvite, TeamMember
from tests.api.codebase_support import call, seeded
from tests.api.rbac_world import B_PACK_ID, world


def _routers():
    from src.api.routers import gdpr

    return (gdpr.router,)


async def _teams_of(w, member: str) -> set[str]:
    async with w.factory() as s:
        return set((await s.scalars(select(TeamMember.team_id).where(
            TeamMember.user_id == w.uid(member)))).all())


async def _join_teams(w, member: str, *teams: str) -> None:
    async with w.factory() as s:
        for team in teams:
            s.add(TeamMember(team_id=w.ids[team], user_id=w.uid(member), role="member"))
        await s.commit()


async def _remove(w, member: str) -> None:
    r = await call(w, "su", "DELETE", f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid(member)}", None)
    assert r.status_code == 204, r.text


async def _add(w, member: str, role: str) -> None:
    r = await call(w, "su", "PUT", f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid(member)}", None,
                   json={"role": role})
    assert r.status_code == 200, r.text


async def test_removing_a_member_takes_them_out_of_the_teams_of_that_workspace_only(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _join_teams(w, "both", "team_a", "team_b")
        before = await _teams_of(w, "editor_a")
        assert await _teams_of(w, "both") == {w.ids["team_a"], w.ids["team_b"]}

        await _remove(w, "both")

        assert await _teams_of(w, "both") == {w.ids["team_b"]}          # B's team is B's
        assert await _teams_of(w, "editor_a") == before                  # nobody else left
        assert await _teams_of(w, "member_b") == {w.ids["team_b"]}


async def test_a_person_added_back_to_the_workspace_does_not_get_the_teams_pack_back(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        ids = await seeded(w)                                            # ptg: teams audience, granted to team-a
        await _join_teams(w, "both", "team_a")
        assert (await call(w, "both", "GET", f"/api/codebase/packs/{ids['ptg']}")).status_code == 200

        await _remove(w, "both")
        await _add(w, "both", "member")

        assert w.ids["team_a"] not in await _teams_of(w, "both")
        assert (await call(w, "both", "GET", f"/api/codebase/packs/{ids['ptg']}")).status_code == 404


async def test_the_invite_links_a_leaver_made_stop_working_and_nobody_elses_do(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        ids = await seeded(w)
        pack = f"/api/codebase/packs/{ids['pw']}"
        mine = await call(w, "editor_a", "POST", f"{pack}/invites", json={})
        theirs = await call(w, "admin_a", "POST", f"{pack}/invites", json={})
        assert mine.status_code == theirs.status_code == 201, (mine.text, theirs.text)
        gone, kept = mine.json(), theirs.json()
        async with w.factory() as s:                                      # a link the leaver made elsewhere
            s.add(KnowledgePackInvite(
                id=str(uuid.uuid4()), pack_id=B_PACK_ID, workspace_id=w.ws["ws-b"],
                token_hash=uuid.uuid4().hex, created_by_id=w.uid("editor_a"),
                expires_at=datetime.now(UTC) + timedelta(days=3)))
            await s.commit()

        async def preview(token: str) -> int:
            r = await call(w, "viewer_a", "POST", "/api/codebase/invites/preview", json={"token": token})
            return r.status_code

        assert await preview(gone["token"]) == await preview(kept["token"]) == 200

        await _remove(w, "editor_a")

        assert await preview(gone["token"]) == 404
        assert await preview(kept["token"]) == 200
        async with w.factory() as s:
            rows = {r.id: r for r in (await s.scalars(select(KnowledgePackInvite))).all()}
        assert rows[gone["invite"]["id"]].revoked_at is not None          # kept as a record
        assert rows[kept["invite"]["id"]].revoked_at is None
        elsewhere = [r for r in rows.values() if r.workspace_id == w.ws["ws-b"]
                     and r.created_by_id == w.uid("editor_a")]
        assert len(elsewhere) == 1 and elsewhere[0].revoked_at is None


async def test_a_link_made_by_a_leaver_does_not_come_back_when_they_are_added_again(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        ids = await seeded(w)
        made = await call(w, "editor_a", "POST", f"/api/codebase/packs/{ids['pw']}/invites", json={})
        assert made.status_code == 201, made.text

        await _remove(w, "editor_a")
        await _add(w, "editor_a", "admin")

        r = await call(w, "viewer_a", "POST", "/api/codebase/invites/preview",
                       json={"token": made.json()["token"]})
        assert r.status_code == 404


async def test_erasing_an_account_revokes_the_invite_links_it_made(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_routers()) as w:
        ids = await seeded(w)
        made = await call(w, "editor_a", "POST", f"/api/codebase/packs/{ids['pw']}/invites", json={})
        other = await call(w, "admin_a", "POST", f"/api/codebase/packs/{ids['pw']}/invites", json={})
        assert made.status_code == other.status_code == 201

        r = await w.client.delete(f"/api/gdpr/user/{w.uid('editor_a')}", headers=w.h("gadmin"))
        assert r.status_code == 200, r.text

        async with w.factory() as s:
            row = await s.get(KnowledgePackInvite, made.json()["invite"]["id"])
            kept = await s.get(KnowledgePackInvite, other.json()["invite"]["id"])
        assert row.revoked_at is not None and row.created_by_id is None
        assert kept.revoked_at is None
        for token, want in ((made.json()["token"], 404), (other.json()["token"], 200)):
            got = await call(w, "viewer_a", "POST", "/api/codebase/invites/preview", json={"token": token})
            assert got.status_code == want
