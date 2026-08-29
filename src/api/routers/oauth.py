"""OAuth 2.1 authorization-code + PKCE server (Stage 19).

Endpoints:
    POST /oauth/register                — dynamic client registration (RFC 7591)
    GET  /oauth/authorize                — start consent flow (browser)
    POST /oauth/authorize/consent        — user approves → return code
    POST /oauth/token                    — exchange code for JWT

Design:
  * Public clients (Claude Code / Cursor) — no secret, PKCE required.
  * Confidential clients — secret hashed with Argon2 (stored, not
    reversible). Rare — most MCP integrations are public.
  * Auth code TTL = 60s. Consumed on first token exchange (one-time).
  * Access token = JWT issued by src.mcp_server.auth.issue_token, so
    the same JwtTokenVerifier already used by /mcp accepts it.
  * Refresh tokens NOT implemented — clients re-obtain via silent
    re-authorize when access token expires (~1h). Follow-up.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_user, require_workspace_admin
from src.db.models import OAuthAuthCode, OAuthClient, OAuthRefreshToken
from src.db.session import get_async_session
from src.users import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth", tags=["oauth"])

_hasher = PasswordHasher()
_CODE_TTL_SECONDS = 60
_TOKEN_TTL_SECONDS = 3600
_REFRESH_TTL_SECONDS = 30 * 24 * 3600   # 30 days


# ─── Dynamic client registration (RFC 7591) ─────────────────────────


class RegisterClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    redirect_uris: list[str] = Field(default_factory=list, max_length=10)
    allowed_scopes: list[str] = Field(default_factory=list)
    public: bool = True   # PKCE-only; no secret returned when true
    model_config = ConfigDict(extra="forbid")


class RegisterClientOut(BaseModel):
    client_id: str
    client_secret: str | None       # None for public clients
    name: str
    redirect_uris: list[str]
    allowed_scopes: list[str]


class ClientSummary(BaseModel):
    client_id: str
    name: str
    redirect_uris: list[str]
    allowed_scopes: list[str]
    is_public: bool
    created_at: str
    created_by: str | None


@router.get("/clients", response_model=list[ClientSummary])
async def list_clients(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_workspace_admin),
) -> list[ClientSummary]:
    """Platform admins see every client; everybody else sees their own.

    This asked for a platform admin, while registration asks for a workspace
    admin — so the person who registered a client could neither list it nor
    delete it afterwards. Minting a credential you can never see again is the
    wrong way round. This is strictly narrower than the old behaviour for
    anyone who is not a platform admin: they used to get a 403, not somebody
    else's clients.
    """
    stmt = select(OAuthClient).order_by(OAuthClient.created_at.desc())
    if not user.is_admin:
        stmt = stmt.where(OAuthClient.created_by == user.email)
    rows = (await session.scalars(stmt)).all()
    return [
        ClientSummary(
            client_id=r.client_id, name=r.name,
            redirect_uris=list(r.redirect_uris or []),
            allowed_scopes=list(r.allowed_scopes or []),
            is_public=r.client_secret_hash is None,
            created_at=r.created_at.isoformat() if r.created_at else "",
            created_by=r.created_by,
        )
        for r in rows
    ]


@router.delete("/clients/{client_id}", status_code=204)
async def delete_client(
    client_id: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_workspace_admin),
) -> None:
    row = await session.get(OAuthClient, client_id)
    if row is None:
        return
    # Registration is open to a workspace admin — "registering one grants no
    # authority the registrant does not already have" — but deletion asked for
    # a PLATFORM admin, so whoever created a client could not revoke it. A
    # credential you can mint and cannot withdraw is the wrong way round; the
    # argument that lets you make it is the same one that lets you unmake it.
    # Platform admins keep deleting anything; everyone else deletes their own.
    if not user.is_admin and row.created_by != user.email:
        raise HTTPException(
            status_code=403,
            detail="only the client's creator or a platform admin may delete it",
        )
    # Also revoke all outstanding refresh tokens for this client.
    tokens = (await session.scalars(
        select(OAuthRefreshToken).where(OAuthRefreshToken.client_id == client_id)
    )).all()
    for t in tokens:
        t.revoked = True
    await session.delete(row)
    await session.commit()
    logger.info("oauth_client_deleted id=%s tokens_revoked=%d by=%s",
                client_id, len(tokens), user.email)


@router.post("/register", response_model=RegisterClientOut, status_code=201)
async def register_client(
    payload: RegisterClientIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(require_workspace_admin),
) -> RegisterClientOut:
    """Register a client that can act through MCP.

    Workspace admin rather than platform admin. A client is bound to whoever
    registered it — the MCP identity resolver reads `created_by` and gives the
    token that person's workspace — so registering one grants no authority the
    registrant does not already have by clicking in the UI. Requiring a
    platform admin instead made the whole automation surface unreachable for
    the people who own the repositories it acts on.

    The scope list is the boundary: a non-platform-admin may only ask for
    scopes they themselves hold, so nobody mints `admin` for a machine.
    """
    if not user.is_admin:
        from src.users.scopes import held_scopes
        held = set(held_scopes(user))
        over = [s for s in payload.allowed_scopes if s not in held]
        if over:
            raise HTTPException(
                status_code=403,
                detail=("You can only grant a client scopes you hold yourself. "
                        f"Not yours: {', '.join(sorted(over))}"),
            )

    client_id = f"ec_{uuid.uuid4().hex[:16]}"
    secret_plain = None if payload.public else secrets.token_urlsafe(32)
    row = OAuthClient(
        client_id=client_id,
        client_secret_hash=_hasher.hash(secret_plain) if secret_plain else None,
        name=payload.name,
        redirect_uris=list(payload.redirect_uris),
        allowed_scopes=list(payload.allowed_scopes),
        created_by=user.email,
    )
    session.add(row)
    await session.commit()
    logger.info("oauth_client_registered id=%s name=%s public=%s by=%s",
                client_id, payload.name, payload.public, user.email)
    return RegisterClientOut(
        client_id=client_id, client_secret=secret_plain,
        name=row.name, redirect_uris=row.redirect_uris,
        allowed_scopes=row.allowed_scopes,
    )


# ─── /authorize + consent ───────────────────────────────────────────


@router.get("/authorize", response_class=HTMLResponse)
async def authorize(
    request: Request,
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query("S256"),
    scope: str = Query(default=""),
    state: str = Query(default=""),
    session: AsyncSession = Depends(get_async_session),
) -> HTMLResponse:
    """Render a minimal consent screen. On POST /consent the caller
    accepts and receives an auth code back via `redirect_uri?code=...`.
    We DO NOT run our own login here — user must already have a
    NextAuth session for the web app (cookie-based). If they don't,
    they see a "please sign in first" screen with a link to the app.
    """
    if response_type != "code":
        raise HTTPException(status_code=400, detail="only response_type=code supported")
    if code_challenge_method not in ("S256", "plain"):
        raise HTTPException(status_code=400, detail="unsupported code_challenge_method")

    client = await session.get(OAuthClient, client_id)
    if client is None:
        raise HTTPException(status_code=404, detail="unknown client")
    if redirect_uri not in (client.redirect_uris or []):
        raise HTTPException(status_code=400, detail="redirect_uri not allowed for client")

    # Confirm scopes are permitted. Fail-CLOSED: a client with an empty
    # allowed_scopes list may request NO scopes — every requested scope
    # must be in the allowlist. (The earlier `and allowed` guard made an
    # empty allowlist accept anything.)
    requested = [s for s in scope.split(" ") if s]
    allowed = set(client.allowed_scopes or [])
    denied = [s for s in requested if s not in allowed]
    if denied:
        raise HTTPException(
            status_code=400,
            detail=f"scopes not allowed for client: {denied}",
        )

    return HTMLResponse(_render_consent(
        client_name=client.name,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        scope=scope,
        state=state,
    ))


@router.post("/authorize/consent")
async def authorize_consent(
    request: Request,
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    code_challenge: str = Form(...),
    code_challenge_method: str = Form(...),
    scope: str = Form(""),
    state: str = Form(""),
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> RedirectResponse:
    """User consented (or the SPA auto-consented on their behalf).
    Mint an auth code + redirect back to client's redirect_uri."""
    client = await session.get(OAuthClient, client_id)
    if client is None or redirect_uri not in (client.redirect_uris or []):
        raise HTTPException(status_code=400, detail="invalid client/redirect")

    # Re-validate scope here too — the consent POST is directly callable,
    # so the GET /authorize check alone is not authoritative. Fail-closed.
    allowed = set(client.allowed_scopes or [])
    denied = [s for s in scope.split(" ") if s and s not in allowed]
    if denied:
        raise HTTPException(status_code=400,
                            detail=f"scopes not allowed for client: {denied}")

    code = secrets.token_urlsafe(32)
    row = OAuthAuthCode(
        code=code, client_id=client_id, user_id=user.id,
        redirect_uri=redirect_uri, code_challenge=code_challenge,
        code_challenge_method=code_challenge_method, scope=scope,
        expires_at=datetime.now(UTC) + timedelta(seconds=_CODE_TTL_SECONDS),
    )
    session.add(row)
    await session.commit()
    sep = "&" if "?" in redirect_uri else "?"
    url = f"{redirect_uri}{sep}code={code}"
    if state:
        url += f"&state={state}"
    logger.info("oauth_code_issued client=%s user=%s", client_id, user.email)
    return RedirectResponse(url, status_code=302)


# ─── /token exchange ────────────────────────────────────────────────


@router.post("/token")
async def token_exchange(
    grant_type: str = Form(...),
    code: str = Form(None),
    redirect_uri: str = Form(None),
    client_id: str = Form(...),
    client_secret: str = Form(None),
    code_verifier: str = Form(None),
    refresh_token: str = Form(None),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    if grant_type == "refresh_token":
        return await _refresh_grant(
            client_id=client_id, client_secret=client_secret,
            refresh_token=refresh_token, session=session,
        )
    if grant_type == "client_credentials":
        return await _client_credentials_grant(
            client_id=client_id, client_secret=client_secret, session=session,
        )
    if grant_type != "authorization_code":
        raise HTTPException(
            status_code=400,
            detail="grant_type must be authorization_code, refresh_token or client_credentials",
        )

    client = await session.get(OAuthClient, client_id)
    if client is None:
        raise HTTPException(status_code=401, detail="unknown client")

    # Confidential-client secret check (public clients skip this).
    if client.client_secret_hash:
        if not client_secret:
            raise HTTPException(status_code=401, detail="client_secret required")
        try:
            _hasher.verify(client.client_secret_hash, client_secret)
        except VerifyMismatchError:
            # A wrong secret is expected traffic, not an internal fault.
            raise HTTPException(status_code=401, detail="invalid client_secret") from None

    row = await session.get(OAuthAuthCode, code)
    if row is None or row.consumed:
        raise HTTPException(status_code=400, detail="invalid or consumed code")
    now = datetime.now(UTC)
    if row.expires_at < now:
        raise HTTPException(status_code=400, detail="code expired")
    if row.client_id != client_id or row.redirect_uri != redirect_uri:
        raise HTTPException(status_code=400, detail="code binding mismatch")

    # PKCE verification.
    if row.code_challenge_method == "S256":
        if not code_verifier:
            raise HTTPException(status_code=400, detail="code_verifier required")
        digest = hashlib.sha256(code_verifier.encode()).digest()
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        if computed != row.code_challenge:
            raise HTTPException(status_code=400, detail="pkce verification failed")
    else:   # plain
        if code_verifier != row.code_challenge:
            raise HTTPException(status_code=400, detail="pkce plain mismatch")

    # Mark consumed BEFORE minting — race-safe against a double POST.
    row.consumed = True
    await session.commit()

    # Mint JWT. Reuse the same secret the MCP JwtTokenVerifier uses so
    # the /mcp/ mount transparently accepts it.
    try:
        from src.mcp_server.auth import JwtConfig, issue_token
        cfg = JwtConfig.from_env()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"OAuth server not fully configured: {exc}",
        ) from exc
    token = issue_token(
        cfg, subject=row.user_id,
        scopes=[s for s in (row.scope or "").split(" ") if s],
        client_id=client_id,
        expires_in=_TOKEN_TTL_SECONDS,
    )
    # Fresh refresh token — new family (rotated_from is None here).
    refresh = await _mint_refresh(
        session=session, client_id=client_id, user_id=row.user_id,
        scope=row.scope, family_id=secrets.token_hex(16),
    )
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": _TOKEN_TTL_SECONDS,
        "refresh_token": refresh,
        "scope": row.scope,
    }


# ─── Client-credentials grant (service-to-service, Stage 21) ────────


async def _client_credentials_grant(
    *, client_id: str, client_secret: str | None, session: AsyncSession,
) -> dict[str, Any]:
    """Machine-to-machine tokens for CI / backend integrations.

    Confidential clients ONLY — a public (PKCE) client has no secret, so
    this grant would be an unauthenticated token mint. Scopes = the
    client's full allowed_scopes (no down-scoping param in the MVP).
    No refresh token is issued (RFC 6749 §4.4.3 — clients just re-auth).
    """
    client = await session.get(OAuthClient, client_id)
    if client is None:
        raise HTTPException(status_code=401, detail="unknown client")
    if not client.client_secret_hash:
        raise HTTPException(
            status_code=400,
            detail="client_credentials requires a confidential client",
        )
    if not client_secret:
        raise HTTPException(status_code=401, detail="client_secret required")
    try:
        _hasher.verify(client.client_secret_hash, client_secret)
    except VerifyMismatchError:
        # A wrong secret is expected traffic, not an internal fault.
        raise HTTPException(status_code=401, detail="invalid client_secret") from None

    from src.mcp_server.auth import JwtConfig, issue_token
    try:
        cfg = JwtConfig.from_env()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500,
                            detail=f"OAuth server not fully configured: {exc}") from exc
    scopes = list(client.allowed_scopes or [])
    token = issue_token(
        cfg, subject=f"client:{client_id}",
        scopes=scopes, client_id=client_id,
        expires_in=_TOKEN_TTL_SECONDS,
    )
    logger.info("oauth_client_credentials_issued client=%s scopes=%s",
                client_id, scopes)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": _TOKEN_TTL_SECONDS,
        "scope": " ".join(scopes),
    }


# ─── Refresh grant ──────────────────────────────────────────────────


async def _refresh_grant(
    *, client_id: str, client_secret: str | None,
    refresh_token: str | None, session: AsyncSession,
) -> dict[str, Any]:
    if not refresh_token:
        raise HTTPException(status_code=400, detail="refresh_token required")
    client = await session.get(OAuthClient, client_id)
    if client is None:
        raise HTTPException(status_code=401, detail="unknown client")
    if client.client_secret_hash:
        if not client_secret:
            raise HTTPException(status_code=401, detail="client_secret required")
        try:
            _hasher.verify(client.client_secret_hash, client_secret)
        except VerifyMismatchError:
            # A wrong secret is expected traffic, not an internal fault.
            raise HTTPException(status_code=401, detail="invalid client_secret") from None

    token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
    row = (await session.scalars(
        select(OAuthRefreshToken).where(OAuthRefreshToken.token_hash == token_hash)
    )).first()
    if row is None:
        raise HTTPException(status_code=400, detail="invalid refresh_token")
    if row.client_id != client_id:
        raise HTTPException(status_code=400, detail="refresh_token belongs to different client")

    now = datetime.now(UTC)

    # Detect reuse — the RFC 6819 mitigation. If this token was already
    # rotated (rotated_to != NULL) OR revoked, the whole family is
    # burned. This defeats the class of attacks where an attacker steals
    # a refresh token and both attacker + legit client try to use it.
    if row.rotated_to is not None or row.revoked:
        await _revoke_family(session, row.family_id)
        raise HTTPException(status_code=400,
                            detail="refresh token reuse detected — family revoked")

    if row.expires_at < now:
        raise HTTPException(status_code=400, detail="refresh_token expired")

    # Atomically claim this token: mark rotated_to='pending' ONLY if it's
    # still unrotated. Two concurrent refreshes with the same token race
    # here — exactly one UPDATE flips the row, the loser sees rowcount 0
    # and is treated as reuse (family burned). This closes the TOCTOU
    # between the read above and the write below.
    claim = await session.execute(
        update(OAuthRefreshToken)
        .where(
            OAuthRefreshToken.token_hash == token_hash,
            OAuthRefreshToken.rotated_to.is_(None),
            OAuthRefreshToken.revoked.is_(False),
        )
        .values(rotated_to="pending")
    )
    if claim.rowcount != 1:
        await session.commit()
        await _revoke_family(session, row.family_id)
        raise HTTPException(status_code=400,
                            detail="refresh token reuse detected — family revoked")

    # Rotate: issue a new refresh token in the same family.
    from src.mcp_server.auth import JwtConfig, issue_token
    try:
        cfg = JwtConfig.from_env()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500,
                            detail=f"OAuth server not fully configured: {exc}") from exc
    access = issue_token(
        cfg, subject=row.user_id,
        scopes=[s for s in (row.scope or "").split(" ") if s],
        client_id=client_id, expires_in=_TOKEN_TTL_SECONDS,
    )
    new_refresh = await _mint_refresh(
        session=session, client_id=client_id, user_id=row.user_id,
        scope=row.scope, family_id=row.family_id,
    )
    # Replace the 'pending' marker with the real new-token hash.
    await session.execute(
        update(OAuthRefreshToken)
        .where(OAuthRefreshToken.token_hash == token_hash)
        .values(rotated_to=hashlib.sha256(new_refresh.encode()).hexdigest())
    )
    await session.commit()
    logger.info("oauth_refresh_rotated client=%s user=%s family=%s",
                client_id, row.user_id, row.family_id)
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": _TOKEN_TTL_SECONDS,
        "refresh_token": new_refresh,
        "scope": row.scope,
    }


async def _mint_refresh(
    *, session: AsyncSession, client_id: str, user_id: str,
    scope: str, family_id: str,
) -> str:
    """Insert a refresh token row + return the raw token string. Only
    the hash is persisted."""
    raw = secrets.token_urlsafe(48)
    row = OAuthRefreshToken(
        id=str(uuid.uuid4()),
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        client_id=client_id, user_id=user_id, scope=scope,
        family_id=family_id,
        expires_at=datetime.now(UTC) + timedelta(seconds=_REFRESH_TTL_SECONDS),
    )
    session.add(row)
    await session.commit()
    return raw


async def _revoke_family(session: AsyncSession, family_id: str) -> None:
    rows = (await session.scalars(
        select(OAuthRefreshToken).where(OAuthRefreshToken.family_id == family_id)
    )).all()
    for r in rows:
        r.revoked = True
    await session.commit()
    logger.warning("oauth_refresh_family_revoked family=%s tokens=%d",
                   family_id, len(rows))


# ─── consent HTML ───────────────────────────────────────────────────


def _render_consent(**kwargs) -> str:
    """The consent screen, with every interpolated value escaped.

    This is an f-string building HTML, and four of the values in it reach it
    straight from the query string. `client_id`, `redirect_uri` and `scope` are
    checked against the registered client before we get here, but `state` and
    `code_challenge` are not checked against anything — they cannot be, they
    are the caller's own opaque data — and both land inside value="...".

    Measured against a running box, `state='"><b>PWNED</b>'` produced

        <input type="hidden" name="state" value=""><b>PWNED</b>">

    a live element on the API's origin, which is the web app's origin too. A
    script there runs as the signed-in operator. Reaching it needs a client_id
    and one of its redirect_uris, and a client_id is not a secret — it is in
    the MCP configuration people paste around.

    `client_name` comes from the database rather than the query, and is escaped
    on the same principle: a workspace admin registering a client must not be
    able to write markup into a page another person is shown.
    """
    e = {k: html.escape(str(v if v is not None else ""), quote=True)
         for k, v in kwargs.items()}
    scopes = e["scope"] or "(no scopes requested)"
    kwargs = e
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Celmis — authorize</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 480px; margin: 4em auto; padding: 0 1em; color: #111; }}
  h1 {{ font-size: 1.4em; }}
  .box {{ border: 1px solid #ccc; border-radius: 8px; padding: 1em 1.2em; margin: 1em 0; }}
  button {{ padding: 0.6em 1.4em; border-radius: 6px; border: 0; cursor: pointer; }}
  .allow {{ background: #4f46e5; color: white; margin-right: 8px; }}
  .deny {{ background: #eee; color: #333; }}
  code {{ background: #f4f4f4; padding: 2px 4px; border-radius: 4px; }}
</style></head><body>
<h1>Grant access to Celmis?</h1>
<div class="box">
  <p><b>{kwargs["client_name"]}</b> is requesting access.</p>
  <p>Scopes: <code>{scopes}</code></p>
  <p>Redirect: <code>{kwargs["redirect_uri"]}</code></p>
</div>
<form method="POST" action="/oauth/authorize/consent">
  <input type="hidden" name="client_id" value="{kwargs["client_id"]}">
  <input type="hidden" name="redirect_uri" value="{kwargs["redirect_uri"]}">
  <input type="hidden" name="code_challenge" value="{kwargs["code_challenge"]}">
  <input type="hidden" name="code_challenge_method" value="{kwargs["code_challenge_method"]}">
  <input type="hidden" name="scope" value="{kwargs["scope"]}">
  <input type="hidden" name="state" value="{kwargs["state"]}">
  <button class="allow" type="submit">Allow</button>
  <button class="deny" type="button" onclick="window.close()">Deny</button>
</form>
<p style="color:#888; font-size: 0.85em; margin-top: 2em">
  You must be signed into the Celmis web app in this browser for the
  consent to attach to your account.
</p>
</body></html>
"""


__all__ = ["router"]
