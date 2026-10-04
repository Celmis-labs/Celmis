"""A team member is named the way people name each other: by email, or picked.

The Teams page asked for "email or internal id". An email answered 404 "Not a
member of this workspace" — the route looked the string up as a user id — and
internal ids appear nowhere in the UI, so the field could only be filled by
somebody who had read the database.

Now the route accepts an id (as before) or an email, compared without case,
resolved among THIS workspace's members only; and the page picks from
`GET /api/teams/{id}/candidates`, the same people the route accepts.

The adversarial half: an email must not become a way around the rule that a
team is a subset of its workspace, nor a way to learn who has an account on
the installation, nor a guess between two accounts.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from tests.api.rbac_world import world

pytestmark = pytest.mark.asyncio

ROOT = Path(__file__).resolve().parents[2]


def _url(w, ref: str) -> str:
    return f"/api/teams/{w.ids['team_a']}/members/{ref}"


async def test_an_email_adds_the_workspace_member_it_names(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.put(_url(w, "Member-A@ACME-corp.io"), json={"role": "reviewer"},
                               headers=w.h("admin_a"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user_id"] == w.uid("member_a")
        assert body["email"] == "member-a@acme-corp.io"

        listed = await w.client.get(f"/api/teams/{w.ids['team_a']}/members",
                                    headers=w.h("admin_a"))
        row = next(m for m in listed.json() if m["user_id"] == w.uid("member_a"))
        assert row["email"] == "member-a@acme-corp.io" and row["name"] == "member_a"


async def test_an_id_still_works(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.put(_url(w, w.uid("member_a")), json={"role": "member"},
                               headers=w.h("admin_a"))
        assert r.status_code == 200, r.text


async def test_an_email_from_another_workspace_is_refused_like_a_stranger(tmp_path, monkeypatch):
    """member_b has an account — in B. The answer must be the same as for an
    address nobody has, or the field is an account-existence oracle."""
    async with world(tmp_path, monkeypatch) as w:
        foreign = await w.client.put(_url(w, "member-b@acme-corp.io"),
                                     json={"role": "member"}, headers=w.h("admin_a"))
        nobody = await w.client.put(_url(w, "nobody@acme-corp.io"),
                                    json={"role": "member"}, headers=w.h("admin_a"))
        assert foreign.status_code == nobody.status_code == 404
        strip = lambda r: r.json()["detail"].replace("member-b", "X").replace("nobody", "X")  # noqa: E731
        assert strip(foreign) == strip(nobody)
        assert "invite" in foreign.json()["detail"].lower()
        from src.db.models import TeamMember
        assert await w.scalar(TeamMember, (w.ids["team_a"], w.uid("member_b"))) is None


async def test_an_ambiguous_email_is_refused_not_guessed(tmp_path, monkeypatch):
    """Two accounts whose stored addresses differ only in case (rows that
    predate lower-casing). Picking one decides whose grants these become."""
    from src.db.models import TeamMember, WorkspaceMember
    from src.users import User
    from src.users import store as users_mod

    async with world(tmp_path, monkeypatch) as w:
        users_mod._default_store.create(User(id="u-twin", email="twin-x@acme-corp.io",
                                             name="twin", is_admin=False))
        with sqlite3.connect(tmp_path / "users.db") as conn:
            conn.execute("UPDATE users SET email = ? WHERE id = ?",
                         ("Member-A@acme-corp.io", "u-twin"))
        async with w.factory() as s:
            s.add(WorkspaceMember(workspace_id=w.ws["ws-a"], user_id="u-twin", role="member"))
            await s.commit()
        r = await w.client.put(_url(w, "member-a@acme-corp.io"), json={"role": "member"},
                               headers=w.h("admin_a"))
        assert r.status_code == 422, r.text
        assert "more than one" in r.json()["detail"].lower()
        assert await w.scalar(TeamMember, (w.ids["team_a"], w.uid("member_a"))) is None
        assert await w.scalar(TeamMember, (w.ids["team_a"], "u-twin")) is None


async def test_a_non_admin_cannot_add_by_email(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.put(_url(w, "viewer-a@acme-corp.io"), json={"role": "member"},
                               headers=w.h("member_a"))
        assert r.status_code == 403, r.text


async def test_another_workspaces_admin_cannot_reach_this_team_by_email(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.put(_url(w, "member-a@acme-corp.io"), json={"role": "member"},
                               headers=w.h("admin_b"))
        assert r.status_code == 404, r.text   # the team is not B's


async def test_removal_by_email(tmp_path, monkeypatch):
    from src.db.models import TeamMember

    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.delete(_url(w, "EDITOR-A@acme-corp.io"), headers=w.h("admin_a"))
        assert r.status_code == 204, r.text
        assert await w.scalar(TeamMember, (w.ids["team_a"], w.uid("editor_a"))) is None


# ─── the picker's list ───────────────────────────────────────────────


async def test_candidates_are_this_workspaces_members_not_yet_in_the_team(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.get(f"/api/teams/{w.ids['team_a']}/candidates",
                               headers=w.h("admin_a"))
        assert r.status_code == 200, r.text
        emails = {c["email"] for c in r.json()["members"]}
        # In A, not in team_a (admin_a, editor_a, owner_a already are).
        assert emails == {"member-a@acme-corp.io", "viewer-a@acme-corp.io",
                          "admin2-a@acme-corp.io", "both@acme-corp.io"}
        assert r.json()["total"] == 4
        assert "member-b@acme-corp.io" not in str(r.json())


async def test_candidates_search_by_name_or_email_without_case(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        by_email = await w.client.get(f"/api/teams/{w.ids['team_a']}/candidates",
                                      params={"q": "VIEWER-A@"}, headers=w.h("admin_a"))
        by_name = await w.client.get(f"/api/teams/{w.ids['team_a']}/candidates",
                                     params={"q": "Both"}, headers=w.h("admin_a"))
        assert [c["user_id"] for c in by_email.json()["members"]] == [w.uid("viewer_a")]
        assert [c["user_id"] for c in by_name.json()["members"]] == [w.uid("both")]


async def test_candidates_need_the_same_gate_as_adding(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        member = await w.client.get(f"/api/teams/{w.ids['team_a']}/candidates",
                                    headers=w.h("member_a"))
        other = await w.client.get(f"/api/teams/{w.ids['team_a']}/candidates",
                                   headers=w.h("admin_b"))
        assert member.status_code == 403
        assert other.status_code == 404
        assert "acme-corp.io" not in member.text + other.text


# ─── the page ────────────────────────────────────────────────────────


def _code(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(^|[^:\"'`])//[^\n]*", r"\1", src)


def test_the_page_picks_a_member_instead_of_asking_for_an_id():
    page = _code(ROOT / "web" / "app" / "(app)" / "admin" / "teams" / "page.tsx")
    assert "<MemberPicker" in page
    assert "teamsApi.candidates" in page
    assert "userIdPlaceholder" not in page
    api = _code(ROOT / "web" / "lib" / "api.ts")
    assert "/candidates?q=${encodeURIComponent(q)}" in api
    assert "members/${encodeURIComponent(user)}" in api
