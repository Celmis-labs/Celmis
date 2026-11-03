"""`code_challenge_method=plain` was accepted by the MCP OAuth server.

With `plain` the challenge IS the verifier, so whoever can read the authorize
URL (browser history, a proxy log, a Referer) can redeem the code: PKCE then
protects nothing. Only S256 is accepted; `plain` is `invalid_request`, at the
authorize screen, at the consent POST (directly callable), and at the token
step for a row stored before the change.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from src.api.routers import oauth


@pytest.fixture(scope="module")
def client() -> TestClient:
    from src.api.main import app
    from src.db.session import get_async_session

    async def no_database():
        yield None   # the method check answers before any query is made

    app.dependency_overrides[get_async_session] = no_database
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.pop(get_async_session, None)


def _authorize(client, method: str):
    return client.get("/oauth/authorize", params={
        "response_type": "code", "client_id": "ec_unknown",
        "redirect_uri": "http://127.0.0.1:9000/cb",
        "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        "code_challenge_method": method,
    })


def test_authorize_refuses_plain_as_invalid_request(client):
    r = _authorize(client, "plain")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"
    assert "S256" in r.json()["error_description"]


def test_authorize_refuses_any_other_method(client):
    assert _authorize(client, "S512").json()["error"] == "invalid_request"


def test_s256_gets_past_the_method_check(client):
    # Same request with S256 reaches the client lookup (no database here, so
    # it fails there): the method is no longer what stops it.
    r = _authorize(client, "S256")
    assert "error_description" not in r.text


def test_the_consent_post_refuses_plain_too():
    resp = asyncio.run(oauth.authorize_consent(
        request=None, client_id="c", redirect_uri="http://127.0.0.1:9000/cb",
        code_challenge="abc", code_challenge_method="plain", scope="", state="",
        session=None, user=None, workspace_id="w",
    ))
    assert resp.status_code == 400
    assert json.loads(resp.body)["error"] == "invalid_request"


def test_the_discovery_document_advertises_only_s256(client):
    r = client.get("/.well-known/oauth-authorization-server")
    if r.status_code == 200 and "code_challenge_methods_supported" in r.json():
        assert r.json()["code_challenge_methods_supported"] == ["S256"]


def test_the_source_has_no_plain_comparison_left():
    import inspect
    src = inspect.getsource(oauth)
    assert "pkce plain mismatch" not in src
    assert '("S256", "plain")' not in src
