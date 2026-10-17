"""Leaving a workspace ends what a person holds on its knowledge packs.

The contract (docs/CODEBASE.md "Lifecycle"):

  * removing a member — by an admin or by the person leaving — deletes their names
    on the workspace's packs and revokes the publish tokens they made there, in
    the same transaction, so adding them back later (in ANY role) restores
    nothing: not the pack they could open, not a token that could publish;
  * a restricted pack that only that person could maintain is left exactly as it
    is (visible to the superadmin alone, never handed to an owner who was not
    named on it) and the removal writes one `codebase.pack_orphaned` audit row
    whose target is the pack; the superadmin names a new maintainer in the
    ordinary Sharing route and the pack is whole again;
  * a pack that somebody else can still maintain, and a pack that is not
    restricted, are not orphaned and write no such row.
"""

from __future__ import annotations

from sqlalchemy import select

from src.db.models import KnowledgePackMember, KnowledgePackToken
from tests.api.codebase_support import call, note_zip, upload_zip
from tests.api.rbac_world import world

PUBLISH = "/api/codebase/publish"


async def _pack(w, who: str, slug: str, audience: str = "restricted") -> str:
    r = await call(w, who, "POST", "/api/codebase/packs", json={
        "slug": slug, "title": f"Pack {slug}", "description": "d",
        "audience": audience, "teams": []})
    assert r.status_code == 201, r.text
    r2 = await upload_zip(w, who, r.json()["id"], note_zip(slug))
    assert r2.status_code == 201, r2.text
    return r.json()["id"]


async def _token(w, who: str, pid: str) -> str:
    r = await call(w, who, "POST", f"/api/codebase/packs/{pid}/tokens",
                   json={"label": "ci", "expires_in_days": 30})
    assert r.status_code == 201, r.text
    return r.json()["token"]


async def _publish_status(w, raw: str) -> int:
    r = await w.client.get(PUBLISH, headers={"Authorization": f"Bearer {raw}"})
    return r.status_code


async def _remove(w, who: str, member: str) -> None:
    r = await call(w, who, "DELETE", f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid(member)}",
                   None)
    assert r.status_code == 204, r.text


async def _add(w, who: str, member: str, role: str) -> None:
    r = await call(w, who, "PUT", f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid(member)}",
                   None, json={"role": role})
    assert r.status_code == 200, r.text


def _orphan_rows(w) -> list[dict]:
    return [row for row in w.audit if row.get("action") == "codebase.pack_orphaned"]


async def test_a_member_added_back_after_being_removed_holds_nothing_on_a_restricted_pack(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w, "editor_a", "vault")
        r = await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/members",
                       json={"email": "member-a@acme-corp.io", "permission": "maintain"})
        assert r.status_code == 200, r.text
        raw = await _token(w, "member_a", pid)
        r = await call(w, "member_a", "GET", f"/api/codebase/packs/{pid}")
        assert r.status_code == 200 and r.json()["can_maintain"] is True
        assert await _publish_status(w, raw) == 200

        await _remove(w, "su", "member_a")

        async with w.factory() as s:
            named = (await s.scalars(select(KnowledgePackMember).where(
                KnowledgePackMember.user_id == w.uid("member_a")))).all()
            tokens = (await s.scalars(select(KnowledgePackToken).where(
                KnowledgePackToken.created_by_id == w.uid("member_a")))).all()
        assert named == []
        assert len(tokens) == 1 and tokens[0].revoked_at is not None     # kept as a record

        await _add(w, "su", "member_a", "viewer")
        r = await call(w, "member_a", "GET", f"/api/codebase/packs/{pid}")
        assert r.status_code == 404
        assert await _publish_status(w, raw) == 401


async def test_a_publish_token_does_not_come_back_when_its_maker_is_added_back_as_an_admin(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w, "editor_a", "open-notes", audience="workspace")
        raw = await _token(w, "admin2_a", pid)
        assert await _publish_status(w, raw) == 200

        await _remove(w, "su", "admin2_a")
        await _add(w, "su", "admin2_a", "admin")

        r = await call(w, "admin2_a", "GET", f"/api/codebase/packs/{pid}")
        assert r.status_code == 200 and r.json()["can_maintain"] is True   # a workspace admin
        assert await _publish_status(w, raw) == 401                        # but not the old token


async def test_the_last_maintainer_of_a_restricted_pack_leaving_orphans_it_for_the_superadmin(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w, "editor_a", "vault")
        for who in ("owner_a", "admin_a"):
            assert (await call(w, who, "GET", f"/api/codebase/packs/{pid}")).status_code == 404

        await _remove(w, "su", "editor_a")

        rows = _orphan_rows(w)
        assert [(r["target"], r["workspace_id"]) for r in rows] == [(pid, w.ws["ws-a"])]
        assert rows[0]["detail"] == {"reason": "member_left"}
        assert pid not in repr(rows[0]["detail"])

        # left exactly as it was: nobody inherits it
        for who in ("owner_a", "admin_a", "gadmin"):
            r = await call(w, who, "GET", f"/api/codebase/packs/{pid}")
            assert r.status_code == 404, who
        assert (await call(w, "su", "GET", f"/api/codebase/packs/{pid}")).status_code == 200

        # the superadmin names a maintainer in the ordinary Sharing route
        r = await call(w, "su", "POST", f"/api/codebase/packs/{pid}/members",
                       json={"email": "admin-a@acme-corp.io", "permission": "maintain"})
        assert r.status_code == 200, r.text
        r = await call(w, "admin_a", "GET", f"/api/codebase/packs/{pid}")
        assert r.status_code == 200 and r.json()["can_maintain"] is True


async def test_a_restricted_pack_somebody_else_can_still_maintain_is_not_orphaned(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _pack(w, "editor_a", "shared-vault")
        r = await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/members",
                       json={"email": "member-a@acme-corp.io", "permission": "maintain"})
        assert r.status_code == 200, r.text

        await _remove(w, "su", "editor_a")

        assert _orphan_rows(w) == []
        assert (await call(w, "member_a", "GET", f"/api/codebase/packs/{pid}")).status_code == 200


async def test_a_pack_that_is_not_restricted_is_never_orphaned(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _pack(w, "editor_a", "everyones", audience="workspace")
        await _pack(w, "editor_a", "for-admins", audience="teams")

        await _remove(w, "su", "editor_a")

        assert _orphan_rows(w) == []
