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
    })
    assert {"celmis-admin", "platform", "/platform"} <= roles


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
def test_without_an_sso_licence_the_endpoint_is_not_there(users, monkeypatch, features):
    """Not a 403 from a mounted route: absent, so /api/capabilities reports
    `sso` off from the route table and the login page hides the button."""
    c = TestClient(_app(users, monkeypatch, features=features))
    assert c.post("/api/auth/oidc", json={"id_token": _token()}).status_code == 404
    assert users.get_by_oidc(ISSUER, "kc-subject-1") is None


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


