"""An MCP client that presses Authenticate ends with a sentence, not a parse error.

Celmis hands MCP access out as tokens. Its OAuth endpoints exist, but a stock
client cannot finish a browser sign-in on any installation: registration needs a
signed-in workspace admin, and the consent step needs the API's bearer token,
which a form post does not carry. So the client's dynamic-registration attempt
is answered in the error shape RFC 7591 / RFC 6749 define (`error`,
`error_description`), which is what the official MCP SDK turns into its message,
and the message says what to do instead.

Nothing is opened up by this: an anonymous registration is still refused, and a
signed-in caller reaches the same admin check as before.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.api.deps import get_users, require_workspace_admin
from src.api.routers import oauth as oauth_router
from src.db.session import get_async_session

#: What the official MCP SDK posts to the registration endpoint.
SDK_REGISTRATION = {
    "client_name": "claude-code",
    "redirect_uris": ["http://localhost:9999/callback"],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}


class _NoUsers:
    def get_by_id(self, _id):
        return None


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MCP_JWT_SECRET", "c4f2a70b9e5d18634ac7f0e2b9d51a86")
    app = FastAPI()
    app.include_router(oauth_router.router)
    app.add_exception_handler(oauth_router.OAuthRefusal, oauth_router.oauth_refusal_handler)
    app.dependency_overrides[get_users] = lambda: _NoUsers()
    return TestClient(app, base_url="https://celmis.example.com")


def _assert_plain_refusal(r):
    assert r.status_code == 401, r.text
    body = r.json()
    assert body["error"] == "access_denied"
    assert "Celmis does not sign MCP clients in through the browser, so Authenticate cannot help" in (
        body["error_description"])
    assert "/plugin configure celmis-code@celmis" in body["error_description"]
    assert body["detail"] == body["error_description"]
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.headers["cache-control"] == "no-store"


def test_a_client_with_no_token_that_tries_to_register_is_told_why_it_cannot(client):
    _assert_plain_refusal(client.post("/oauth/register", json=SDK_REGISTRATION))


def test_a_client_with_a_refused_token_gets_the_same_sentence(client):
    """The SDK sends its configured MCP token on every request, registration included."""
    r = client.post("/oauth/register", json=SDK_REGISTRATION, headers={"Authorization": "Bearer not-a-token"})
    _assert_plain_refusal(r)


def test_a_signed_token_of_a_user_that_does_not_exist_is_not_a_session(client):
    from src.api.jwt_auth import issue_token

    token, _ = issue_token(user_id="ghost-user", email="ghost@celmis.example.com")
    r = client.post("/oauth/register", json=SDK_REGISTRATION, headers={"Authorization": f"Bearer {token}"})
    _assert_plain_refusal(r)


def test_the_refusal_does_not_say_whether_a_token_exists(client):
    """Missing and unknown tokens get the same body, so the endpoint is no oracle."""
    none = client.post("/oauth/register", json=SDK_REGISTRATION)
    bogus = client.post("/oauth/register", json=SDK_REGISTRATION, headers={"Authorization": "Bearer abc"})
    assert none.json() == bogus.json()


def test_a_signed_in_non_admin_still_reaches_the_admin_check_and_is_refused_by_it(client, monkeypatch):
    """The new gate answers only to a caller with no session. A real session goes on
    to `require_workspace_admin` exactly as before."""

    class _Member:
        is_active = True

    monkeypatch.setattr(oauth_router, "get_current_user", lambda authorization=None, users=None: _Member())

    def _not_admin():
        raise HTTPException(status_code=403, detail="Workspace admin scope required")

    client.app.dependency_overrides[require_workspace_admin] = _not_admin
    client.app.dependency_overrides[get_async_session] = lambda: None
    r = client.post("/oauth/register", json={"name": "x", "redirect_uris": ["http://localhost/cb"]},
                    headers={"Authorization": "Bearer session"})
    assert r.status_code == 403
    assert r.json()["detail"] == "Workspace admin scope required"
    assert "error" not in r.json()


def test_the_registration_route_still_names_the_workspace_admin_gate():
    """The surface-gate test pins `require_workspace_admin` on this route; the new
    dependency sits next to it, it does not replace it."""
    route = next(r for r in oauth_router.router.routes
                 if getattr(r, "path", "") == "/oauth/register" and "POST" in getattr(r, "methods", set()))
    names = {d.call.__name__ for d in route.dependant.dependencies}
    assert "_refuse_anonymous_registration" in names
    assert "require_workspace_admin" in names
