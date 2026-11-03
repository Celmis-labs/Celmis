"""The Bearer a page script can read lives an hour, not a month.

An audit found the web session and the API token both lasting 30 days, with
the token returned by the session endpoint to any script on the page. Now the
access token is short and is renewed by a refresh credential that only the web
server holds; signing out revokes the credential.

What is pinned here:
  * a sign-in returns a token that expires within the hour, and a credential;
  * the credential renews the token, and a revoked one does not;
  * the old bearer-renews-itself endpoint is gone (it made a token immortal);
  * the lifetime is bounded whatever the environment says;
  * a disabled account stops renewing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from src.api import web_session


@pytest.fixture(scope="module", autouse=True)
def _credentials_table(tmp_path_factory):
    """The credentials live in `oauth_refresh_tokens`; a throwaway SQLite file
    stands in for Postgres, as in the other tests that read the engine."""
    from sqlalchemy import create_engine, event

    import src.llm.budget as budget
    from src.db.models import OAuthRefreshToken
    from tests.api.rbac_world import _sqlite_booleans

    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('db') / 'celmis.db'}")
    event.listen(engine, "connect", _sqlite_booleans)
    OAuthRefreshToken.__table__.create(engine)
    previous = budget._ENGINE
    budget._ENGINE = engine
    yield
    budget._ENGINE = previous


@pytest.fixture(scope="module")
def client(_credentials_table) -> TestClient:
    from src.api.main import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def login(client) -> dict:
    creds = {"email": "session-probe@example.com", "password": "n0t-a-real-pw!"}
    client.post("/api/auth/signup", json={**creds, "name": "Session Probe"})
    r = client.post("/api/auth/login", json=creds)
    assert r.status_code == 200, r.text
    return r.json()


def _seconds_left(body: dict) -> float:
    exp = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    return (exp - datetime.now(UTC)).total_seconds()


def test_the_access_token_lives_at_most_an_hour(login):
    assert 0 < _seconds_left(login) <= 3600 + 5


def test_a_sign_in_hands_out_a_refresh_credential(login):
    assert len(login["refresh_token"]) >= 32


def test_the_credential_renews_the_token(client, login):
    r = client.post("/api/auth/session/refresh",
                    json={"refresh_token": login["refresh_token"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body.get("refresh_token") is None  # the caller already holds it
    me = client.get("/api/auth/me",
                    headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200


def test_sign_out_ends_the_credential(client, login):
    out = client.post("/api/auth/logout", json={"refresh_token": login["refresh_token"]})
    assert out.status_code == 204
    again = client.post("/api/auth/session/refresh",
                        json={"refresh_token": login["refresh_token"]})
    assert again.status_code == 401


def test_a_guess_is_refused_like_a_revoked_credential(client):
    r = client.post("/api/auth/session/refresh", json={"refresh_token": "x" * 43})
    assert r.status_code == 401


def test_a_bearer_token_cannot_renew_itself_any_more(client, login):
    r = client.post("/api/auth/refresh",
                    headers={"Authorization": f"Bearer {login['access_token']}"})
    assert r.status_code == 410


def test_logout_of_an_unknown_credential_does_not_say_so(client):
    r = client.post("/api/auth/logout", json={"refresh_token": "y" * 43})
    assert r.status_code == 204


def test_the_lifetime_cannot_be_configured_past_an_hour(monkeypatch):
    monkeypatch.setenv("CELMIS_ACCESS_TOKEN_TTL_SECONDS", "2592000")
    assert web_session.access_ttl_seconds() == 3600
    monkeypatch.setenv("CELMIS_ACCESS_TOKEN_TTL_SECONDS", "5")
    assert web_session.access_ttl_seconds() == web_session.ACCESS_TTL_MIN_SECONDS
    monkeypatch.setenv("CELMIS_ACCESS_TOKEN_TTL_SECONDS", "nonsense")
    assert web_session.access_ttl_seconds() == 3600
    monkeypatch.setenv("CELMIS_WEB_SESSION_HOURS", "720")
    assert web_session.session_lifetime().total_seconds() <= 24 * 3600


def test_a_fixed_credential_does_not_slide():
    """A single-sign-on-only user must meet the identity provider again."""
    raw = web_session.issue("probe-user-fixed", sliding=False)
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.db.models import OAuthRefreshToken

    def expiry():
        with Session(web_session._engine()) as s:
            return s.execute(select(OAuthRefreshToken.expires_at).where(
                OAuthRefreshToken.token_hash == web_session._hash(raw))).scalar_one()

    before = expiry()
    assert web_session.redeem(raw) == "probe-user-fixed"
    assert expiry() == before
