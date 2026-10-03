"""A password-reset link is the whole account — so membership must not buy one.

The chain this pins, found in review of the roles track:

  1. In multi_tenant mode everybody owns a personal workspace, so "workspace
     admin" is a role any fresh signup holds somewhere.
  2. POST /api/invites with the address of an EXISTING account enrolled that
     account on the spot — no consent, no "do you know this person" check.
  3. POST /api/workspaces/{ws}/members/{user}/reset-link read membership of
     THAT workspace as the authority to mint a reset link, and bounded it by
     the target's role in that workspace only ("member" — delegable).

Sign up, invite the admin of another tenant by email, mint the link, set the
password, log in as them. Each refusal below is followed by reading the state
back: a 403 that enrolled anyway would pass a status-only test.

Also here, because they are the same door from the other side: a global admin
minting a reset link for the master identity (which `is_superadmin` honours,
including a password account the master login adopted), and the authority of
an invite being resolved by the issuer's id rather than their mutable email.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from tests.api.rbac_world import MASTER_EMAIL, world


@pytest.fixture
def _no_real_reset_tokens(monkeypatch):
    import src.api.routers.users as users_router

    monkeypatch.setattr(users_router, "build_reset_link",
                        lambda target: ("/reset/x", "2099-01-01T00:00:00+00:00"))


def _reset(w, actor: str, target: str, ws: str = "ws-a"):
    return w.client.post(
        f"/api/workspaces/{w.ws[ws]}/members/{w.uid(target)}/reset-link",
        headers=w.h(actor))


# ─── the chain, end to end ───────────────────────────────────────────


@pytest.mark.parametrize("attacker", ["owner_a", "admin_a"])
async def test_inviting_a_stranger_enrols_nobody_and_buys_no_reset_link(
        tmp_path, monkeypatch, _no_real_reset_tokens, attacker):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.post("/api/invites", headers=w.h(attacker),
                                json={"role": "member", "email": "admin-b@acme-corp.io"})
        # Still an invitation — delivered as a token they have to accept.
        assert r.status_code == 201, r.text
        assert not r.json().get("added_directly")
        assert r.json()["token"]
        assert await w.role("admin_b", "ws-a") is None
        assert not [a for a in w.audit if a["action"] == "workspace.member_role_changed"]

        link = await _reset(w, attacker, "admin_b")
        assert link.status_code == 404, link.text
        # B is untouched.
        assert await w.role("admin_b", "ws-b") == "admin"


async def test_the_stranger_joins_by_accepting(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.post("/api/invites", headers=w.h("admin_a"),
                                json={"role": "member", "email": "admin-b@acme-corp.io"})
        acc = await w.client.post("/api/invites/accept", json={"token": r.json()["token"]},
                                  headers=w.h("admin_b"))
        assert acc.status_code == 200, acc.text
        assert await w.role("admin_b", "ws-a") == "member"


async def test_direct_add_still_works_for_someone_already_shared(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.post("/api/invites", headers=w.h("admin_a"),
                                json={"role": "viewer", "email": "both@acme-corp.io"})
        assert r.status_code == 201 and r.json()["added_directly"], r.text
        assert await w.role("both", "ws-a") == "viewer"


# ─── the reset link is bounded by every workspace the target is in ───


@pytest.mark.parametrize("actor, target, ok", [
    ("admin_a", "member_a", True),     # only in A, as a member: still allowed
    ("owner_a", "viewer_a", True),
    ("admin_a", "both", False),        # also a member of B, where admin_a is nobody
    ("owner_a", "both", False),
    ("su", "both", True),
])
async def test_a_reset_link_needs_authority_in_every_workspace_of_the_target(
        tmp_path, monkeypatch, _no_real_reset_tokens, actor, target, ok):
    async with world(tmp_path, monkeypatch) as w:
        r = await _reset(w, actor, target)
        assert r.status_code == (200 if ok else 403), r.text


async def test_an_admin_of_b_enrolled_in_a_cannot_be_reset_from_a(
        tmp_path, monkeypatch, _no_real_reset_tokens):
    """However admin_b came to be a member of A (here: the superadmin added
    them), A's admin may not mint the link — it would be admin of B."""
    async with world(tmp_path, monkeypatch) as w:
        put = await w.client.put(f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid('admin_b')}",
                                 headers=w.h("su"), json={"role": "member"})
        assert put.status_code == 200, put.text
        for actor in ("admin_a", "owner_a"):
            r = await _reset(w, actor, "admin_b")
            assert r.status_code == 403, (actor, r.text)
        assert (await _reset(w, "su", "admin_b")).status_code == 200


# ─── the master identity is never reset ──────────────────────────────


async def test_no_workspace_reset_link_for_the_master_identity(
        tmp_path, monkeypatch, _no_real_reset_tokens):
    async with world(tmp_path, monkeypatch) as w:
        # The route refuses to enrol a platform id, so the row is written
        # directly: an older release, or a superadmin-created workspace.
        from src.db.models import WorkspaceMember

        async with w.factory() as s:
            s.add(WorkspaceMember(workspace_id=w.ws["ws-a"], user_id="master-admin",
                                  role="member"))
            await s.commit()
        for actor in ("su", "gadmin", "admin_a"):
            r = await w.client.post(
                f"/api/workspaces/{w.ws['ws-a']}/members/master-admin/reset-link",
                headers=w.h(actor))
            assert r.status_code == 403, (actor, r.text)


async def test_no_workspace_reset_link_for_an_adopted_master_account(
        tmp_path, monkeypatch, _no_real_reset_tokens):
    """A password account the master login adopted keeps its own id and is
    matched by the master ADDRESS. Point the address at member_a."""
    async with world(tmp_path, monkeypatch) as w:
        monkeypatch.setenv("CELMIS_MASTER_EMAIL", "member-a@acme-corp.io")
        for actor in ("su", "gadmin", "admin_a"):
            r = await _reset(w, actor, "member_a")
            assert r.status_code == 403, (actor, r.text)


def _users_store(tmp_path):
    from src.users import User
    from src.users.store import UserStore

    store = UserStore(tmp_path / "users.db")
    store.create(User(id="u-adopted", email=MASTER_EMAIL, name="m", is_admin=True))
    store.create(User(id="u-g", email="g@acme-corp.io", name="g", is_admin=True))
    store.create(User(id="u-plain", email="p@acme-corp.io", name="p"))
    return store


async def test_a_global_admin_cannot_reset_the_adopted_master(tmp_path, monkeypatch):
    """POST /api/users/{id}/reset-link refused only the fixed id. The adopted
    account is the superadmin too, and its reset link was the way in."""
    import src.api.routers.auth as auth_router
    from src.api.routers.users import create_reset_link
    from src.users.roles import is_superadmin

    monkeypatch.setenv("CELMIS_MASTER_EMAIL", MASTER_EMAIL)
    monkeypatch.setattr(auth_router, "issue_reset_token",
                        lambda uid: ("raw", "2099-01-01T00:00:00+00:00"))
    store = _users_store(tmp_path)
    adopted, gadmin = store.get_by_id("u-adopted"), store.get_by_id("u-g")
    assert is_superadmin(adopted) and not is_superadmin(gadmin)

    with pytest.raises(HTTPException) as exc:
        await create_reset_link("u-adopted", admin=gadmin, users=store)
    assert exc.value.status_code == 400

    # The route still works for an ordinary account.
    out = await create_reset_link("u-plain", admin=gadmin, users=store)
    assert out.email == "p@acme-corp.io"


# ─── workspace creation leaves the same audit row as any grant ───────


async def test_creating_a_workspace_records_the_owner_grant(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.post("/api/workspaces", headers=w.h("su"),
                                json={"name": "Gamma", "slug": "ws-g", "description": ""})
        assert r.status_code == 201, r.text
        ws_id = r.json()["id"]
        rows = [a for a in w.audit if a["action"] == "workspace.member_role_changed"]
        assert len(rows) == 1
        row = rows[0]
        assert row["workspace_id"] == ws_id and row["target"] == "master-admin"
        assert row["actor_id"] == "master-admin"
        assert row["detail"] == {"old_role": None, "new_role": "owner",
                                 "via": "workspace_create"}
        from src.db.models import WorkspaceMember

        m = await w.scalar(WorkspaceMember, (ws_id, "master-admin"))
        assert m is not None and m.role == "owner"


async def test_a_duplicate_slug_is_still_a_400(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.post("/api/workspaces", headers=w.h("su"),
                                json={"name": "Again", "slug": "ws-a", "description": ""})
        assert r.status_code == 400, r.text
        assert not [a for a in w.audit if a["action"] == "workspace.member_role_changed"]


# ─── an invite's authority is its issuer by id ───────────────────────


async def test_a_superadmin_invite_survives_the_master_address_changing(tmp_path, monkeypatch):
    """The operator moves CELMIS_MASTER_EMAIL; `_master_login` rewrites the
    master row to the new address. Pending invites must still be honoured —
    and the old address, now free, must not be whose authority is checked."""
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.post("/api/invites", headers=w.h("su", "ws-a"),
                                json={"role": "editor", "max_uses": 2})
        assert r.status_code == 201, r.text
        token = r.json()["token"]

        import src.users.store as users_mod

        store = users_mod._default_store
        su = store.get_by_id("master-admin")
        su.email = "new-root@acme-corp.io"
        store.update(su)
        monkeypatch.setenv("CELMIS_MASTER_EMAIL", "new-root@acme-corp.io")

        acc = await w.client.post("/api/invites/accept", json={"token": token},
                                  headers=w.h("loner"))
        assert acc.status_code == 200, acc.text
        assert await w.role("loner", "ws-a") == "editor"


async def test_an_admin_invite_follows_the_admin_not_their_old_address(tmp_path, monkeypatch):
    """Somebody registering the issuer's former address does not inherit the
    right to honour their invites — nor does the issuer lose it."""
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.post("/api/invites", headers=w.h("admin_a"),
                                json={"role": "member", "max_uses": 2})
        token = r.json()["token"]

        import src.users.store as users_mod

        store = users_mod._default_store
        a = store.get_by_id("u-admin_a")
        a.email = "renamed-a@acme-corp.io"
        store.update(a)

        acc = await w.client.post("/api/invites/accept", json={"token": token},
                                  headers=w.h("loner"))
        assert acc.status_code == 200, acc.text
        assert await w.role("loner", "ws-a") == "member"


async def test_a_legacy_invite_row_without_an_issuer_id_resolves_by_email(tmp_path,
                                                                         monkeypatch):
    import hashlib
    from datetime import UTC, datetime, timedelta

    from src.db.models import WorkspaceInvite

    async with world(tmp_path, monkeypatch) as w:
        raw = "y" * 40
        async with w.factory() as s:
            s.add(WorkspaceInvite(
                id="legacy-member", workspace_id=w.ws["ws-a"],
                token_hash=hashlib.sha256(raw.encode()).hexdigest(), email=None,
                role="member", max_uses=1,
                expires_at=datetime.now(UTC) + timedelta(days=1),
                created_by="admin-a@acme-corp.io"))
            await s.commit()
        acc = await w.client.post("/api/invites/accept", json={"token": raw},
                                  headers=w.h("loner"))
        assert acc.status_code == 200, acc.text
        assert await w.role("loner", "ws-a") == "member"
