"""MCP server — exposes code intelligence to Claude Code / Cursor / other MCP clients.

Stage 8 (May 2026, MCP SDK 1.23.x):
    - FastMCP-based tool registration
    - Stdio transport (default — for IDE integration)
    - Streamable HTTP transport (for remote scenarios)
    - Read-only by default (Cypher whitelist)

User invocation: `analyzer mcp serve [--transport stdio|http] [--port 8080]`
"""

from src.mcp_server.server import build_server, run_server

__all__ = ["build_server", "run_server"]
