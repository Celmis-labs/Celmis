# Celmis Enterprise Edition tests. Licensed under LICENSE_EE, not the AGPL —
# see LICENSING.md and ee/README.md in the repository root.
"""Company SSO (Keycloak / OIDC) sign-in: the token is checked, not trusted.

`POST /api/auth/oidc` receives an id_token from the web app and hands back a
Celmis session. Everything that makes that safe is in the checks, so each one
is attacked here with a token that fails exactly that check and passes the
rest: a foreign issuer, a token minted for another client, an expired one, one
signed by a key the issuer never published, and one that tries the HS256
"sign with the public key" confusion.

The JWKS comes from a locally generated RSA key served through a stubbed
discovery fetch, so the parsing path in `_fetch_jwks` runs for real.

The second half is account linking. Linking by email gives the existing
account to whoever controls that email at the IdP, so it requires
`email_verified`. The same rule on the Google endpoint, the password-login
switch and the refusal to renew an SSO-only session are AGPL and are tested in
tests/api/test_password_login_and_google_linking.py.

The endpoint is enterprise (src/ee/sso), so the app under test mounts it the
way production does — through `mount_enterprise` with a licence signed by the
suite's throwaway key — and one test checks that without one it is not there.
"""

from __future__ import annotations

import dataclasses
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.ee import license as lic
from src.ee.sso import oidc
from tests.ee.licensing import mint_test_license, trust_test_key

ISSUER = "https://sso.example.com/realms/celmis"
CLIENT_ID = "celmis-web"
KID = "test-key-1"


def _rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNING_KEY = _rsa_key()
FOREIGN_KEY = _rsa_key()


def _jwk(private_key, kid: str) -> dict:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return jwk


def _token(*, key=SIGNING_KEY, kid=KID, alg="RS256", **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        # The shape of a real Keycloak id_token: client in aud and azp, typ ID.
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "typ": "ID",
        "sub": "kc-subject-1",
        "email": "dev@example.com",
        "email_verified": True,
        "name": "Dev Person",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


@pytest.fixture(autouse=True)
def issuer(monkeypatch):
    """OIDC configured, discovery + JWKS served from memory."""
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.delenv("OIDC_ADMIN_ROLE", raising=False)
    monkeypatch.delenv("OIDC_ADMIN_ROLE_SYNC", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD_LOGIN", raising=False)
    fetched: list[str] = []

    def fake_get_json(url: str, _issuer: str) -> dict:
        fetched.append(url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs"}
        if url == f"{ISSUER}/protocol/openid-connect/certs":
            return {"keys": [_jwk(SIGNING_KEY, KID)]}
        raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr(oidc, "_get_json", fake_get_json)
    oidc.reset_cache()
    yield fetched
    oidc.reset_cache()


def _config() -> oidc.OidcConfig:
    cfg = oidc.oidc_config()
    assert cfg is not None
    return cfg


# ─── verification ───────────────────────────────────────────────────


def test_a_valid_token_is_accepted():
    claims = oidc.verify_id_token(_token(), _config())
    assert claims["sub"] == "kc-subject-1"


def test_the_client_in_aud_without_azp_or_typ_is_enough():
    """A generic IdP: no azp, no Keycloak typ claim."""
    claims = oidc.verify_id_token(_token(azp=None, typ=None), _config())
    assert claims["email"] == "dev@example.com"


def test_a_keycloak_access_token_does_not_pass_as_an_id_token():
    """aud=account, azp=celmis-web: what any resource server receiving the
    user's access token holds. It carries email + email_verified too."""
    access = _token(aud="account", typ="Bearer")
    with pytest.raises(oidc.OidcError):
        oidc.verify_id_token(access, _config())
    # Even with typ stripped, azp alone does not name the audience.
    with pytest.raises(oidc.OidcError):
        oidc.verify_id_token(_token(aud="account", typ=None), _config())


def test_a_multi_audience_token_needs_azp_to_be_the_client():
    with pytest.raises(oidc.OidcError):
        oidc.verify_id_token(_token(aud=[CLIENT_ID, "other"], azp="other"), _config())
    assert oidc.verify_id_token(_token(aud=[CLIENT_ID, "other"]), _config())


@pytest.mark.parametrize("configured, iss", [
    pytest.param("https://tenant.auth0.com/", "https://tenant.auth0.com/", id="both-slash"),
    pytest.param("https://tenant.auth0.com", "https://tenant.auth0.com/", id="iss-slash"),
    pytest.param("https://tenant.auth0.com/", "https://tenant.auth0.com", id="config-slash"),
])
def test_a_trailing_slash_issuer_is_accepted(monkeypatch, configured, iss):
    """Auth0 and Azure AD v1 end `iss` with a slash; Keycloak does not."""
    base = "https://tenant.auth0.com"

    def fake_get_json(url: str, _issuer: str) -> dict:
        if url == f"{base}/.well-known/openid-configuration":
            return {"issuer": f"{base}/", "jwks_uri": f"{base}/.well-known/jwks.json"}
        if url == f"{base}/.well-known/jwks.json":
            return {"keys": [_jwk(SIGNING_KEY, KID)]}
        raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr(oidc, "_get_json", fake_get_json)
    monkeypatch.setenv("OIDC_ISSUER", configured)
    oidc.reset_cache()
    claims = oidc.verify_id_token(_token(iss=iss), _config())
    assert claims["sub"] == "kc-subject-1"
    with pytest.raises(oidc.OidcError):
        oidc.verify_id_token(_token(iss=f"{base}.evil.com/"), _config())


def test_jwks_is_fetched_once_and_cached(issuer):
    oidc.verify_id_token(_token(), _config())
    oidc.verify_id_token(_token(), _config())
    assert len(issuer) == 2  # discovery + certs, once


@pytest.mark.parametrize("bad", [
    pytest.param({"iss": "https://evil.example.com/realms/celmis"}, id="wrong-issuer"),
    pytest.param({"aud": "other-client", "azp": "other-client"}, id="wrong-audience"),
    pytest.param({"exp": int(time.time()) - 3600}, id="expired"),
    pytest.param({"exp": None}, id="no-exp"),
])
def test_a_token_failing_one_claim_check_is_refused(bad):
    with pytest.raises(oidc.OidcError):
        oidc.verify_id_token(_token(**bad), _config())


def test_a_token_signed_by_an_unpublished_key_is_refused():
    with pytest.raises(oidc.OidcError):
        oidc.verify_id_token(_token(key=FOREIGN_KEY), _config())


def test_an_unknown_kid_is_refused():
    with pytest.raises(oidc.OidcError):
        oidc.verify_id_token(_token(key=FOREIGN_KEY, kid="other"), _config())


def test_hmac_with_the_public_key_is_refused():
    """The classic confusion attack: HS256 'signed' with the public key.

    PyJWT refuses to HMAC-sign with a PEM, so the forged token carries a dummy
    secret; what matters is that the alg is refused before any key is used.
    """
    forged = jwt.encode(
        {"iss": ISSUER, "azp": CLIENT_ID, "sub": "x", "iat": int(time.time()),
         "exp": int(time.time()) + 60},
        "a-dummy-hmac-secret-of-sufficient-length", algorithm="HS256",
        headers={"kid": KID},
    )
    with pytest.raises(oidc.OidcError, match="not accepted"):
        oidc.verify_id_token(forged, _config())


def test_roles_are_read_from_realm_access_and_groups():
    roles = oidc.token_roles({
        "realm_access": {"roles": ["celmis-admin"]},
        "groups": ["/platform"],
    }, CLIENT_ID)
    assert {"celmis-admin", "platform", "/platform"} <= roles


def test_client_roles_count_only_for_the_celmis_client():
    """A client-roles mapper without a client filter puts every client of the
    realm into resource_access. `admin` on another client is not a Celmis
    role, so it must not reach the global-admin check."""
    claims = {"resource_access": {
        "grafana": {"roles": ["admin"]},
        CLIENT_ID: {"roles": ["celmis-admin"]},
    }}
    roles = oidc.token_roles(claims, CLIENT_ID)
    assert "celmis-admin" in roles
    assert "admin" not in roles


def test_an_admin_role_on_another_client_does_not_make_a_celmis_admin(
        client, users, monkeypatch):
    monkeypatch.setenv("OIDC_ADMIN_ROLE", "admin")
    tok = _token(resource_access={"grafana": {"roles": ["admin"]}})
    assert client.post("/api/auth/oidc", json={"id_token": tok}).status_code == 200
    assert users.get_by_oidc(ISSUER, "kc-subject-1").is_admin is False

    tok = _token(resource_access={CLIENT_ID: {"roles": ["admin"]}})
    assert client.post("/api/auth/oidc", json={"id_token": tok}).status_code == 200
    assert users.get_by_oidc(ISSUER, "kc-subject-1").is_admin is True


def test_email_verified_accepts_the_string_form():
    assert oidc.email_is_verified({"email_verified": "true"})
    assert not oidc.email_is_verified({"email_verified": "false"})
    assert not oidc.email_is_verified({})


# ─── the endpoint ───────────────────────────────────────────────────


@pytest.fixture
def users(tmp_path):
    from src.users import UserStore
    return UserStore(tmp_path / "users.db")


def _app(users, monkeypatch, *, features=("sso",)) -> FastAPI:
    from src.api.deps import get_users
    from src.api.routers import auth as auth_router
    from src.ee import mount_enterprise
    from src.ee.sso import router as sso_router

    monkeypatch.setattr(
        "src.api.workspace_provision.provision_personal_workspace",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(auth_router, "record_action", lambda **k: None)
    monkeypatch.setattr(sso_router, "record_action", lambda **k: None)
    trust_test_key(monkeypatch)
    app = FastAPI()
    app.include_router(auth_router.router)
    env = {lic.ENV_KEY: mint_test_license(features)} if features else {}
    mount_enterprise(app, env=env)
    app.dependency_overrides[get_users] = lambda: users
    return app


@pytest.fixture
def client(users, monkeypatch):
    return TestClient(_app(users, monkeypatch))


@pytest.mark.parametrize("features", [(), ("analytics",)], ids=["no-licence", "analytics-only"])
def test_without_an_sso_licence_the_endpoint_is_the_free_allowance(users, monkeypatch, features):
    """LICENSE_EE lets an installation use single sign-on without a licence
    while no more than five people sign in through it, so the route is there
    (and /api/capabilities reports `sso` on) with no licence and with one for
    other features. The limit itself is tested in test_the_sso_allowance.py."""
    c = TestClient(_app(users, monkeypatch, features=features))
    assert c.post("/api/auth/oidc", json={"id_token": _token()}).status_code == 200
    assert users.get_by_oidc(ISSUER, "kc-subject-1") is not None


def _password_user(users, email="dev@example.com"):
    from src.users import User, UserAuthMethod, hash_password
    user = User(id="pw-1", email=email, auth_method=UserAuthMethod.PASSWORD,
                password_hash=hash_password("Vq7#mLz2-Rk9wTp4!"))
    users.create(user)
    return user


def test_first_sign_in_creates_an_oidc_user(client, users):
    r = client.post("/api/auth/oidc", json={"id_token": _token()})
    assert r.status_code == 200, r.text
    user = users.get_by_oidc(ISSUER, "kc-subject-1")
    assert user is not None
    assert user.auth_method.value == "oidc"
    assert user.is_admin is False


def test_a_bad_token_is_a_401_not_a_500(client):
    r = client.post("/api/auth/oidc", json={"id_token": _token(key=FOREIGN_KEY)})
    assert r.status_code == 401


def test_unverified_email_does_not_take_over_a_password_account(client, users):
    _password_user(users)
    r = client.post("/api/auth/oidc",
                    json={"id_token": _token(email_verified=False)})
    assert r.status_code == 403
    assert users.get_by_email("dev@example.com").oidc_sub is None


def test_unverified_email_does_not_create_an_account(client, users):
    """A new account under an unverified address would be resolved by email
    later: an email invite adds it to the workspace, a Google sign-in of the
    real owner links to it."""
    r = client.post("/api/auth/oidc",
                    json={"id_token": _token(email="victim@corp.example",
                                             email_verified=False)})
    assert r.status_code == 403
    assert users.get_by_oidc(ISSUER, "kc-subject-1") is None
    assert users.get_by_email("victim@corp.example") is None


def test_an_existing_sso_user_signs_in_by_subject_even_if_unverified_later(client, users):
    """The (iss, sub) match is the identity; email_verified gates only the
    email-based steps."""
    assert client.post("/api/auth/oidc", json={"id_token": _token()}).status_code == 200
    r = client.post("/api/auth/oidc", json={"id_token": _token(email_verified=False)})
    assert r.status_code == 200, r.text


def test_verified_email_links_the_existing_account(client, users):
    _password_user(users)
    r = client.post("/api/auth/oidc", json={"id_token": _token()})
    assert r.status_code == 200, r.text
    linked = users.get_by_email("dev@example.com")
    assert linked.id == "pw-1"
    assert linked.oidc_sub == "kc-subject-1"
    assert linked.has_password  # the password is not dropped by linking


def test_an_account_bound_to_another_subject_is_not_rebound(client, users):
    _password_user(users)
    assert client.post("/api/auth/oidc", json={"id_token": _token()}).status_code == 200
    r = client.post("/api/auth/oidc", json={"id_token": _token(sub="kc-subject-2")})
    assert r.status_code == 409


def test_the_master_account_cannot_be_reached_through_sso(client, users, monkeypatch):
    from src.users import User
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", "root@example.com")
    users.create(User(id="master-admin", email="root@example.com", is_admin=True))
    r = client.post("/api/auth/oidc", json={"id_token": _token(email="root@example.com")})
    assert r.status_code == 403
    assert users.get_by_id("master-admin").oidc_sub is None


def test_sso_cannot_create_the_master_address_before_the_first_master_login(
        client, users, monkeypatch):
    """No account holds the master email yet. An IdP identity for it must not
    create one: the first master login would adopt it, and from then on the
    IdP user would sign in by subject as a global admin."""
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", "root@example.com")
    monkeypatch.setenv("CELMIS_MASTER_KEY", "sk-master-correct-horse-battery")
    r = client.post("/api/auth/oidc", json={"id_token": _token(email="ROOT@example.com")})
    assert r.status_code == 403
    assert users.get_by_oidc(ISSUER, "kc-subject-1") is None
    assert users.get_by_email("root@example.com") is None

    r = client.post("/api/auth/login", json={
        "email": "root@example.com", "password": "sk-master-correct-horse-battery"})
    assert r.status_code == 200, r.text
    master = users.get_by_id("master-admin")
    assert master is not None and master.oidc_sub is None


def test_an_sso_identity_bound_to_the_master_address_is_refused(
        client, users, monkeypatch):
    """Found by (iss, sub), not by email: the guard runs on that branch too,
    whatever address the token now carries."""
    assert client.post("/api/auth/oidc", json={"id_token": _token()}).status_code == 200
    monkeypatch.setenv("CELMIS_MASTER_EMAIL", "dev@example.com")
    r = client.post("/api/auth/oidc", json={"id_token": _token(email="other@example.com")})
    assert r.status_code == 403


def test_an_erased_sso_user_can_sign_in_again_as_a_new_account(
        client, users, monkeypatch):
    """GDPR erasure drops the IdP subject like it drops google_sub. Left on
    the erased row, it would be found by (iss, sub) and refused as disabled,
    and the unique index would block a fresh account: locked out for good."""
    import asyncio

    from src.api.routers import gdpr

    assert client.post("/api/auth/oidc", json={"id_token": _token()}).status_code == 200
    original = users.get_by_oidc(ISSUER, "kc-subject-1")

    class _Session:
        async def scalars(self, *_a, **_k):
            class _R:
                def all(self):
                    return []
            return _R()

        async def execute(self, *_a, **_k):
            class _R:
                rowcount = 0

                def all(self):
                    return []
            return _R()

        def begin_nested(self):
            # the pack cleanup runs in a savepoint; this fake has nothing to roll back
            class _Savepoint:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_exc):
                    return False
            return _Savepoint()

        async def commit(self):
            return None

    class _Creds:
        def list(self, **_k):
            return []

    monkeypatch.setattr("src.users.get_user_store", lambda: users)
    monkeypatch.setattr("src.credentials.get_credential_store", lambda: _Creds())
    from src.users import User
    admin = User(id="admin-1", email="admin@example.com", is_admin=True)
    asyncio.run(gdpr.erase_user(original.id, session=_Session(), admin=admin))

    erased = users.get_by_id(original.id)
    assert erased.oidc_iss is None and erased.oidc_sub is None
    assert users.get_by_oidc(ISSUER, "kc-subject-1") is None

    r = client.post("/api/auth/oidc", json={"id_token": _token()})
    assert r.status_code == 200, r.text
    fresh = users.get_by_oidc(ISSUER, "kc-subject-1")
    assert fresh.id != original.id and fresh.is_active


def test_admin_role_grants_and_sync_revokes(client, users, monkeypatch):
    monkeypatch.setenv("OIDC_ADMIN_ROLE", "celmis-admin")
    tok = _token(realm_access={"roles": ["celmis-admin"]})
    assert client.post("/api/auth/oidc", json={"id_token": tok}).status_code == 200
    assert users.get_by_oidc(ISSUER, "kc-subject-1").is_admin is True

    # Role gone, no sync: the flag stays (a hand-made admin is not demoted).
    assert client.post("/api/auth/oidc", json={"id_token": _token()}).status_code == 200
    assert users.get_by_oidc(ISSUER, "kc-subject-1").is_admin is True

    monkeypatch.setenv("OIDC_ADMIN_ROLE_SYNC", "true")
    assert client.post("/api/auth/oidc", json={"id_token": _token()}).status_code == 200
    assert users.get_by_oidc(ISSUER, "kc-subject-1").is_admin is False


def test_unconfigured_is_503(client, monkeypatch):
    monkeypatch.delenv("OIDC_ISSUER")
    monkeypatch.delenv("AUTH_OIDC_ISSUER", raising=False)
    r = client.post("/api/auth/oidc", json={"id_token": _token()})
    assert r.status_code == 503




# ─── Addresses the operator vouches for ─────────────────────────────
# A company directory often never sets email_verified. The operator names
# the domains whose mailboxes only the directory's administrator hands out,
# or trusts every address the IdP sends.

def test_a_trusted_domain_lets_an_unverified_address_create_an_account(client, users, monkeypatch):
    monkeypatch.setenv("OIDC_TRUSTED_EMAIL_DOMAINS", " Corp.Example , @other.example")
    r = client.post("/api/auth/oidc",
                    json={"id_token": _token(email="new.person@corp.example",
                                             email_verified=False)})
    assert r.status_code == 200, r.text
    assert users.get_by_oidc(ISSUER, "kc-subject-1").email == "new.person@corp.example"


def test_a_trusted_domain_links_an_unverified_address_to_its_account(client, users, monkeypatch):
    monkeypatch.setenv("OIDC_TRUSTED_EMAIL_DOMAINS", "example.com")
    _password_user(users)
    r = client.post("/api/auth/oidc", json={"id_token": _token(email_verified=False)})
    assert r.status_code == 200, r.text
    assert users.get_by_email("dev@example.com").oidc_sub == "kc-subject-1"


@pytest.mark.parametrize("address", ["x@sub.corp.example", "x@corp.example.evil", "x@other.example"])
def test_only_the_listed_domains_are_trusted(client, users, monkeypatch, address):
    monkeypatch.setenv("OIDC_TRUSTED_EMAIL_DOMAINS", "corp.example")
    r = client.post("/api/auth/oidc",
                    json={"id_token": _token(email=address, email_verified=False)})
    assert r.status_code == 403
    assert users.get_by_email(address) is None


def test_require_verified_email_false_trusts_every_address(client, users, monkeypatch):
    monkeypatch.setenv("OIDC_REQUIRE_VERIFIED_EMAIL", "false")
    r = client.post("/api/auth/oidc",
                    json={"id_token": _token(email="anyone@anywhere.example",
                                             email_verified=None)})
    assert r.status_code == 200, r.text


def test_the_default_still_requires_a_verified_address():
    config = oidc.oidc_config()
    assert config.require_verified_email is True
    assert config.trusted_email_domains == frozenset()
    assert not oidc.email_is_trusted("dev@example.com", {"email": "dev@example.com"}, config)


# ─── A token without an email claim ─────────────────────────────────

def test_an_address_shaped_username_stands_for_a_missing_email(client, users, monkeypatch):
    monkeypatch.setenv("OIDC_TRUSTED_EMAIL_DOMAINS", "corp.example")
    r = client.post("/api/auth/oidc",
                    json={"id_token": _token(email=None, email_verified=None,
                                             preferred_username="jo@corp.example")})
    assert r.status_code == 200, r.text
    assert users.get_by_oidc(ISSUER, "kc-subject-1").email == "jo@corp.example"


def test_an_address_shaped_username_is_not_trusted_by_itself(client, users):
    """email_verified speaks about the email claim, not about the username."""
    r = client.post("/api/auth/oidc",
                    json={"id_token": _token(email=None, email_verified=True,
                                             preferred_username="jo@corp.example")})
    assert r.status_code == 403
    assert users.get_by_email("jo@corp.example") is None


def test_without_any_address_a_stand_in_creates_the_account(client, users):
    token = _token(email=None, email_verified=None, preferred_username="jo", name="Jo Person")
    r = client.post("/api/auth/oidc", json={"id_token": token})
    assert r.status_code == 200, r.text
    user = users.get_by_oidc(ISSUER, "kc-subject-1")
    assert user.email == "sso-kc-subject-1@sso.example.com"
    assert user.name == "Jo Person"
    # Found by subject the next time.
    assert client.post("/api/auth/oidc", json={"id_token": token}).status_code == 200
    assert len([u for u in users.list(active_only=False) if u.oidc_sub == "kc-subject-1"]) == 1


def test_a_stand_in_never_takes_over_an_account_that_holds_its_address(client, users):
    stand_in, _ = oidc.address_of({"sub": "kc-subject-1"}, oidc.oidc_config())
    _password_user(users, email=stand_in)
    r = client.post("/api/auth/oidc",
                    json={"id_token": _token(email=None, email_verified=None)})
    assert r.status_code == 409
    assert users.get_by_email(stand_in).oidc_sub is None


def test_an_unusual_subject_is_hashed_into_the_stand_in():
    address, stand_in = oidc.address_of({"sub": "auth0|abc/../x y"}, oidc.oidc_config())
    assert stand_in is True
    local = address.split("@")[0]
    assert local.startswith("sso-") and len(local) == 4 + 32 and "|" not in address


def test_a_token_without_a_subject_is_still_refused(client):
    r = client.post("/api/auth/oidc", json={"id_token": _token(sub=None)})
    assert r.status_code == 401


# ─── A stand-in gives way to the first trusted address ──────────────
# The IdP sent no address when the account was made, and the directory's
# administrator fills one in later: the account is found by subject and takes
# the real address, so invites and shares to it reach this person.

def _stand_in_account(client, users, **claims):
    r = client.post("/api/auth/oidc",
                    json={"id_token": _token(email=None, email_verified=None, **claims)})
    assert r.status_code == 200, r.text
    user = users.get_by_oidc(ISSUER, "kc-subject-1")
    assert user.email == "sso-kc-subject-1@sso.example.com"
    return user


def test_a_trusted_address_replaces_the_stand_in(client, users, monkeypatch):
    monkeypatch.setenv("OIDC_TRUSTED_EMAIL_DOMAINS", "corp.example")
    before = _stand_in_account(client, users, name="Jo Person")
    r = client.post("/api/auth/oidc",
                    json={"id_token": _token(email="jo@corp.example", email_verified=False)})
    assert r.status_code == 200, r.text
    after = users.get_by_oidc(ISSUER, "kc-subject-1")
    assert (after.id, after.email, after.name) == (before.id, "jo@corp.example", "Jo Person")
    assert len(users.list(active_only=False)) == 1


def test_a_name_that_was_the_stand_in_goes_with_it(client, users):
    before = _stand_in_account(client, users, name=None, preferred_username=None)
    assert before.name == "sso-kc-subject-1"
    r = client.post("/api/auth/oidc", json={"id_token": _token(email="jo@example.com")})
    assert r.status_code == 200, r.text
    assert users.get_by_oidc(ISSUER, "kc-subject-1").name == "Dev Person"


def test_an_untrusted_address_leaves_the_stand_in(client, users):
    _stand_in_account(client, users)
    r = client.post("/api/auth/oidc",
                    json={"id_token": _token(email="jo@corp.example", email_verified=False)})
    assert r.status_code == 200, r.text
    assert users.get_by_oidc(ISSUER, "kc-subject-1").email == "sso-kc-subject-1@sso.example.com"


def test_an_address_another_account_holds_leaves_the_stand_in(client, users):
    _stand_in_account(client, users)
    _password_user(users)
    r = client.post("/api/auth/oidc", json={"id_token": _token()})
    assert r.status_code == 200, r.text
    assert users.get_by_oidc(ISSUER, "kc-subject-1").email == "sso-kc-subject-1@sso.example.com"
    assert users.get_by_email("dev@example.com").oidc_sub is None


def test_an_address_taken_between_the_lookup_and_the_write_leaves_the_stand_in(
        client, users, monkeypatch):
    from src.users import UserExistsError

    _stand_in_account(client, users)
    _password_user(users)
    found = users.get_by_email
    monkeypatch.setattr(users, "get_by_email",
                        lambda email: None if email == "dev@example.com" else found(email))
    r = client.post("/api/auth/oidc", json={"id_token": _token()})
    assert r.status_code == 200, r.text
    assert users.get_by_oidc(ISSUER, "kc-subject-1").email == "sso-kc-subject-1@sso.example.com"
    sso_user = users.get_by_oidc(ISSUER, "kc-subject-1")
    with pytest.raises(UserExistsError):
        users.update(dataclasses.replace(sso_user, email="dev@example.com"))


def test_a_real_address_is_never_replaced(client, users):
    """Only the stand-in of this very subject gives way: an IdP that renames a
    mailbox does not rename the account, as before."""
    assert client.post("/api/auth/oidc", json={"id_token": _token()}).status_code == 200
    r = client.post("/api/auth/oidc", json={"id_token": _token(email="other@example.com")})
    assert r.status_code == 200, r.text
    assert users.get_by_oidc(ISSUER, "kc-subject-1").email == "dev@example.com"
