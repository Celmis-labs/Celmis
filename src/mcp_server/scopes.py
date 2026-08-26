"""Per-tool scope enforcement for the MCP server (Stage 16.2).

Pattern: decorate a tool handler with `@require_scopes("read:graph")`. On
invocation the decorator checks the AccessToken context (via MCP SDK
middleware) — if the token does not have the scope, it raises
PermissionError → the MCP SDK returns a 403/error response.

If auth is disabled (stdio transport or enable_auth=False), the decorator
silently allows the call (no token = no enforcement).

Scope hierarchy (recommended convention):
    read:graph    — find_symbol, get_symbol, find_callers, find_callees
    read:groups   — list_groups, list_repos, cross_repo_edges
    write:groups  — (V2) create/modify groups via MCP
    admin         — wildcard for all operations + system management
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)


T = TypeVar("T", bound=Callable[..., Any])


# Special scope that grants everything — bypasses per-tool checks
ADMIN_SCOPE = "admin"


class ScopeError(PermissionError):
    """Tool invocation denied because of a missing scope."""

    def __init__(self, required: tuple[str, ...], actual: list[str]) -> None:
        self.required = required
        self.actual = actual
        msg = (
            f"Required scope(s): {list(required)}. "
            f"Token has: {actual}. "
            f"Hint: ask the issuer for a token with the required scope."
        )
        super().__init__(msg)


def require_scopes(*scopes: str) -> Callable[[T], T]:
    """Decorator: enforce that the current MCP request has all required scopes.

    Usage:
        @mcp.tool(name="find_symbol", description="...")
        @require_scopes("read:graph")
        def _find_symbol(name: str, repo_slug: str) -> dict:
            ...

    NOTE: Decorator order matters — @require_scopes() must come AFTER
    @mcp.tool() so that the scope check runs during the wrapped function call.

    Behavior:
        - If auth disabled (no AccessToken in context): allow (no enforcement)
        - If the token has the 'admin' scope: always allow
        - If the token is missing a required scope: raise ScopeError
        - If ALL required scopes are present: allow
    """
    if not scopes:
        raise ValueError("require_scopes() called without arguments")

    required_set = set(scopes)

    def decorator(func: T) -> T:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            _check_scopes(required_set, func.__name__)
            return func(*args, **kwargs)

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            _check_scopes(required_set, func.__name__)
            return await func(*args, **kwargs)

        # Auto-detect sync vs async — preserve original return shape
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper  # type: ignore[return-value]
        return wrapper  # type: ignore[return-value]

    return decorator


def _check_scopes(required: set[str], tool_name: str) -> None:
    """Inspect current AccessToken + raise ScopeError if missing scopes."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except ImportError:
        # MCP SDK without auth_context — auth disabled, skip enforcement
        return

    try:
        token = get_access_token()
    except (RuntimeError, LookupError):
        # No active request context (e.g. stdio transport or unit test)
        return

    if token is None:
        # Auth disabled or token not set — allow
        return

    actual = list(token.scopes or [])
    actual_set = set(actual)

    # Admin bypass
    if ADMIN_SCOPE in actual_set:
        logger.debug("scope_check_admin_bypass tool=%s", tool_name)
        return

    missing = required - actual_set
    if missing:
        logger.warning(
            "scope_denied tool=%s required=%s actual=%s missing=%s",
            tool_name, sorted(required), actual, sorted(missing),
        )
        raise ScopeError(tuple(sorted(required)), actual)

    logger.debug("scope_check_pass tool=%s", tool_name)
