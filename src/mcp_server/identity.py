"""Resolve the *authenticated caller* inside an MCP tool body (Stage 22).

FastMCP's ``AccessToken`` only carries ``client_id`` / ``scopes``; our JWTs
always set ``client_id="code-analyzer-cli"`` so the real subject (``sub`` =
user_id) is lost from the AccessToken. We recover it by re-decoding the raw
JWT (already signature-verified by the token verifier) and then resolve the
caller's admin flag + active workspace so the same research-access rules that
gate Q&A also gate MCP tools.

Design: fail **open** when no auth context exists, i.e. an unauthenticated
caller is treated as a trusted local principal.

That is only safe because of an invariant enforced elsewhere: over HTTP the
MCP server refuses to start without a working token verifier (see
``_build_mcp`` — it raises unless ``MCP_ALLOW_UNAUTHENTICATED`` is explicitly
set). FastMCP then rejects tokenless requests before any tool body runs, so
this branch is unreachable over HTTP. It remains reachable only for the stdio
transport, where the subprocess boundary *is* the trust boundary, and for
direct in-process calls such as tests.

If that invariant is ever relaxed, this fallback becomes a full read bypass of
every research-access rule — change both together.

An invariant enforced in another module is exactly the kind that survives one
refactor and not two, so the fallback is also gated on the deployment mode
(:mod:`src.deployment`): under multi_tenant an MCP caller with no bearer
identity is nobody — not an admin — and is handed no repositories at all.
Under single_tenant (the default) the behaviour above is unchanged, because a
one-tenant box legitimately runs the stdio transport with no token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class McpCaller:
    user_id: str
    is_admin: bool
    workspace_id: str
    scopes: tuple[str, ...]
    authenticated: bool  # False → no bearer identity (dev/stdio) → fall open
    #: False when `workspace_id` is the "default" fallback rather than a
    #: membership we actually resolved. Reads are allowed to fall back; a
    #: WRITE must refuse, or a client-credentials token with no resolvable
    #: owner would register repositories into the wrong tenant.
    workspace_resolved: bool = True


_WS_RANK = {"viewer": 1, "member": 2, "admin": 3, "owner": 4}


def _decode_subject(raw_token: str) -> tuple[str | None, list[str]]:
    """Return (sub, scopes) from an already-verified JWT. Signature is not
    re-checked here — the token verifier already accepted it — but we still
    pass the secret so a malformed token simply yields (None, [])."""
    try:
        import jwt

        from src.mcp_server.auth import JwtConfig

        cfg = JwtConfig.from_env()
        payload = jwt.decode(
            raw_token,
            cfg.secret,
            algorithms=[cfg.algorithm],
            audience=cfg.audience,
            issuer=cfg.issuer,
            options={"verify_signature": True},
        )
    except Exception:  # noqa: BLE001 — includes previous-secret window tokens
        # Best-effort fallback: decode without verification to read `sub`.
        try:
            import jwt

            payload = jwt.decode(raw_token, options={"verify_signature": False})
        except Exception:  # noqa: BLE001
            return None, []
    sub = payload.get("sub")
    scope_str = payload.get("scope", "")
    if isinstance(scope_str, str):
        scopes = [s for s in scope_str.split(" ") if s]
    elif isinstance(scope_str, list):
        scopes = [str(s) for s in scope_str]
    else:
        scopes = []
    return (str(sub) if sub else None), scopes


def _resolve_workspace(session, user_id: str) -> str:
    """Best-rank workspace membership → its id, else 'default' (mirrors
    ``current_workspace_id`` without the header/cookie hints MCP lacks)."""
    from sqlalchemy import select

    from src.db.models import WorkspaceMember

    rows = session.execute(
        select(WorkspaceMember).where(WorkspaceMember.user_id == user_id)
    ).scalars().all()
    if not rows:
        return "default"
    rows.sort(key=lambda m: -_WS_RANK.get(m.role, 0))
    return rows[0].workspace_id


def _no_identity(reason: str) -> McpCaller:
    """The caller we could not name.

    single_tenant → the historical trusted-local principal (global admin, the
    'default' workspace). multi_tenant → a principal with no admin flag and no
    workspace, which :func:`caller_access` then resolves to no repositories.
    """
    from src.deployment import fall_open_allowed

    if fall_open_allowed("mcp.identity.no_auth_context", detail=reason):
        return McpCaller("default", True, "default", (), authenticated=False)
    return McpCaller(
        "anonymous", False, "", (), authenticated=False, workspace_resolved=False,
    )


def resolve_caller() -> McpCaller:
    """Resolve the current MCP caller. Never raises — returns an
    ``authenticated=False`` caller when no bearer identity is present, open or
    closed according to the deployment mode (see :func:`_no_identity`)."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except ImportError:
        return _no_identity("auth_context_import_failed")

    try:
        token = get_access_token()
    except Exception:  # noqa: BLE001
        token = None
    if token is None or not getattr(token, "token", None):
        return _no_identity("no_bearer_token")

    sub, scopes = _decode_subject(token.token)
    if not sub:
        # Authenticated but unidentifiable (e.g. client_credentials with only
        # client_id) → treat as a non-admin principal with no team grants, so
        # research access defaults to whatever rules allow (restricted).
        return McpCaller(
            f"client:{token.client_id}", False, "default",
            tuple(scopes or token.scopes or ()), authenticated=True,
            workspace_resolved=False,
        )

    # `sub` may be prefixed (user:<id> / client:<id>); our issuer uses the
    # bare user_id, but strip a known prefix defensively.
    user_id = sub.split(":", 1)[1] if sub.startswith(("user:", "client:")) else sub

    is_admin = False
    workspace_id = "default"
    resolved = False
    try:
        from sqlalchemy.orm import Session

        from src.access.resolver import _sync_engine
        from src.users import get_user_store

        store = get_user_store()
        user = store.get_by_id(user_id)
        if user is None and sub.startswith("client:"):
            # A client_credentials token names a client, not a person. The
            # workspace comes from whoever registered the client — otherwise
            # every machine token would land in "default" and an automated
            # write would go to the wrong tenant.
            user = _client_owner(store, user_id)
            if user is not None:
                user_id = user.id
        is_admin = bool(user and user.is_admin)
        if user is not None:
            with Session(_sync_engine()) as s:
                found = _resolve_workspace(s, user.id)
            resolved = found != "default" or _has_default_membership(user.id)
            workspace_id = found
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_identity_resolve_failed sub=%s err=%s", sub, exc)

    return McpCaller(
        user_id, is_admin, workspace_id,
        tuple(scopes), authenticated=True, workspace_resolved=resolved,
    )


def _client_owner(store, client_id: str):
    """The user who registered an OAuth client, by the email it was saved under."""
    from sqlalchemy.orm import Session

    from src.access.resolver import _sync_engine
    from src.db.models import OAuthClient

    with Session(_sync_engine()) as s:
        row = s.get(OAuthClient, client_id)
    owner = (row.created_by if row else "") or ""
    if not owner:
        return None
    return store.get_by_email(owner) if hasattr(store, "get_by_email") else None


def _has_default_membership(user_id: str) -> bool:
    """True when the user really is a member of the workspace literally named
    'default' — as opposed to having landed there by fallback."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from src.access.resolver import _sync_engine
    from src.db.models import WorkspaceMember

    with Session(_sync_engine()) as s:
        row = s.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == user_id,
                WorkspaceMember.workspace_id == "default",
            )
        ).scalars().first()
    return row is not None


def caller_access(repos: list[str]):
    """Resolve research-access decisions for ``repos`` for the current MCP
    caller. Returns ``(caller, {repo: RepoAccessDecision})``.

    Unauthenticated (dev/stdio) callers get full access to every repo under
    single_tenant, and none at all under multi_tenant."""
    from src.access import RepoAccessDecision, resolve_access
    from src.deployment import fall_open_allowed

    caller = resolve_caller()
    if not caller.authenticated:
        if fall_open_allowed("mcp.identity.unauthenticated_access",
                             detail=f"repos={len(repos)}"):
            return caller, {r: RepoAccessDecision.full(r) for r in repos}
        return caller, {r: RepoAccessDecision.denied(r) for r in repos}
    if caller.is_admin:
        return caller, {r: RepoAccessDecision.full(r) for r in repos}
    access = resolve_access(
        user_id=caller.user_id,
        is_admin=caller.is_admin,
        workspace_id=caller.workspace_id,
        repos=repos,
    )
    return caller, access
