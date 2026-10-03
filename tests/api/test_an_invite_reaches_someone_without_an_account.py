"""An invite for somebody who has no account yet — the whole way through.

Product owner's decision:

  * the inviter gets a link (and an email when SMTP is configured);
  * the invitee opens it signed out, signs up or signs in, comes back and
    accepts — the grant rule re-checked at that moment, audited, single use;
  * a sign-in whose identity provider has VERIFIED the email (Google's or
    the company IdP's `email_verified`) redeems the invites addressed to it
    automatically, through that same path;
  * a PASSWORD account is never redeemed by its address, because nobody
    verified it: anyone can sign up with somebody else's email. It has to
    open the link, which went to the real mailbox;
  * expired / revoked / used / somebody else's links grant nothing and say so.

Real routers (invites, auth, the SSO exchange) over one SQLite database. The
OIDC token check itself is tested in tests/ee/test_oidc_sign_in.py; here the
verified claims are handed in, because what is under test is what happens
AFTER the IdP has spoken.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests.api.rbac_world import world

PASSWORD = "Vq7#mLz2-Rk9wTp4!"
NEWBIE = "newbie@outside.io"


@pytest.fixture(autouse=True)
def _no_provisioning(monkeypatch):
    """Personal-workspace provisioning opens its own blocking engine on the
    production URL; it is not what these tests are about."""
    monkeypatch.setattr(
        "src.api.workspace_provision.provision_personal_workspace", lambda *a, **k: None)
    monkeypatch.delenv("AUTH_PASSWORD_LOGIN", raising=False)


def _auth_routers():
    from src.api.routers import auth
    from src.ee.sso import router as sso

    return (auth.router, sso.router)


async def _invite(w, who="admin_a", email=NEWBIE, role="member") -> str:
    r = await w.client.post("/api/invites", json={"email": email, "role": role},
                            headers=w.h(who, "ws-a"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["added_directly"] is False, "a stranger was enrolled without accepting"
    assert body["invite_url"] == f"/invite/{body['token']}"
    return body["token"]


def _adopt(w, name: str, email: str) -> None:
    """Make an account created through a real route addressable as X-Test-User."""
    import src.users.store as users_mod

    w.users[name] = users_mod._default_store.get_by_email(email)
    assert w.users[name] is not None


async def _invite_row(w, token: str):
    from sqlalchemy import select

    from src.db.models import WorkspaceInvite

    async with w.factory() as s:
        return (await s.scalars(select(WorkspaceInvite).where(
            WorkspaceInvite.token_hash == hashlib.sha256(token.encode()).hexdigest()))).first()


def _google(monkeypatch, *, email: str, verified: str = "true", sub: str = "g-newbie"):
    from src.api.routers import auth as auth_router

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "g-client")
    payload = {"aud": "g-client", "iss": "https://accounts.google.com",
               "sub": sub, "email": email, "email_verified": verified, "name": "New"}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(auth_router, "build_client", lambda **k: _Client())


def _oidc(monkeypatch, *, email: str, verified: bool, sub: str = "kc-newbie"):
    from src.ee.sso import oidc

    cfg = oidc.OidcConfig(issuer="https://sso.example.com/realms/celmis",
                          client_id="celmis-web")
    monkeypatch.setattr(oidc, "oidc_config", lambda: cfg)
    monkeypatch.setattr(oidc, "verify_id_token", lambda _t, _c: {
        "sub": sub, "email": email, "email_verified": verified, "name": "New"})


# ─── the link, for a password signup ─────────────────────────────────


async def test_a_password_signup_accepts_through_the_link_and_only_through_it(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_auth_routers()) as w:
        token = await _invite(w)

        # Signed out: the link says what it is for, and who sent it.
        p = (await w.client.get(f"/api/invites/preview/{token}")).json()
        assert p["valid"] is True and p["workspace_name"] == "Alpha"
        assert p["role"] == "member" and p["email_bound"] is True
        assert p["invited_by"] == "admin_a"

        # Sign up with the invited address — and land in NO workspace yet:
        # the address was typed, not verified.
        r = await w.client.post("/api/auth/signup",
                                json={"email": NEWBIE, "password": PASSWORD})
        assert r.status_code == 200, r.text
        _adopt(w, "newbie", NEWBIE)
        assert await w.role("newbie", "ws-a") is None
        assert (await _invite_row(w, token)).used_count == 0
        # …nor at a later password login.
        r = await w.client.post("/api/auth/login", json={"email": NEWBIE, "password": PASSWORD})
        assert r.status_code == 200, r.text
        assert await w.role("newbie", "ws-a") is None

        # Back on the link, signed in: accept.
        r = await w.client.post("/api/invites/accept", json={"token": token},
                                headers=w.h("newbie"))
        assert r.status_code == 200, r.text
        assert r.json()["workspace_slug"] == "ws-a"
        assert await w.role("newbie", "ws-a") == "member"
        row = [a for a in w.audit if a["action"] == "workspace.member_role_changed"][-1]
        assert row["detail"]["via"] == "invite_accept"
        assert row["detail"]["granted_by"] == "admin-a@acme-corp.io"
        assert row["actor_id"] == w.uid("newbie")

        # Single use — and opening it again is not an error for the redeemer.
        again = await w.client.post("/api/invites/accept", json={"token": token},
                                    headers=w.h("newbie"))
        assert again.status_code == 200 and again.json().get("already") is True
        assert (await _invite_row(w, token)).used_count == 1


async def test_someone_else_cannot_use_an_email_bound_link(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        token = await _invite(w)
        r = await w.client.post("/api/invites/accept", json={"token": token},
                                headers=w.h("loner"))
        assert r.status_code == 403
        assert "different email" in r.json()["detail"]
        assert await w.role("loner", "ws-a") is None
        assert (await _invite_row(w, token)).used_count == 0


# ─── automatic redemption, verified addresses only ───────────────────


async def test_a_new_google_account_with_a_verified_email_joins_at_sign_in(
        tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_auth_routers()) as w:
        token = await _invite(w)
        _google(monkeypatch, email=NEWBIE, verified="true")
        r = await w.client.post("/api/auth/google", json={"id_token": "x"})
        assert r.status_code == 200, r.text
        _adopt(w, "newbie", NEWBIE)
        assert await w.role("newbie", "ws-a") == "member"
        assert (await _invite_row(w, token)).used_count == 1
        row = [a for a in w.audit if a["action"] == "workspace.member_role_changed"][-1]
        assert row["detail"]["via"] == "invite_auto_redeem"
        assert row["detail"]["granted_by"] == "admin-a@acme-corp.io"


async def test_a_new_sso_account_with_a_verified_email_joins_at_sign_in(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_auth_routers()) as w:
        token = await _invite(w, role="viewer")
        _oidc(monkeypatch, email=NEWBIE, verified=True)
        r = await w.client.post("/api/auth/oidc", json={"id_token": "x"})
        assert r.status_code == 200, r.text
        _adopt(w, "newbie", NEWBIE)
        assert await w.role("newbie", "ws-a") == "viewer"
        assert (await _invite_row(w, token)).used_count == 1


async def test_an_unverified_sso_email_redeems_nothing(tmp_path, monkeypatch):
    """An identity already bound by subject signs in even when the IdP no
    longer marks the address verified — and then must not collect the
    invites addressed to that address."""
    from src.users import User, UserAuthMethod

    async with world(tmp_path, monkeypatch, extra_routers=_auth_routers()) as w:
        import src.users.store as users_mod

        users_mod._default_store.create(User(
            id="u-sso-newbie", email=NEWBIE, auth_method=UserAuthMethod.OIDC,
            oidc_iss="https://sso.example.com/realms/celmis", oidc_sub="kc-newbie"))
        token = await _invite(w)
        _oidc(monkeypatch, email=NEWBIE, verified=False)
        r = await w.client.post("/api/auth/oidc", json={"id_token": "x"})
        assert r.status_code == 200, r.text
        _adopt(w, "newbie", NEWBIE)
        assert await w.role("newbie", "ws-a") is None
        assert (await _invite_row(w, token)).used_count == 0
        # The link still works for them.
        ok = await w.client.post("/api/invites/accept", json={"token": token},
                                 headers=w.h("newbie"))
        assert ok.status_code == 200, ok.text


async def test_an_unverified_google_email_redeems_nothing(tmp_path, monkeypatch):
    """A Google identity already linked by subject, unverified claim now."""
    from src.users import User, UserAuthMethod

    async with world(tmp_path, monkeypatch, extra_routers=_auth_routers()) as w:
        import src.users.store as users_mod

        users_mod._default_store.create(User(
            id="u-g-newbie", email=NEWBIE, auth_method=UserAuthMethod.GOOGLE_OAUTH,
            google_sub="g-newbie"))
        token = await _invite(w)
        _google(monkeypatch, email=NEWBIE, verified="false")
        r = await w.client.post("/api/auth/google", json={"id_token": "x"})
        assert r.status_code == 200, r.text
        _adopt(w, "newbie", NEWBIE)
        assert await w.role("newbie", "ws-a") is None
        assert (await _invite_row(w, token)).used_count == 0


async def test_a_verified_email_that_is_not_the_accounts_own_redeems_nothing(
        tmp_path, monkeypatch):
    """Google says `other@…` is verified, but the account it is bound to by
    subject carries NEWBIE: the invite for NEWBIE is not vouched for."""
    from src.users import User, UserAuthMethod

    async with world(tmp_path, monkeypatch, extra_routers=_auth_routers()) as w:
        import src.users.store as users_mod

        users_mod._default_store.create(User(
            id="u-g-newbie", email=NEWBIE, auth_method=UserAuthMethod.GOOGLE_OAUTH,
            google_sub="g-newbie"))
        await _invite(w)
        _google(monkeypatch, email="other@outside.io", verified="true")
        assert (await w.client.post("/api/auth/google", json={"id_token": "x"})).status_code \
            == 200
        _adopt(w, "newbie", NEWBIE)
        assert await w.role("newbie", "ws-a") is None


async def test_auto_redeem_rechecks_the_inviters_right(tmp_path, monkeypatch):
    """The inviter was demoted after sending the invite: at sign-in nothing is
    granted, the invite stays unused, and the refusal is in the audit file."""
    async with world(tmp_path, monkeypatch, extra_routers=_auth_routers()) as w:
        token = await _invite(w, who="admin_a")
        demote = await w.client.put(
            f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid('admin_a')}",
            json={"role": "member"}, headers=w.h("su"))
        assert demote.status_code == 200, demote.text

        _google(monkeypatch, email=NEWBIE, verified="true")
        r = await w.client.post("/api/auth/google", json={"id_token": "x"})
        assert r.status_code == 200, r.text   # the sign-in itself still works
        _adopt(w, "newbie", NEWBIE)
        assert await w.role("newbie", "ws-a") is None
        assert (await _invite_row(w, token)).used_count == 0
        refused = [a for a in w.audit if a["action"] == "invite.auto_redeem_refused"]
        assert refused and refused[-1]["workspace_id"] == w.ws["ws-a"]


async def test_one_refused_invite_does_not_block_the_others(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch, extra_routers=_auth_routers()) as w:
        dead = await _invite(w, who="admin_a")
        live = await _invite(w, who="owner_a", role="viewer")
        await w.client.put(f"/api/workspaces/{w.ws['ws-a']}/members/{w.uid('admin_a')}",
                           json={"role": "member"}, headers=w.h("su"))
        _google(monkeypatch, email=NEWBIE, verified="true")
        assert (await w.client.post("/api/auth/google", json={"id_token": "x"})).status_code \
            == 200
        _adopt(w, "newbie", NEWBIE)
        assert await w.role("newbie", "ws-a") == "viewer"
        assert (await _invite_row(w, dead)).used_count == 0
        assert (await _invite_row(w, live)).used_count == 1


# ─── links that no longer work ───────────────────────────────────────


async def _raw_invite(w, **overrides) -> str:
    from src.db.models import WorkspaceInvite

    raw = uuid.uuid4().hex + uuid.uuid4().hex
    fields = dict(
        id=str(uuid.uuid4()), workspace_id=w.ws["ws-a"],
        token_hash=hashlib.sha256(raw.encode()).hexdigest(), email=NEWBIE,
        role="member", max_uses=1, used_count=0,
        expires_at=datetime.now(UTC) + timedelta(days=3),
        created_by="admin-a@acme-corp.io", created_by_id=w.uid("admin_a"),
    )
    fields.update(overrides)
    async with w.factory() as s:
        s.add(WorkspaceInvite(**fields))
        await s.commit()
    return raw


@pytest.mark.parametrize("state, overrides", [
    ("expired", {"expires_at": datetime.now(UTC) - timedelta(minutes=1)}),
    ("revoked", {"revoked": True}),
    ("used", {"used_count": 1}),
])
async def test_a_dead_link_says_why_and_grants_nothing(tmp_path, monkeypatch, state, overrides):
    async with world(tmp_path, monkeypatch, extra_routers=_auth_routers()) as w:
        token = await _raw_invite(w, **overrides)
        p = (await w.client.get(f"/api/invites/preview/{token}")).json()
        assert p["valid"] is False and p["reason"] == state
        # An invalid link does not say what it was for.
        assert p["workspace_name"] == "" and p["role"] == ""

        r = await w.client.post("/api/auth/signup", json={"email": NEWBIE, "password": PASSWORD})
        assert r.status_code == 200
        _adopt(w, "newbie", NEWBIE)
        r = await w.client.post("/api/invites/accept", json={"token": token},
                                headers=w.h("newbie"))
        assert r.status_code == 400
        assert state.split("_")[0] in r.json()["detail"].lower() or state == "used"
        assert await w.role("newbie", "ws-a") is None

        # Nor does a verified sign-in pick it up.
        _google(monkeypatch, email=NEWBIE, verified="true")
        assert (await w.client.post("/api/auth/google", json={"id_token": "x"})).status_code \
            == 200
        assert await w.role("newbie", "ws-a") is None


async def test_an_unknown_token_is_not_found(tmp_path, monkeypatch):
    async with world(tmp_path, monkeypatch) as w:
        p = (await w.client.get("/api/invites/preview/" + "z" * 40)).json()
        assert p["valid"] is False and p["reason"] == "not_found"
        r = await w.client.post("/api/invites/accept", json={"token": "z" * 40},
                                headers=w.h("loner"))
        assert r.status_code == 400


async def test_the_invite_is_emailed_when_smtp_is_configured(tmp_path, monkeypatch):
    import src.notifications.mailer as mailer

    sent: list[tuple] = []
    monkeypatch.setattr(mailer, "mailer_configured", lambda: True)
    monkeypatch.setattr(mailer, "send_email_background", lambda *a, **k: sent.append(a))
    async with world(tmp_path, monkeypatch) as w:
        r = await w.client.post("/api/invites", json={"email": NEWBIE, "role": "member"},
                                headers=w.h("admin_a", "ws-a"))
        assert r.status_code == 201
        assert r.json()["emailed"] is True and r.json()["invite_url"]
        assert sent and sent[0][0] == NEWBIE and r.json()["token"] in sent[0][2]
