"""A client that finds `/mcp/dev` can also find out who issues its token."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routers.oauth_metadata import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, base_url="https://celmis.example.com")


def test_the_dev_profile_has_its_own_protected_resource_document():
    body = _client().get("/.well-known/oauth-protected-resource/mcp/dev").json()
    assert body["resource"].endswith("/mcp/dev")
    assert body["scopes_supported"] == ["read:code"]
    assert body["authorization_servers"]


def test_the_full_servers_document_is_unchanged_and_does_not_advertise_the_dev_scope():
    body = _client().get("/.well-known/oauth-protected-resource").json()
    assert body["resource"].endswith("/mcp")
    assert "read:code" not in body["scopes_supported"]
