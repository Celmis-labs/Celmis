"""OAuth 2.1 metadata endpoints for MCP client auto-discovery.

MCP 2025-06-18 spec requires servers that need OAuth to publish two
RFC-8414 / RFC-9728 documents so clients can discover:

    * `/.well-known/oauth-authorization-server` — issuer / authz / token URLs
    * `/.well-known/oauth-protected-resource` — audience + which authz servers issue for us

The Celmis MCP server runs in **resource-server mode**: it does not
issue tokens itself; users mint a JWT via the CLI (`analyzer mcp
issue-token`) or through the future browser-based OAuth flow. This
router exposes just enough metadata that clients like Claude Code /
Cursor / Zed can validate our tokens without hand-configuration.

Backed by `src.mcp_server.auth.JwtConfig` — same source of truth as the
verifier the MCP server uses.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

router = APIRouter(tags=["oauth"])


def _issuer_from(request: Request) -> str:
    # Prefer explicit env override so deployments behind proxies work.
    explicit = os.environ.get("MCP_OAUTH_ISSUER", "").strip()
    if explicit:
        return explicit.rstrip("/")
    # Fall back to request scheme/host.
    return f"{request.url.scheme}://{request.url.netloc}"


@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server(request: Request) -> dict[str, Any]:
    """RFC 8414 — advertises we behave as an authorization server just
    enough for MCP clients to know how to obtain a token. Real OAuth
    flow is out of band (CLI or admin-issued); we surface the endpoints
    they would call.
    """
    issuer = _issuer_from(request)
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post", "none",  # 'none' = public/PKCE-only clients
        ],
        "registration_endpoint": f"{issuer}/oauth/register",
        "scopes_supported": [
            "read:groups", "read:graph", "read:reviews",
            "write:reviews", "write:policies", "write:repos",
        ],
        # Explicit `note` so integrators know the current state.
        "service_documentation": (
            f"{issuer}/docs — Celmis runs in resource-server mode; "
            "the authorization endpoint is a placeholder until the "
            "browser-based OAuth flow ships."
        ),
    }


@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource(request: Request) -> dict[str, Any]:
    """RFC 9728 — MCP-specific metadata pointing clients at the
    authorization server that issues tokens accepted by us."""
    issuer = _issuer_from(request)
    audience = os.environ.get("MCP_JWT_AUDIENCE", "mcp-code-analyzer")
    return {
        "resource": f"{issuer}/mcp",
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{issuer}/docs",
        "resource_signing_alg_values_supported": [
            os.environ.get("MCP_JWT_ALGORITHM", "HS256"),
        ],
        "scopes_supported": [
            "read:groups", "read:graph", "read:reviews",
            "write:reviews", "write:policies", "write:repos",
        ],
        "audience": audience,
    }


__all__ = ["router"]
