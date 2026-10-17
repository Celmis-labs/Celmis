"""A restricted pack with no team grant is a private pack: its creator, the people
named on it, and the superadmin see it — nobody else does.

Not the creator's teammates, not another team, not the workspace's owners and
admins, not a global admin. For all of them it does not exist: the same 404 as a
pack nobody made, absent from the catalogue and the search, its versions, items
and archive just as unreachable. Naming someone (by email) is what lets them in,
and removing them takes it away again. The web shows the same facts in plain
words (`tests/web/test_the_codebase_private_pack_wording.py`).
"""

from __future__ import annotations

import pytest

from src.db.models import Team, TeamMember
from tests.api.codebase_support import call, marker, note_zip, upload_zip
from tests.api.rbac_world import world

BASE = "/api/codebase/packs"
MISSING = "6b1f0c5e-0000-4000-8000-0000000000ff"
#: Everybody who must NOT see it, with why they might have been expected to.
OUTSIDERS = {
    "teammate": "a member of the creator's own team (no grant)",
    "owner_a": "workspace owner",
    "admin_a": "workspace admin, and in the creator's team",
    "admin2_a": "workspace admin",
    "gadmin": "global admin",
    "other_team": "a member of another team of the workspace",
}


async def _setup(w):
    """`secret` — private, created by editor_a — plus a teammate and an other-team member."""
    async with w.factory() as s:
        s.add(Team(id="team-c", name="gamma-team", description="", workspace_id=w.ws["ws-a"]))
        s.add(TeamMember(team_id=w.ids["team_a"], user_id=w.uid("both"), role="member"))
        s.add(TeamMember(team_id="team-c", user_id=w.uid("viewer_a"), role="member"))
        await s.commit()
    r = await call(w, "editor_a", "POST", BASE, json={
        "slug": "secret", "title": "Secret", "audience": "restricted", "teams": []})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert (await upload_zip(w, "editor_a", pid, note_zip("secret"))).status_code == 201
    return pid


def _who(name: str) -> str:
    return {"teammate": "both", "other_team": "viewer_a"}.get(name, name)


def _tails(pid: str):
    return [("GET", ""), ("GET", "/versions"), ("GET", "/versions/1/archive"),
            ("GET", "/versions/1/diff?against=1"), ("GET", "/invites"), ("GET", "/tokens"),
            ("PATCH", ""), ("DELETE", "")]


@pytest.mark.parametrize("name", list(OUTSIDERS))
async def test_an_outsider_gets_the_answer_for_a_pack_nobody_made(tmp_path, monkeypatch, name):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _setup(w)
        who = _who(name)
        for method, tail in _tails(pid):
            kw = {"json": {"title": "x"}} if method == "PATCH" else {}
            hidden = await call(w, who, method, f"{BASE}/{pid}{tail}", **kw)
            absent = await call(w, who, method, f"{BASE}/{MISSING}{tail}", **kw)
            assert (hidden.status_code, hidden.content) == (absent.status_code, absent.content), (
                name, method, tail)
            assert hidden.status_code == 404, (name, method, tail)
        # still there, unchanged
        mine = (await call(w, "editor_a", "GET", f"{BASE}/{pid}")).json()
        assert (mine["title"], mine["audience"]) == ("Secret", "restricted")


@pytest.mark.parametrize("name", list(OUTSIDERS))
async def test_an_outsider_is_not_told_it_exists_by_the_catalogue_or_the_search(tmp_path, monkeypatch, name):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _setup(w)
        who = _who(name)
        listed = (await call(w, who, "GET", BASE)).json()["packs"]
        assert pid not in {p["id"] for p in listed} and "Secret" not in str(listed)
        found = await call(w, who, "GET", f"/api/codebase/search?q={marker('secret')}")
        assert found.status_code == 200 and found.json()["hits"] == []
        assert marker("secret") not in found.text
        brief = await call(w, who, "POST", "/api/codebase/brief", json={"task": f"use the {marker('secret')} recipe"})
        assert marker("secret") not in brief.text and "Secret" not in brief.text


@pytest.mark.parametrize("who", ["editor_a", "su"])
async def test_the_creator_and_the_superadmin_see_it_and_everything_in_it(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _setup(w)
        got = await call(w, who, "GET", f"{BASE}/{pid}")
        assert got.status_code == 200 and got.json()["can_maintain"] is True
        assert pid in {p["id"] for p in (await call(w, who, "GET", BASE)).json()["packs"]}
        hits = (await call(w, who, "GET", f"/api/codebase/search?q={marker('secret')}")).json()["hits"]
        assert [h["pack_id"] for h in hits] == [pid]
        assert (await call(w, who, "GET", f"{BASE}/{pid}/versions/1/archive")).status_code == 200


async def test_naming_a_person_lets_them_in_and_removing_them_puts_them_out(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _setup(w)
        before = await call(w, "member_a", "GET", f"{BASE}/{pid}")
        assert before.status_code == 404
        added = await call(w, "editor_a", "POST", f"{BASE}/{pid}/members",
                           json={"email": w.users["member_a"].email, "permission": "read"})
        assert added.status_code == 200, added.text
        seen = await call(w, "member_a", "GET", f"{BASE}/{pid}")
        assert seen.status_code == 200 and seen.json()["can_maintain"] is False
        assert [h["pack_id"] for h in (await call(
            w, "member_a", "GET", f"/api/codebase/search?q={marker('secret')}")).json()["hits"]] == [pid]
        # a reader reads: they do not maintain
        refused = await call(w, "member_a", "PATCH", f"{BASE}/{pid}", json={"title": "mine"})
        assert (refused.status_code, refused.json()["detail"]) == (403, "Requires a pack maintainer")
        # and nobody else was let in by that
        for name in OUTSIDERS:
            assert (await call(w, _who(name), "GET", f"{BASE}/{pid}")).status_code == 404, name
        removed = await call(w, "editor_a", "DELETE", f"{BASE}/{pid}/members/{w.uid('member_a')}")
        assert removed.status_code == 204
        assert (await call(w, "member_a", "GET", f"{BASE}/{pid}")).status_code == 404


async def test_granting_a_team_ends_the_privacy_and_taking_it_back_restores_it(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _setup(w)
        grant = await call(w, "editor_a", "PUT", f"{BASE}/{pid}/teams",
                           json={"teams": [{"team_id": w.ids["team_a"], "permission": "read"}]})
        assert grant.status_code == 200, grant.text
        assert (await call(w, "both", "GET", f"{BASE}/{pid}")).status_code == 200        # the team now sees it
        assert (await call(w, "viewer_a", "GET", f"{BASE}/{pid}")).status_code == 404    # another team still does not
        assert (await call(w, "owner_a", "GET", f"{BASE}/{pid}")).status_code == 200     # in the team, so yes
        assert (await call(w, "admin2_a", "GET", f"{BASE}/{pid}")).status_code == 404    # admin, not in it
        await call(w, "editor_a", "PUT", f"{BASE}/{pid}/teams", json={"teams": []})
        for name in OUTSIDERS:
            assert (await call(w, _who(name), "GET", f"{BASE}/{pid}")).status_code == 404, name
