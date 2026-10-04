"""Who may grant which workspace role — over the real routes.

The rule (src/users/roles.py `can_change`), product owner's decision:

  * owner — the SUPERADMIN only (the env master account): grant, change to
    or from, remove.
  * admin / editor — the superadmin, or that workspace's OWNER.
  * member / viewer — the superadmin, or that workspace's owner/admin.
  * A shared workspace is created by the superadmin only.
  * A global admin who is not the master account is NOT a superadmin.

Every path that writes a membership is exercised: PUT/DELETE members, invite
create (link and direct-add), invite accept, the reset link (a takeover is a
grant), workspace creation and deletion, and the superadmin's Users page.
Each refusal is followed by reading the row back — a 403 that wrote anyway
would pass a status-only test.
"""

from __future__ import annotations

import pytest

from tests.api.rbac_world import world

# ─── PUT /api/workspaces/{id}/members/{user} ─────────────────────────


@pytest.mark.parametrize("actor, target, new_role, ok", [
    # an admin manages members and viewers …
    ("admin_a", "member_a", "viewer", True),
    ("admin_a", "viewer_a", "member", True),
    ("owner_a", "member_a", "viewer", True),
    # … and nothing above them, in either direction
    ("admin_a", "member_a", "editor", False),
    ("admin_a", "member_a", "admin", False),
    ("admin_a", "member_a", "owner", False),
    ("admin_a", "owner_a", "member", False),      # admin cannot demote the owner
    ("admin_a", "admin2_a", "member", False),     # nor another admin
    ("admin_a", "editor_a", "viewer", False),     # nor an editor
    ("admin_a", "admin_a", "owner", False),       # nor promote themselves
    # the owner manages admins and editors too …
    ("owner_a", "admin_a", "member", True),
    ("owner_a", "member_a", "admin", True),
    ("owner_a", "member_a", "editor", True),
    ("owner_a", "editor_a", "admin", True),
    ("owner_a", "admin2_a", "editor", True),
    # … but never owner, nor their own row
    ("owner_a", "member_a", "owner", False),
    ("owner_a", "admin_a", "owner", False),
    ("owner_a", "owner_a", "admin", False),
    # editor / member / viewer grant nothing
    ("editor_a", "member_a", "viewer", False),
    ("member_a", "viewer_a", "member", False),
    ("viewer_a", "member_a", "viewer", False),
    # a global admin who is not the master account is not a superadmin
    ("gadmin", "member_a", "admin", False),
    ("gadmin", "member_a", "viewer", False),
    # the superadmin may do all of it
    ("su", "member_a", "editor", True),
    ("su", "owner_a", "member", True),
    ("su", "admin_a", "owner", True),
    ("su", "editor_a", "viewer", True),
])
async def test_put_member_follows_the_grant_matrix(tmp_path, monkeypatch, actor, target,
                                                   new_role, ok):
    async with world(tmp_path, monkeypatch) as w:
        before = await w.role(target, "ws-a")
        r = await w.client.put(
            f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid(target)}",
            json={"role": new_role}, headers=w.h(actor))
        after = await w.role(target, "ws-a")
        if ok:
            assert r.status_code == 200, r.text
            assert after == new_role
            row = [a for a in w.audit if a["action"] == "workspace.member_role_changed"]
            assert row and row[-1]["detail"]["old_role"] == before
            assert row[-1]["detail"]["new_role"] == new_role
            assert row[-1]["actor_id"] == w.uid(actor)
            assert row[-1]["target"] == w.uid(target)
            assert row[-1]["workspace_id"] == w.ws["ws-a"]
        else:
            assert r.status_code == 403, r.text
            assert after == before, "refused, and wrote anyway"
            assert not [a for a in w.audit if a["action"] == "workspace.member_role_changed"]


async def test_enrolling_a_stranger_is_still_refused(tmp_path, monkeypatch):
    """The enrolment boundary (tests/security/test_member_enrolment_boundary.py)
    still holds: member_a shares no workspace with B's admin, so even a
    grantable role cannot be written for them — they join by invite."""
    async with world(tmp_path, monkeypatch) as w:
        url = f"/api/workspaces/{w.ws['ws-b']}/members/{w.uid('member_a')}"
        r = await w.client.put(url, json={"role": "viewer"}, headers=w.h("admin_b"))
        assert r.status_code == 403
        assert await w.role("member_a", "ws-b") is None


async def test_the_superadmin_adds_anyone_with_any_role(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        url = f"/api/workspaces/{w.ws['ws-b']}/members/{w.uid('loner')}"
        r = await w.client.put(url, json={"role": "editor"}, headers=w.h("su"))
        assert r.status_code == 200, r.text
        assert await w.role("loner", "ws-b") == "editor"


@pytest.mark.parametrize("actor, target, ok", [
    ("admin_a", "member_a", True),
    ("admin_a", "viewer_a", True),
    ("admin_a", "owner_a", False),
    ("admin_a", "admin2_a", False),
    ("admin_a", "editor_a", False),
    ("owner_a", "admin_a", True),
    ("owner_a", "editor_a", True),
    ("owner_a", "owner_a", False),
    ("editor_a", "member_a", False),
    ("gadmin", "member_a", False),
    ("su", "owner_a", True),
    ("su", "editor_a", True),
])
async def test_delete_member_follows_the_grant_matrix(tmp_path, monkeypatch, actor, target, ok):
    async with world(tmp_path, monkeypatch) as w:
        before = await w.role(target, "ws-a")
        r = await w.client.delete(
            f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid(target)}", headers=w.h(actor))
        after = await w.role(target, "ws-a")
        if ok:
            assert r.status_code == 204, r.text
            assert after is None
            row = [a for a in w.audit if a["action"] == "workspace.member_role_changed"][-1]
            assert (row["detail"]["old_role"], row["detail"]["new_role"]) == (before, None)
        else:
            assert r.status_code == 403, r.text
            assert after == before


@pytest.mark.parametrize("actor, target, ok", [
    ("admin_a", "member_a", True),
    ("admin_a", "owner_a", False),     # the takeover the demotion rule would leave open
    ("admin_a", "admin2_a", False),
    ("admin_a", "editor_a", False),
    ("owner_a", "admin_a", True),      # the owner may re-role them, so may reset
    ("owner_a", "editor_a", True),
    ("owner_a", "both", False),        # ... but not past their own workspace
    ("admin_a", "both", False),
    ("su", "owner_a", True),
])
async def test_a_reset_link_is_bounded_like_a_grant(tmp_path, monkeypatch, actor, target, ok):
    import src.api.routers.users as users_router

    monkeypatch.setattr(users_router, "build_reset_link",
                        lambda target: ("/reset/x", "2099-01-01T00:00:00+00:00"))
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.post(
            f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid(target)}/reset-link",
            headers=w.h(actor))
        assert r.status_code == (200 if ok else 403), r.text


# ─── workspace creation / deletion ───────────────────────────────────


@pytest.mark.parametrize("actor, status", [
    ("su", 201), ("gadmin", 403), ("owner_a", 403), ("admin_a", 403), ("loner", 403),
])
async def test_only_the_superadmin_creates_a_shared_workspace(tmp_path, monkeypatch, actor,
                                                              status):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.post("/api/workspaces", headers=w.h(actor),
                                json={"name": "Gamma", "slug": "gamma"})
        assert r.status_code == status, r.text


@pytest.mark.parametrize("actor, status", [
    ("admin_a", 403), ("editor_a", 403), ("gadmin", 403), ("owner_a", 204), ("su", 204),
])
async def test_only_the_owner_or_superadmin_deletes_a_workspace(tmp_path, monkeypatch, actor,
                                                                status):
    from src.db.models import Workspace

    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.delete(f"/api/workspaces/{w.ws['ws-a']}", headers=w.h(actor))
        assert r.status_code == status, r.text
        still = await w.scalar(Workspace, w.ws["ws-a"])
        assert (still is None) == (status == 204)


def test_personal_workspace_provisioning_still_makes_an_owner():
    """Signup/login provisioning is not the shared-workspace route: everybody
    still owns their own personal workspace."""
    import ast
    import inspect

    import src.api.workspace_provision as wp

    tree = ast.parse(inspect.getsource(wp))
    roles = {
        kw.value.value
        for n in ast.walk(tree) if isinstance(n, ast.Call)
        and getattr(n.func, "id", "") == "WorkspaceMember"
        for kw in n.keywords if kw.arg == "role" and isinstance(kw.value, ast.Constant)
    }
    assert roles == {"owner"}


# ─── invites: create ─────────────────────────────────────────────────


@pytest.mark.parametrize("actor, role, status", [
    ("admin_a", "member", 201),
    ("admin_a", "viewer", 201),
    ("owner_a", "viewer", 201),
    ("owner_a", "editor", 201),
    ("owner_a", "admin", 201),
    ("admin_a", "editor", 403),
    ("admin_a", "admin", 403),
    ("owner_a", "owner", 403),
    ("admin_a", "superuser", 422),
    ("editor_a", "member", 403),     # editors do not manage people
    ("member_a", "member", 403),
    ("su", "editor", 201),
    ("su", "owner", 201),
])
async def test_an_invite_carries_only_a_grantable_role(tmp_path, monkeypatch, actor, role,
                                                       status):
    async with world(tmp_path, monkeypatch) as w:
        # The superadmin is not a member of A; it addresses A by header.
        headers = w.h(actor, "ws-a" if actor == "su" else None)
        r = await w.client.post("/api/invites", headers=headers, json={"role": role})
        assert r.status_code == status, r.text


async def test_direct_add_cannot_rerole_an_admin(tmp_path, monkeypatch):
    """Inviting an EXISTING account by email re-roles it on the spot — the
    same write as PUT /members, so the same rule: an admin's member-invite
    addressed to the owner must not demote them."""
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.post("/api/invites", headers=w.h("admin_a"),
                                json={"role": "member", "email": "owner-a@acme-corp.io"})
        assert r.status_code == 403, r.text
        assert await w.role("owner_a", "ws-a") == "owner"
        ok = await w.client.post("/api/invites", headers=w.h("admin_a"),
                                 json={"role": "viewer", "email": "member-a@acme-corp.io"})
        assert ok.status_code == 201 and ok.json()["added_directly"]
        assert await w.role("member_a", "ws-a") == "viewer"


# ─── invites: accept ─────────────────────────────────────────────────


async def _link(w, actor: str, role: str, ws: str | None = None) -> str:
    r = await w.client.post("/api/invites", headers=w.h(actor, ws), json={"role": role,
                                                                       "max_uses": 5})
    assert r.status_code == 201, r.text
    return r.json()["token"]


async def test_accepting_a_member_link_adds_a_member(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        token = await _link(w, "admin_a", "member")
        r = await w.client.post("/api/invites/accept", json={"token": token},
                                headers=w.h("loner"))
        assert r.status_code == 200, r.text
        assert await w.role("loner", "ws-a") == "member"


async def test_a_link_from_a_removed_admin_grants_nothing(tmp_path, monkeypatch):
    """The right is re-checked at redemption, against the inviter as they are
    THEN. A never-expiring link outlives the admin who made it."""
    async with world(tmp_path, monkeypatch) as w:
        token = await _link(w, "admin_a", "member")
        r = await w.client.delete(f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid('admin_a')}",
                                  headers=w.h("su"))
        assert r.status_code == 204
        r = await w.client.post("/api/invites/accept", json={"token": token},
                                headers=w.h("loner"))
        assert r.status_code == 403, r.text
        assert await w.role("loner", "ws-a") is None


async def test_a_member_link_does_not_demote_the_owner_who_clicks_it(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        token = await _link(w, "admin_a", "viewer")
        r = await w.client.post("/api/invites/accept", json={"token": token},
                                headers=w.h("owner_a"))
        assert r.status_code == 403, r.text
        assert await w.role("owner_a", "ws-a") == "owner"


async def test_an_owner_admin_link_makes_an_admin(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        token = await _link(w, "owner_a", "admin")
        r = await w.client.post("/api/invites/accept", json={"token": token},
                                headers=w.h("loner"))
        assert r.status_code == 200, r.text
        assert await w.role("loner", "ws-a") == "admin"


async def test_an_owner_admin_link_dies_when_the_owner_is_no_longer_owner(tmp_path,
                                                                         monkeypatch):
    """Accept re-checks against the inviter's CURRENT right: an owner made
    admin since may hand out members and viewers only."""
    async with world(tmp_path, monkeypatch) as w:
        token = await _link(w, "owner_a", "editor")
        r = await w.client.put(
            f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid('owner_a')}",
            json={"role": "admin"}, headers=w.h("su"))
        assert r.status_code == 200, r.text
        r = await w.client.post("/api/invites/accept", json={"token": token},
                                headers=w.h("loner"))
        assert r.status_code == 403, r.text
        assert await w.role("loner", "ws-a") is None


async def test_owner_direct_add_promotes_a_member_to_admin(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.post("/api/invites", headers=w.h("owner_a"),
                                json={"role": "admin", "email": "member-a@acme-corp.io"})
        assert r.status_code == 201 and r.json()["added_directly"], r.text
        assert await w.role("member_a", "ws-a") == "admin"


async def test_a_superadmin_editor_link_makes_an_editor(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        token = await _link(w, "su", "editor", "ws-a")
        r = await w.client.post("/api/invites/accept", json={"token": token},
                                headers=w.h("loner"))
        assert r.status_code == 200, r.text
        assert await w.role("loner", "ws-a") == "editor"
        row = [a for a in w.audit if a["action"] == "workspace.member_role_changed"][-1]
        assert row["detail"]["via"] == "invite_accept"
        assert row["detail"]["granted_by"] == "root@acme-corp.io"


async def test_a_privileged_invite_row_written_by_an_admin_is_not_honoured(tmp_path,
                                                                            monkeypatch):
    """Belt and braces: an editor-role invite that exists in the table (older
    release, hand-written row) but was created by an admin grants nothing."""
    import hashlib
    from datetime import UTC, datetime, timedelta

    from src.db.models import WorkspaceInvite

    async with world(tmp_path, monkeypatch) as w:
        raw = "x" * 40
        async with w.factory() as s:
            s.add(WorkspaceInvite(
                id="legacy", workspace_id=w.ws["ws-a"],
                token_hash=hashlib.sha256(raw.encode()).hexdigest(), email=None,
                role="admin", max_uses=1,
                expires_at=datetime.now(UTC) + timedelta(days=1),
                created_by="admin-a@acme-corp.io"))
            await s.commit()
        r = await w.client.post("/api/invites/accept", json={"token": raw},
                                headers=w.h("loner"))
        assert r.status_code == 403, r.text
        assert await w.role("loner", "ws-a") is None


# ─── teams: the role table and the membership boundary ───────────────


async def test_team_roles_come_from_the_shared_table(tmp_path, monkeypatch):
    from src.api.routers import teams
    from src.users.roles import TEAM_ROLES, VALID_WORKSPACE_ROLES

    assert teams._VALID_ROLES is TEAM_ROLES
    assert VALID_WORKSPACE_ROLES <= TEAM_ROLES and "reviewer" in TEAM_ROLES
    async with world(tmp_path, monkeypatch) as w:
        url = f"/api/teams/{w.ids['team_a']}/members/{w.uid('member_a')}"
        r = await w.client.put(url, json={"role": "editor"}, headers=w.h("admin_a"))
        assert r.status_code == 200, r.text   # 'editor' was refused by the old list


async def test_a_team_cannot_take_someone_from_outside_the_workspace(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        url = f"/api/teams/{w.ids['team_a']}/members/{w.uid('member_b')}"
        r = await w.client.put(url, json={"role": "member"}, headers=w.h("admin_a"))
        assert r.status_code == 404, r.text
