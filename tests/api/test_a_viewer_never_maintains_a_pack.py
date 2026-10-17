"""A viewer reads and never writes a knowledge pack, whatever the pack names.

The contract (docs/CODEBASE.md "Who may do what", docs/ROLES_AND_ACCESS.md):

  * demoting a pack maintainer to viewer takes the write right with it: the pack
    page answers `can_maintain: false`, every change is the maintainer 403, and
    a publish token they made stops on its next use with the identical 401 -
    for a person named on the pack and for a person who maintains through a team;
  * the viewer still SEES what they were named on (read is not taken away) and
    promoting them back gives the right back - nothing was deleted;
  * a maintain grant cannot be handed to a viewer in the first place: naming one
    as a maintainer is a 422 and writes no row, while naming them as a reader is fine;
  * a viewer does not count as "another maintainer": the last real maintainer of a
    restricted pack cannot step down because a viewer is named beside them, and a
    restricted pack whose only real maintainer leaves is reported as orphaned
    even though a viewer is still named maintainer on it.
"""

from __future__ import annotations

from sqlalchemy import func, select

from src.db.models import KnowledgePackMember
from tests.api.codebase_support import call, note_zip, upload_zip
from tests.api.rbac_world import world
from tests.api.test_a_pack_token_publishes_one_pack_and_nothing_else import NOT_VALID, publish

BASE = "/api/codebase/packs"


async def _pack(w, who: str, slug: str, audience: str = "restricted", teams=None) -> str:
    r = await call(w, who, "POST", BASE, json={
        "slug": slug, "title": f"Pack {slug}", "description": "d",
        "audience": audience, "teams": teams or []})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _name(w, who: str, pid: str, member: str, permission: str):
    return await call(w, who, "POST", f"{BASE}/{pid}/members",
                      json={"email": w.users[member].email, "permission": permission})


async def _set_role(w, member: str, role: str) -> None:
    r = await call(w, "su", "PUT", f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid(member)}",
                   None, json={"role": role})
    assert r.status_code == 200, r.text


async def _token(w, who: str, pid: str) -> str:
    r = await call(w, who, "POST", f"{BASE}/{pid}/tokens", json={"label": "ci"})
    assert r.status_code == 201, r.text
    return r.json()["token"]


async def _member_count(w, pid: str) -> int:
    async with w.factory() as s:
        return await s.scalar(select(func.count()).select_from(KnowledgePackMember)
                              .where(KnowledgePackMember.pack_id == pid))


async def test_a_named_maintainer_demoted_to_viewer_can_read_the_pack_and_change_nothing(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w, "editor_a", "vault")
        assert (await _name(w, "editor_a", pid, "member_a", "maintain")).status_code == 200
        assert (await call(w, "member_a", "GET", f"{BASE}/{pid}")).json()["can_maintain"] is True

        await _set_role(w, "member_a", "viewer")

        seen = await call(w, "member_a", "GET", f"{BASE}/{pid}")
        assert seen.status_code == 200 and seen.json()["can_maintain"] is False
        assert [p["id"] for p in (await call(w, "member_a", "GET", BASE)).json()["packs"]] == [pid]
        changes = [
            call(w, "member_a", "PATCH", f"{BASE}/{pid}", json={"title": "Taken over"}),
            call(w, "member_a", "DELETE", f"{BASE}/{pid}"),
            upload_zip(w, "member_a", pid, note_zip("vault")),
            _name(w, "member_a", pid, "editor_a", "read"),
            call(w, "member_a", "POST", f"{BASE}/{pid}/tokens", json={"label": "ci"}),
            call(w, "member_a", "PUT", f"{BASE}/{pid}/teams", json={"teams": []}),
        ]
        for pending in changes:
            r = await pending
            assert r.status_code == 403, (r.status_code, r.text)
        assert (await call(w, "editor_a", "GET", f"{BASE}/{pid}")).json()["title"] == "Pack vault"

        await _set_role(w, "member_a", "member")                 # nothing was deleted: promoted back, it is theirs again
        assert (await call(w, "member_a", "GET", f"{BASE}/{pid}")).json()["can_maintain"] is True
        assert (await call(w, "member_a", "PATCH", f"{BASE}/{pid}", json={"title": "Back"})).status_code == 200


async def test_a_publish_token_ends_when_its_maker_is_demoted_to_viewer_and_returns_with_the_role(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w, "editor_a", "ledger")
        assert (await _name(w, "editor_a", pid, "member_a", "maintain")).status_code == 200
        raw = await _token(w, "member_a", pid)
        assert (await publish(w, raw)).status_code == 200

        await _set_role(w, "member_a", "viewer")
        for method in ("GET", "POST"):
            r = await publish(w, raw, method)
            assert (r.status_code, r.content) == NOT_VALID, method
        r = await publish(w, raw, "GET", "/api/codebase/publish/archive")
        assert (r.status_code, r.content) == NOT_VALID

        await _set_role(w, "member_a", "member")
        assert (await publish(w, raw)).status_code == 200


async def test_a_maintainer_through_a_team_loses_the_write_right_as_a_viewer_and_keeps_the_read(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        team = w.ids["team_a"]
        pid = await _pack(w, "editor_a", "viateam", teams=[{"team_id": team, "permission": "maintain"}])
        raw = await _token(w, "owner_a", pid)                    # owner_a is in team-a
        assert (await call(w, "owner_a", "PATCH", f"{BASE}/{pid}", json={"title": "Mine"})).status_code == 200

        await _set_role(w, "owner_a", "viewer")

        seen = await call(w, "owner_a", "GET", f"{BASE}/{pid}")
        assert seen.status_code == 200 and seen.json()["can_maintain"] is False
        denied = await call(w, "owner_a", "PATCH", f"{BASE}/{pid}", json={"title": "Again"})
        assert denied.status_code == 403
        assert (await publish(w, raw)).content == NOT_VALID[1]


async def test_a_viewer_cannot_be_named_a_maintainer_but_can_be_named_a_reader(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w, "editor_a", "vault")
        before = await _member_count(w, pid)

        refused = await _name(w, "editor_a", pid, "viewer_a", "maintain")
        assert refused.status_code == 422, refused.text
        assert "viewer" in refused.json()["detail"].lower()
        assert await _member_count(w, pid) == before
        assert (await call(w, "viewer_a", "GET", f"{BASE}/{pid}")).status_code == 404   # still not on it

        reader = await _name(w, "editor_a", pid, "viewer_a", "read")
        assert reader.status_code == 200, reader.text
        seen = await call(w, "viewer_a", "GET", f"{BASE}/{pid}")
        assert seen.status_code == 200 and seen.json()["can_maintain"] is False

        again = await _name(w, "editor_a", pid, "viewer_a", "maintain")      # an existing reader is no exception
        assert again.status_code == 422
        assert (await call(w, "viewer_a", "GET", f"{BASE}/{pid}")).json()["can_maintain"] is False


async def test_a_viewer_named_beside_the_last_real_maintainer_does_not_let_them_step_down(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w, "editor_a", "vault")
        assert (await _name(w, "editor_a", pid, "member_a", "maintain")).status_code == 200
        # editor_a steps down while member_a still maintains: allowed
        assert (await _name(w, "member_a", pid, "editor_a", "read")).status_code == 200
        assert (await _name(w, "member_a", pid, "editor_a", "maintain")).status_code == 200

        await _set_role(w, "member_a", "viewer")                 # the other "maintainer" is now a reader in effect
        r = await _name(w, "editor_a", pid, "editor_a", "read")
        assert r.status_code == 422, r.text
        removed = await call(w, "editor_a", "DELETE", f"{BASE}/{pid}/members/{w.uid('editor_a')}")
        assert removed.status_code == 422
        assert (await call(w, "editor_a", "GET", f"{BASE}/{pid}")).json()["can_maintain"] is True


async def test_a_pack_whose_only_real_maintainer_leaves_is_reported_orphaned_beside_a_viewer_named_maintainer(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w, "editor_a", "vault")
        assert (await _name(w, "editor_a", pid, "member_a", "maintain")).status_code == 200
        await _set_role(w, "member_a", "viewer")

        removed = await call(w, "su", "DELETE", f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid('editor_a')}", None)
        assert removed.status_code == 204, removed.text
        orphaned = [row for row in w.audit if row.get("action") == "codebase.pack_orphaned"]
        assert [row["target"] for row in orphaned] == [pid]
