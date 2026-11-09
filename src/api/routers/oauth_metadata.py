"""OAuth 2.1 metadata endpoints for MCP client auto-discovery.

MCP 2025-06-18 spec requires servers that need OAuth to publish two
RFC-8414 / RFC-9728 documents so clients can discover:

    * `/.well-known/oauth-authorization-server` — issuer / authz / token URLs
    * `/.well-known/oauth-protected-resource` — audience + which authz servers issue for us

The Celmis MCP server is its own authorization server: a client discovers it
here, registers itself (`/oauth/register`), and the person signs in and approves
in the web app (`/oauth/authorize` hands the browser to it). Tokens minted by the
CLI (`analyzer mcp issue-token`) or in Settings keep working as a bearer header.

Backed by `src.mcp_server.auth.JwtConfig` — same source of truth as the
verifier the MCP server uses.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Request, Response

from src.mcp_server.public_url import base_from_request

logger = logging.getLogger(__name__)

router = APIRouter(tags=["oauth"])

#: What a token may carry: the read scopes and the write scopes the grant model
#: knows (`src.mcp_server.token_store`). The authorization server advertises all
#: of them; whether a person may hand out a write scope is decided at consent.
_FULL_SCOPES = [
    "read:groups", "read:graph", "read:reviews", "read:code",
    "write:reviews", "write:config", "write:repos",
]

#: What the full endpoint advertises as ITS scopes. A client that sends no scope
#: asks for "all of scopes_supported" (MCP authorization, scope selection), so
#: this is the read-only set: the minimum for basic use, never a write scope. A
#: client that wants one names it, and the person is asked.
_RESOURCE_SCOPES = ["read:groups", "read:graph", "read:reviews"]


def _issuer_from(request: Request) -> str:
    """The public base of this API as the caller reached it.

    Derived, not read from the request URL: behind the TLS proxy that URL is
    `http://api:8000` and says nothing of the `/backend` prefix, so a document
    built from it named an address no client can use. See
    :mod:`src.mcp_server.public_url` for where each part comes from, and for
    `MCP_OAUTH_ISSUER`, the explicit override.
    """
    return base_from_request(request)


def _never_cached(response: Response) -> None:
    # The URLs inside depend on the host the caller used; a shared cache keyed
    # on the path must never serve one host's document to another.
    response.headers["Cache-Control"] = "no-store"


@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server(request: Request, response: Response) -> dict[str, Any]:
    """RFC 8414 — the authorization server an MCP client signs in with.

    Public clients register themselves anonymously (`token_endpoint_auth_method`
    `none`), authorize with PKCE S256 and the RFC 8707 `resource`, and keep the
    session alive with rotating refresh tokens. Responses to the authorization
    request carry `iss` (RFC 9207).
    """
    _never_cached(response)
    issuer = _issuer_from(request)
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "authorization_response_iss_parameter_supported": True,
        "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials"],
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post", "none",  # 'none' = public/PKCE-only clients
        ],
        "registration_endpoint": f"{issuer}/oauth/register",
        "scopes_supported": list(_FULL_SCOPES),
        "service_documentation": f"{issuer}/docs",
    }


def _protected_resource(request: Request, resource_path: str, scopes: list[str]) -> dict[str, Any]:
    issuer = _issuer_from(request)
    audience = os.environ.get("MCP_JWT_AUDIENCE", "mcp-code-analyzer")
    return {
        # The URL a client connects to, so that its own check "is this the
        # resource I asked for" (RFC 9728 section 3.3) passes.
        "resource": f"{issuer}{resource_path}",
        "authorization_servers": [issuer],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{issuer}/docs",
        "resource_signing_alg_values_supported": [
            os.environ.get("MCP_JWT_ALGORITHM", "HS256"),
        ],
        "scopes_supported": scopes,
        "audience": audience,
    }


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def oauth_protected_resource(request: Request, response: Response) -> dict[str, Any]:
    """RFC 9728 — MCP-specific metadata pointing clients at the
    authorization server that issues tokens accepted by us.

    Served at the bare well-known path and at the path-suffixed one (`/mcp`),
    which is where the 401 challenge of the full endpoint points.
    """
    _never_cached(response)
    return _protected_resource(request, "/mcp", list(_RESOURCE_SCOPES))


@router.get("/.well-known/oauth-protected-resource/mcp/dev")
async def oauth_protected_resource_dev(request: Request, response: Response) -> dict[str, Any]:
    """RFC 9728 path-suffixed metadata of the compact developer profile.

    Same authorization server and audience as `/mcp`; the resource is
    `/mcp/dev` and the scope that opens it is `read:code`.
    """
    _never_cached(response)
    return _protected_resource(request, "/mcp/dev", ["read:code"])


__all__ = ["router"]
