"""Sign-in rules that hold in every edition.

Split out of the SSO tests when the OIDC endpoint moved behind the enterprise
licence (src/ee/sso). What stays AGPL, and is tested here without any
licence:

  * AUTH_PASSWORD_LOGIN=false refuses signup, login and reset — but never the
    master key, the break-glass account for when the IdP is down;
  * an SSO-only session is not renewed by /api/auth/refresh, so deprovisioning
    in the IdP takes effect. The account is created directly with an OIDC
    subject: an SSO user created under a licence must keep these rules after
    the licence lapses;
  * Google links or creates an account only for a verified email.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ISSUER = "https://sso.example.com/realms/celmis"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AUTH_PASSWORD_LOGIN", raising=False)


@pytest.fixture
def users(tmp_path):
    from src.users import UserStore
    return UserStore(tmp_path / "users.db")


@pytest.fixture
def client(users, monkeypatch):
    from src.api.deps import get_users
    from src.api.routers import auth as auth_router

    monkeypatch.setattr(
        "src.api.workspace_provision.provision_personal_workspace",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(auth_router, "record_action", lambda **k: None)
    app = FastAPI()
    app.include_router(auth_router.router)
    app.dependency_overrides[get_users] = lambda: users
    return TestClient(app)


def _password_user(users, email="dev@example.com"):
    from src.users import User, UserAuthMethod, hash_password
    user = User(id="pw-1", email=email, auth_method=UserAuthMethod.PASSWORD,
                password_hash=hash_password("Vq7#mLz2-Rk9wTp4!"))
    users.create(user)
    return user


def _sso_user(users):
    from src.users import User, UserAuthMethod
    user = User(id="sso-1", email="sso@example.com", auth_method=UserAuthMethod.OIDC,
                oidc_iss=ISSUER, oidc_sub="kc-subject-1")
    users.create(user)
    return users.get_by_oidc(ISSUER, "kc-subject-1")


def test_the_oidc_endpoint_is_not_part_of_the_agpl_router(client):
    """It moved to src/ee/sso; the AGPL auth router alone does not serve it."""
    assert client.post("/api/auth/oidc", json={"id_token": "x"}).status_code == 404


# ─── session renewal ────────────────────────────────────────────────


def test_an_sso_only_session_is_not_renewed(client, users):
    """Deprovisioning in the IdP must take effect: /refresh would otherwise
    roll an SSO-only session forever without asking the IdP again."""
    from src.api.deps import get_current_user

    sso_user = _sso_user(users)
    client.app.dependency_overrides[get_current_user] = lambda: sso_user
    assert client.post("/api/auth/refresh").status_code == 401

    pw = _password_user(users, email="pw@example.com")
    client.app.dependency_overrides[get_current_user] = lambda: pw
    assert client.post("/api/auth/refresh").status_code == 200


def test_sso_with_password_is_renewed_only_while_password_login_is_on(
        client, users, monkeypatch):
    from src.api.deps import get_current_user

    linked = _password_user(users)
    linked.oidc_iss, linked.oidc_sub = ISSUER, "kc-subject-1"
    users.update(linked)
    linked = users.get_by_email("dev@example.com")
    client.app.dependency_overrides[get_current_user] = lambda: linked
    assert client.post("/api/auth/refresh").status_code == 200
    monkeypatch.setenv("AUTH_PASSWORD_LOGIN", "false")
    assert client.post("/api/auth/refresh").status_code == 401


# ─── password login switch ──────────────────────────────────────────


def test_password_login_off_refuses_signup_login_and_reset(client, users, monkeypatch):
    _password_user(users, email="pw@example.com")
    monkeypatch.setenv("AUTH_PASSWORD_LOGIN", "false")
    assert client.post("/api/auth/signup", json={
        "email": "new@example.com", "password": "Vq7#mLz2-Rk9wTp4!"}).status_code == 403
    assert client.post("/api/auth/login", json={
        "email": "pw@example.com", "password": "Vq7#mLz2-Rk9wTp4!"}).status_code == 403
    assert client.post("/api/auth/forgot-password",
                       json={"email": "pw@example.com"}).status_code == 403


def test_master_key_still_works_with_password_login_off(client, monkeypatch):
    """Break-glass: the IdP being down must not lock the operator out."""
    monkeypatch.setenv("AUTH_PASSWORD_LOGIN", "false")
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", "root@example.com")
    monkeypatch.setenv("CELMIS_MASTER_KEY", "sk-master-correct-horse-battery")
    r = client.post("/api/auth/login", json={
        "email": "root@example.com", "password": "sk-master-correct-horse-battery"})
    assert r.status_code == 200, r.text


# ─── Google: the same linking rule ──────────────────────────────────


def test_google_unverified_email_does_not_link(client, users, monkeypatch):
    from src.api.routers import auth as auth_router

    _password_user(users)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "g-client")

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"aud": "g-client", "iss": "https://accounts.google.com",
                    "sub": "g-1", "email": "dev@example.com",
                    "email_verified": "false"}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(auth_router, "build_client", lambda **k: _Client())
    r = client.post("/api/auth/google", json={"id_token": "x"})
    assert r.status_code == 403
    assert users.get_by_email("dev@example.com").google_sub is None


def test_google_unverified_email_does_not_create_an_account(client, users, monkeypatch):
    from src.api.routers import auth as auth_router

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "g-client")

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"aud": "g-client", "iss": "https://accounts.google.com",
                    "sub": "g-2", "email": "victim@corp.example",
                    "email_verified": "false"}

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(auth_router, "build_client", lambda **k: _Client())
    r = client.post("/api/auth/google", json={"id_token": "x"})
    assert r.status_code == 403
    assert users.get_by_email("victim@corp.example") is None


# ─── the master address is never an external identity ──────────────

MASTER_EMAIL = "root@example.com"
MASTER_KEY = "sk-master-correct-horse-battery"


def _google_says(monkeypatch, **claims):
    from src.api.routers import auth as auth_router

    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "g-client")
    payload = {"aud": "g-client", "iss": "https://accounts.google.com",
               "sub": "g-master", "email": MASTER_EMAIL, "email_verified": "true",
               **claims}

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


def test_google_cannot_create_the_master_address_before_the_first_master_login(
        client, users, monkeypatch):
    """No account holds the master email yet. A verified Google identity for
    it must not create one: the first master login would adopt that account
    and the Google identity would sign in as global admin from then on."""
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", MASTER_EMAIL)
    monkeypatch.setenv("CELMIS_MASTER_KEY", MASTER_KEY)
    _google_says(monkeypatch, email="Root@Example.com")
    assert client.post("/api/auth/google", json={"id_token": "x"}).status_code == 403
    assert users.get_by_email(MASTER_EMAIL) is None
    assert users.get_by_google_sub("g-master") is None

    r = client.post("/api/auth/login", json={"email": MASTER_EMAIL, "password": MASTER_KEY})
    assert r.status_code == 200, r.text
    master = users.get_by_email(MASTER_EMAIL)
    assert master.id == "master-admin" and master.google_sub is None


def test_the_master_login_does_not_adopt_an_externally_bound_account(
        client, users, monkeypatch):
    """An account bound to Google/SSO that already carries the master email
    (created before the env named it) is refused, not made global admin."""
    from src.users import User, UserAuthMethod

    users.create(User(id="g-user", email=MASTER_EMAIL,
                      auth_method=UserAuthMethod.GOOGLE_OAUTH, google_sub="g-master"))
    users.create(User(id="sso-user", email="ops@example.com",
                      auth_method=UserAuthMethod.OIDC,
                      oidc_iss=ISSUER, oidc_sub="kc-ops"))
    monkeypatch.setenv("CELMIS_MASTER_KEY", MASTER_KEY)

    monkeypatch.setenv("CELMIS_MASTER_EMAIL", MASTER_EMAIL)
    r = client.post("/api/auth/login", json={"email": MASTER_EMAIL, "password": MASTER_KEY})
    assert r.status_code == 401
    assert users.get_by_id("g-user").is_admin is False

    monkeypatch.setenv("CELMIS_MASTER_EMAIL", "ops@example.com")
    r = client.post("/api/auth/login", json={"email": "ops@example.com", "password": MASTER_KEY})
    assert r.status_code == 401
    assert users.get_by_id("sso-user").is_admin is False
    assert users.get_by_id("master-admin") is None


def test_a_google_identity_bound_to_the_master_address_is_refused(
        client, users, monkeypatch):
    """Found by subject, not by email: the guard runs on that branch too."""
    from src.users import User, UserAuthMethod

    users.create(User(id="g-user", email=MASTER_EMAIL,
                      auth_method=UserAuthMethod.GOOGLE_OAUTH, google_sub="g-master"))
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", MASTER_EMAIL)
    _google_says(monkeypatch, email="someone-else@example.com")
    assert client.post("/api/auth/google", json={"id_token": "x"}).status_code == 403
