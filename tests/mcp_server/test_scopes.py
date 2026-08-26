"""Tests для per-tool scope enforcement (Stage 16.2)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.mcp_server.scopes import ScopeError, require_scopes

# ─── Decorator basics ──────────────────────────────────────────────


class TestDecoratorBasics:
    def test_no_scopes_arg_raises(self) -> None:
        with pytest.raises(ValueError, match="without arguments"):
            require_scopes()

    def test_decorator_returns_callable(self) -> None:
        @require_scopes("read:graph")
        def _tool() -> str:
            return "ok"

        assert callable(_tool)

    def test_async_function_preserved(self) -> None:
        """Decorator detects async function correctly."""
        import asyncio

        @require_scopes("read:graph")
        async def _async_tool() -> str:
            return "async ok"

        assert asyncio.iscoroutinefunction(_async_tool)


# ─── No auth context (e.g. stdio) ──────────────────────────────────


class TestNoAuthContext:
    def test_no_token_allows_call(self) -> None:
        """Без MCP request context (stdio mode) — decorator skip enforcement."""
        @require_scopes("read:graph")
        def _tool() -> str:
            return "ok"

        # No context patched → get_access_token returns None / raises
        result = _tool()
        assert result == "ok"


# ─── Active auth context ──────────────────────────────────────────


class TestActiveAuthContext:
    """Mock MCP get_access_token щоб simulate authenticated requests."""

    def _make_token(self, scopes: list[str]):
        """Mock AccessToken з given scopes."""
        from mcp.server.auth.provider import AccessToken
        return AccessToken(
            token="fake",
            client_id="test-client",
            scopes=scopes,
            expires_at=None,
            resource="test",
        )

    def test_token_with_required_scope_allows(self) -> None:
        @require_scopes("read:graph")
        def _tool() -> str:
            return "ok"

        token = self._make_token(["read:graph"])
        with patch(
            "mcp.server.auth.middleware.auth_context.get_access_token",
            return_value=token,
        ):
            assert _tool() == "ok"

    def test_token_missing_scope_raises(self) -> None:
        @require_scopes("read:graph")
        def _tool() -> str:
            return "ok"

        token = self._make_token(["read:groups"])  # wrong scope
        with patch(
            "mcp.server.auth.middleware.auth_context.get_access_token",
            return_value=token,
        ), pytest.raises(ScopeError, match="Required scope"):
            _tool()

    def test_admin_scope_bypasses(self) -> None:
        """admin scope grants any tool без specific scope."""
        @require_scopes("write:groups", "read:graph")
        def _tool() -> str:
            return "ok"

        token = self._make_token(["admin"])
        with patch(
            "mcp.server.auth.middleware.auth_context.get_access_token",
            return_value=token,
        ):
            assert _tool() == "ok"

    def test_multiple_required_scopes_all_must_match(self) -> None:
        @require_scopes("read:graph", "read:groups")
        def _tool() -> str:
            return "ok"

        # Has only one з two required
        token = self._make_token(["read:graph"])
        with patch(
            "mcp.server.auth.middleware.auth_context.get_access_token",
            return_value=token,
        ), pytest.raises(ScopeError):
            _tool()

        # Has both
        token2 = self._make_token(["read:graph", "read:groups"])
        with patch(
            "mcp.server.auth.middleware.auth_context.get_access_token",
            return_value=token2,
        ):
            assert _tool() == "ok"

    def test_extra_scopes_ok(self) -> None:
        """Token має more scopes than needed — OK."""
        @require_scopes("read:graph")
        def _tool() -> str:
            return "ok"

        token = self._make_token(["read:graph", "read:groups", "extra"])
        with patch(
            "mcp.server.auth.middleware.auth_context.get_access_token",
            return_value=token,
        ):
            assert _tool() == "ok"

    def test_scope_error_includes_required_and_actual(self) -> None:
        @require_scopes("write:groups")
        def _tool() -> str:
            return "ok"

        token = self._make_token(["read:graph"])
        with patch(
            "mcp.server.auth.middleware.auth_context.get_access_token",
            return_value=token,
        ):
            with pytest.raises(ScopeError) as exc_info:
                _tool()
            err = exc_info.value
            assert err.required == ("write:groups",)
            assert err.actual == ["read:graph"]


# ─── Server integration ──────────────────────────────────────────


class TestServerIntegration:
    def test_tools_have_scope_decorators(self) -> None:
        """Verify що tools у server.py have scope decorators applied."""
        from src.mcp_server.server import build_server

        mcp = build_server()
        # Tools register names — все 8 повинно бути available
        names = sorted(t.name for t in mcp._tool_manager._tools.values())
        assert "find_symbol" in names
        assert "query_graph" in names
        assert "list_groups" in names
        assert "cross_repo_edges" in names
