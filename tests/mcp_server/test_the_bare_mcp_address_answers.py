"""`/mcp/dev` without the trailing slash is the resource the OAuth metadata names and
what a person types into a connector form. It used to fall through to the `/mcp`
mount and come back 404; it must reach the dev endpoint, without a redirect."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

from src.mcp_server.http_app import _alias_bare_paths


def _endpoint(name: str):
    async def app(scope, receive, send):
        await PlainTextResponse(f"{name} {scope['path']}")(scope, receive, send)
    return app


def _client() -> TestClient:
    app = Starlette()
    app.mount("/mcp/dev", _endpoint("dev"))
    app.mount("/mcp", _endpoint("main"))
    _alias_bare_paths(app, ["/mcp/dev"])
    return TestClient(app, follow_redirects=False)


def test_the_bare_dev_address_reaches_the_dev_endpoint():
    c = _client()
    for path in ("/mcp/dev", "/mcp/dev/"):
        r = c.post(path, json={})
        assert r.status_code == 200 and r.text.startswith("dev "), (path, r.status_code, r.text)


def test_other_paths_still_go_where_they_went():
    c = _client()
    assert c.get("/mcp/other").text.startswith("main ")
    assert c.get("/mcp/dev/x").text.startswith("dev ")


def test_the_real_app_answers_the_bare_dev_address_with_the_challenge():
    from src.api.main import app

    c = TestClient(app, follow_redirects=False)
    for path in ("/mcp/dev", "/mcp/dev/"):
        r = c.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                   headers={"Accept": "application/json, text/event-stream"})
        assert r.status_code == 401, (path, r.status_code)
        assert "resource_metadata" in r.headers.get("www-authenticate", "")
    # The bare main address keeps the router's slash redirect.
    assert c.post("/mcp", json={}).status_code == 307
