"""``howto`` answers only for repos the caller may read as code, and says nothing
about the ones it may not."""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

from mcp import types
from mcp.server.fastmcp import FastMCP

from src.access import RepoAccessDecision
from src.access.resolver import _RuleView
from src.mcp_server.howto import engine, register_howto, run_howto
from src.mcp_server.output_guard import install_output_guard

NOT_FOUND = "idx: none\nhowto: repo not found or not accessible"


def test_a_repo_the_caller_cannot_read_answers_exactly_like_one_that_does_not_exist(leaky, monkeypatch):
    monkeypatch.setattr(engine, "accessible_code_repos", lambda: {})
    denied = run_howto("db", "acme/shop")
    missing = run_howto("db", "acme/nothing-here")
    assert denied == missing == NOT_FOUND


def test_the_requested_name_is_not_echoed_back(leaky, monkeypatch):
    monkeypatch.setattr(engine, "accessible_code_repos", lambda: {})
    assert "zzz-secret-project" not in run_howto("db", "zzz-secret-project")


def test_the_repo_is_matched_loosely_but_only_among_accessible_ones(leaky):
    for name in ("acme-shop", "acme/shop", "github_acme-shop", "Acme_Shop"):
        assert run_howto("db", name).splitlines()[1].startswith("howto db acme-shop"), name


def test_an_ambiguous_name_lists_only_accessible_candidates(leaky, monkeypatch):
    dec = RepoAccessDecision.full("x")
    monkeypatch.setattr(engine, "accessible_code_repos", lambda: {"gh_acme-shop": dec, "gl_acme-shop": dec})
    out = run_howto("db", "acme/shop")
    assert "ambiguous" in out and "gh_acme-shop" in out and "gl_acme-shop" in out


def test_only_repos_readable_as_code_are_candidates(monkeypatch):
    from src.mcp_server import identity
    from src.mcp_server import tools as legacy

    decisions = {
        "code-repo": RepoAccessDecision.full("code-repo"),
        "meta-repo": RepoAccessDecision(repo_slug="meta-repo", visibility="metadata", open_default=False),
        "none-repo": RepoAccessDecision.denied("none-repo"),
    }
    monkeypatch.setattr(legacy, "list_repo_slugs", lambda *a, **k: list(decisions))
    monkeypatch.setattr(identity, "caller_access", lambda slugs: (
        SimpleNamespace(user_id="u", workspace_id="w", token_id=None, kind="stdio", allow_write=False),
        {s: decisions[s] for s in slugs}))
    assert set(engine.accessible_code_repos()) == {"code-repo"}


def test_paths_the_callers_rules_hide_are_never_scanned_or_traced(leaky, monkeypatch):
    rule = _RuleView("code", (), ("src/db/**", "src/config.py"), ())
    dec = RepoAccessDecision(repo_slug=leaky.slug, visibility="code", rules=(rule,), open_default=False)
    monkeypatch.setattr(engine, "accessible_code_repos", lambda: {leaky.slug: dec})
    out = run_howto("db", "acme/shop", detail="detailed", budget_tokens=6000)
    assert "src/db/" not in out and "src/config.py" not in out
    assert leaky.canaries["dsn"] not in out
    assert "web/db.ts" in out


def test_a_repo_with_no_clone_on_disk_reads_as_nothing_found_not_as_an_error(leaky, monkeypatch):
    dec = RepoAccessDecision.full("ghost")
    monkeypatch.setattr(engine, "accessible_code_repos", lambda: {"ghost": dec})
    out = run_howto("db", "ghost")
    assert out.splitlines()[0].startswith("idx: ghost") and "no db pattern found" in out


# ─── through the MCP server ──────────────────────────────────────────


def _call(mcp, args):
    handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name="howto", arguments=args))
    return asyncio.run(handler(req)).root


def test_the_registered_tool_answers_through_the_guard_without_a_canary(leaky):
    mcp = FastMCP("t")
    register_howto(mcp)
    install_output_guard(mcp)
    result = _call(mcp, {"topic": "db", "repo": "acme/shop", "detail": "detailed", "budget_tokens": 6000})
    assert not result.isError
    text = result.content[0].text
    assert text.startswith("idx: acme-shop")
    blob = json.dumps(result.model_dump(mode="json"))
    for c in leaky.all_canaries():
        assert c not in blob


def test_the_tool_is_read_only_and_says_plainly_that_values_are_never_returned():
    mcp = FastMCP("t")
    register_howto(mcp)
    tool = mcp._tool_manager._tools["howto"]
    assert "never returned" in tool.description
    assert tool.annotations is not None and tool.annotations.readOnlyHint is True
    assert len(tool.description) <= 200


def test_the_tool_is_registered_on_the_http_server_under_the_graph_scope_and_on_stdio(monkeypatch):
    import secrets

    monkeypatch.setenv("MCP_JWT_SECRET", secrets.token_urlsafe(48))
    from src.mcp_server import build_server
    from src.mcp_server.http_app import _TOOL_SCOPES, _build_mcp

    assert "howto" in _build_mcp()._tool_manager._tools
    assert "howto" in build_server()._tool_manager._tools
    assert _TOOL_SCOPES["howto"] == "read:graph"


def test_a_token_without_the_graph_scope_cannot_call_the_tool(monkeypatch, leaky):
    from mcp.server.auth.provider import AccessToken

    from src.mcp_server.scopes import ScopeError

    mcp = FastMCP("t")
    from src.mcp_server.ops_tools import enforcing

    register_howto(mcp, scoped=enforcing)
    token = AccessToken(token="t", client_id="c", scopes=["read:reviews"])
    import mcp.server.auth.middleware.auth_context as ac

    monkeypatch.setattr(ac, "get_access_token", lambda: token)
    import pytest

    with pytest.raises(ScopeError):
        asyncio.run(mcp._tool_manager._tools["howto"].fn(topic="db", repo="acme/shop"))
    token2 = AccessToken(token="t", client_id="c", scopes=["read:graph"])
    monkeypatch.setattr(ac, "get_access_token", lambda: token2)
    assert asyncio.run(mcp._tool_manager._tools["howto"].fn(topic="db", repo="acme/shop")).startswith("idx: ")


def test_the_scan_runs_off_the_event_loop_thread(leaky, monkeypatch):
    import threading

    seen = {}
    real = engine.run_howto

    def spy(*a, **k):
        seen["thread"] = threading.current_thread()
        return real(*a, **k)

    import src.mcp_server.howto as pkg

    monkeypatch.setattr(pkg, "run_howto", spy)
    asyncio.run(pkg.howto("db", "acme/shop"))
    assert seen["thread"] is not threading.main_thread()
    assert re.match(r"idx: ", run_howto("db", "acme/shop"))
