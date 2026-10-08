"""Nothing a tool returns leaves an MCP server unredacted.

The SDK's own ``tools/call`` handler is driven for every tool registered on the
HTTP ``/mcp`` server and on the stdio server. Each tool's body is replaced by a
stub that returns canaries (in free text, in a secret-named field and in a
dsn), so what is tested is the wrapper and the serialisation, for every tool,
including the ones added tomorrow: a tool missing from ARGS fails the suite.
"""

from __future__ import annotations

import asyncio
import json
import secrets

from mcp import types

# Minimal valid arguments for the tools that have required parameters.
ARGS: dict[str, dict] = {
    "find_symbol": {"name": "x", "repo_slug": "r"},
    "get_symbol": {"symbol_id": "x", "repo_slug": "r"},
    "find_callers": {"symbol_id": "x", "repo_slug": "r"},
    "find_callees": {"symbol_id": "x", "repo_slug": "r"},
    "cross_repo_edges": {"group_name": "g"},
    "review_pr": {"repo_slug": "r", "provider": "github", "repo": "a/b", "pr_number": 1},
    "query_graph": {"cypher": "MATCH (n) RETURN n"},
    "add_repo": {"url": "https://example.com/a/b"},
    "list_dep_findings": {"run_id": "x"},
    "update_issue": {"issue_ids": ["x"], "status": "open"},
    "ask_code": {"question": "q"},
    "set_budget": {"monthly_usd_cap": 1},
    "ack_alert": {"alert_id": "x"},
    "retry_job": {"job_id": "x"},
    "cancel_job": {"job_id": "x"},
    "update_review_setting": {"scope": "workspace", "key": "k"},
    "propose_review_rules": {"rules": [{"title": "t"}]},
    "generate_review_rules": {"repo_slug": "r"},
    "howto": {"topic": "db", "repo": "r"},
    "get_project": {"project_id": "p"},
    "search_symbols": {"project_id": "p", "query": "q"},
    "find_consumers": {"project_id": "p", "symbol": "s"},
    "get_api_surface": {"repo_slug": "r"},
    "get_my_access": {"repo_slug": "r"},
    "get_review": {"pr_ref": "a/b#1"},
    "get_review_policy": {"repo_slug": "r"},
    "get_owner": {"repo_slug": "r", "path": "p"},
    "get_architecture": {"repo_slug": "r"},
    "bootstrap_client": {"project_id": "p", "target_repo_slug": "r"},
    "start_integration_walk": {"project_id": "p", "target_repo_slug": "r"},
    "migrate_consumers": {"project_id": "p", "symbol": "s", "old_text": "a", "new_text": "b"},
    "route_incident": {"repo_slug": "r", "stack_trace": "t"},
    # the compact /mcp/dev profile (howto is shared with /mcp, see above)
    "find": {"query": "x"},
    "outline": {"repo": "r", "path": "p"},
    "read_symbol": {"repo": "r", "name": "x"},
    "refs": {"repo": "r", "symbol": "x"},
    "grep": {"pattern": "x"},
    "map": {"repo": "r"},
    "ask": {"question": "q"},
    # the project-token tools (only a project token reaches them)
    "search_project": {"query": "qq"},
    "ask_project": {"question": "q"},
}
# Tools whose parameters are all optional.
NO_REQUIRED = {
    "list_groups", "list_repos", "start_dep_audit", "get_dep_audit", "list_reviews",
    "get_review_run", "index_repo", "list_issues", "search_code", "get_spend", "get_usage",
    "get_budget", "list_alerts", "list_jobs", "audit_delta", "export_sbom", "list_members",
    "get_review_settings", "cancel_dep_audit", "list_projects", "list_accessible_repos",
    "list_deprecations", "generate_docs", "set_auto_review", "list_workspace_repos", "repos",
}


def _servers(monkeypatch):
    monkeypatch.setenv("MCP_JWT_SECRET", secrets.token_urlsafe(48))
    from src.mcp_server import build_server
    from src.mcp_server.dev_profile import build_dev_mcp
    from src.mcp_server.http_app import _build_mcp

    return {"stdio": build_server(), "http": _build_mcp(), "dev": build_dev_mcp()}


def _stub(canaries: dict[str, str], is_async: bool):
    payload = {
        "note": f"the password is password = '{canaries['text']}' ok",
        "dsn": f"postgresql+asyncpg://app:{canaries['dsn']}@db/app",
        "password": canaries["field"],
        "nested": [{"client_secret": canaries["nested"]}],
        "indexed_sha": "3f2a91c8d7e6b5a4938271605f4e3d2c1b0a9f8e",
    }
    if is_async:
        async def stub(**_kw):
            return dict(payload)
    else:
        def stub(**_kw):
            return dict(payload)
    return stub


def _call(mcp, name: str, args: dict) -> types.CallToolResult:
    handler = mcp._mcp_server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call", params=types.CallToolRequestParams(name=name, arguments=args)
    )
    return asyncio.run(handler(req)).root


def test_every_tool_on_every_server_is_in_the_sample_args_table(monkeypatch):
    for label, mcp in _servers(monkeypatch).items():
        unknown = [n for n in mcp._tool_manager._tools if n not in ARGS and n not in NO_REQUIRED]
        assert not unknown, f"{label}: add sample args for {unknown}"


def test_no_canary_leaves_any_tool_on_any_server(monkeypatch):
    for label, mcp in _servers(monkeypatch).items():
        for name, tool in mcp._tool_manager._tools.items():
            canaries = {k: secrets.token_urlsafe(9) for k in ("text", "dsn", "field", "nested")}
            tool.fn = _stub(canaries, tool.is_async)
            result = _call(mcp, name, ARGS.get(name, {}))
            assert not result.isError, f"{label}:{name}: sample args rejected: {result.content[0].text[:200]}"
            blob = json.dumps(result.model_dump(mode="json"), default=str)
            for k, c in canaries.items():
                assert c not in blob, f"{label}:{name} leaked the {k} canary"
            assert "3f2a91c8d7e6b5a4938271605f4e3d2c1b0a9f8e" in blob, (
                f"{label}:{name} lost the indexed sha"
            )


def test_the_check_has_teeth_a_server_without_the_guard_does_leak():
    from mcp.server.fastmcp import FastMCP

    from src.mcp_server.output_guard import install_output_guard

    canary = secrets.token_urlsafe(9)
    mcp = FastMCP("bare")

    @mcp.tool(name="leaky")
    def leaky() -> dict:
        return {"password": canary}

    assert canary in json.dumps(_call(mcp, "leaky", {}).model_dump(mode="json"))
    install_output_guard(mcp)
    assert canary not in json.dumps(_call(mcp, "leaky", {}).model_dump(mode="json"))
