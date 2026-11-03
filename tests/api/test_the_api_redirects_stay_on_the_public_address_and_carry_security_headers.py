"""An audit measured, against the bundled proxy:

    GET /backend/mcp  ->  307 Location: http://<host>/mcp/

The scheme dropped to http and the `/backend` prefix was gone, so a client that
followed it sent its Authorization header over plain http to a path that does
not exist. The API never saw its public address: the proxy takes the prefix off
and reports its own plain connection.

Pinned here: a same-host redirect is rewritten onto the public address, other
hosts are left alone, and every API response carries the security headers (with
HSTS only over https, and the CSP switchable by CELMIS_CSP).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

PROXIED = {
    "Host": "celmis.example.com",
    "X-Forwarded-Proto": "http",      # what the bundled :80 proxy reports
    "X-Forwarded-Host": "celmis.example.com",
    "X-Forwarded-Prefix": "/backend",
}


@pytest.fixture(scope="module")
def client() -> TestClient:
    from src.api.main import app
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False,
                      base_url="http://celmis.example.com")


@pytest.fixture(autouse=True)
def _public_base(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://celmis.example.com")
    monkeypatch.delenv("CELMIS_CSP", raising=False)


@pytest.mark.parametrize("path, expected", [
    ("/mcp", "https://celmis.example.com/backend/mcp/"),
    ("/api/projects/", "https://celmis.example.com/backend/api/projects"),
])
def test_the_slash_redirect_keeps_https_and_the_prefix(client, path, expected):
    r = client.get(path, headers=PROXIED)
    assert r.status_code in (301, 307, 308), (r.status_code, r.text[:200])
    assert r.headers["location"] == expected


def test_the_query_survives_the_rewrite(client):
    r = client.get("/api/projects/?x=1", headers=PROXIED)
    assert r.headers["location"].endswith("/backend/api/projects?x=1")


def test_a_redirect_to_another_host_is_left_alone():
    import asyncio

    from src.api.edge import PublicRedirectMiddleware

    seen: list[dict] = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 302,
                    "headers": [(b"location", b"http://127.0.0.1:9000/cb?code=1")]})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        seen.append(message)

    scope = {"type": "http", "scheme": "http", "path": "/x", "server": ("api", 8000),
             "headers": [(b"host", b"celmis.example.com"),
                         (b"x-forwarded-prefix", b"/backend")]}
    asyncio.run(PublicRedirectMiddleware(app)(scope, None, send))
    assert dict(seen[0]["headers"])[b"location"] == b"http://127.0.0.1:9000/cb?code=1"


def test_without_a_proxy_nothing_is_invented(client, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    r = client.get("/api/projects/")
    assert r.headers["location"].startswith("http://celmis.example.com/api/projects")
    assert "/backend" not in r.headers["location"]


def test_every_response_carries_the_security_headers(client):
    r = client.get("/healthz")
    h = r.headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert h["x-frame-options"] == "DENY"
    for denied in ("camera=()", "microphone=()", "geolocation=()"):
        assert denied in h["permissions-policy"]
    assert "frame-ancestors 'none'" in h["content-security-policy"]


def test_an_error_response_has_them_too(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401
    assert r.headers["x-content-type-options"] == "nosniff"


def test_hsts_only_over_https(client, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert "strict-transport-security" not in client.get("/healthz").headers
    r = client.get("/healthz", headers={"X-Forwarded-Proto": "https"})
    assert r.headers["strict-transport-security"].startswith("max-age=")


def test_the_csp_switch(client, monkeypatch):
    monkeypatch.setenv("CELMIS_CSP", "report-only")
    h = client.get("/healthz").headers
    assert "content-security-policy" not in h
    assert "content-security-policy-report-only" in h
    monkeypatch.setenv("CELMIS_CSP", "off")
    h = client.get("/healthz").headers
    assert "content-security-policy" not in h
    assert "content-security-policy-report-only" not in h
    assert h["x-content-type-options"] == "nosniff"   # only the CSP is switchable


def test_the_capabilities_document_does_not_fingerprint_an_anonymous_caller(client):
    body = client.get("/api/capabilities").json()
    for key in ("api_version", "edition", "edition_source"):
        assert key not in body
    assert body["product"] == "celmis" and "features" in body


def test_a_signed_in_caller_still_gets_the_version_and_edition(client):
    from src.api.jwt_auth import issue_token

    token, _ = issue_token(user_id="u", email="u@example.com")
    body = client.get("/api/capabilities",
                      headers={"Authorization": f"Bearer {token}"}).json()
    assert body["api_version"] and body["edition"]
