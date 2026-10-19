"""Every request `celmis-pack` makes says "User-Agent: celmis-pack/<version>", and
a refusal from something in front of the server is not called a refused token.

The contract (bin/celmis-pack):

  * a CDN bot check (Cloudflare Browser Integrity Check) refuses urllib's own
    `Python-urllib/3.x` with 403 "error code: 1010" and lets `celmis-pack/3.0.1`
    through, so the tool names itself on EVERY request: the publish routes
    (status, publish, archive) and the MCP endpoint `verify` asks, session close
    included;
  * the version in that name is the package's: bumping `src.__version__` without
    the tool would send last release's name for ever;
  * a 403 whose body is not Celmis JSON did not come from Celmis. It is reported
    as "something in front of the server refused the request", not as "the
    server did not accept your token", which would send people to re-issue a
    token that was never the problem. A Celmis 403 (JSON) keeps its wording.
"""

from __future__ import annotations

import json

import pytest

from src import __version__
from tests.plugin.test_the_celmis_pack_tool_publishes_checks_and_fetches_a_pack import (
    CONSUMER,
    GOOD,
    cli,
    pack_folder,
    run,
    status_json,
    stub,
    zip_bytes,
)

AGENT = f"celmis-pack/{__version__}"
JSON = {"Content-Type": "application/json"}


def agent_of(headers: dict) -> str | None:
    return {k.lower(): v for k, v in headers.items()}.get("user-agent")


def celmis(method, path, _headers, body):
    """A server that answers every route the tool uses, MCP included."""
    if path.startswith("/mcp/dev/"):
        if method == "DELETE":
            return 204, {}, b""
        message = json.loads(body)
        if message.get("method") == "notifications/initialized":
            return 202, {}, b""
        if message.get("method") == "initialize":
            result = {"protocolVersion": "2025-03-26"}
        else:
            result = {"content": [{"type": "text", "text": "ledger/billing-api — How the billing API is called"}]}
        reply = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
        return 200, {**JSON, "Mcp-Session-Id": "sess-1"}, reply
    if path.endswith("/publish/archive"):
        return 200, {"Content-Type": "application/zip"}, zip_bytes([("README.md", "# Ledger\n")])
    if method == "POST":
        done = {"version": {"number": 2, "file_count": 3, "sha": "0" * 64}, "skills": ["billing-api"]}
        return 200, JSON, json.dumps(done).encode()
    return 200, JSON, status_json({})


def test_the_name_the_tool_sends_carries_the_version_the_package_has():
    assert __version__ == cli.VERSION
    assert f"celmis-pack/{__version__}" == cli.USER_AGENT


@pytest.mark.parametrize("command,routes", [
    ("push", {("GET", "/api/codebase/publish"), ("POST", "/api/codebase/publish")}),
    ("status", {("GET", "/api/codebase/publish")}),
    ("pull", {("GET", "/api/codebase/publish"), ("GET", "/api/codebase/publish/archive")}),
    ("verify", {("GET", "/api/codebase/publish"), ("POST", "/mcp/dev/"), ("DELETE", "/mcp/dev/")}),
])
def test_every_request_a_command_makes_carries_the_celmis_pack_user_agent(tmp_path, command, routes):
    folder = pack_folder(tmp_path)
    args = ["status"] if command == "status" else [command, tmp_path / "out" if command == "pull" else folder]
    with stub(celmis) as s:
        run(*args, url=s.url, token=GOOD, consumer=CONSUMER)
    assert {(method, path) for method, path, _h, _b in s.requests} >= routes, s.requests
    for method, path, headers, _body in s.requests:
        assert agent_of(headers) == AGENT, (method, path, agent_of(headers))


@pytest.mark.parametrize("command,route", [("status", "/api/codebase"), ("push", "/api/codebase"),
                                           ("pull", "/api/codebase"), ("verify", "/mcp/dev/")])
@pytest.mark.parametrize("body,ctype", [(b"error code: 1010", "text/plain"),
                                        (b"<html><title>Attention Required! | Cloudflare</title></html>", "text/html"),
                                        (b"", "text/plain")])
def test_a_403_from_something_in_front_of_the_server_is_not_called_a_refused_token(tmp_path, command, route,
                                                                                    body, ctype):
    def answer(method, path, headers, payload):
        if path.startswith(route):
            return 403, {"Content-Type": ctype}, body
        return celmis(method, path, headers, payload)

    folder = pack_folder(tmp_path)
    args = ["status"] if command == "status" else [command, tmp_path / "out" if command == "pull" else folder]
    with stub(answer) as s:
        code, out, err = run(*args, url=s.url, token=GOOD, consumer=CONSUMER)
    assert code == 1, (out, err)
    assert "in front of the server" in err and "HTTP 403" in err and AGENT in err
    assert "did not accept" not in err
    assert GOOD not in out + err and CONSUMER not in out + err


def test_a_403_that_celmis_itself_sent_keeps_its_own_wording(tmp_path):
    def answer(method, path, headers, body):
        if path.startswith("/mcp/dev/"):
            return 403, JSON, json.dumps({"error": "access_denied", "error_description": "left the workspace"}).encode()
        return celmis(method, path, headers, body)

    with stub(answer) as s:
        code, _out, err = run("verify", pack_folder(tmp_path), url=s.url, token=GOOD, consumer=CONSUMER)
    assert code == 1 and "did not accept your Celmis token" in err and "in front of the server" not in err

    with stub(lambda *a: (403, JSON, b'{"detail":"the pack is closed"}')) as s:
        code, _out, err = run("status", url=s.url, token=GOOD)
    assert code == 1 and "HTTP 403: the pack is closed" in err and "in front of the server" not in err
