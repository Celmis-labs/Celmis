"""Naming people and sharing a link, asked of the real routes.

The contract (docs/CODEBASE.md, "Sharing"):

  * a maintainer names a person of THIS workspace by email — a person who is not
    here and a person who does not exist get the same answer — and a named
    person sees the pack at the permission named, however its audience reads;
  * an invite link is a bearer token shown once, stored only as a hash, always
    for reading; it opens the pack to someone who already holds a role in the
    workspace and to nobody else, never creates an account, and is spent by use,
    expiry or revocation;
  * every way an invite can fail — unknown, revoked, spent, expired, someone
    else's workspace, no role here — is the same `404`, byte for byte;
  * an invite is previewed before it is used, and previewing changes nothing.
"""

from __future__ import annotations

from tests.api.codebase_support import call, note_zip, upload_zip
from tests.api.rbac_world import B_PACK_ID, B_PACK_TOKEN, B_SECRET, world

HIDDEN = (404, b'{"detail":"pack not found or not accessible"}')
GONE = (404, b'{"detail":"invite not found or expired"}')


async def _restricted(w, slug="vault") -> str:
    r = await call(w, "editor_a", "POST", "/api/codebase/packs",
                   json={"slug": slug, "title": "Vault", "audience": "restricted"})
    pid = r.json()["id"]
    assert (await upload_zip(w, "editor_a", pid, note_zip(slug))).status_code == 201
    return pid


async def _invite(w, pid: str, **body) -> tuple[str, str]:
    r = await call(w, "editor_a", "POST", f"/api/codebase/packs/{pid}/invites", json=body)
    assert r.status_code == 201, r.text
    return r.json()["invite"]["id"], r.json()["token"]


async def _answer(w, who: str, path: str, token: str, ws: str = "ws-a"):
    r = await call(w, who, "POST", f"/api/codebase/invites/{path}", ws, json={"token": token})
    return r.status_code, r.content


async def test_a_named_person_sees_the_pack_at_the_permission_named_until_removed(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _restricted(w)
        base = f"/api/codebase/packs/{pid}"
        assert (await call(w, "member_a", "GET", base)).status_code == 404

        r = await call(w, "editor_a", "POST", f"{base}/members",
                       json={"email": "member-a@acme-corp.io", "permission": "read"})
        assert r.status_code == 200, r.text
        assert {(m["email"], m["permission"]) for m in r.json()["members"]} >= {
            ("member-a@acme-corp.io", "read")}
        seen = await call(w, "member_a", "GET", base)
        assert seen.status_code == 200 and seen.json()["can_maintain"] is False

        # upgrading the same person is the same call
        r = await call(w, "editor_a", "POST", f"{base}/members",
                       json={"email": "member-a@acme-corp.io", "permission": "maintain"})
        assert r.status_code == 200
        assert (await call(w, "member_a", "GET", base)).json()["can_maintain"] is True

        gone = await call(w, "editor_a", "DELETE", f"{base}/members/u-member_a")
        assert gone.status_code == 204
        assert (await call(w, "member_a", "GET", base)).status_code == 404


async def test_an_unknown_and_a_foreign_person_get_the_same_answer(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _restricted(w)
        base = f"/api/codebase/packs/{pid}"
        answers = set()
        for email in ("member-b@acme-corp.io", "loner@acme-corp.io", "nobody@acme-corp.io",
                      "MEMBER-B@ACME-CORP.IO"):
            r = await call(w, "editor_a", "POST", f"{base}/members",
                           json={"email": email, "permission": "read"})
            answers.add((r.status_code, r.content))
        assert answers == {(404, b'{"detail":"person not found in this workspace"}')}
        bad = await call(w, "editor_a", "POST", f"{base}/members",
                         json={"email": "member-a@acme-corp.io", "permission": "root"})
        assert bad.status_code == 422
        extra = await call(w, "editor_a", "POST", f"{base}/members",
                           json={"email": "member-a@acme-corp.io", "role": "owner"})
        assert extra.status_code == 422


async def test_the_last_maintainer_of_a_restricted_pack_cannot_step_down(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _restricted(w)
        base = f"/api/codebase/packs/{pid}"
        lock = (422, b'{"detail":"That change would leave you without access to the pack."}')
        gone = await call(w, "editor_a", "DELETE", f"{base}/members/u-editor_a")
        down = await call(w, "editor_a", "POST", f"{base}/members",
                          json={"email": "editor-a@acme-corp.io", "permission": "read"})
        assert (gone.status_code, gone.content) == lock and (down.status_code, down.content) == lock
        assert (await call(w, "editor_a", "GET", base)).json()["can_maintain"] is True

        # with a second maintainer named, stepping down is allowed
        named = await call(w, "editor_a", "POST", f"{base}/members",
                           json={"email": "member-a@acme-corp.io", "permission": "maintain"})
        assert named.status_code == 200
        assert (await call(w, "editor_a", "DELETE", f"{base}/members/u-editor_a")).status_code == 204
        assert (await call(w, "editor_a", "GET", base)).status_code == 404
        assert (await call(w, "member_a", "GET", base)).json()["can_maintain"] is True


async def test_a_team_grant_must_name_a_team_of_this_workspace(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _restricted(w)
        base = f"/api/codebase/packs/{pid}"
        r = await call(w, "editor_a", "PUT", f"{base}/teams",
                       json={"teams": [{"team_id": w.ids["team_b"], "permission": "read"}]})
        assert (r.status_code, r.content) == (404, b'{"detail":"team not found"}')
        assert B_SECRET.encode() not in r.content
        ok = await call(w, "editor_a", "PUT", f"{base}/teams",
                        json={"teams": [{"team_id": w.ids["team_a"], "permission": "read"}]})
        assert ok.status_code == 200 and [t["name"] for t in ok.json()["teams"]] == ["alpha-team"]
        # team-a's members now read it; the others still cannot
        assert (await call(w, "owner_a", "GET", base)).status_code == 200
        assert (await call(w, "member_a", "GET", base)).status_code == 404


async def test_an_invite_is_shown_once_for_reading_and_opens_the_pack_to_one_member(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _restricted(w)
        base = f"/api/codebase/packs/{pid}"
        invite_id, token = await _invite(w, pid, max_uses=2)
        assert len(token) >= 40
        listed = await call(w, "editor_a", "GET", f"{base}/invites")
        assert token not in listed.text and "token_hash" not in listed.text
        assert [(i["id"], i["max_uses"], i["used_count"], i["revoked"])
                for i in listed.json()["invites"]] == [(invite_id, 2, 0, False)]

        preview = await call(w, "member_a", "POST", "/api/codebase/invites/preview",
                             json={"token": token})
        assert preview.status_code == 200, preview.text
        assert preview.json() == {"pack_title": "Vault", "pack_description": "",
                                  "workspace_slug": "ws-a", "workspace_name": preview.json()["workspace_name"],
                                  "already_member": False}
        assert (await call(w, "member_a", "GET", base)).status_code == 404     # preview opens nothing

        redeemed = await call(w, "member_a", "POST", "/api/codebase/invites/redeem",
                              json={"token": token})
        assert redeemed.status_code == 200 and redeemed.json() == {
            "pack_id": pid, "workspace_slug": "ws-a"}
        mine = await call(w, "member_a", "GET", base)
        assert mine.status_code == 200 and mine.json()["can_maintain"] is False   # reading only
        denied = await call(w, "member_a", "PATCH", base, json={"title": "x"})
        assert denied.status_code == 403
        again = await call(w, "member_a", "POST", "/api/codebase/invites/redeem",
                           json={"token": token})
        assert again.status_code == 200
        used = (await call(w, "editor_a", "GET", f"{base}/invites")).json()["invites"][0]
        assert used["used_count"] == 1                                         # once per person
        assert (await call(w, "member_a", "GET", f"{base}/invites")).status_code == 403


async def test_every_way_an_invite_can_fail_is_the_same_404(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _restricted(w)
        invite_id, token = await _invite(w, pid, max_uses=1)
        revoked_id, revoked = await _invite(w, pid)
        await call(w, "editor_a", "DELETE", f"/api/codebase/packs/{pid}/invites/{revoked_id}")
        spent_id, spent = await _invite(w, pid, max_uses=1)
        assert (await _answer(w, "viewer_a", "redeem", spent))[0] == 200            # spends it

        outcomes = {
            "garbage": await _answer(w, "member_a", "redeem", "x" * 43),
            "revoked": await _answer(w, "member_a", "redeem", revoked),
            "spent": await _answer(w, "owner_a", "redeem", spent),
            "no role here": await _answer(w, "loner", "redeem", token, "ws-a"),
            "other workspace": await _answer(w, "member_b", "redeem", token, "ws-b"),
            "garbage preview": await _answer(w, "member_a", "preview", "y" * 43),
            "revoked preview": await _answer(w, "member_a", "preview", revoked),
            "spent preview": await _answer(w, "owner_a", "preview", spent),
            "no role preview": await _answer(w, "loner", "preview", token),
        }
        assert set(outcomes.values()) == {GONE}, outcomes
        # a hash is not a token
        import hashlib
        digest = hashlib.sha256(token.encode()).hexdigest()
        assert await _answer(w, "member_a", "redeem", digest) == GONE


async def test_revoking_an_invite_needs_a_maintainer_and_the_invites_own_pack(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _restricted(w, "one")
        other = await _restricted(w, "two")
        invite_id, token = await _invite(w, pid)
        url = f"/api/codebase/packs/{pid}/invites/{invite_id}"
        assert (await call(w, "member_a", "DELETE", url)).status_code == 404       # cannot see it
        wrong = await call(w, "editor_a", "DELETE", f"/api/codebase/packs/{other}/invites/{invite_id}")
        assert wrong.status_code == 404
        assert (await call(w, "editor_a", "DELETE",
                           f"/api/codebase/packs/{pid}/invites/not-an-id")).status_code == 404
        assert (await call(w, "editor_a", "DELETE", url)).status_code == 204
        assert await _answer(w, "member_a", "redeem", token) == GONE


async def test_a_member_of_both_workspaces_can_use_bs_link_and_a_member_of_a_cannot(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        ok = await call(w, "both", "POST", "/api/codebase/invites/redeem", "ws-b",
                        json={"token": B_PACK_TOKEN})
        assert ok.status_code == 200
        assert ok.json() == {"pack_id": B_PACK_ID, "workspace_slug": "ws-b"}
        for who in ("member_a", "viewer_a", "loner", "owner_a"):
            for path in ("preview", "redeem"):
                got = await _answer(w, who, path, B_PACK_TOKEN)
                assert got == GONE, (who, path)
                assert B_SECRET.encode() not in got[1]


async def test_an_invite_for_a_hidden_pack_is_invisible_to_those_who_cannot_see_it(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        pid = await _restricted(w)
        base = f"/api/codebase/packs/{pid}"
        for who in ("member_a", "admin_a", "gadmin"):
            for method, path, kw in (("GET", f"{base}/invites", {}),
                                     ("POST", f"{base}/invites", {"json": {}}),
                                     ("DELETE", f"{base}/invites/anything", {})):
                r = await call(w, who, method, path, **kw)
                assert (r.status_code, r.content) == HIDDEN, (who, method)
