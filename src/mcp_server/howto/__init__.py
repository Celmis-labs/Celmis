"""The ``howto`` MCP tool: "do it like service X", without the secrets.

``register_howto(mcp, scoped=...)`` adds the tool to any FastMCP instance, so
the HTTP ``/mcp`` server, the stdio server and the compact ``/mcp/dev`` profile
share one implementation:

    from src.mcp_server.howto import register_howto
    register_howto(mcp, scoped=require_scopes_or_enforcing)

The tool is read-only. It answers with code slices (redacted), the NAMES of the
environment variables / settings the code needs, and where each value comes
from. It never returns a value; its description says so, so agents stop
searching for them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from src.mcp_server.howto.detectors import TOPICS, topic_for
from src.mcp_server.howto.engine import NOT_ACCESSIBLE, IdxInfo, run_howto, set_idx_provider

# ``read:graph`` is issued to every token (standard, PAT and OAuth), so the tool is callable;
# what a caller may see is decided by the access rules, not by this scope.
SCOPE = "read:graph"
TOOL_NAME = "howto"

DESCRIPTION = (
    "How repo X does db/auth/config/http_client/logging/messaging/cache: code slices, env/"
    "setting NAMES and where values come from. Values are never returned: copy the pattern, "
    "ask user/ops."
)


async def howto(
    topic: str,
    repo: str,
    path: str | None = None,
    detail: str = "concise",
    budget_tokens: int = 1500,
    cursor: str | None = None,
) -> str:
    """Implementation shared by every registration. Runs the scan off the loop."""
    return await asyncio.to_thread(
        run_howto, topic, repo, path=path, detail=detail,
        budget_tokens=budget_tokens, cursor=cursor,
    )


def register_howto(
    mcp: Any,
    *,
    scoped: Callable[[str], Callable[[Any], Any]] | None = None,
) -> None:
    """Register ``howto`` on ``mcp``. ``scoped(scope)`` is the builder's
    per-tool scope decorator (``enforcing`` on HTTP, ``require_scopes`` on
    stdio); without one the tool is registered unscoped (the dev profile
    enforces its scope at the mount)."""

    async def _howto(
        topic: str,
        repo: str,
        path: str | None = None,
        detail: str = "concise",
        budget_tokens: int = 1500,
        cursor: str | None = None,
    ) -> str:
        return await howto(topic, repo, path, detail, budget_tokens, cursor)

    fn: Any = _howto
    if scoped is not None:
        fn = scoped(SCOPE)(_howto)
    try:
        from mcp.types import ToolAnnotations

        annotations = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
    except ImportError:  # older SDK: no annotations
        annotations = None
    kwargs: dict[str, Any] = {"name": TOOL_NAME, "description": DESCRIPTION}
    if annotations is not None:
        kwargs["annotations"] = annotations
    mcp.tool(**kwargs, structured_output=False)(fn)


__all__ = [
    "DESCRIPTION", "NOT_ACCESSIBLE", "SCOPE", "TOOL_NAME", "TOPICS", "IdxInfo",
    "howto", "register_howto", "run_howto", "set_idx_provider", "topic_for",
]
