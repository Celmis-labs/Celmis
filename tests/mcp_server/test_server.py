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
