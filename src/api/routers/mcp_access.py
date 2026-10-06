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

A token is READ-ONLY by default. An MCP client is software on somebody's
laptop that a language model drives, so a plain browser click mints a token
that cannot write a review policy or register a repo. Write scopes are issued
only when the request NAMES them (`scopes`), and only the ones the caller's
workspace role could use anyway:

    write:config   owner / admin   budget, review settings and rules
    write:repos    owner / admin   register repos, audits, docs, alerts, jobs
    write:reviews  editor and up   cross-repo migration PRs

A scope is a ceiling, never a grant: every tool still applies the caller's own
role, so asking for one the role cannot use is refused here rather than issued
as a token that fails later. A scope outside this list is refused too.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.deps import current_workspace_id, get_current_user
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

#: What a browser-issued token may do. Read-only on purpose — see the module
#: docstring. `write:*` stays with the CLI, where an operator is the one asking.
_TOKEN_SCOPES = ["read:graph", "read:groups", "read:reviews"]

#: The write scopes a caller may ASK for, and who may have each one. Roles come
#: from `src.users.roles`, so a role added there is not forgotten here.
def _write_scope_roles() -> dict[str, frozenset[str]]:
    from src.users.roles import PROMPT_EDITOR_ROLES, WORKSPACE_ADMIN_ROLES

    return {
        "write:config": WORKSPACE_ADMIN_ROLES,
        "write:repos": WORKSPACE_ADMIN_ROLES,
        "write:reviews": PROMPT_EDITOR_ROLES,
    }


#: Names of the requestable write scopes, for the page's selector.
WRITE_SCOPES = ("write:repos", "write:config", "write:reviews")

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
    #: Write scopes to add to the read-only default. Omitted or empty = a
    #: read-only token. Each one must be allowed for the caller's workspace role.
    scopes: list[str] | None = Field(default=None, max_length=8)


def _granted_scopes(user: User, workspace_id: str,
                    requested: list[str] | None) -> list[str]:
    """The read scopes, plus each requested write scope the caller's role in
    this workspace allows. Raises 422 for a name that is not a requestable
    scope and 403 for one the role cannot use."""
    granted = list(_TOKEN_SCOPES)
    wanted = list(dict.fromkeys(s.strip() for s in (requested or []) if s and s.strip()))
    if not wanted:
        return granted
    roles = _write_scope_roles()
    unknown = [s for s in wanted if s not in roles and s not in _TOKEN_SCOPES]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(f"Unknown scope: {', '.join(unknown)}. Requestable write "
                    f"scopes: {', '.join(WRITE_SCOPES)}."))
    from src.api.deps import workspace_role

    role = None if user.is_admin else workspace_role(user.id, workspace_id)
    for scope in wanted:
        if scope in _TOKEN_SCOPES:
            continue
        if not user.is_admin and role not in roles[scope]:
            raise HTTPException(
                status_code=403,
                detail=(f"{scope} needs one of these roles on this workspace: "
                        f"{', '.join(sorted(roles[scope]))} (yours: "
                        f"{role or 'none'})."))
        granted.append(scope)
    return granted


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
    from src.mcp_server import token_store
    from src.mcp_server.auth import JwtConfigError

    # Default off: tokens are issued by the superadmin, per person and per repo
    # list (POST /api/admin/mcp-tokens). A browser click minting a token that
    # reaches everything its holder can see is exactly what that replaces.
    if not token_store.self_service_enabled():
        raise HTTPException(
            status_code=403,
            detail=("MCP tokens are issued by the administrator. Ask your "
                    "superadmin to issue one for the repositories you need."))

    label = (payload.label if payload else None) or "celmis-mcp-client"
    granted = _granted_scopes(user, workspace_id,
                              payload.scopes if payload else None)
    wants_write = any(sc.startswith("write:") for sc in granted)
    try:
        # A self-issued token carries "*": the ceiling is the holder's OWN
        # access (the resolver intersects it), not a list somebody chose.
        token, view = token_store.mint(
            kind="self", workspace_id=workspace_id, user_id=user.id,
            issued_by=user.id, label=label, patterns=["*"],
            allow_write=wants_write, profile="full" if wants_write else "dev",
            expires_in_days=token_store.max_days(),
            # exactly the write scopes the role allowed, not all of them
            write_scopes=[sc for sc in granted if sc.startswith("write:")],
        )
    except JwtConfigError as exc:
        # A misconfigured install should say what is missing, not 500.
        raise HTTPException(
            status_code=503,
            detail=("MCP is not configured on this server: set MCP_JWT_SECRET "
                    "(or CELMIS_JWT_SECRET) and restart. " + str(exc)[:200]),
        ) from exc
    except token_store.TokenError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    settings = get_settings()
    base = str(getattr(settings, "public_base_url", "") or "").rstrip("/")
    seconds = max(1, int((view.expires_at - datetime.now(UTC)).total_seconds()))
    logger.info("mcp_token_issued id=%s kind=self user=%s ws=%s write=%s", view.id,
                user.id, workspace_id, wants_write)
    return McpTokenOut(
        token=token,
        expires_in=seconds,
        scopes=list(view.scopes),
        # Trailing slash on purpose: without it Starlette answers 307, and a
        # redirected POST is not something the MCP streamable-HTTP client is
        # guaranteed to follow.
        url=(f"{base}/mcp/" if base else "/mcp/") if wants_write
        else (f"{base}/mcp/dev/" if base else "/mcp/dev/"),
        workspace_id=workspace_id,
    )


__all__ = ["WRITE_SCOPES", "router"]
