"""`/mcp/dev` is a second server next to `/mcp`, behind its own scope."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.mcp_server.auth import JwtConfig, issue_token

SECRET = "c4f2a70b9e5d18634ac7f0e2b9d51a86"
INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "t", "version": "1"}},
}
HEADERS = {"Accept": "application/json, text/event-stream",
           "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def _grantless_tokens(monkeypatch):
    """These tests are about the mount and the scope claim. They sign tokens
    with no grant row, which the server refuses by default (the access tests
    cover that); accept them here."""
    from src.config import get_settings

    monkeypatch.setenv("CELMIS_MCP_LEGACY_TOKENS", "accept")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MCP_JWT_SECRET", SECRET)
    from src.mcp_server.http_app import mount_mcp

    app = FastAPI()
    assert mount_mcp(app) is True
    with TestClient(app, base_url="http://localhost") as c:
        yield c


def _bearer(*scopes: str) -> dict:
    token = issue_token(JwtConfig(secret=SECRET), subject="u-1", scopes=list(scopes))
    return {**HEADERS, "Authorization": f"Bearer {token}"}


def test_the_dev_endpoint_refuses_a_call_without_a_token(client):
    assert client.post("/mcp/dev/", json=INIT, headers=HEADERS).status_code == 401


def test_the_dev_endpoint_refuses_a_token_that_lacks_the_read_code_scope(client):
    r = client.post("/mcp/dev/", json=INIT, headers=_bearer("read:graph"))
    assert r.status_code == 403


def test_the_dev_endpoint_answers_a_token_with_the_read_code_scope(client):
    r = client.post("/mcp/dev/", json=INIT, headers=_bearer("read:code"))
    assert r.status_code == 200, r.text
    assert "celmis-dev" in r.text


def test_the_full_server_still_answers_at_its_own_path_after_the_dev_mount(client):
    r = client.post("/mcp/", json=INIT, headers=_bearer("read:graph"))
    assert r.status_code == 200, r.text
    assert "celmis-dev" not in r.text


def test_the_dev_server_is_mounted_before_the_full_one(client):
    paths = [getattr(r, "path", "") for r in client.app.routes]
    assert paths.index("/mcp/dev") < paths.index("/mcp")


def test_a_failing_dev_profile_does_not_take_the_full_server_down(monkeypatch):
    monkeypatch.setenv("MCP_JWT_SECRET", SECRET)
    from src.mcp_server import dev_profile
    from src.mcp_server.http_app import mount_mcp

    def boom():
        raise RuntimeError("dev build failed")

    monkeypatch.setattr(dev_profile, "build_dev_mcp", boom)
    app = FastAPI()
    assert mount_mcp(app) is True
    with TestClient(app, base_url="http://localhost") as c:
        assert c.post("/mcp/", json=INIT, headers=_bearer("read:graph")).status_code == 200
        assert c.post("/mcp/dev/", json=INIT, headers=_bearer("read:code")).status_code == 404
