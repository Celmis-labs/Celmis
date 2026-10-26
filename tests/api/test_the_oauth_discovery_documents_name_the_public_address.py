"""The discovery documents carry the address the client used, not the API's own.

Behind the bundled proxy the API is `http://api:8000` and the client reached
`https://celmis.example.com/backend/...`. A document that said `http://api:8000/mcp`
either failed the client's own "is this the resource I asked for" check (RFC 9728,
section 3.3: same origin, and the resource is a path prefix of the requested URL)
or sent it to an address it cannot reach.
"""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routers.oauth_metadata import router

PUBLIC = "https://celmis.example.com"
PROXY = {
    "X-Forwarded-Proto": "https", "X-Forwarded-Host": "celmis.example.com",
    "X-Forwarded-For": "203.0.113.7", "X-Forwarded-Prefix": "/backend",
}


@pytest.fixture(autouse=True)
def _public_address(monkeypatch):
    from src.config import get_settings

    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC)
    for name in ("MCP_OAUTH_ISSUER", "MCP_ALLOWED_HOSTS", "CELMIS_PUBLIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    # What the API sees behind the proxy: plain http, the public name as Host.
    return TestClient(app, base_url="http://celmis.example.com")


def _get(path: str, **extra) -> dict:
    r = _client().get(path, headers={**PROXY, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _allowed(requested: str, resource: str) -> bool:
    """The MCP SDK's `checkResourceAllowed`: same origin, resource a path prefix."""
    a, b = urlsplit(requested), urlsplit(resource)
    if (a.scheme, a.netloc) != (b.scheme, b.netloc):
        return False
    req = a.path if a.path.endswith("/") else a.path + "/"
    res = b.path if b.path.endswith("/") else b.path + "/"
    return req.startswith(res)


def test_the_dev_resource_is_the_public_url_a_client_connects_to():
    body = _get("/.well-known/oauth-protected-resource/mcp/dev")
    assert body["resource"] == f"{PUBLIC}/backend/mcp/dev"
    assert body["scopes_supported"] == ["read:code"]
    assert _allowed(f"{PUBLIC}/backend/mcp/dev/", body["resource"])


@pytest.mark.parametrize("path", ["/.well-known/oauth-protected-resource",
                                  "/.well-known/oauth-protected-resource/mcp"])
def test_the_full_resource_is_served_at_the_bare_and_the_path_suffixed_address(path):
    body = _get(path)
    assert body["resource"] == f"{PUBLIC}/backend/mcp"
    assert "read:code" not in body["scopes_supported"]
    assert _allowed(f"{PUBLIC}/backend/mcp/", body["resource"])


def test_the_authorization_server_is_named_by_its_public_address():
    body = _get("/.well-known/oauth-protected-resource/mcp/dev")
    assert body["authorization_servers"] == [f"{PUBLIC}/backend"]


def test_every_endpoint_of_the_authorization_server_document_is_public_https():
    body = _get("/.well-known/oauth-authorization-server")
    assert body["issuer"] == f"{PUBLIC}/backend"
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        assert body[key].startswith(f"{PUBLIC}/backend/oauth/"), (key, body[key])
    assert body["registration_endpoint"] == f"{PUBLIC}/backend/oauth/register"
    assert "api:8000" not in str(body) and "http://" not in str(body)


def test_the_issuer_of_the_protected_resource_is_the_issuer_of_the_authorization_server():
    """RFC 8414 section 3.3: the issuer in the document is the one it was fetched under."""
    prm = _get("/.well-known/oauth-protected-resource/mcp/dev")
    meta = _get("/.well-known/oauth-authorization-server")
    assert prm["authorization_servers"] == [meta["issuer"]]


def test_a_document_is_never_cached_because_its_urls_depend_on_the_host_asked():
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource",
                 "/.well-known/oauth-protected-resource/mcp/dev"):
        assert _client().get(path, headers=PROXY).headers["cache-control"] == "no-store"


def test_a_host_nobody_declared_is_not_echoed_into_the_documents():
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp/dev"):
        text = _client().get(path, headers={**PROXY, "X-Forwarded-Host": "evil.example.net",
                                            "Host": "evil.example.net"}).text
        assert "evil" not in text


def test_a_prefix_that_is_not_a_plain_path_never_reaches_a_document():
    text = _client().get("/.well-known/oauth-authorization-server",
                         headers={**PROXY, "X-Forwarded-Prefix": "/../../admin"}).text
    assert ".." not in text


def test_the_explicit_setting_overrides_the_derivation(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_ISSUER", "https://id.celmis.example.com")
    body = _get("/.well-known/oauth-authorization-server")
    assert body["issuer"] == "https://id.celmis.example.com"
    assert body["token_endpoint"] == "https://id.celmis.example.com/oauth/token"


def test_the_token_audience_and_algorithm_the_verifier_checks_are_still_advertised_as_they_were(monkeypatch):
    """Only the addresses changed: the JWT audience is a claim in a token, not a URL."""
    monkeypatch.delenv("MCP_JWT_AUDIENCE", raising=False)
    monkeypatch.delenv("MCP_JWT_ALGORITHM", raising=False)
    body = _get("/.well-known/oauth-protected-resource/mcp/dev")
    assert body["audience"] == "mcp-code-analyzer"
    assert body["resource_signing_alg_values_supported"] == ["HS256"]


def test_reached_directly_without_a_proxy_a_document_names_the_address_it_was_fetched_at(monkeypatch):
    """A developer machine: no forwarded headers, no prefix."""
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    from src.config import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(router)
    r = TestClient(app, base_url="http://localhost:8000").get("/.well-known/oauth-protected-resource/mcp/dev")
    assert r.json()["resource"] == "http://localhost:8000/mcp/dev"


def test_service_documentation_is_a_bare_url_and_the_state_of_the_flow_is_a_separate_member():
    """RFC 8414: `service_documentation` is a URL. Prose appended to it was accepted by
    lenient parsers and refused by strict ones."""
    body = _get("/.well-known/oauth-authorization-server")
    assert body["service_documentation"] == f"{PUBLIC}/backend/docs"
    assert " " not in body["service_documentation"]
    assert "resource-server mode" in body["note"]


def test_the_authorization_server_document_passes_the_mcp_sdks_own_model():
    models = pytest.importorskip("mcp.shared.auth")
    meta = models.OAuthMetadata.model_validate(_get("/.well-known/oauth-authorization-server"))
    assert str(meta.service_documentation) == f"{PUBLIC}/backend/docs"


def test_behind_a_tls_terminator_only_an_https_public_base_url_or_the_issuer_setting_makes_the_documents_https(
        monkeypatch):
    """Caddy's `:80` site replaces the terminator's `X-Forwarded-Proto: https` with `http`.
    With the host declared only in `MCP_ALLOWED_HOSTS` the documents say `http` (the
    registration endpoint included); the two settings the 421 message names fix it."""
    behind = {**PROXY, "X-Forwarded-Proto": "http"}
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "celmis.example.com")
    from src.config import get_settings

    get_settings.cache_clear()
    doc = _client().get("/.well-known/oauth-authorization-server", headers=behind).json()
    assert doc["registration_endpoint"] == "http://celmis.example.com/backend/oauth/register"

    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC)
    get_settings.cache_clear()
    doc = _client().get("/.well-known/oauth-authorization-server", headers=behind).json()
    assert doc["registration_endpoint"] == f"{PUBLIC}/backend/oauth/register"

    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("MCP_OAUTH_ISSUER", f"{PUBLIC}/backend")
    get_settings.cache_clear()
    doc = _client().get("/.well-known/oauth-authorization-server", headers=behind).json()
    assert doc["registration_endpoint"] == f"{PUBLIC}/backend/oauth/register"
