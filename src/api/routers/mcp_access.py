"""Issuing an MCP token to the person who is already signed in.

Until now the only way to get one was `analyzer mcp issue-token` on the server,
which needs MCP_JWT_SECRET and a shell. That is fine for an operator and
useless for the person the feature is for: connecting Celmis to Claude Code is
a thing a developer does on their own laptop, and "ask an administrator to SSH
in" is not an instruction, it is a description of a gap.

The browser OAuth flow advertised in .well-known/oauth-authorization-server has
not shipped — that endpoint says so itself. This is the bridge until it does:
the user is already authenticated to the API, so the API can hand them a token
for the resource server it also runs.

Scopes are READ-ONLY and not negotiable from the request. An MCP client is
software on somebody's laptop that a language model drives; a token minted from
a browser click should not be able to write a review policy or register a repo,
and the way to guarantee that is to never let the caller name the scope.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.deps import current_workspace_id, get_current_user
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

#: What a browser-issued token may do. Read-only on purpose — see the module
#: docstring. `write:*` stays with the CLI, where an operator is the one asking.
_TOKEN_SCOPES = ["read:graph", "read:groups", "read:reviews"]

#: Long enough to be worth pasting into a config file, short enough that a
#: leaked one expires. Thirty days: an MCP client config is edited rarely, and
#: a token that expires in an hour would make the feature useless.
_TOKEN_TTL_SECONDS = 30 * 24 * 3600


class McpTokenOut(BaseModel):
    token: str
    expires_in: int
    scopes: list[str]
    #: The URL to put in the client config. Built from the request so an
    #: install behind a proxy or on a custom domain gets its own address rather
    #: than localhost.
    url: str
    workspace_id: str


class McpTokenIn(BaseModel):
    #: Free-text label so a user can tell two clients apart in the audit log.
    #: Not a scope and not a permission — it only travels as the client_id.
    label: str = Field(default="celmis-mcp-client", max_length=64)


@router.post("/token", response_model=McpTokenOut)
def issue_mcp_token(
    payload: McpTokenIn | None = None,
    user: User = Depends(get_current_user),
    workspace_id: str = Depends(current_workspace_id),
) -> McpTokenOut:
    """A bearer token for this user's MCP clients.

    Issued against the caller's own identity, so everything the MCP server
    answers is already filtered by what this user can see — the scopes bound
    here are a ceiling, not a grant.
    """
    from src.config import get_settings
    from src.mcp_server.auth import JwtConfig, JwtConfigError, issue_token

    try:
        config = JwtConfig.from_env()
    except JwtConfigError as exc:
        # A misconfigured install should say what is missing, not 500. This is
        # the one prerequisite an operator has to set, and naming it is the
        # difference between a five-minute fix and a support thread.
        raise HTTPException(
            status_code=503,
            detail=("MCP is not configured on this server: set MCP_JWT_SECRET "
                    "(or CELMIS_JWT_SECRET) and restart. " + str(exc)[:200]),
        ) from exc

    label = (payload.label if payload else None) or "celmis-mcp-client"
    token = issue_token(
        config,
        subject=user.id,
        scopes=list(_TOKEN_SCOPES),
        client_id=label,
        expires_in=_TOKEN_TTL_SECONDS,
        # The workspace travels in the token so the MCP server answers for the
        # one the user was looking at, not whichever it would default to.
        extra_claims={"workspace_id": workspace_id},
    )
    settings = get_settings()
    base = str(getattr(settings, "public_base_url", "") or "").rstrip("/")
    logger.info("mcp_token_issued user=%s ws=%s label=%s ttl=%ds",
                user.email, workspace_id, label, _TOKEN_TTL_SECONDS)
    return McpTokenOut(
        token=token,
        expires_in=_TOKEN_TTL_SECONDS,
        scopes=list(_TOKEN_SCOPES),
        # Trailing slash on purpose: without it Starlette answers 307, and a
        # redirected POST is not something the MCP streamable-HTTP client is
        # guaranteed to follow.
        url=f"{base}/mcp/" if base else "/mcp/",
        workspace_id=workspace_id,
    )


__all__ = ["router"]
