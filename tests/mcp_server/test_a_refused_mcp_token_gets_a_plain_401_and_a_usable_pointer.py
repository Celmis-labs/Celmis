"""A refused MCP token is told so in words, and the 401 points somewhere real.

What a person saw before: `JSON Parse error: Unrecognized token '<'` and an
Authenticate button. The 401's `resource_metadata` named the JWT audience as a
host (`https://mcp-code-analyzer/...`), the client fell back to the web app's
root, got its sign-in page and tried to read HTML as JSON.

The 401 is now an invitation that can be followed: a plain sentence that says
whether a token was missing or not accepted, and a `resource_metadata` that is
the public `https` address of THAT endpoint's document, under the prefix the API
sits behind. Nothing about who is let in changes, and the sentence does not say
whether a token ever existed.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.mcp_server.auth import JwtConfig, issue_token

SECRET = "c4f2a70b9e5d18634ac7f0e2b9d51a86"
PUBLIC = "https://celmis.example.com"
INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "t", "version": "1"}},
}
BASE = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
#: What Caddy adds for https://celmis.example.com/backend/mcp/dev/
PROXY = {
    "X-Forwarded-Proto": "https", "X-Forwarded-Host": "celmis.example.com",
    "X-Forwarded-For": "203.0.113.7", "X-Forwarded-Prefix": "/backend",
}


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    from src.config import get_settings

    monkeypatch.setenv("MCP_JWT_SECRET", SECRET)
    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC)
    monkeypatch.delenv("MCP_OAUTH_ISSUER", raising=False)
    monkeypatch.delenv("CELMIS_MCP_SELF_SERVICE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    from src.mcp_server.http_app import mount_mcp

    app = FastAPI()
    assert mount_mcp(app) is True
    # The Host the proxy forwards: the public name, over plain http inside.
    with TestClient(app, base_url="http://celmis.example.com") as c:
        yield c


def _challenge(response) -> dict[str, str]:
    """The auth-params of a `WWW-Authenticate: Bearer ...` header."""
    header = response.headers["www-authenticate"]
    assert header.startswith("Bearer "), header
    return dict(re.findall(r'(\w+)="([^"]*)"', header))


def _post(client, path, token=None, extra=None):
    headers = {**BASE, **PROXY, **(extra or {})}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(path, json=INIT, headers=headers)


def _jwt(*scopes: str, secret: str = SECRET) -> str:
    return issue_token(JwtConfig(secret=secret), subject="u-1", scopes=list(scopes))


def test_the_401_points_at_the_public_https_document_of_the_dev_endpoint(client):
    r = _post(client, "/mcp/dev/")
    assert r.status_code == 401
    assert _challenge(r)["resource_metadata"] == (
        f"{PUBLIC}/backend/.well-known/oauth-protected-resource/mcp/dev")


def test_the_401_of_the_full_endpoint_points_at_its_own_document(client):
    r = _post(client, "/mcp/")
    assert r.status_code == 401
    assert _challenge(r)["resource_metadata"] == f"{PUBLIC}/backend/.well-known/oauth-protected-resource/mcp"


def test_the_pointer_never_names_the_token_audience_or_an_internal_host(client):
    for path in ("/mcp/dev/", "/mcp/"):
        for token in (None, "not-a-token"):
            url = _challenge(_post(client, path, token))["resource_metadata"]
            assert url.startswith("https://celmis.example.com/")
            assert "mcp-code-analyzer" not in url and "code-analyzer" not in url and ":8000" not in url


def test_the_pointer_follows_the_prefix_the_proxy_sent(client):
    r = _post(client, "/mcp/dev/", extra={"X-Forwarded-Prefix": "/api"})
    assert _challenge(r)["resource_metadata"].startswith(f"{PUBLIC}/api/.well-known/")


def test_a_proxy_that_sends_no_prefix_still_gets_the_backend_one_on_the_app_host(client):
    """An install whose Caddyfile predates `X-Forwarded-Prefix`."""
    headers = {**BASE, **{k: v for k, v in PROXY.items() if k != "X-Forwarded-Prefix"}}
    r = client.post("/mcp/dev/", json=INIT, headers=headers)
    assert _challenge(r)["resource_metadata"].startswith(f"{PUBLIC}/backend/.well-known/")


def test_a_forwarded_host_nobody_declared_is_not_written_into_the_challenge(client):
    r = _post(client, "/mcp/dev/", extra={"X-Forwarded-Host": "evil.example.net"})
    assert "evil" not in r.headers["www-authenticate"]
    assert _challenge(r)["resource_metadata"].startswith(f"{PUBLIC}/")


def test_the_explicit_setting_decides_the_address_when_the_operator_gave_it(client, monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_ISSUER", "https://mcp.celmis.example.com/edge")
    r = _post(client, "/mcp/dev/")
    assert _challenge(r)["resource_metadata"] == (
        "https://mcp.celmis.example.com/edge/.well-known/oauth-protected-resource/mcp/dev")


def test_no_token_says_a_token_is_needed_and_where_it_goes(client):
    r = _post(client, "/mcp/dev/")
    assert r.status_code == 401
    params = _challenge(r)
    assert params["error"] == "invalid_token"
    assert params["error_description"].startswith("Authentication required: add your Celmis token")
    assert "Authorization: Bearer" in params["error_description"]


def test_a_refused_token_says_it_was_not_accepted_and_that_authenticate_is_not_the_way(client):
    r = _post(client, "/mcp/dev/", token="not-a-token")
    assert r.status_code == 401
    description = _challenge(r)["error_description"]
    assert "The token was not accepted: it is unknown, revoked or expired" in description
    assert "/plugin configure celmis-code@celmis" in description
    assert "Authentication required" not in description


def test_the_body_is_json_with_the_same_words_as_the_header(client):
    r = _post(client, "/mcp/dev/", token="not-a-token")
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["error"] == "invalid_token"
    assert body["error_description"] == _challenge(r)["error_description"]


def test_the_401_is_never_cached(client):
    assert _post(client, "/mcp/dev/").headers["cache-control"] == "no-store"


def test_the_description_is_safe_inside_a_header(client):
    for token in (None, "not-a-token", "cmcp_" + "a" * 30):
        r = _post(client, "/mcp/dev/", token=token)
        description = _challenge(r)["error_description"]
        assert '"' not in description and "\n" not in description and "\r" not in description
        r.headers["www-authenticate"].encode("ascii")


def test_without_self_service_a_new_token_comes_from_the_administrator(client, monkeypatch):
    from src.mcp_server import token_store

    monkeypatch.setattr(token_store, "self_service_enabled", lambda: False)
    description = _challenge(_post(client, "/mcp/dev/", token="x"))["error_description"]
    assert "Ask your Celmis administrator for a new token" in description
    assert "settings page" not in description


def test_with_self_service_a_new_token_comes_from_the_settings_page(client, monkeypatch):
    from src.mcp_server import token_store

    monkeypatch.setattr(token_store, "self_service_enabled", lambda: True)
    description = _challenge(_post(client, "/mcp/dev/", token="x"))["error_description"]
    assert "Create a new token on the MCP settings page of Celmis" in description
    assert "administrator" not in description


def test_a_token_that_never_existed_and_a_token_that_was_revoked_get_the_same_words(client):
    """A signed token with no grant stands for a revoked one: same status, same text.
    The answer must not let a stranger tell which tokens exist."""
    unknown = _post(client, "/mcp/dev/", token="not-a-token")
    forged = _post(client, "/mcp/dev/", token=_jwt("read:code", secret="a-different-secret-entirely-32bytes"))
    ungranted = _post(client, "/mcp/dev/", token=_jwt("read:code"))
    assert unknown.status_code == forged.status_code == 401
    assert _challenge(unknown)["error_description"] == _challenge(forged)["error_description"]
    if ungranted.status_code == 401:
        assert _challenge(ungranted)["error_description"] == _challenge(unknown)["error_description"]
    assert json.loads(unknown.text) == json.loads(forged.text)


def test_a_project_token_on_the_dev_endpoint_is_told_which_endpoint_takes_it(client):
    from src.mcp_server.http_app import _challenge_sentence

    on_dev = _challenge(_post(client, "/mcp/dev/", token="cmcp_" + "a" * 30))["error_description"]
    assert "A project token (cmcp_...) works on the full /mcp endpoint only" in on_dev
    # The full endpoint is where that token belongs, so it is not sent away from it.
    scope = {"headers": [(b"authorization", b"Bearer cmcp_" + b"a" * 30)]}
    assert "project token" not in _challenge_sentence(scope, "/mcp")
    assert "project token" in _challenge_sentence(scope, "/mcp/dev")


def test_a_token_signed_for_another_audience_is_still_refused(client):
    """The verifier is untouched: only the words around the refusal changed."""
    wrong = issue_token(JwtConfig(secret=SECRET, audience="another-service"), subject="u-1",
                        scopes=["read:code"])
    r = _post(client, "/mcp/dev/", token=wrong)
    assert r.status_code == 401
    assert _challenge(r)["error"] == "invalid_token"


def test_a_missing_scope_keeps_its_403_and_gains_the_same_pointer(client, monkeypatch):
    from src.config import get_settings

    monkeypatch.setenv("CELMIS_MCP_LEGACY_TOKENS", "accept")
    get_settings.cache_clear()
    r = _post(client, "/mcp/dev/", token=_jwt("read:graph"))
    assert r.status_code == 403
    header = r.headers["www-authenticate"]
    assert "insufficient_scope" in header
    assert header.count("resource_metadata=") == 1
    assert _challenge(r)["resource_metadata"] == f"{PUBLIC}/backend/.well-known/oauth-protected-resource/mcp/dev"


def test_an_accepted_token_is_answered_as_before_with_no_challenge(client, monkeypatch):
    from src.config import get_settings

    monkeypatch.setenv("CELMIS_MCP_LEGACY_TOKENS", "accept")
    get_settings.cache_clear()
    r = _post(client, "/mcp/dev/", token=_jwt("read:code"))
    assert r.status_code == 200, r.text
    assert "www-authenticate" not in r.headers
    assert "celmis-dev" in r.text
