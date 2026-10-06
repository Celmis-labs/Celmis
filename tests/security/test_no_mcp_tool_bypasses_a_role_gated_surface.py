"""No MCP tool carries a role-gated review feature around its gate.

Memories are for editors and above, productivity metrics (DORA, cycle time,
developer tables) for owners and admins, the Jira task context and the stored
connections for owners and admins. The REST routes hold those gates
(`tests/api/test_every_endpoint_of_a_restricted_surface_names_its_gate.py`); an
MCP tool that returned the same data would reach it through a token whose
holder may be a plain member. Today no tool does. This test keeps it that way:
a tool that names one of these surfaces must be added to ``GATED_TOOLS`` here,
with the gate it calls, and then the matrix of
`tests/security/test_every_review_feature_obeys_repository_access.py` has to
cover it.

The tools are read from every server the product mounts: the legacy `/mcp`, the
stdio server and the compact `/mcp/dev` profile.
"""

from __future__ import annotations

import secrets

#: Words that make a tool "about" a role-gated surface.
GATED_WORDS = (
    "memor", "productivity", "dora", "cycle_time", "developer_table", "task_context",
    "jira", "learning", "requirement", "credential", "connection",
)

#: tool name -> the gate it calls (``may_use_memories`` / ``is_workspace_admin`` /
#: ``readable_repo_slugs``-family). Empty on purpose: nothing is exposed yet.
GATED_TOOLS: dict[str, str] = {}


def _servers(monkeypatch):
    monkeypatch.setenv("MCP_JWT_SECRET", secrets.token_urlsafe(48))
    from src.mcp_server import build_server
    from src.mcp_server.dev_profile import build_dev_mcp
    from src.mcp_server.http_app import _build_mcp

    return {"stdio": build_server(), "http": _build_mcp(), "dev": build_dev_mcp()}


def test_no_tool_is_about_a_role_gated_surface_unless_it_is_listed(monkeypatch):
    stray: list[str] = []
    for label, mcp in _servers(monkeypatch).items():
        for name, tool in mcp._tool_manager._tools.items():
            haystack = f"{name} {tool.description or ''}".lower().replace("-", "_")
            if any(word in haystack for word in GATED_WORDS) and name not in GATED_TOOLS:
                stray.append(f"{label}:{name}")
    assert not stray, (
        "These tools name a role-gated surface (memories, productivity, Jira, "
        f"connections). Gate them and list them in GATED_TOOLS: {stray}")


def test_the_walk_sees_the_tools_it_guards(monkeypatch):
    servers = _servers(monkeypatch)
    names = {n for mcp in servers.values() for n in mcp._tool_manager._tools}
    assert {"list_issues", "get_review_settings", "find", "howto"} <= names
