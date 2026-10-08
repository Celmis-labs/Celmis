"""OAuth 2.1 / JWT Bearer token authentication for the MCP HTTP transport.

Stage 15.3 (May 2026, MCP SDK 1.23.x).

Architecture:
    MCP server runs in **resource server mode** — it does NOT issue tokens,
    it only validates incoming Bearer tokens. This is a cleaner separation:

        ┌──────────────────────┐       ┌────────────────┐
        │ Code Analyzer App    │       │ MCP Server     │
        │ (issues JWTs)        │  →    │ (verifies)     │
        │                      │       │                │
        │ user login + OAuth   │       │ token_verifier │
        └──────────────────────┘       └────────────────┘

    Stdio transport — auth is not needed (subprocess trust boundary).
    HTTP transport (streamable-http / sse) — auth is mandatory for
    multi-tenant scenarios.

Token format: JWT (RFC 7519) signed with an HMAC-SHA256 (HS256) symmetric
secret. Production — RS256 asymmetric with public key rotation; HS256 — for
simple deploys.

Required claims:
    iss   — issuer (our app domain)
    sub   — subject (user_id in `default` or `user_<id>`)
    aud   — audience (usually "mcp-code-analyzer")
    exp   — expiration timestamp
    iat   — issued at
    scope — space-separated scopes (e.g. "read:graph read:groups")
    client_id — for traceability

Workspace claim:
    workspace_id — the workspace the token was minted in. The MCP server
                   answers for that workspace only, and refuses the token
                   once its holder is no longer a member there. Tokens without
                   it are legacy: see src/mcp_server/identity.py.

Local dev: `analyzer mcp issue-token --user default --duration 3600` →
prints a JWT that can be used as a Bearer header.
"""

from __future__ import annotations

import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

logger = logging.getLogger(__name__)


# Default issuer + audience (used unless overridden via env)
DEFAULT_ISSUER = "code-analyzer"
DEFAULT_AUDIENCE = "mcp-code-analyzer"
DEFAULT_ALGORITHM = "HS256"
DEFAULT_EXPIRES_IN = 3600  # 1 hour


class JwtConfigError(RuntimeError):
    """JWT secret/key not properly configured."""


@dataclass
class JwtConfig:
    """JWT config — secret, issuer, audience, algorithm.

    Loaded from env vars with sensible defaults:
        MCP_JWT_SECRET    — required (no default — must be explicitly set)
        MCP_JWT_ISSUER    — default: 'code-analyzer'
        MCP_JWT_AUDIENCE  — default: 'mcp-code-analyzer'
        MCP_JWT_ALGORITHM — default: 'HS256'
    """

    secret: str
    issuer: str = DEFAULT_ISSUER
    audience: str = DEFAULT_AUDIENCE
    algorithm: str = DEFAULT_ALGORITHM

    @classmethod
    def from_env(cls) -> JwtConfig:
        # Prefer explicit MCP_JWT_SECRET; fall back to CELMIS_JWT_SECRET
        # so single-container deployments don't need two env vars for the
        # same purpose. Production should still set MCP_JWT_SECRET
        # explicitly so key rotation is scoped to the MCP surface.
        secret = (
            os.environ.get("MCP_JWT_SECRET", "").strip()
            or os.environ.get("CELMIS_JWT_SECRET", "").strip()
        )
        if not secret:
            raise JwtConfigError(
                "Neither MCP_JWT_SECRET nor CELMIS_JWT_SECRET set. Generate one:\n"
                "  python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        # The same bar the web surface applies. It refused a shipped
        # placeholder and this did not, so a stack on the compose defaults
        # ended up with the browser locked and MCP — the surface an EXTERNAL
        # agent connects to — signing tokens with a value printed in a public
        # file. Half a refusal is a refusal that tells you it is safe.
        try:
            from src.api.jwt_auth import secret_problem

            problem = secret_problem(secret)
        except Exception:  # noqa: BLE001 — never make MCP depend on the API
            problem = None
        if problem:
            raise JwtConfigError(
                f"The MCP signing secret {problem}. Anyone holding it can mint "
                f"a token for any tool this server exposes. Generate one:\n"
                "  python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        return cls(
            secret=secret,
            issuer=os.environ.get("MCP_JWT_ISSUER", DEFAULT_ISSUER),
            audience=os.environ.get("MCP_JWT_AUDIENCE", DEFAULT_AUDIENCE),
            algorithm=os.environ.get("MCP_JWT_ALGORITHM", DEFAULT_ALGORITHM),
        )


# ─── Token issuance (server-side, for local dev) ─────────────────────


def issue_token(
    config: JwtConfig,
    *,
    subject: str,
    scopes: list[str] | None = None,
    client_id: str = "code-analyzer-cli",
    expires_in: int = DEFAULT_EXPIRES_IN,
    extra_claims: dict | None = None,
    jti: str | None = None,
    typ: str | None = None,
) -> str:
    """Generate a JWT for local development / testing.

    Args:
        subject: 'sub' claim — user_id or 'default'.
        scopes: list of scopes for the 'scope' claim.
        client_id: for tracing.
        expires_in: lifetime in seconds (default 1 hour).
        extra_claims: additional claims (for example tenant_id).
        jti: the id of the ``mcp_tokens`` row behind this token. A token
            without one is a *legacy* token (see ``CELMIS_MCP_LEGACY_TOKENS``).
        typ: ``pat`` | ``cli`` | ``self`` | ``oauth`` — which kind of grant
            the row is.

    Returns:
        Encoded JWT string ready for Authorization: Bearer <token>.
    """
    now = int(time.time())
    payload = {
        "iss": config.issuer,
        "aud": config.audience,
        "sub": subject,
        "iat": now,
        "exp": now + expires_in,
        "scope": " ".join(scopes or []),
        "client_id": client_id,
    }
    if extra_claims:
        # Don't allow overriding standard claims
        for k in ("iss", "aud", "sub", "iat", "exp", "scope", "jti", "typ"):
            extra_claims.pop(k, None)
        payload.update(extra_claims)
    if jti:
        payload["jti"] = jti
    if typ:
        payload["typ"] = typ

    token = jwt.encode(payload, config.secret, algorithm=config.algorithm)
    logger.info(
        "jwt_issued sub=%s client_id=%s scopes=%s expires_in=%ds",
        subject, client_id, scopes or [], expires_in,
    )
    return token


# ─── Workspace binding ───────────────────────────────────────────────

#: Per-request slot for "why was this token refused". Set to a fresh dict by
#: the HTTP wrapper (:class:`src.mcp_server.http_app._ExplainRefusal`) before
#: the request enters the SDK; the verifier writes into it. A dict rather than
#: a value so a context copied into a child task still shares it.
_REFUSAL: ContextVar[dict | None] = ContextVar("mcp_token_refusal", default=None)


def note_refusal(reason: str) -> None:
    holder = _REFUSAL.get()
    if holder is not None:
        holder["reason"] = reason


def _workspace_problem(payload: dict) -> str | None:
    """Why a token that names a workspace may not be used there, or None.

    A token without the claim (minted before it existed, or a
    client_credentials token) is not judged here — the identity resolver
    falls back to the old resolution for it. A token WITH the claim is
    checked against the membership table now, not when it was minted.
    """
    from src.mcp_server.identity import token_workspace

    claimed = token_workspace(payload)
    if claimed is None:
        return None
    sub = str(payload.get("sub") or "")
    if not sub or sub.startswith("client:"):
        # Nobody to ask. Such a token is not minted with a claim; one that
        # carries it anyway is refused rather than trusted.
        from src.mcp_server.identity import refusal_message

        return refusal_message(claimed)
    user_id = sub.split(":", 1)[1] if sub.startswith("user:") else sub
    try:
        from src.mcp_server.identity import refusal_message, token_workspace_problem
        from src.users import get_user_store

        user = get_user_store().get_by_id(user_id)
        if user is None or not getattr(user, "is_active", True):
            return refusal_message(claimed)
        return token_workspace_problem(user.id, bool(user.is_admin), claimed)
    except Exception as exc:  # noqa: BLE001 — fail closed, the claim is the boundary
        logger.warning("jwt_workspace_check_failed err=%s", exc)
        return ("Could not confirm your membership of the workspace this MCP "
                "token was issued for. Try again shortly.")


def _audit_refused(payload: dict, reason: str) -> None:
    """A signed token that was refused before any tool ran leaves an audit row
    (token id, person, reason code — no arguments)."""
    try:
        from src.mcp_server import audit

        audit.record_denied(payload, reason)
    except Exception:  # noqa: BLE001 — an audit problem never changes the answer
        pass


# ─── TokenVerifier implementation ────────────────────────────────────


class JwtTokenVerifier(TokenVerifier):
    """MCP TokenVerifier that validates JWT Bearer tokens.

    Validates:
        - Signature (HMAC or RSA depending on the algorithm)
        - exp claim (token is not expired)
        - iss claim (issuer matches expected)
        - aud claim (audience matches expected)
        - the ``mcp_tokens`` row behind it (revoked / expired / unknown /
          legacy tokens are refused, see ``CELMIS_MCP_LEGACY_TOKENS``)

    Returns an AccessToken instance or raises (per the verify_token contract).
    """

    def __init__(self, config: JwtConfig | None = None, *,
                 accept_project_tokens: bool = False) -> None:
        self.config = config or JwtConfig.from_env()
        #: Only the full /mcp endpoint serves project tokens (it alone has the
        #: project tools); every other server leaves them unrecognised.
        self.accept_project_tokens = accept_project_tokens

    async def _verify_project_token(self, token: str) -> AccessToken | None:
        """An opaque ``cmcp_`` token: judged by its database row, every call."""
        import asyncio

        from src.mcp_server import project_tokens as pt

        try:
            view = await asyncio.to_thread(pt.lookup_hash, token)
        except Exception as exc:  # noqa: BLE001 — fail closed
            logger.warning("mcp_project_token_lookup_failed err=%s", type(exc).__name__)
            note_refusal("Could not verify this project token. Try again shortly.")
            return None
        if view is None:
            note_refusal(pt.UNKNOWN)
            return None
        problem = view.problem()
        if problem:
            logger.warning("mcp_project_token_refused id=%s", view.id)
            note_refusal(problem)
            return None
        # At most one bookkeeping write a minute per token: a burst of calls
        # must not become a burst of database writes.
        last = view.last_used_at
        if last is None or (datetime.now(UTC) - last).total_seconds() >= 60:
            await asyncio.to_thread(pt.touch, view.id)
        return AccessToken(
            token=token, client_id=f"{pt.CLIENT_PREFIX}{view.id}", scopes=list(view.scopes),
            expires_at=int(view.expires_at.timestamp()), resource=self.config.audience,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify Bearer token. Returns AccessToken on success, None on failure.

        MCP SDK passes the raw token string (without the 'Bearer ' prefix).
        If verification fails — return None so that the SDK returns 401.
        """
        if not token or not token.strip():
            logger.debug("verify_token_empty")
            return None

        from src.mcp_server import project_tokens as pt

        if pt.is_project_token(token):
            # Never fall through to the JWT path: an opaque token is judged by
            # its row or not at all, and only on the server that serves it.
            return await self._verify_project_token(token) if self.accept_project_tokens else None

        try:
            try:
                payload = jwt.decode(
                    token,
                    self.config.secret,
                    algorithms=[self.config.algorithm],
                    audience=self.config.audience,
                    issuer=self.config.issuer,
                )
            except jwt.InvalidSignatureError:
                # Stage 21 — key-rotation window: accept tokens signed with
                # the previous secret (MCP_JWT_SECRET_PREVIOUS falls back to
                # CELMIS_JWT_SECRET_PREVIOUS, mirroring from_env()).
                prev = (
                    os.environ.get("MCP_JWT_SECRET_PREVIOUS", "").strip()
                    or os.environ.get("CELMIS_JWT_SECRET_PREVIOUS", "").strip()
                )
                if not prev:
                    raise
                payload = jwt.decode(
                    token,
                    prev,
                    algorithms=[self.config.algorithm],
                    audience=self.config.audience,
                    issuer=self.config.issuer,
                )
                logger.info("mcp_jwt_verified_with_previous_secret")
        except jwt.ExpiredSignatureError:
            logger.warning("jwt_expired")
            return None
        except jwt.InvalidIssuerError:
            logger.warning("jwt_invalid_issuer")
            return None
        except jwt.InvalidAudienceError:
            logger.warning("jwt_invalid_audience")
            return None
        except jwt.InvalidTokenError as exc:
            logger.warning("jwt_invalid err=%s", exc)
            return None

        # Build AccessToken response
        scope_str = payload.get("scope", "")
        if isinstance(scope_str, str):
            scopes = [s for s in scope_str.split(" ") if s]
        elif isinstance(scope_str, list):
            scopes = [str(s) for s in scope_str]
        else:
            scopes = []

        problem = _workspace_problem(payload)
        if problem:
            # Authentic, unexpired, and no longer good for the workspace it
            # names. Refused here, before any tool runs, and the reason is
            # handed to the response rewriter so the client reads a sentence
            # rather than "Authentication required".
            logger.warning("jwt_workspace_refused sub=%s", payload.get("sub"))
            note_refusal(problem)
            _audit_refused(payload, problem)
            return None

        # The grant behind the token: revoked, expired, unknown or legacy
        # tokens are refused here, before any tool runs. The row — not the
        # claims the token was signed with — says what it may do now.
        from src.mcp_server.identity import resolve_grant

        sub = str(payload.get("sub") or "")
        grant_user = sub.split(":", 1)[1] if sub.startswith(("user:", "client:")) else sub
        grant = resolve_grant(payload, grant_user)
        if grant.refused:
            logger.warning("jwt_grant_refused sub=%s grant=%s", sub, grant.token_id)
            note_refusal(grant.refused)
            _audit_refused(payload, grant.refused)
            return None
        if grant.token_id:
            from src.mcp_server import token_store

            holder = _REFUSAL.get() or {}
            token_store.touch(grant.token_id, holder.get("ip"))

        client_id = str(payload.get("client_id") or payload.get("sub") or "unknown")
        expires_at = int(payload["exp"]) if "exp" in payload else None

        logger.debug(
            "jwt_verified sub=%s client_id=%s scopes=%s",
            payload.get("sub"), client_id, scopes,
        )

        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=expires_at,
            resource=self.config.audience,
        )
