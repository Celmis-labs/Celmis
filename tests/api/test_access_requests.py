"""General access requests — over the real routes, against the real rule.

Product owner's decision: a signed-in person with no workspace but their own
personal one may ASK for access, in general — not to a named workspace, and
without ever learning which workspaces exist. Only the SUPERADMIN decides:
approve with one or more (workspace, role) grants, applied all or nothing
through `change_memberships`, or reject with a reason.

Every refusal below is followed by reading the state back: a 403 or a 422
that wrote anyway would pass a status-only test.
"""

from __future__ import annotations

import json

import pytest

from tests.api.rbac_world import B_SECRET, world

ME = "/api/access-requests/me"
CREATE = "/api/access-requests"
ADMIN = "/api/admin/access-requests"


async def _personal(w, who: str) -> str:
    """Give `who` a personal workspace (slug u-{id}), as sign-in would."""
    from src.api.workspace_provision import personal_slug
    from src.db.models import Workspace, WorkspaceMember

    uid = w.uid(who)
    ws_id = f"personal-{uid}"
    async with w.factory() as s:
        s.add(Workspace(id=ws_id, name=f"{who}'s workspace", slug=personal_slug(uid),
                        description=""))
        s.add(WorkspaceMember(workspace_id=ws_id, user_id=uid, role="owner"))
        await s.commit()
    return ws_id


async def _rows(w):
    from sqlalchemy import select

    from src.db.models import AccessRequest

    async with w.factory() as s:
        return (await s.scalars(select(AccessRequest))).all()


def _actions(w, prefix: str = "access_request.") -> list[str]:
    return [a["action"] for a in w.audit if a["action"].startswith(prefix)]


# ─── the requester ───────────────────────────────────────────────────


async def test_a_person_with_only_a_personal_workspace_may_ask(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await _personal(w, "loner")
        me = (await w.client.get(ME, headers=w.h("loner"))).json()
        assert me == {"eligible": True, "has_team_access": False, "request": None}

        r = await w.client.post(CREATE, json={"comment": "I am on the payments team"},
                                headers=w.h("loner"))
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "pending"

        me = (await w.client.get(ME, headers=w.h("loner"))).json()
        assert me["request"]["status"] == "pending"
        assert me["request"]["comment"] == "I am on the payments team"
        assert _actions(w) == ["access_request.created"]
        created = next(a for a in w.audit if a["action"] == "access_request.created")
        assert created["actor_id"] == w.uid("loner")


async def test_one_pending_request_at_a_time(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        assert (await w.client.post(CREATE, json={}, headers=w.h("loner"))).status_code == 201
        again = await w.client.post(CREATE, json={"comment": "again"}, headers=w.h("loner"))
        assert again.status_code == 409
        assert len(await _rows(w)) == 1


async def test_a_comment_is_bounded(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.post(CREATE, json={"comment": "x" * 1001}, headers=w.h("loner"))
        assert r.status_code == 422
        assert await _rows(w) == []
        ok = await w.client.post(CREATE, json={"comment": "x" * 1000}, headers=w.h("loner"))
        assert ok.status_code == 201


async def test_someone_with_team_access_is_not_offered_the_form(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        me = (await w.client.get(ME, headers=w.h("member_a"))).json()
        assert me["eligible"] is False and me["has_team_access"] is True
        r = await w.client.post(CREATE, json={}, headers=w.h("member_a"))
        assert r.status_code == 409
        assert await _rows(w) == []


async def test_cancel_then_ask_again(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        await w.client.post(CREATE, json={}, headers=w.h("loner"))
        assert (await w.client.delete(ME, headers=w.h("loner"))).status_code == 204
        assert (await w.client.get(ME, headers=w.h("loner"))).json()["request"]["status"] \
            == "cancelled"
        # nothing pending any more
        assert (await w.client.delete(ME, headers=w.h("loner"))).status_code == 404
        assert (await w.client.post(CREATE, json={}, headers=w.h("loner"))).status_code == 201
        assert _actions(w) == [
            "access_request.created", "access_request.cancelled", "access_request.created"]


async def test_a_new_request_is_allowed_after_a_rejection(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        rid = (await w.client.post(CREATE, json={}, headers=w.h("loner"))).json()["id"]
        r = await w.client.post(f"{ADMIN}/{rid}/reject", json={"reason": "Who are you?"},
                                headers=w.h("su"))
        assert r.status_code == 200, r.text
        me = (await w.client.get(ME, headers=w.h("loner"))).json()
        assert me["request"]["status"] == "rejected"
        assert me["request"]["decision_note"] == "Who are you?"
        assert me["request"]["grants"] == []
        assert (await w.client.post(CREATE, json={}, headers=w.h("loner"))).status_code == 201


async def test_the_requester_never_sees_a_workspace_name_before_approval(tmp_path, monkeypatch):
    """Not while pending, not after a rejection — nothing names a workspace.
    After an approval the GRANTED ones appear, and only those."""
    async with world(tmp_path, monkeypatch) as w:
        await _personal(w, "loner")
        created = await w.client.post(CREATE, json={"comment": "hi"}, headers=w.h("loner"))
        bodies = [created.text, (await w.client.get(ME, headers=w.h("loner"))).text]
        rid = created.json()["id"]
        await w.client.post(f"{ADMIN}/{rid}/reject", json={"reason": "not yet"},
                            headers=w.h("su"))
        bodies.append((await w.client.get(ME, headers=w.h("loner"))).text)
        for body in bodies:
            assert "Alpha" not in body and B_SECRET not in body
            assert w.ws["ws-a"] not in body and w.ws["ws-b"] not in body

        rid2 = (await w.client.post(CREATE, json={}, headers=w.h("loner"))).json()["id"]
        ok = await w.client.post(f"{ADMIN}/{rid2}/approve", headers=w.h("su"), json={
            "grants": [{"workspace_id": w.ws["ws-a"], "role": "viewer"}]})
        assert ok.status_code == 200, ok.text
        after = (await w.client.get(ME, headers=w.h("loner"))).json()
        assert after["request"]["status"] == "approved"
        assert [(g["workspace_name"], g["role"]) for g in after["request"]["grants"]] == [
            ("Alpha", "viewer")]
        assert B_SECRET not in json.dumps(after)
        # …and the switcher's source now lists it, with no new sign-in.
        mine = (await w.client.get("/api/workspaces", headers=w.h("loner"))).json()
        assert "Alpha" in {x["name"] for x in mine["workspaces"]}
        assert after["has_team_access"] is True and after["eligible"] is False


# ─── the superadmin ──────────────────────────────────────────────────


async def test_approve_applies_several_grants_with_audit(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        rid = (await w.client.post(CREATE, json={}, headers=w.h("loner"))).json()["id"]
        r = await w.client.post(f"{ADMIN}/{rid}/approve", headers=w.h("su"), json={"grants": [
            {"workspace_id": w.ws["ws-a"], "role": "editor"},
            {"workspace_id": w.ws["ws-b"], "role": "admin"},
        ]})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"
        assert await w.role("loner", "ws-a") == "editor"
        assert await w.role("loner", "ws-b") == "admin"

        grants = [a for a in w.audit if a["action"] == "workspace.member_role_changed"]
        assert {(a["workspace_id"], a["detail"]["new_role"]) for a in grants} == {
            (w.ws["ws-a"], "editor"), (w.ws["ws-b"], "admin")}
        assert all(a["detail"]["via"] == "access_request" for a in grants)
        assert all(a["actor_id"] == w.uid("su") and a["target"] == w.uid("loner")
                   for a in grants)
        assert _actions(w) == ["access_request.created", "access_request.approved"]


@pytest.mark.parametrize("bad", [
    {"workspace_id": "no-such-workspace", "role": "member"},
    {"workspace_id": "wsid-b", "role": "superuser"},
    "personal",
])
async def test_one_bad_grant_and_nothing_is_applied(tmp_path, monkeypatch, bad):
    async with world(tmp_path, monkeypatch) as w:
        other_personal = await _personal(w, "member_b")
        if bad == "personal":
            bad = {"workspace_id": other_personal, "role": "member"}
        rid = (await w.client.post(CREATE, json={}, headers=w.h("loner"))).json()["id"]
        r = await w.client.post(f"{ADMIN}/{rid}/approve", headers=w.h("su"), json={"grants": [
            {"workspace_id": w.ws["ws-a"], "role": "member"}, bad]})
        assert r.status_code == 422, r.text
        assert await w.role("loner", "ws-a") is None, "refused, and granted anyway"
        assert (await _rows(w))[0].status == "pending"
        assert not [a for a in w.audit if a["action"] == "workspace.member_role_changed"]


async def test_a_workspace_named_twice_is_refused(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        rid = (await w.client.post(CREATE, json={}, headers=w.h("loner"))).json()["id"]
        r = await w.client.post(f"{ADMIN}/{rid}/approve", headers=w.h("su"), json={"grants": [
            {"workspace_id": w.ws["ws-a"], "role": "member"},
            {"workspace_id": w.ws["ws-a"], "role": "admin"}]})
        assert r.status_code == 422
        assert await w.role("loner", "ws-a") is None


async def test_approve_needs_at_least_one_grant(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        rid = (await w.client.post(CREATE, json={}, headers=w.h("loner"))).json()["id"]
        r = await w.client.post(f"{ADMIN}/{rid}/approve", headers=w.h("su"),
                                json={"grants": []})
        assert r.status_code == 422
        assert (await _rows(w))[0].status == "pending"


async def test_a_grant_that_fails_at_write_time_rolls_the_approval_back(tmp_path, monkeypatch):
    """Validation passed, the membership writer refused: the request must stay
    pending and no membership may exist. Simulated by deleting the second
    workspace between the router's check and the write."""
    import src.api.routers.access_requests as ar

    real = ar.change_memberships

    async def _vanish_then_write(session, **kw):
        from src.db.models import Workspace

        gone = await session.get(Workspace, w.ws["ws-b"])
        await session.delete(gone)
        await session.flush()
        return await real(session, **kw)

    async with world(tmp_path, monkeypatch) as w:
        monkeypatch.setattr(ar, "change_memberships", _vanish_then_write)
        rid = (await w.client.post(CREATE, json={}, headers=w.h("loner"))).json()["id"]
        r = await w.client.post(f"{ADMIN}/{rid}/approve", headers=w.h("su"), json={"grants": [
            {"workspace_id": w.ws["ws-a"], "role": "member"},
            {"workspace_id": w.ws["ws-b"], "role": "member"}]})
        assert r.status_code == 404, r.text
        assert await w.role("loner", "ws-a") is None
        assert (await _rows(w))[0].status == "pending"
        assert _actions(w) == ["access_request.created"]


async def test_reject_requires_a_reason(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        rid = (await w.client.post(CREATE, json={}, headers=w.h("loner"))).json()["id"]
        for body in ({}, {"reason": ""}, {"reason": "   "}):
            r = await w.client.post(f"{ADMIN}/{rid}/reject", json=body, headers=w.h("su"))
            assert r.status_code == 422, body
        assert (await _rows(w))[0].status == "pending"
        assert "access_request.rejected" not in _actions(w)


@pytest.mark.parametrize("first, second", [
    ("approve", "approve"), ("approve", "reject"), ("reject", "approve"), ("reject", "reject"),
])
async def test_deciding_twice_is_409(tmp_path, monkeypatch, first, second):
    async with world(tmp_path, monkeypatch) as w:
        rid = (await w.client.post(CREATE, json={}, headers=w.h("loner"))).json()["id"]
        bodies = {"approve": {"grants": [{"workspace_id": w.ws["ws-a"], "role": "viewer"}]},
                  "reject": {"reason": "no"}}
        one = await w.client.post(f"{ADMIN}/{rid}/{first}", json=bodies[first],
                                  headers=w.h("su"))
        assert one.status_code == 200, one.text
        status_after_first = (await _rows(w))[0].status
        two = await w.client.post(f"{ADMIN}/{rid}/{second}", json=bodies[second],
                                  headers=w.h("su"))
        assert two.status_code == 409
        assert (await _rows(w))[0].status == status_after_first


async def test_a_cancelled_request_cannot_be_decided(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        rid = (await w.client.post(CREATE, json={}, headers=w.h("loner"))).json()["id"]
        await w.client.delete(ME, headers=w.h("loner"))
        r = await w.client.post(f"{ADMIN}/{rid}/approve", headers=w.h("su"), json={
            "grants": [{"workspace_id": w.ws["ws-a"], "role": "viewer"}]})
        assert r.status_code == 409
        assert await w.role("loner", "ws-a") is None


@pytest.mark.parametrize("who", ["gadmin", "owner_a", "admin_a", "member_a", "loner"])
async def test_only_the_superadmin_reaches_the_admin_endpoints(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        rid = (await w.client.post(CREATE, json={}, headers=w.h("loner"))).json()["id"]
        assert (await w.client.get(ADMIN, headers=w.h(who))).status_code == 403
        r = await w.client.post(f"{ADMIN}/{rid}/approve", headers=w.h(who), json={
            "grants": [{"workspace_id": w.ws["ws-a"], "role": "member"}]})
        assert r.status_code == 403
        r = await w.client.post(f"{ADMIN}/{rid}/reject", headers=w.h(who),
                                json={"reason": "no"})
        assert r.status_code == 403
        assert (await _rows(w))[0].status == "pending"
        assert await w.role("loner", "ws-a") is None


async def test_the_list_puts_pending_first_and_filters(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        # An older rejected request, then a newer one still pending.
        old = (await w.client.post(CREATE, json={"comment": "first"},
                                   headers=w.h("loner"))).json()["id"]
        await w.client.post(f"{ADMIN}/{old}/reject", json={"reason": "later"},
                            headers=w.h("su"))
        await w.client.post(CREATE, json={"comment": "second"}, headers=w.h("loner"))

        rows = (await w.client.get(ADMIN, headers=w.h("su"))).json()
        assert [r["status"] for r in rows] == ["pending", "rejected"]
        first = rows[0]
        assert first["email"] == "loner@acme-corp.io" and first["name"] == "loner"
        assert first["comment"] == "second"
        assert isinstance(first["sign_in_methods"], list) and first["created_at"]

        only = (await w.client.get(f"{ADMIN}?status=rejected", headers=w.h("su"))).json()
        assert [r["decision_note"] for r in only] == ["later"]
        assert (await w.client.get(f"{ADMIN}?status=bogus", headers=w.h("su"))).status_code \
            == 422


# ─── /admin/users "No team access" ───────────────────────────────────


async def test_the_users_filter_finds_people_with_no_team_access(tmp_path, monkeypatch):
    """Any sign-in method; a personal workspace does not count as access;
    newest first."""
    import src.users.store as users_mod
    from src.users import User, UserAuthMethod

    async with world(tmp_path, monkeypatch) as w:
        store = users_mod._default_store
        for name, method, extra, created in [
            ("pw-new", UserAuthMethod.PASSWORD, {"password_hash": b"x"}, "2026-09-03T00:00:00+00:00"),
            ("google-new", UserAuthMethod.GOOGLE_OAUTH, {"google_sub": "g-1"},
             "2026-09-02T00:00:00+00:00"),
            ("sso-new", UserAuthMethod.OIDC, {"oidc_iss": "https://idp", "oidc_sub": "s-1"},
             "2026-09-01T00:00:00+00:00"),
        ]:
            u = User(id=f"u-{name}", email=f"{name}@example.com", name=name,
                     auth_method=method, created_at=created, **extra)
            store.create(u)
            w.users[name] = store.get_by_id(u.id)
            await _personal(w, name)

        r = await w.client.get("/api/admin/users?no_team_access=true", headers=w.h("su"))
        assert r.status_code == 200, r.text
        rows = r.json()
        emails = [x["email"] for x in rows]
        new = [e for e in emails if e.endswith("@example.com")]
        assert new == ["pw-new@example.com", "google-new@example.com", "sso-new@example.com"]
        assert "loner@acme-corp.io" in emails
        assert not {"member_a@acme-corp.io", "both@acme-corp.io",
                    "root@acme-corp.io"} & set(emails)
        by_email = {x["email"]: x for x in rows}
        assert by_email["google-new@example.com"]["sign_in_methods"] == ["google"]
        assert by_email["sso-new@example.com"]["sign_in_methods"] == ["oidc"]
        assert by_email["pw-new@example.com"]["sign_in_methods"] == ["password"]
        assert all(x["has_team_access"] is False for x in rows)

        # Granting from the users page takes them out of the filter.
        put = await w.client.put(
            f"/api/admin/users/{w.uid('sso-new')}/memberships/{w.ws['ws-a']}",
            json={"role": "member"}, headers=w.h("su"))
        assert put.status_code == 200, put.text
        rows = (await w.client.get("/api/admin/users?no_team_access=true",
                                   headers=w.h("su"))).json()
        assert "sso-new@example.com" not in {x["email"] for x in rows}


@pytest.mark.parametrize("who", ["gadmin", "admin_a"])
async def test_the_users_filter_is_superadmin_only(tmp_path, monkeypatch, who):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.get("/api/admin/users?no_team_access=true", headers=w.h(who))
        assert r.status_code == 403
