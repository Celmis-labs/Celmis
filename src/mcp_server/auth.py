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

Local dev: `analyzer mcp issue-token --user default --duration 3600` →
prints a JWT that can be used as a Bearer header.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

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
) -> str:
    """Generate a JWT for local development / testing.

    Args:
        subject: 'sub' claim — user_id or 'default'.
        scopes: list of scopes for the 'scope' claim.
        client_id: for tracing.
        expires_in: lifetime in seconds (default 1 hour).
        extra_claims: additional claims (for example tenant_id).

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
        for k in ("iss", "aud", "sub", "iat", "exp", "scope"):
            extra_claims.pop(k, None)
        payload.update(extra_claims)

    token = jwt.encode(payload, config.secret, algorithm=config.algorithm)
    logger.info(
        "jwt_issued sub=%s client_id=%s scopes=%s expires_in=%ds",
        subject, client_id, scopes or [], expires_in,
    )
    return token


# ─── TokenVerifier implementation ────────────────────────────────────


class JwtTokenVerifier(TokenVerifier):
    """MCP TokenVerifier that validates JWT Bearer tokens.

    Validates:
        - Signature (HMAC or RSA depending on the algorithm)
        - exp claim (token is not expired)
        - iss claim (issuer matches expected)
        - aud claim (audience matches expected)

    Returns an AccessToken instance or raises (per the verify_token contract).
    """

    def __init__(self, config: JwtConfig | None = None) -> None:
        self.config = config or JwtConfig.from_env()

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify Bearer token. Returns AccessToken on success, None on failure.

        MCP SDK passes the raw token string (without the 'Bearer ' prefix).
        If verification fails — return None so that the SDK returns 401.
        """
        if not token or not token.strip():
            logger.debug("verify_token_empty")
            return None

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
