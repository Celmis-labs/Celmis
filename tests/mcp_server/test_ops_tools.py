"""MCP: the operations and review-configuration tools, and their scopes.

One registration (`register_ops_tools`) serves both builders, so the stdio
server and the HTTP mount cannot drift. What is pinned here:

  * every tool the scope map names is registered, and registered under exactly
    that scope — reads under `read:*`, operational writes under `write:repos`,
    configuration writes under `write:config`;
  * the HTTP mount REFUSES the call itself, not only hides the tool from
    tools/list: a read token that names a write tool by hand is denied, a token
    with the scope passes, and a scope-less (legacy full-access) token behaves as
    the listing already treats it;
  * a refusal from the action comes back as `{"ok": False, "error": ...}`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from src.mcp_server import ops_tools
from src.mcp_server.scopes import ScopeError

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "src" / "mcp_server" / "http_app.py").read_text(encoding="utf-8")

READS = {"get_spend", "get_budget", "list_alerts", "list_jobs", "audit_delta",
         "export_sbom", "list_members", "get_usage", "get_review_settings"}
OPS_WRITES = {"ack_alert", "retry_job", "cancel_job", "cancel_dep_audit"}
CONFIG_WRITES = {"set_budget", "update_review_setting", "propose_review_rules",
                 "generate_review_rules"}


def _register(scoped):
    tools: dict[str, object] = {}
    scopes: dict[str, str] = {}

    class Stub:
        def tool(self, name=None, description=None, **_kw):
            assert description and len(description) > 30, f"{name}: describe it"

            def keep(fn):
                tools[name] = fn
                return fn
            return keep

    def recording(scope):
        inner = scoped(scope)

        def deco(fn):
            scopes[fn.__name__.lstrip("_")] = scope
            return inner(fn)
        return deco

    ops_tools.register_ops_tools(Stub(), lambda *a, **k: None, lambda fn: None, recording)
    return tools, scopes


def test_every_mapped_tool_is_registered_under_its_scope():
    tools, scopes = _register(lambda scope: (lambda fn: fn))
    assert set(tools) == set(ops_tools.OPS_TOOL_SCOPES)
    assert scopes == ops_tools.OPS_TOOL_SCOPES


def test_scopes_follow_the_kind_of_tool():
    scopes = ops_tools.OPS_TOOL_SCOPES
    assert {scopes[t] for t in READS} <= {"read:graph", "read:reviews"}
    assert {scopes[t] for t in OPS_WRITES} == {"write:repos"}
    assert {scopes[t] for t in CONFIG_WRITES} == {"write:config"}
    assert set(scopes) == READS | OPS_WRITES | CONFIG_WRITES


def test_the_http_scope_map_carries_them_all():
    from src.mcp_server.http_app import _TOOL_SCOPES

    for name, scope in ops_tools.OPS_TOOL_SCOPES.items():
        assert _TOOL_SCOPES[name] == scope
    # and the older tools keep theirs
    assert _TOOL_SCOPES["add_repo"] == "write:repos"
    assert _TOOL_SCOPES["get_dep_audit"] == "read:graph"


def test_the_http_mount_enforces_the_scope_of_the_older_write_tools_too():
    """The map only hides tools in tools/list. Without a per-call check a read
    token could still call `add_repo` by name."""
    for fn, scope in (("_add_repo", "write:repos"), ("_start_dep_audit", "write:repos"),
                      ("_generate_docs", "write:repos"), ("_set_auto_review", "write:repos"),
                      ("_migrate_consumers", "write:reviews")):
        idx = APP.index(f"def {fn}(")
        assert f'@enforcing("{scope}")' in APP[max(0, idx - 120):idx], fn


# ─── enforcing: the call itself is refused ───────────────────────────


def _token(scopes):
    from mcp.server.auth.provider import AccessToken

    return AccessToken(token="t", client_id="c", scopes=scopes, expires_at=None,
                       resource="r")


def _called(token, scope="write:config"):
    @ops_tools.enforcing(scope)
    async def _tool():
        return "ran"

    with patch("mcp.server.auth.middleware.auth_context.get_access_token",
               return_value=token):
        return asyncio.run(_tool())


def test_a_read_token_cannot_call_a_write_tool_by_name():
    with pytest.raises(ScopeError):
        _called(_token(["read:graph", "read:groups", "read:reviews"]))


def test_a_token_with_the_scope_passes():
    assert _called(_token(["read:graph", "write:config"])) == "ran"
    # write:repos is not write:config
    with pytest.raises(ScopeError):
        _called(_token(["write:repos"]))


def test_the_admin_scope_and_a_legacy_scopeless_token_pass():
    assert _called(_token(["admin"])) == "ran"
    assert _called(_token([])) == "ran"
    assert _called(None) == "ran"


def test_enforcing_keeps_sync_tools_sync():
    @ops_tools.enforcing("write:repos")
    def _sync_tool():
        return "ok"

    assert not asyncio.iscoroutinefunction(_sync_tool)
    with patch("mcp.server.auth.middleware.auth_context.get_access_token",
               return_value=_token(["read:graph"])), pytest.raises(ScopeError):
        _sync_tool()


# ─── the stdio build ─────────────────────────────────────────────────


def test_the_stdio_server_registers_all_of_them():
    from src.mcp_server import build_server

    names = {t.name for t in build_server()._tool_manager._tools.values()}
    assert set(ops_tools.OPS_TOOL_SCOPES) <= names


def test_the_stdio_server_denies_a_read_token_a_write_tool():
    from src.mcp_server import build_server

    tool = build_server()._tool_manager._tools["set_budget"]
    with patch("mcp.server.auth.middleware.auth_context.get_access_token",
               return_value=_token(["read:graph"])), pytest.raises(ScopeError):
        asyncio.run(tool.fn(monthly_usd_cap=10))


# ─── a refusal is an answer ──────────────────────────────────────────


def test_a_refusal_from_the_action_comes_back_as_an_error_object():
    from src.automation.actions import ActionError

    tools: dict[str, object] = {}

    class Stub:
        def tool(self, name=None, **_kw):
            return lambda fn: tools.setdefault(name, fn)

    def actor(label, writing=False):
        raise ActionError("This token is not tied to a workspace.")

    async def in_session(fn):
        raise AssertionError("must not open a session for a refused caller")

    ops_tools.register_ops_tools(Stub(), actor, in_session, lambda s: (lambda f: f))
    out = asyncio.run(tools["set_budget"](monthly_usd_cap=5))
    assert out == {"ok": False, "error": "This token is not tied to a workspace."}
    out = asyncio.run(tools["list_jobs"]())
    assert out["ok"] is False
