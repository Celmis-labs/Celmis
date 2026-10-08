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

Which workspace a token answers for
------------------------------------
A token is minted in a workspace — the one the person was looking at when they
pressed "Issue token" (``/api/mcp/token``) or consented to an OAuth client
(``/oauth/authorize/consent``) — and carries it as the ``workspace_id`` claim.
That claim is what the caller resolves to, not the person's highest-ranked
membership: the latter is usually their personal workspace, so a token issued
inside a neighbouring team's workspace used to read the wrong tenant.

The claim is a statement about the past. Every call re-checks it against the
membership table: somebody removed from the workspace since the token was
minted is refused (see :func:`token_workspace_problem`), with a sentence that
says why, rather than silently re-homed into whichever workspace they still
belong to. A global admin may hold a token for any existing workspace, as
``current_workspace_id`` lets them pick one through the header.

Tokens minted before the claim existed (and client_credentials tokens, which
have no person to ask) keep the old resolution — best-ranked membership — and
the first such token per subject is logged as ``mcp_token_without_workspace``
so an operator can see who still needs to re-issue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.users.roles import WORKSPACE_ROLE_RANK

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
    #: Set when the token names a workspace the caller may no longer act in:
    #: the sentence to show. Every repository then resolves to denied.
    refused: str = ""
    #: The ``mcp_tokens`` row behind the call, when there is one.
    token_id: str | None = None
    #: pat | cli | self | oauth | legacy | stdio — which kind of grant the call
    #: runs under (``legacy``: a token that predates per-person grants).
    kind: str = "legacy"
    #: The token's repo list (exact slugs / globs). ``None`` = the token does
    #: not narrow or widen anything: the person's own access decides.
    repo_patterns: tuple[str, ...] | None = None
    #: Write tools are refused unless the grant explicitly allows them.
    allow_write: bool = False


_WS_RANK = WORKSPACE_ROLE_RANK

#: The JWT claim carrying the workspace a token was minted in. ``ws`` is read
#: as an alias so a hand-made token in the short spelling is not silently
#: treated as a legacy one.
WORKSPACE_CLAIMS = ("workspace_id", "ws")


def token_workspace(payload: dict | None) -> str | None:
    """The workspace a token was minted in, or None for a legacy token."""
    for key in WORKSPACE_CLAIMS:
        value = (payload or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _decode_subject(raw_token: str) -> tuple[str | None, list[str]]:
    """Return (sub, scopes) from an already-verified JWT."""
    sub, scopes, _payload = _decode_token(raw_token)
    return sub, scopes


def _decode_token(raw_token: str) -> tuple[str | None, list[str], dict]:
    """Return (sub, scopes, payload) from an already-verified JWT. Signature
    is not re-checked here — the token verifier already accepted it — but we
    still pass the secret so a malformed token simply yields (None, [], {})."""
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
            return None, [], {}
    if not isinstance(payload, dict):
        return None, [], {}
    sub = payload.get("sub")
    scope_str = payload.get("scope", "")
    if isinstance(scope_str, str):
        scopes = [s for s in scope_str.split(" ") if s]
    elif isinstance(scope_str, list):
        scopes = [str(s) for s in scope_str]
    else:
        scopes = []
    return (str(sub) if sub else None), scopes, payload


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


def refusal_message(workspace_id: str) -> str:
    """What a caller refused for its token's workspace is told."""
    return (
        f"This MCP token was issued for workspace '{workspace_id}', and you are "
        "no longer a member of it. Ask an owner or admin of that workspace to "
        "add you back, or issue a new token (Settings > MCP) while the "
        "workspace you want to use is selected."
    )


_UNVERIFIABLE = ("Could not confirm your membership of the workspace this MCP "
                 "token was issued for. Try again shortly.")


def token_workspace_problem(user_id: str, is_admin: bool,
                            workspace_id: str) -> str | None:
    """None when ``user_id`` may act in ``workspace_id`` now; else why not.

    * a member of the workspace — yes;
    * a global admin — yes, for a workspace that exists (the same freedom
      ``current_workspace_id`` gives them through the X-Workspace header);
    * anything else, including a database we cannot ask — no. The claim is
      the access boundary here, so an unverifiable one fails closed. That
      covers ``default`` too: it is accepted for a member of the workspace
      literally named so, not for somebody who landed there by fallback.
    """
    from sqlalchemy.orm import Session

    from src.access.resolver import _sync_engine
    from src.db.models import Workspace, WorkspaceMember

    try:
        with Session(_sync_engine()) as s:
            if s.get(WorkspaceMember, (workspace_id, user_id)) is not None:
                return None
            if is_admin and s.get(Workspace, workspace_id) is not None:
                return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_token_workspace_check_failed user=%s ws=%s err=%s",
                       user_id, workspace_id, exc)
        return _UNVERIFIABLE
    return refusal_message(workspace_id)


#: Subjects already reported as holding a token without a workspace claim —
#: one log line per subject per process, not one per call.
_LEGACY_LOGGED: set[str] = set()
_LEGACY_LOG_CAP = 1024


def _log_legacy_token(sub: str) -> None:
    if sub in _LEGACY_LOGGED or len(_LEGACY_LOGGED) >= _LEGACY_LOG_CAP:
        return
    _LEGACY_LOGGED.add(sub)
    logger.warning(
        "mcp_token_without_workspace sub=%s — the token predates the "
        "workspace claim, so it answers for the best-ranked membership. "
        "Re-issue it to bind it to one workspace.", sub,
    )


def _no_identity(reason: str) -> McpCaller:
    """The caller we could not name.

    single_tenant → the historical trusted-local principal (global admin, the
    'default' workspace). multi_tenant → a principal with no admin flag and no
    workspace, which :func:`caller_access` then resolves to no repositories.
    """
    from src.deployment import fall_open_allowed

    if fall_open_allowed("mcp.identity.no_auth_context", detail=reason):
        return McpCaller("default", True, "default", (), authenticated=False,
                         kind="stdio", allow_write=True)
    return McpCaller(
        "anonymous", False, "", (), authenticated=False, workspace_resolved=False,
    )


LEGACY_REFUSAL = ("This MCP token predates per-person grants — ask the "
                  "superadmin to issue a new one (Admin > MCP tokens).")
GRANT_UNKNOWN = ("This MCP token is not recognised (it was deleted or never "
                 "issued here). Ask the administrator for a new one.")
GRANT_MISMATCH = "This MCP token does not belong to the person using it."
ACCOUNT_INACTIVE = ("The account this MCP token was issued for is no longer "
                    "active. Ask the administrator.")


def _person_is_inactive(user_id: str) -> bool:
    """True only when the account EXISTS and is switched off. A lookup that
    cannot be made refuses (fail closed): an unreadable user store must not
    keep a deactivated person's token alive."""
    try:
        from src.users import get_user_store

        user = get_user_store().get_by_id(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_user_lookup_failed err=%s", type(exc).__name__)
        return True
    return user is not None and not getattr(user, "is_active", True)


@dataclass
class _Grant:
    """What the ``mcp_tokens`` row behind a token says about the call."""

    refused: str = ""
    token_id: str | None = None
    kind: str = "legacy"
    patterns: tuple[str, ...] | None = None
    allow_write: bool = False
    workspace_id: str | None = None


def grant_claim(payload: dict | None) -> str | None:
    """The id of the grant row a token names: the OAuth ``grant`` claim, else ``jti``."""
    for key in ("grant", "jti"):
        value = (payload or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def resolve_grant(payload: dict, user_id: str) -> _Grant:
    """Judge a token by its row. Never raises: a row that cannot be read is a
    refusal, not an admission."""
    gid = grant_claim(payload)
    if gid is None:
        from src.mcp_server.token_store import legacy_tokens_accepted

        if legacy_tokens_accepted():
            # Temporary: read-only, default-deny, the person's own access.
            return _Grant(kind="legacy", allow_write=False)
        return _Grant(refused=LEGACY_REFUSAL)
    try:
        from src.mcp_server import token_store

        view = token_store.lookup(gid)
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("mcp_grant_lookup_failed grant=%s err=%s", gid, type(exc).__name__)
        return _Grant(refused=_UNVERIFIABLE)
    if view is None:
        return _Grant(refused=GRANT_UNKNOWN)
    problem = view.problem()
    if problem:
        return _Grant(refused=problem)
    if view.user_id != user_id:
        return _Grant(refused=GRANT_MISMATCH)
    if _person_is_inactive(view.user_id):
        # A grant is issued FOR a person. When the account is deactivated or
        # erased the grant dies with it, at once and not at the end of its TTL.
        return _Grant(refused=ACCOUNT_INACTIVE)
    claimed = token_workspace(payload)
    if claimed is not None and claimed != view.workspace_id:
        return _Grant(refused=GRANT_MISMATCH)
    return _Grant(
        token_id=view.id,
        kind="oauth" if view.kind == "oauth_grant" else view.kind,
        patterns=view.patterns if view.authoritative else None,
        allow_write=view.allow_write,
        workspace_id=view.workspace_id,
    )


def _project_caller(client_id: str) -> McpCaller:
    """The caller behind a project-scoped token (see
    :mod:`src.mcp_server.project_tokens`): not a person, no admin flag, read
    only. The row is re-read, so a revoked token is a refused caller."""
    from src.mcp_server import project_tokens as pt

    token_id = client_id[len(pt.CLIENT_PREFIX):]
    try:
        view = pt.lookup_id(token_id)
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning("mcp_project_caller_failed err=%s", type(exc).__name__)
        view = None
    reason = pt.UNKNOWN if view is None else view.problem()
    return McpCaller(
        f"project-token:{token_id}", False, view.workspace_id if view else "",
        tuple(view.scopes) if view else (), authenticated=True,
        workspace_resolved=view is not None, refused=reason, kind="project",
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

    if str(getattr(token, "client_id", "") or "").startswith("mcp-project:"):
        return _project_caller(str(token.client_id))

    sub, scopes, payload = _decode_token(token.token)
    claimed_ws = token_workspace(payload)
    if not sub:
        # Authenticated but unidentifiable (e.g. client_credentials with only
        # client_id) → treat as a non-admin principal with no team grants, so
        # research access defaults to whatever rules allow (restricted).
        nameless = resolve_grant(payload, "")
        return McpCaller(
            f"client:{token.client_id}", False, "default",
            tuple(scopes or token.scopes or ()), authenticated=True,
            workspace_resolved=False, refused=nameless.refused or GRANT_UNKNOWN,
        )

    # `sub` may be prefixed (user:<id> / client:<id>); our issuer uses the
    # bare user_id, but strip a known prefix defensively.
    user_id = sub.split(":", 1)[1] if sub.startswith(("user:", "client:")) else sub

    grant = resolve_grant(payload, user_id)
    if grant.refused:
        # Revoked, expired, unknown, legacy-and-refused: nothing is readable
        # and nothing may be written, whatever the signature says.
        return McpCaller(
            user_id, False, "", tuple(scopes), authenticated=True,
            workspace_resolved=False, refused=grant.refused,
            token_id=grant.token_id, kind=grant.kind,
        )
    if grant.workspace_id is not None:
        claimed_ws = grant.workspace_id

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
        if user is not None and not getattr(user, "is_active", True):
            return McpCaller(
                user_id, False, "", tuple(scopes), authenticated=True,
                workspace_resolved=False, refused=ACCOUNT_INACTIVE,
                token_id=grant.token_id, kind=grant.kind,
            )
        is_admin = bool(user and user.is_admin)
        if user is not None and claimed_ws is not None:
            # The token names its workspace: answer for that one or refuse.
            problem = token_workspace_problem(user.id, is_admin, claimed_ws)
            if problem:
                logger.warning("mcp_token_workspace_refused user=%s ws=%s",
                               user.id, claimed_ws)
                return McpCaller(
                    user_id, False, "", tuple(scopes), authenticated=True,
                    workspace_resolved=False, refused=problem,
                )
            workspace_id = claimed_ws
            resolved = True
        elif user is not None:
            _log_legacy_token(sub)
            with Session(_sync_engine()) as s:
                found = _resolve_workspace(s, user.id)
            resolved = found != "default" or _has_default_membership(user.id)
            workspace_id = found
        elif claimed_ws is not None:
            # A claim with nobody behind it: the account was deleted.
            return McpCaller(
                user_id, False, "", tuple(scopes), authenticated=True,
                workspace_resolved=False, refused=refusal_message(claimed_ws),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("mcp_identity_resolve_failed sub=%s err=%s", sub, exc)
        if claimed_ws is not None:
            # Fail closed: the claim is the boundary and it went unchecked.
            return McpCaller(
                user_id, False, "", tuple(scopes), authenticated=True,
                workspace_resolved=False, refused=_UNVERIFIABLE,
            )

    return McpCaller(
        user_id, is_admin, workspace_id,
        tuple(scopes), authenticated=True, workspace_resolved=resolved,
        token_id=grant.token_id, kind=grant.kind,
        repo_patterns=grant.patterns, allow_write=grant.allow_write,
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

    The decision is :func:`src.access.effective.effective_access`: only the
    repositories registered in the caller's workspace exist, a token's repo
    list is authoritative, a repository nobody granted anything on is closed.

    Unauthenticated (dev/stdio) callers get full access to every repo under
    single_tenant, and none at all under multi_tenant. Under multi_tenant a
    repo not registered to the caller's workspace (alone) is denied for
    everyone, admins included — see :mod:`src.mcp_server.tenancy`."""
    from src.access import RepoAccessDecision
    from src.access.effective import Principal, effective_access
    from src.deployment import fall_open_allowed

    caller = resolve_caller()
    if caller.refused:
        # The token's workspace is no longer the caller's: nothing is readable,
        # in any mode (a refused caller is never an admin either).
        return caller, {r: RepoAccessDecision.denied(r) for r in repos}
    if not caller.authenticated:
        if fall_open_allowed("mcp.identity.unauthenticated_access",
                             detail=f"repos={len(repos)}"):
            return caller, {r: RepoAccessDecision.full(r) for r in repos}
        return caller, {r: RepoAccessDecision.denied(r) for r in repos}

    # Tenant binding (multi_tenant only). Graphs, vaults and clones live flat on
    # disk, keyed by slug alone, so a slug registered to more than one
    # workspace (or to none) is nobody's. Applies to admins too: an operator
    # reaches another tenant by switching workspace, not through a token
    # resolved to their own.
    from src.mcp_server import tenancy

    decided: dict = {}
    own = list(repos)
    if tenancy.enforced():
        if not tenancy.caller_may_bind(caller):
            return caller, {r: RepoAccessDecision.denied(r) for r in repos}
        foreign = [r for r in repos
                   if not tenancy.workspace_owns_slug(caller.workspace_id, r)]
        own = [r for r in repos if r not in foreign]
        decided = {r: RepoAccessDecision.denied(r) for r in foreign}
    if own:
        decided.update(effective_access(
            Principal(caller.user_id, caller.is_admin, caller.repo_patterns),
            caller.workspace_id, own,
        ))
    if len(repos) <= 20:
        from src.mcp_server import callctx

        callctx.note_repos(*[r for r in repos
                             if (decided.get(r) is not None and decided[r].researchable)])
    return caller, decided


READ_ONLY_TOKEN = ("This MCP token is read-only. Ask the administrator for a token "
                   "that allows writing if you need to change something.")


def actor_for(label: str, *, writing: bool = False):  # noqa: ANN201 — automation Actor
    """The automation :class:`Actor` for the current MCP caller.

    The verbs behind it (``src.automation.actions*``) resolve repositories
    through ``Actor.token_filter``, so a token's repo list narrows them as it
    narrows the graph tools. A write refuses unless the grant allows it.
    """
    from src.automation.actions import ActionError, Actor

    caller = resolve_caller()
    if caller.refused:
        # The token names a workspace its holder has left. Neither a read
        # nor a write may land anywhere else instead.
        raise ActionError(caller.refused)
    if writing and not caller.allow_write:
        from src.mcp_server import callctx

        callctx.set_status("denied")
        raise ActionError(READ_ONLY_TOKEN)
    if writing and caller.authenticated and not caller.workspace_resolved:
        # A client_credentials token whose owner cannot be resolved lands
        # on the "default" workspace by fallback. Reading there is
        # harmless; writing would register a repository into a tenant
        # nobody chose. Refuse and say what to fix.
        raise ActionError(
            "This token is not tied to a workspace. Register the OAuth "
            "client from an account that belongs to the workspace you "
            "want to write to, or use a user token.",
        )
    return Actor(
        user_id=caller.user_id,
        email=getattr(caller, "email", "") or caller.user_id,
        workspace_id=caller.workspace_id,
        label=label,
        token_filter=caller.repo_patterns,
    )
