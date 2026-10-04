"""Every MCP tool refuses a refused caller itself.

A token whose holder has left the workspace it names is refused by the HTTP
verifier before any tool runs (see :func:`src.mcp_server.auth._workspace_problem`).
That is one check in one place, and a tool reached any other way — the stdio
server, an in-process call, a transport added later, a verifier that is
refactored — would then rely on each tool body happening to route through
``caller_access``. Some do not (project listings, review lookups, the
automation verbs resolve their own caller).

So the refusal is also applied to every registered tool, wholesale, after
registration: a tool added tomorrow is covered without anybody remembering
to. The answer is a tool error carrying the sentence, not an empty result
that would read like "nothing here".
"""

from __future__ import annotations

import functools
import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

_MARK = "_celmis_refusal_guarded"


def refuse_if_refused() -> None:
    """Raise a tool error when the current caller's token was refused."""
    from mcp.server.fastmcp.exceptions import ToolError

    from src.mcp_server import identity

    caller = identity.resolve_caller()
    reason = getattr(caller, "refused", "") or ""
    if reason:
        raise ToolError(reason)


def _guarded(fn):  # noqa: ANN001, ANN202
    if getattr(fn, _MARK, False):
        return fn
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            refuse_if_refused()
            return await fn(*args, **kwargs)
    else:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            refuse_if_refused()
            return fn(*args, **kwargs)
    setattr(wrapper, _MARK, True)
    return wrapper


def guard_every_tool(mcp) -> int:  # noqa: ANN001
    """Wrap every tool registered on ``mcp``. Returns how many it wrapped.

    Raises if the SDK no longer exposes its tool table: a guard that silently
    covers nothing is worse than a server that does not start.
    """
    tools = mcp._tool_manager._tools
    for tool in tools.values():
        tool.fn = _guarded(tool.fn)
    logger.info("mcp_refusal_guard_installed tools=%d", len(tools))
    return len(tools)


def is_guarded(fn) -> bool:  # noqa: ANN001
    return bool(getattr(fn, _MARK, False))


__all__ = ["guard_every_tool", "is_guarded", "refuse_if_refused"]
