"""MCP server smoke tests — verify FastMCP integration."""

from __future__ import annotations

from src.mcp_server import build_server


def test_server_builds_with_all_tools() -> None:
    """The exact registered set — reads, review, and the automation verbs.

    Kept exact rather than a subset check: the point is that a tool cannot
    appear or disappear from the surface without somebody editing this line.
    The four automation tools were added to `server.py` after this list was
    written, and the stale assertion is what let that go unnoticed.
    """
    mcp = build_server()
    tool_names = sorted(t.name for t in mcp._tool_manager._tools.values())
    assert tool_names == sorted([
        # graph / groups
        "list_groups",
        "list_repos",
        "find_symbol",
        "get_symbol",
        "find_callers",
        "find_callees",
        "cross_repo_edges",
        "query_graph",
        "review_pr",  # Phase 17c
        # automation surface — an external caller registers a repo, audits it
        # and reads the findings back without a person clicking through pages
        "add_repo",
        "start_dep_audit",
        "get_dep_audit",
        "list_dep_findings",
        # reviews, issues, indexing and code questions
        # (src/automation/actions_reviews.py); `review_pr` above is the older
        # synchronous one, the queued twin is HTTP-only
        "list_reviews",
        "get_review_run",
        "index_repo",
        "list_issues",
        "update_issue",
        "ask_code",
        "search_code",
        # operations + review configuration (src/mcp_server/ops_tools.py)
        "get_spend",
        "get_usage",
        "get_budget",
        "set_budget",
        "list_alerts",
        "ack_alert",
        "list_jobs",
        "retry_job",
        "cancel_job",
        "cancel_dep_audit",
        "audit_delta",
        "export_sbom",
        "list_members",
        "get_review_settings",
        "update_review_setting",
        "propose_review_rules",
        "generate_review_rules",
    ])


def test_server_has_name_and_instructions() -> None:
    mcp = build_server()
    # FastMCP stores name + instructions
    assert "code-analyzer" in str(mcp.name)


def test_tool_descriptions_non_empty() -> None:
    """Кожен tool має description (важливо для LLM щоб обирати правильний tool)."""
    mcp = build_server()
    for tool in mcp._tool_manager._tools.values():
        assert tool.description, f"tool {tool.name} has no description"
        assert len(tool.description) > 30, (
            f"tool {tool.name} description too short — Claude won't know коли use"
        )
